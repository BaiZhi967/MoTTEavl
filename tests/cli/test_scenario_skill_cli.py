"""M5-T11：scenario validate/run 与 workflow publish/list/convert-legacy 的 CLI 契约。

覆盖工作包要求：
* 稳定 JSON 输出与正确的非 0 退出码（校验失败 / 资源冲突 / 非法文档）；
* scenario validate 与 API /api/v1/workflows/validate 是**同一校验**（同一 code、
  同一逐字段错误、同一成功载荷）；
* workflow publish 走 API 同一校验语义（草稿拒绝、同内容幂等、异内容冲突）；
* scenario run 走 prepare_run + RunService.create_run 这一条公开创建路径；
* 拒绝路径不创建任何 Run。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_cli.main import main
from motte_scenario.targets import (
    TargetAdapter,
    TargetCapabilities,
    register_target_adapter,
    unregister_target_adapter,
)
from motte_storage.resource_store import InMemoryResourceStore

FIXTURE_HASH = "sha256:" + "a" * 64
PROBE_KIND = "m5t11-cli-probe-target"


def workflow_body(**overrides):
    body = {
        "workflow_id": "order-cancel-confirmed",
        "version": "1",
        "target_requirements": {
            "multi_turn": True,
            "min_turns": 2,
            "required_tools": ["orders.cancel"],
            "tool_modes": ["real"],
            "evidence": ["events"],
        },
        "steps": [
            {"step_id": "request", "kind": "send_message", "message": "请取消订单 order-1"},
            {
                "step_id": "before-confirm",
                "kind": "checkpoint",
                "assertions": [{"op": "eq", "path": "state.order.status", "value": "active"}],
            },
            {"step_id": "confirm", "kind": "send_message", "message": "我确认取消"},
            {
                "step_id": "final-check",
                "kind": "checkpoint",
                "assertions": [
                    {"op": "eq", "path": "state.order.status", "value": "cancelled"}
                ],
            },
        ],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    body.update(overrides)
    return body


def bounded_workflow_body():
    return workflow_body(
        fixture_refs=[
            {
                "fixture_id": "order-state",
                "version": 1,
                "kind": "json",
                "content_hash": FIXTURE_HASH,
            }
        ]
    )


def legacy_body(**overrides):
    body = {
        "scenario_id": "legacy-order-flow",
        "version": "1",
        "steps": [
            {"step_id": "ask", "when": {"send_message": "请取消订单 order-1"}},
            {"step_id": "check", "expect": "state.order.status == 'active'"},
        ],
        "final_assertions": ["state.order.status == 'cancelled'"],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    body.update(overrides)
    return body


class _FixtureRepository:
    """测试内只读 fixture 替身（T02 的 fixture 资源仓库尚未落库）。"""

    def __init__(self):
        self.records = {
            ("order-state", "1"): {
                "fixture_id": "order-state",
                "version": "1",
                "kind": "json",
                "content_hash": FIXTURE_HASH,
            }
        }

    def get(self, fixture_id, version):
        return self.records.get((fixture_id, str(version)))


class _ProbeAdapter:
    def __init__(self, kind, capabilities):
        self.kind = kind
        self._capabilities = capabilities
        self.sessions_opened = 0

    def capabilities(self, manifest):
        return self._capabilities

    def open_session(self, manifest):
        self.sessions_opened += 1
        raise AssertionError("the CLI must not open a scenario target session")


def _register_probe(kind, **overrides):
    capabilities = TargetCapabilities(
        kind=kind,
        multi_turn=overrides.pop("multi_turn", True),
        tool_modes=overrides.pop("tool_modes", ("real", "mock")),
        tools=overrides.pop("tools", ("orders.cancel",)),
        interrupt=overrides.pop("interrupt", True),
        skill_injection=overrides.pop("skill_injection", False),
        evidence=overrides.pop("evidence", ("events",)),
        source=overrides.pop("source", "adapter"),
    )
    assert not overrides, overrides
    probe = _ProbeAdapter(kind, capabilities)
    register_target_adapter(
        TargetAdapter(
            kind=kind, capabilities=probe.capabilities, open_session=probe.open_session
        )
    )
    return probe


def _scenario_backend_available(available):
    from motte_sdk import scenario_backend

    original = scenario_backend.SCENARIO_BACKEND_AVAILABLE
    scenario_backend.install_scenario_backend(available=available)
    return original


def _restore_scenario_backend(original):
    from motte_sdk import scenario_backend

    scenario_backend.install_scenario_backend(available=original)


def _write(tmp_path, payload, name="document.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _run_cli(capsys, argv):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _api_client():
    resources = InMemoryResourceStore()
    return TestClient(create_app(None, resource_store=resources))


# ------------------------------------------------------------------ validate


def test_cli_scenario_validate_matches_api_validation(tmp_path, capsys):
    document = workflow_body()
    path = _write(tmp_path, document)

    code, out, err = _run_cli(capsys, ["scenario", "validate", path])
    assert code == 0, err
    cli_report = json.loads(out)
    assert cli_report["ok"] is True
    assert cli_report["executed"] is False
    assert cli_report["ref"] == "order-cancel-confirmed@1"
    assert cli_report["defaulted_fields"] == ["published_at"]

    # 与 API 预检端点是同一校验：成功载荷逐字段相同。
    api_response = _api_client().post("/api/v1/workflows/validate", json=document)
    assert api_response.status_code == 200
    assert api_response.json() == cli_report


def test_cli_scenario_validate_failure_matches_api_error_and_exit_code(tmp_path, capsys):
    api = _api_client()
    cases = {
        "draft.json": workflow_body(lifecycle="draft"),
        "duplicate.json": workflow_body(
            steps=[
                {"step_id": "same", "kind": "send_message", "message": "a"},
                {"step_id": "same", "kind": "send_message", "message": "b"},
            ]
        ),
        "forbidden-root.json": workflow_body(
            completion_assertions=[{"op": "eq", "path": "input.answer", "value": "x"}]
        ),
        "hash.json": workflow_body(content_hash="sha256:" + "0" * 64),
    }
    for name, document in cases.items():
        path = _write(tmp_path, document, name)
        code, out, err = _run_cli(capsys, ["scenario", "validate", path])
        # 校验失败：非 0 退出 + stdout 上是稳定 JSON 结果（不是异常堆栈）。
        assert code == 1, (name, out, err)
        assert err == ""
        payload = json.loads(out)
        assert payload["ok"] is False
        error = payload["error"]
        api_response = api.post("/api/v1/workflows/validate", json=document)
        assert api_response.status_code == 422
        assert api_response.json()["error"]["code"] == error["code"], name
        assert api_response.json()["error"].get("fields", []) == error.get("fields", []), name


def test_cli_scenario_validate_reports_unusable_documents(tmp_path, capsys):
    missing = tmp_path / "missing.json"
    code, out, _err = _run_cli(capsys, ["scenario", "validate", str(missing)])
    assert code == 1
    assert json.loads(out)["error"]["code"] == "DOCUMENT_UNREADABLE"

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    code, out, _err = _run_cli(capsys, ["scenario", "validate", str(broken)])
    assert code == 1
    assert json.loads(out)["error"]["code"] == "DOCUMENT_NOT_JSON"

    from motte_sdk.resolve import ManifestResolutionError, resolve_manifest

    # 预检不是发布：CLI 从不写资源仓库。
    try:
        resolve_manifest({"workflow": "order-cancel-confirmed@1"}, InMemoryResourceStore())
    except ManifestResolutionError as error:
        assert error.code == "WORKFLOW_NOT_FOUND"
    else:  # pragma: no cover - 未发布引用必须解析失败
        raise AssertionError("unpublished workflow reference must not resolve")


# ------------------------------------------------------ workflow publish/list


def test_cli_workflow_publish_is_idempotent_and_conflicts(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    path = _write(tmp_path, workflow_body())

    code, out, err = _run_cli(
        capsys, ["workflow", "publish", path, "--db", db, "--json"]
    )
    assert code == 0, err
    stored = json.loads(out)
    assert stored["content_hash"].startswith("sha256:")

    # 同内容重复发布：幂等，同一 content_hash。
    code, out, err = _run_cli(
        capsys, ["workflow", "publish", path, "--db", db, "--json"]
    )
    assert code == 0, err
    assert json.loads(out)["content_hash"] == stored["content_hash"]

    # 同版本异内容：非 0 退出 + 结构化 RESOURCE_CONFLICT。
    changed = _write(
        tmp_path,
        workflow_body(
            steps=[{"step_id": "request", "kind": "send_message", "message": "不同内容"}]
        ),
        "changed.json",
    )
    code, out, err = _run_cli(capsys, ["workflow", "publish", changed, "--db", db])
    assert code == 2
    assert out == ""
    assert json.loads(err)["error"]["code"] == "RESOURCE_CONFLICT"

    code, out, err = _run_cli(capsys, ["workflow", "list", "--db", db, "--json"])
    assert code == 0, err
    listing = json.loads(out)
    assert listing["total"] == 1
    assert listing["items"][0]["workflow_id"] == "order-cancel-confirmed"

    # 与 API 发布共用同一校验：草稿在 CLI 上也是 WORKFLOW_INVALID + 逐字段错误。
    draft = _write(tmp_path, workflow_body(lifecycle="draft"), "draft.json")
    code, out, err = _run_cli(capsys, ["workflow", "publish", draft, "--db", db])
    assert code == 2
    assert out == ""
    error = json.loads(err)["error"]
    assert error["code"] == "WORKFLOW_INVALID"
    assert [item["field"] for item in error["fields"]] == ["lifecycle"]


def test_cli_workflow_convert_legacy_reports_diagnostics(tmp_path, capsys):
    refused = _write(
        tmp_path,
        legacy_body(setup=["echo seed"], cleanup=["rm -rf /tmp/legacy"]),
        "legacy.json",
    )
    code, out, _err = _run_cli(capsys, ["workflow", "convert-legacy", refused])
    assert code == 1
    report = json.loads(out)
    assert report["publishable"] is False
    assert report["published"] is False
    assert report["runs_executed"] == 0
    assert "LEGACY_FIELD_REFUSED" in {item["code"] for item in report["diagnostics"]}

    convertible = _write(tmp_path, legacy_body(), "convertible.json")
    code, out, _err = _run_cli(capsys, ["workflow", "convert-legacy", convertible])
    assert code == 0
    clean = json.loads(out)
    assert clean["publishable"] is True
    assert clean["candidate"]["ref"] == "legacy-order-flow@1"


# ------------------------------------------------------------------- run


def test_cli_scenario_run_creates_a_run_through_the_public_path(tmp_path, capsys, monkeypatch):
    resources = InMemoryResourceStore()
    resources.fixtures = _FixtureRepository()
    resources.publish_workflow(
        {
            **bounded_workflow_body(),
            "published_at": "2026-09-21T00:00:00Z",
        }
    )
    resources.scenarios.put({"name": "order-cancel", "version": "1"})
    # _resources() 在给出 --db 时构造 SQLiteResourceStore（并装配内容存储）：
    # 这里换成已准备的内存仓库，签名必须与真实构造器一致。
    monkeypatch.setattr(
        "motte_storage.resource_store.SQLiteResourceStore",
        lambda path, content_store=None: resources,
    )
    db = str(tmp_path / "cli.db")
    probe = _register_probe(PROBE_KIND)
    original = _scenario_backend_available(True)
    try:
        code, out, err = _run_cli(
            capsys,
            [
                "scenario",
                "run",
                "--scenario",
                "order-cancel@1",
                "--workflow",
                "order-cancel-confirmed@1",
                "--target-agent",
                f"{PROBE_KIND}@1",
                "--db",
                db,
            ],
        )
    finally:
        _restore_scenario_backend(original)
        unregister_target_adapter(PROBE_KIND)

    assert code == 0, (out, err)
    payload = json.loads(out)
    assert payload["status"] == "queued"
    assert payload["scenario"] == "order-cancel@1"
    assert payload["execution"]["backend_id"] == "scenario"
    assert payload["workflow"] == "order-cancel-confirmed@1"
    assert probe.sessions_opened == 0

    # Run 落在正常 Run 存储里（与 CLI run / API POST /runs 同一条路径）。
    from motte_storage.run_store import SQLiteRunStore

    stored = SQLiteRunStore(db).runs.list()
    assert [run["id"] for run in stored] == [payload["id"]]
    assert stored[0]["manifest"]["workflow"] == "order-cancel-confirmed@1"


def test_cli_scenario_run_refuses_without_creating_a_run(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    resources = InMemoryResourceStore()
    resources.publish_workflow(
        {**workflow_body(), "published_at": "2026-09-21T00:00:00Z"}
    )
    resources.scenarios.put({"name": "order-cancel", "version": "1"})
    import motte_cli.main as cli

    original_resources = cli._resources
    cli._resources = lambda args: resources
    try:
        code, out, err = _run_cli(
            capsys,
            [
                "scenario",
                "run",
                "--scenario",
                "order-cancel@1",
                "--workflow",
                "order-cancel-confirmed@1",
                "--target-agent",
                "m5t11-absent-target@1",
                "--db",
                db,
            ],
        )
    finally:
        cli._resources = original_resources

    # 创建期结构化拒绝（目标不可用 / backend 未启用 / fixture 未固定），绝不排队。
    assert code == 2, (out, err)
    assert out == ""
    error = json.loads(err)["error"]
    assert error["code"].startswith("SCENARIO_") or error["code"] == (
        "EXECUTION_BACKEND_UNAVAILABLE"
    )
    assert "Traceback" not in err

    from motte_storage.run_store import SQLiteRunStore

    assert SQLiteRunStore(db).runs.list() == []


def test_cli_scenario_targets_emits_stable_json(capsys):
    probe = _register_probe(PROBE_KIND, source="declared")
    try:
        code, out, err = _run_cli(capsys, ["scenario", "targets", "--json"])
    finally:
        unregister_target_adapter(PROBE_KIND)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["total"] == len(payload["items"])
    by_kind = {item["kind"]: item for item in payload["items"]}
    assert by_kind[PROBE_KIND]["available"] is False
    assert by_kind[PROBE_KIND]["tool_modes"] == ["real", "mock"]
    assert probe.sessions_opened == 0
