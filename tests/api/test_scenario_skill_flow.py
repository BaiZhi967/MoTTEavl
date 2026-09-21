"""M5-T11：Workflow 资源 / 预检 / 转换 / Target 能力的 API 链路与零副作用保证。

覆盖工作包要求：
* 发布幂等（同内容）与同版本异内容冲突；
* 预检与转换**不发布、不执行**（无 Run、无 CaseAttempt、无版本资源）；
* 所有 GET/list/预检/转换端点零模型调用、不开 Target 会话；
* 目标不可用或 target_requirements 不满足时在创建期结构化拒绝，绝不改选后端；
* 错误体不泄漏堆栈或内部模块名。

目标注册表与执行 backend 是进程级全局状态，其他工作包会注册/切换它们；本文件
只对自己注册的探针 kind 下断言，并在 finally 里注销、恢复开关。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from motte_scenario.targets import (
    TargetAdapter,
    TargetCapabilities,
    register_target_adapter,
    unregister_target_adapter,
)
from motte_storage.resource_store import InMemoryResourceStore
from motte_storage.run_store import InMemoryRunStore

WORKFLOW_ID = "order-cancel-confirmed"
SCENARIO_REF = "order-cancel@1"
FIXTURE_HASH = "sha256:" + "a" * 64
#: 本文件独占的 target kind：不与其他工作包注册的 adapter 竞争。
UNREGISTERED_KIND = "m5t11-unregistered-target"
PROBE_KIND = "m5t11-probe-target"
DECLARED_KIND = "m5t11-declared-only-target"


def workflow_body(**overrides):
    body = {
        "workflow_id": WORKFLOW_ID,
        "version": "1",
        "description": "取消订单前必须取得用户确认",
        "target_requirements": {
            "multi_turn": True,
            "min_turns": 2,
            "required_tools": ["orders.cancel"],
            "tool_modes": ["real"],
            "interrupt": False,
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
                "label": "final",
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
    """带固定 fixture 引用的 Workflow：创建期能走完 fixture 校验，进入目标能力校验。"""
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
        "given": {"initial_state": {"order": {"status": "active"}}},
        "steps": [
            {"step_id": "ask", "when": {"send_message": "请取消订单 order-1"}},
            {"step_id": "check", "expect": "state.order.status == 'active'"},
            {"step_id": "confirm", "when": {"send_message": "我确认取消"}},
        ],
        "final_assertions": ["state.order.status == 'cancelled'"],
        "limits": {"max_total_steps": 20, "max_turns": 4, "wall_time_sec": 30},
    }
    body.update(overrides)
    return body


class _FixtureRepository:
    """测试内只读 fixture 替身。

    M5-T02 的 fixture 资源仓库尚未落入 motte_storage（ResourceStore 还没有
    fixtures 仓库），创建期快照因此无法由生产代码装配。这里注入一个**测试内
    只读替身**，让创建期真正走到 Target 能力校验；它不写任何资源，也不改变
    生产存储语义。
    """

    def __init__(self):
        self.records = {
            ("order-state", "1"): {
                "fixture_id": "order-state",
                "version": "1",
                "kind": "json",
                "content_hash": FIXTURE_HASH,
                "initial_state": {"order": {"status": "active"}},
            }
        }

    def get(self, fixture_id, version):
        return self.records.get((fixture_id, str(version)))


def app_client():
    resources = InMemoryResourceStore()
    store = InMemoryRunStore()
    return TestClient(create_app(store, resource_store=resources)), store, resources


def published_client(workflow=None, with_fixtures=False):
    client, store, resources = app_client()
    if with_fixtures:
        resources.fixtures = _FixtureRepository()
        workflow = workflow if workflow is not None else bounded_workflow_body()
    created = client.post("/api/v1/workflows", json=workflow or workflow_body())
    assert created.status_code == 201, created.text
    scenario = client.post("/api/v1/scenarios", json={"name": "order-cancel", "version": "1"})
    assert scenario.status_code == 201, scenario.text
    return client, store, resources


def _counts(store, run_id):
    return {
        "runs": len(store.runs.list()),
        "cases": len(store.case_runs.list_for_run(run_id)),
        "events": len(store.events.list_for_run(run_id)),
        "attempts": len(store.attempts.list_for_run(run_id)),
        "invocations": len(store.invocations.list_for_run(run_id)),
    }


def _scenario_run_request(target_kind, workflow_ref=f"{WORKFLOW_ID}@1"):
    return {
        "scenario_version": SCENARIO_REF,
        "manifest": {"workflow": workflow_ref, "agent": f"{target_kind}@1"},
        "case_ids": ["case-1"],
    }


class _ProbeAdapter:
    """记录能力读取与 session 打开；session 永远不该被任何端点打开。"""

    def __init__(self, kind, capabilities):
        self.kind = kind
        self._capabilities = capabilities
        self.sessions_opened = 0
        self.capability_reads = 0

    def capabilities(self, manifest):
        self.capability_reads += 1
        return self._capabilities

    def open_session(self, manifest):
        self.sessions_opened += 1
        raise AssertionError("no API endpoint may open a scenario target session")


def _register_probe(kind, **overrides):
    capabilities = TargetCapabilities(
        kind=kind,
        multi_turn=overrides.pop("multi_turn", True),
        tool_modes=overrides.pop("tool_modes", ("real", "mock")),
        tools=overrides.pop("tools", ("orders.cancel",)),
        interrupt=overrides.pop("interrupt", True),
        skill_injection=overrides.pop("skill_injection", False),
        evidence=overrides.pop("evidence", ("events", "invocations")),
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
    """在测试内固定 scenario backend 的可用性；返回调用前的真实值以便恢复。"""
    from motte_sdk import scenario_backend

    original = scenario_backend.SCENARIO_BACKEND_AVAILABLE
    scenario_backend.install_scenario_backend(available=available)
    return original


def _restore_scenario_backend(original):
    from motte_sdk import scenario_backend

    scenario_backend.install_scenario_backend(available=original)


# --------------------------------------------------------------- 发布与读取


def test_workflow_publish_is_idempotent_and_conflicts_on_changed_content():
    client, _store, resources = app_client()
    first = client.post("/api/v1/workflows", json=workflow_body())
    assert first.status_code == 201
    stored = first.json()
    assert stored["workflow_id"] == WORKFLOW_ID
    assert stored["version"] == "1"
    assert stored["content_hash"].startswith("sha256:")
    assert stored["lifecycle"] == "published"
    assert stored["published_at"]

    # 同内容重复发布（服务端补的发布时刻不同）必须幂等返回原记录。
    second = client.post("/api/v1/workflows", json=workflow_body())
    assert second.status_code == 201
    assert second.json() == stored
    assert len(resources.workflows.list()) == 1

    # 同版本异内容是 409，不是静默覆盖。
    changed = client.post(
        "/api/v1/workflows",
        json=workflow_body(
            steps=[{"step_id": "request", "kind": "send_message", "message": "不同内容"}]
        ),
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "RESOURCE_CONFLICT"
    assert resources.workflows.get(WORKFLOW_ID, "1")["content_hash"] == stored["content_hash"]

    listed = client.get("/api/v1/workflows")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    fetched = client.get(f"/api/v1/workflows/{WORKFLOW_ID}/1")
    assert fetched.status_code == 200
    assert fetched.json() == stored
    assert client.get(f"/api/v1/workflows/{WORKFLOW_ID}/2").status_code == 404
    immutable = client.delete(f"/api/v1/workflows/{WORKFLOW_ID}/1")
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "PUBLISHED_RESOURCE_IMMUTABLE"
    assert client.get(f"/api/v1/workflows/{WORKFLOW_ID}/1").status_code == 200


def test_workflow_publish_rejects_drafts_and_reports_structured_field_errors():
    client, _store, resources = app_client()

    draft = client.post("/api/v1/workflows", json=workflow_body(lifecycle="draft"))
    assert draft.status_code == 422
    assert draft.json()["error"]["code"] == "WORKFLOW_INVALID"
    assert [item["field"] for item in draft.json()["error"]["fields"]] == ["lifecycle"]

    duplicate = client.post(
        "/api/v1/workflows",
        json=workflow_body(
            steps=[
                {"step_id": "same", "kind": "send_message", "message": "a"},
                {"step_id": "same", "kind": "send_message", "message": "b"},
            ]
        ),
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "WORKFLOW_INVALID"
    assert duplicate.json()["error"]["fields"]

    unbounded = client.post(
        "/api/v1/workflows",
        json=workflow_body(
            steps=[
                {
                    "step_id": "loop",
                    "kind": "loop",
                    "body": [{"step_id": "inner", "kind": "send_message", "message": "x"}],
                }
            ]
        ),
    )
    assert unbounded.status_code == 422
    assert unbounded.json()["error"]["code"] == "WORKFLOW_INVALID"

    leaked = client.post(
        "/api/v1/workflows", json=workflow_body(api_key="sk-live-secret-value")
    )
    assert leaked.status_code == 422
    assert leaked.json()["error"]["code"] == "CREDENTIALS_REJECTED"
    assert "sk-live-secret-value" not in leaked.text

    assert client.get("/api/v1/workflows").json()["total"] == 0
    assert resources.workflows.list() == []


def test_workflow_preflight_compiles_conditions_without_publishing():
    client, _store, resources = app_client()

    ok = client.post("/api/v1/workflows/validate", json=workflow_body())
    assert ok.status_code == 200
    report = ok.json()
    assert report["ok"] is True
    assert report["executed"] is False
    assert report["ref"] == f"{WORKFLOW_ID}@1"
    assert report["step_count"] == 4
    assert report["top_level_step_ids"] == ["request", "before-confirm", "confirm", "final-check"]
    assert report["condition_count"] == 2
    assert report["target_requirements"]["required_tools"] == ["orders.cancel"]
    assert report["defaulted_fields"] == ["published_at"]
    # 预检不是发布：仓库仍然是空的。
    assert resources.workflows.list() == []

    # 条件根白名单是编译器独有的检查：契约允许写、预检必须拒绝。
    forbidden_root = client.post(
        "/api/v1/workflows/validate",
        json=workflow_body(
            completion_assertions=[{"op": "eq", "path": "input.answer", "value": "x"}]
        ),
    )
    assert forbidden_root.status_code == 422
    assert forbidden_root.json()["error"]["code"] == "CONDITION_PATH_FORBIDDEN"
    assert resources.workflows.list() == []

    # 内容 hash 不一致：服务端不猜，直接拒绝。
    mismatched = client.post(
        "/api/v1/workflows/validate",
        json=workflow_body(content_hash="sha256:" + "0" * 64),
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "WORKFLOW_CONTENT_HASH_MISMATCH"


# ----------------------------------------------------------------- 转换报告


def test_legacy_conversion_reports_diagnostics_and_never_publishes():
    client, _store, resources = app_client()

    refused = client.post(
        "/api/v1/workflows/legacy-conversion",
        json={"legacy": legacy_body(setup=["echo seed"], cleanup=["rm -rf /tmp/legacy"])},
    )
    assert refused.status_code == 200
    report = refused.json()
    assert report["publishable"] is False
    assert report["published"] is False
    assert report["executed"] is False
    assert report["runs_executed"] == 0
    codes = {item["code"] for item in report["diagnostics"]}
    assert "LEGACY_FIELD_REFUSED" in codes
    assert {
        item["path"] for item in report["diagnostics"] if item["code"] == "LEGACY_FIELD_REFUSED"
    } == {"setup", "cleanup"}
    assert "setup" in {item["legacy"] for item in report["mapping"]}
    assert report["candidate"] is None
    # 转换是只读的：没有版本资源、没有 Run。
    assert resources.workflows.list() == []
    assert client.get("/api/v1/workflows").json()["total"] == 0

    convertible = client.post(
        "/api/v1/workflows/legacy-conversion", json={"legacy": legacy_body()}
    )
    assert convertible.status_code == 200
    clean = convertible.json()
    assert clean["publishable"] is True
    assert clean["runs_executed"] == 0
    assert clean["candidate"]["ref"] == "legacy-order-flow@1"
    assert clean["workflow_draft"]["steps"][1]["assertions"] == [
        {"op": "eq", "path": "state.order.status", "value": "active"}
    ]
    # 即使可转换也不发布：端点没有写入权限。
    assert resources.workflows.list() == []
    assert client.get("/api/v1/workflows").json()["total"] == 0

    missing = client.post("/api/v1/workflows/legacy-conversion", json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "LEGACY_DOCUMENT_REQUIRED"


# ------------------------------------------------------------- Target 能力


def test_scenario_targets_reports_only_registered_capabilities():
    client, _store, _resources = app_client()
    probe = _register_probe(PROBE_KIND)
    declared = _register_probe(DECLARED_KIND, source="declared")
    try:
        response = client.get("/api/v1/scenario-targets")
    finally:
        unregister_target_adapter(PROBE_KIND)
        unregister_target_adapter(DECLARED_KIND)
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == len(payload["items"])
    assert payload["available"] is (payload["total"] > 0)
    kinds = [item["kind"] for item in payload["items"]]
    assert len(kinds) == len(set(kinds))
    by_kind = {item["kind"]: item for item in payload["items"]}
    assert by_kind[PROBE_KIND] == {
        "kind": PROBE_KIND,
        "available": True,
        "multi_turn": True,
        "tool_modes": ["real", "mock"],
        "tools": ["orders.cancel"],
        "interrupt": True,
        "skill_injection": False,
        "evidence": ["events", "invocations"],
    }
    # 只有声明、没有实现的 capability 不许被当成可用目标。
    assert by_kind[DECLARED_KIND]["available"] is False
    # 目录端点只读能力声明，绝不开会话。
    assert probe.sessions_opened == 0
    assert declared.sessions_opened == 0


# ------------------------------------------------------------- Skill 静态验证


def instruction_skill_body(**overrides):
    body = {
        "version": "1.0.0",
        "kind": "instruction",
        "instruction": "取消订单前必须先向用户确认。",
        "published_at": "2026-09-21T00:00:00Z",
    }
    body.update(overrides)
    return body


def test_skill_versions_are_read_only_and_validation_is_static():
    client, _store, resources = app_client()
    assert client.get("/api/v1/skills/versions").json() == {"items": [], "total": 0}

    validated = client.post(
        "/api/v1/skills/validate", json=instruction_skill_body(skill_id="order-confirm")
    )
    assert validated.status_code == 200, validated.text
    report = validated.json()
    assert report["ok"] is True
    assert report["executed"] is False
    assert report["validation_scope"] == "static"
    # 只读 manifest 不等于核验过资源字节：不谎称已执行（M5-A10）。
    assert report["resource_bytes_verified"] is False
    assert report["kind"] == "instruction"
    assert report["executable"] is False
    assert report["ref"] == "order-confirm@1.0.0"
    assert report["content_hash"].startswith("sha256:")
    # 校验不是发布：仓库仍然是空的。
    assert resources.skills.list() == []

    # executable 缺受控入口/输入输出 schema：具名拒绝 + 逐字段错误。
    executable = client.post(
        "/api/v1/skills/validate",
        json={
            "skill_id": "runner",
            "version": "1",
            "kind": "executable",
            "instruction": "run it",
        },
    )
    assert executable.status_code == 422
    assert executable.json()["error"]["code"] == "SKILL_INVALID"
    assert executable.json()["error"]["fields"]

    # 未固定依赖（latest 不是 pin）：静态校验就拒绝。
    unpinned = client.post(
        "/api/v1/skills/validate",
        json=instruction_skill_body(
            skill_id="dep",
            dependency_refs=[{"name": "requests", "version": "latest"}],
        ),
    )
    assert unpinned.status_code == 422
    assert unpinned.json()["error"]["code"] == "SKILL_INVALID"

    mismatched = client.post(
        "/api/v1/skills/validate",
        json=instruction_skill_body(skill_id="hash", content_hash="sha256:" + "0" * 64),
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "SKILL_INVALID"
    assert resources.skills.list() == []

    # 已发布版本可以被读取（由资源仓库直接落库，证明读端点只读）。
    from motte_skill.versions import SkillVersion, skill_content_hash

    record = SkillVersion.model_validate(instruction_skill_body(skill_id="published-skill"))
    stored = resources.skills.put(
        {**record.model_dump(mode="json"), "content_hash": skill_content_hash(record)}
    )
    listed = client.get("/api/v1/skills/versions").json()
    assert listed["total"] == 1
    fetched = client.get("/api/v1/skills/published-skill/versions/1.0.0")
    assert fetched.status_code == 200
    assert fetched.json()["content_hash"] == stored["content_hash"]
    assert client.get("/api/v1/skills/published-skill/versions/9").status_code == 404

    # v0 内存注册路由必须保持不变（同一条 GET/POST /api/v1/skills）。
    legacy = client.post(
        "/api/v1/skills",
        json={"name": "legacy", "version": "1", "entrypoint": "skills/legacy/main.py"},
    )
    assert legacy.status_code == 201
    assert client.get("/api/v1/skills").json()["total"] == 1


# --------------------------------------------- 零执行 / 零模型调用保证


def test_read_and_preflight_endpoints_create_nothing_and_open_no_target():
    client, store, resources = published_client()
    # 先造一个 Run 作为基线：任何端点若排队/执行，都会改变这些计数。
    run = client.post(
        "/api/v1/runs", json={"scenario_version": "replay@1", "case_ids": ["case-1"]}
    ).json()
    run_id = run["id"]
    before = _counts(store, run_id)

    probe = _register_probe(PROBE_KIND)
    try:
        responses = [
            client.get("/api/v1/workflows"),
            client.get(f"/api/v1/workflows/{WORKFLOW_ID}/1"),
            client.get(f"/api/v1/workflows/{WORKFLOW_ID}/missing"),
            client.post("/api/v1/workflows/validate", json=workflow_body()),
            client.post("/api/v1/workflows/validate", json=workflow_body(lifecycle="draft")),
            client.post("/api/v1/workflows/legacy-conversion", json={"legacy": legacy_body()}),
            client.post(
                "/api/v1/workflows/legacy-conversion",
                json={"legacy": legacy_body(cleanup=["rm -rf /tmp/x"])},
            ),
            client.get("/api/v1/scenario-targets"),
            client.get("/api/v1/runs"),
            client.get(f"/api/v1/runs/{run_id}"),
            client.get(f"/api/v1/runs/{run_id}/report"),
            client.get(f"/api/v1/runs/{run_id}/scoring-passes"),
            client.get(f"/api/v1/runs/{run_id}/invocations"),
            client.get(f"/api/v1/runs/{run_id}/commands"),
        ]
    finally:
        unregister_target_adapter(PROBE_KIND)

    assert all(response.status_code < 500 for response in responses)
    assert probe.sessions_opened == 0
    # 一个 Run 都不多、没有新的 case/事件/attempt/invocation。
    assert _counts(store, run_id) == before
    assert len(resources.workflows.list()) == 1


def test_creation_refuses_unmet_target_requirements_without_backend_fallback():
    client, store, _resources = published_client(with_fixtures=True)
    original = _scenario_backend_available(True)
    try:
        # 没有注册 adapter：目标不可用，创建期具名拒绝。
        unregistered = client.post(
            "/api/v1/runs", json=_scenario_run_request(UNREGISTERED_KIND)
        )
        assert unregistered.status_code == 422, unregistered.text
        assert unregistered.json()["error"]["code"] == "SCENARIO_TARGET_UNSUPPORTED"
        assert store.runs.list() == []

        # 已注册但 tool_modes 不满足 target_requirements：同样拒绝，不改选后端。
        probe = _register_probe(PROBE_KIND, tool_modes=("mock",))
        try:
            mismatched = client.post("/api/v1/runs", json=_scenario_run_request(PROBE_KIND))
        finally:
            unregister_target_adapter(PROBE_KIND)
        assert mismatched.status_code == 422, mismatched.text
        assert mismatched.json()["error"]["code"] == "SCENARIO_TARGET_TOOL_MODE_UNSUPPORTED"
        assert probe.sessions_opened == 0
        assert store.runs.list() == []
    finally:
        _restore_scenario_backend(original)


def test_creation_refuses_when_scenario_backend_is_unavailable():
    """backend 不可用时创建期明确拒绝，绝不静默改选 replay/direct-llm。"""
    client, store, _resources = published_client(with_fixtures=True)
    original = _scenario_backend_available(False)
    try:
        refused = client.post("/api/v1/runs", json=_scenario_run_request(UNREGISTERED_KIND))
    finally:
        _restore_scenario_backend(original)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "EXECUTION_BACKEND_UNAVAILABLE"
    assert store.runs.list() == []


def test_client_supplied_creation_snapshots_are_rejected():
    client, store, _resources = published_client()
    forged = client.post(
        "/api/v1/runs",
        json={
            "scenario_version": SCENARIO_REF,
            "manifest": {
                "workflow": f"{WORKFLOW_ID}@1",
                "agent": f"{PROBE_KIND}@1",
                "workflow_snapshot": {"steps": []},
            },
            "case_ids": ["case-1"],
        },
    )
    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == "SNAPSHOT_RESERVED"
    assert store.runs.list() == []


def test_error_bodies_do_not_leak_implementation_details():
    client, _store, _resources = app_client()
    responses = [
        client.post("/api/v1/workflows", json=workflow_body(lifecycle="draft")),
        client.post("/api/v1/workflows", json=workflow_body(steps=[])),
        client.get(f"/api/v1/workflows/{WORKFLOW_ID}/missing"),
        client.post(
            "/api/v1/workflows/legacy-conversion",
            json={"legacy": legacy_body(setup=["echo x"])},
        ),
        client.post(
            "/api/v1/runs",
            json={"scenario_version": "missing@1", "manifest": {}, "case_ids": []},
        ),
    ]
    forbidden = ("Traceback", 'File "', "site-packages", "packages/", "motte_", ".py")
    for response in responses:
        assert response.status_code < 500, response.text
        for marker in forbidden:
            assert marker not in response.text, (marker, response.text)
