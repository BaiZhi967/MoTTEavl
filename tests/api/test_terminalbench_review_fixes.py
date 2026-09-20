"""review R08/R16/R17/R19/R20：公共 API/CLI 的请求校验、脱敏与内容读取。

对应反例：

- R20：不支持 agent_id / ``n_trials:"oops"`` / 不存在 task_key 都返回 500；
  preflight 对不支持 agent 返回 ``ok=true``；
- R16：Web 发 ``timeout_sec`` 被 API 静默忽略（只有 ``agent_timeout_sec`` 生效）；
- R08：Trial 详情原样返回含合成凭据的错误消息；
- R19：真实 API 只给 ``terminal_ref``，没有可读的终端文本/工件内容；
- R17：CLI ``status`` 丢掉冻结计划，覆盖率比 API 虚高。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
#: 合成哨兵：形状像密钥以便验证脱敏，不是任何真实凭据。
SYNTHETIC_KEY = "sk-review-sentinel-0123456789abcdef"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    application = create_app()
    with TestClient(application) as test_client:
        test_client.app = application
        yield test_client


def _prepared(client: TestClient) -> dict:
    response = client.post("/api/v1/benchmarks/terminal-bench/prepare", json={
        "task_root": str(FIXTURE_ROOT), "source_id": "motte-harbor-fixtures",
        "revision": "fixtures-2026-09-20", "license_id": "Apache-2.0",
    })
    assert response.status_code == 201, response.text
    return response.json()


def _runner_probe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
    from motte_benchmark.registry import register_adapter, unregister_adapter

    unregister_adapter(ADAPTER_ID)  # 同一进程内其余用例可能已注册
    register_adapter(ADAPTER_ID, lambda: HarborJobAdapter(argv=["/nonexistent/harbor-entry"]))
    report_path = Path(str(client.app.state.__dict__.get("tmp", "/tmp")))/ "preflight.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "available": True, "server_version": "27.4.0", "platform": "linux/arm64",
    }), encoding="utf-8")
    monkeypatch.setenv("MOTTE_HARBOR_PREFLIGHT_REPORT", str(report_path))


def _settings(limit: int = 3) -> list[dict]:
    return [
        {
            "name": f"setting-{index}", "task_key": f"task-{index}",
            "repeat_index": repeat, "reward": reward,
            "disposition": disposition, "status": verifier_status,
        }
        for index, (repeat, reward, disposition, verifier_status) in enumerate(
            [
                (0, 1.0, "succeeded", "scored"),
                (1, 0.0, "failed", "scored"),
                (2, None, "indeterminate", "verifier_error"),
            ][:limit],
        )
    ]


# ------------------------------------------------------------------ R20

def test_invalid_creation_requests_are_4xx_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共创建参数非法一律可解释的 4xx，且不产生 Run/Job。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)

    bad_agent = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "agent_id": "not-a-real-agent", "n_trials": 1,
    })
    assert bad_agent.status_code == 422, bad_agent.text
    assert bad_agent.json()["error"]["code"] == "HARBOR_AGENT_UNSUPPORTED"

    bad_type = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": "oops",
    })
    assert bad_type.status_code == 422, bad_type.text
    assert bad_type.json()["error"]["code"] == "REQUEST_FIELD_TYPE_INVALID"

    bad_task = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1, "task_keys": ["no-such-task-key"],
    })
    assert bad_task.status_code == 422, bad_task.text
    assert bad_task.json()["error"]["code"] == "TASK_KEY_UNKNOWN"

    unknown_field = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1, "timeout_sec": 7,
    })
    assert unknown_field.status_code == 422, unknown_field.text
    assert unknown_field.json()["error"]["code"] == "REQUEST_FIELD_UNKNOWN"
    assert "timeouts" in unknown_field.json()["error"]["details"]["allowed"]

    unlisted_timeout = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1, "timeouts": {"agent_timeout_sec": 7},
    })
    assert unlisted_timeout.status_code == 422, unlisted_timeout.text
    assert unlisted_timeout.json()["error"]["code"] == "REQUEST_FIELD_UNKNOWN"

    assert client.get("/api/v1/runs").json()["total"] == 0, "拒绝请求不得留下 Run"


def test_preflight_uses_the_same_agent_verdict_as_creation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预检不再对不支持的 Agent 放行（review R20 的双重标准）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    report = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight",
        params={"agent_id": "not-a-real-agent"},
    ).json()
    assert report["ok"] is False
    assert "AGENT_UNSUPPORTED" in report["reasons"]
    assert report["messages"]["AGENT_UNSUPPORTED"]


def test_real_agent_preflight_requires_model_and_credential_ref(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Agent 需要模型与凭据引用；oracle 不需要（review R10/R20）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    missing = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight",
        params={"agent_id": "claude-code", "agent_version": "2.0.30"},
    ).json()
    assert missing["ok"] is False
    assert "AGENT_MODEL_REQUIRED" in missing["reasons"]

    oracle = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight", params={"n_trials": 1},
    ).json()
    assert oracle["ok"] is True, oracle


# ------------------------------------------------------------------ R16

def test_declared_timeouts_reach_the_frozen_native_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """声明多少就执行多少：期限必须进入冻结的原生配置（review R16/R07）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    created = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1,
        "timeouts": {"agent_sec": 7, "agent_setup_sec": 11, "verifier_sec": 13},
    })
    assert created.status_code == 202, created.text
    run = client.get(f"/api/v1/runs/{created.json()['id']}").json()
    external = run["manifest"]["external_benchmark"]
    native = external["runner_config"]["harbor"]
    agent = native["job"]["agents"][0]
    assert agent["override_timeout_sec"] == 7
    assert agent["override_setup_timeout_sec"] == 11
    assert native["job"]["verifier"]["override_timeout_sec"] == 13
    assert native["timeouts"]["agent_sec"] == 7


def test_job_deadline_becomes_the_supervisor_wall_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Job 总期限必须是 Supervisor 真实读取的 max_wall_seconds（review R07）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    created = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1, "timeouts": {"job_sec": 600},
    })
    assert created.status_code == 202, created.text
    run = client.get(f"/api/v1/runs/{created.json()['id']}").json()
    limits = run["manifest"]["external_benchmark"]["limits"]
    assert limits["max_wall_seconds"] == 600


def test_environment_build_timeout_is_refused_not_silently_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harbor 0.23.0 无法精确映射的期限必须创建时拒绝（review R07）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    response = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "n_trials": 1, "timeouts": {"environment_build_sec": 900},
    })
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "HARBOR_TIMEOUT_UNSUPPORTED"
    assert client.get("/api/v1/runs").json()["total"] == 0


# ------------------------------------------------------- R08/R19 展示与内容

def _trial_with_secret(client: TestClient, *, terminal_text: str) -> tuple[str, str, str]:
    """写一个 Trial：错误消息与终端日志里含合成哨兵，证据冻结在工作区外。"""
    from motte_sdk import terminalbench as tb
    from motte_sdk.service import build_run_service
    from motte_storage.artifacts import ArtifactStore
    import os

    service = build_run_service(None)
    record = tb.prepared_dataset(service.store)
    inputs = tb.build_run_inputs(
        record=record, run_id="run-secret", job_id="job-secret",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id="run-secret",
    )
    trials = inputs["trials"]
    service.store.trials.create_plans([dict(trial) for trial in trials])
    trial_id = str(trials[0]["trial_id"])
    artifacts = ArtifactStore(os.environ["ARTIFACT_ROOT"])
    log_artifact = artifacts.put_bytes(
        "external-jobs/run-secret/job-secret/evidence/trial.log",
        terminal_text.encode("utf-8"),
    )
    import hashlib

    service.store.trials.put_result(
        trial_id,
        {
            "trial_id": trial_id,
            "disposition": "indeterminate",
            "termination": {"reason": "verifier_error",
                            "detail": f"provider rejected key {SYNTHETIC_KEY}"},
            "verifier_observation": {
                "status": "verifier_error",
                "error": {"code": "VERIFIER_ERROR",
                          "message": f"verifier crashed with {SYNTHETIC_KEY}"},
            },
            "artifact_refs": [
                {"artifact_id": "external-jobs/run-secret/job-secret/evidence/trial.log",
                 "kind": "harbor-trial-log", "sha256": "sha256:" + log_artifact.sha256,
                 "size_bytes": len(terminal_text.encode("utf-8")),
                 "media_type": "text/plain", "complete": True, "truncated": False},
            ],
            "usage": {"cost_usd": None},
            "coverage": {"items": {"reward": "unavailable"}},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:secret-case", parser_version="harbor-terminal-bench-parser@1",
    )
    return ("run-secret", trial_id, log_artifact.id)


def test_trial_detail_redacts_content_but_keeps_identity(
    client: TestClient,
) -> None:
    """错误/日志字段脱敏，身份与 hash 逐字保留（review R08）。"""
    _prepared(client)
    run_id, trial_id, _artifact_id = _trial_with_secret(
        client, terminal_text=f"claude: using {SYNTHETIC_KEY}\n",
    )
    response = client.get(f"/api/v1/runs/{run_id}/trials/{trial_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    body = json.dumps(detail, ensure_ascii=False)
    assert SYNTHETIC_KEY not in body, "合成凭据不得出现在公共详情里"
    assert "[REDACTED-SECRET]" in body
    # 身份/hash 必须原样：不能用改写身份的方式假装脱敏。
    assert detail["trial_id"] == trial_id
    assert detail["task_key"]
    for ref in detail["artifacts"]:
        assert ref["sha256"].startswith("sha256:")
        assert ref["artifact_id"]


def test_trial_detail_exposes_readable_terminal_text(client: TestClient) -> None:
    """终端文本与工件内容可读（有界 + 脱敏），不再只有引用（review R19）。"""
    _prepared(client)
    run_id, trial_id, artifact_id = _trial_with_secret(
        client, terminal_text="step 1 ok\nstep 2 ok\n" + SYNTHETIC_KEY + "\n",
    )
    detail = client.get(f"/api/v1/runs/{run_id}/trials/{trial_id}").json()
    terminal = detail["terminal"]
    assert terminal is not None, "详情必须带可读终端文本（或明确不可读原因）"
    assert terminal["verified"] is True, terminal
    assert terminal["encoding"] == "utf-8"
    assert "step 1 ok" in terminal["text"]
    assert SYNTHETIC_KEY not in terminal["text"]
    assert terminal["truncated"] is False

    content = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{artifact_id}",
    )
    assert content.status_code == 200, content.text
    payload = content.json()
    assert payload["verified"] is True
    assert "step 2 ok" in payload["text"]

    # 不属于该 Trial 的 Artifact 身份必须 404（不串证据）。
    foreign = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/external-jobs/other/x",
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


def test_missing_artifact_content_is_honest_not_fabricated(client: TestClient) -> None:
    """引用存在但内容不可读时必须如实说明，不能返回一段无法验证的文本。"""
    _prepared(client)
    from motte_sdk import terminalbench as tb
    from motte_sdk.service import build_run_service

    service = build_run_service(None)
    record = tb.prepared_dataset(service.store)
    inputs = tb.build_run_inputs(
        record=record, run_id="run-missing", job_id="job-missing",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id="run-missing",
    )
    trial_id = str(inputs["trials"][0]["trial_id"])
    service.store.trials.create_plans([dict(inputs["trials"][0])])
    service.store.trials.put_result(
        trial_id,
        {
            "trial_id": trial_id, "disposition": "not_attempted",
            "verifier_observation": {"status": "missing_verifier_evidence"},
            "artifact_refs": [
                {"artifact_id": "harbor/trials/x/trial.log", "kind": "harbor-trial-log",
                 "sha256": "sha256:" + "b" * 64, "size_bytes": 5,
                 "complete": True, "truncated": False},
            ],
            "usage": {}, "coverage": {},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:missing", parser_version="harbor-terminal-bench-parser@1",
    )
    payload = client.get(
        f"/api/v1/runs/run-missing/trials/{trial_id}/artifacts/harbor/trials/x/trial.log",
    ).json()
    assert payload["text"] is None
    assert payload["verified"] is False
    assert payload["note"], "不可读必须给原因"
