"""review Round 2：真实 Agent 凭据引用入口、原始字节下载边界、DTO 校验顺序。

对应反例：

- R2-06：合法 ``{"provider": {"ref": "env:ANTHROPIC_API_KEY"}}`` 创建返回
  ``REQUEST_FIELD_UNKNOWN``（DTO 根本不接受 credentials）；preflight 也没有
  传引用的入口，永远 ``AGENT_CREDENTIAL_REF_MISSING``；
- R2-07：含合成哨兵的文本工件，普通内容接口脱敏，``/bytes`` 返回原始哨兵；
- R2-12：已准备数据集时 ``dataset_revision: {"bad": "type"}`` 得到 500。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "tasks"
#: 合成哨兵：形状像密钥以便验证脱敏，不是任何真实凭据。
SYNTHETIC_KEY = "sk-review-sentinel-0123456789abcdef"
#: 真实 Agent 的凭据引用（平台只接受引用，值永远来自 Runner 环境）。
CREDENTIAL_REF = "env:ANTHROPIC_API_KEY"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOTTE_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("MOTTE_ALLOW_RAW_ARTIFACT_EXPORT", raising=False)
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


def _published_model(client: TestClient, model_id: str = "m3-harbor-model") -> str:
    provider = client.post("/api/v1/providers", json={
        "name": "m3-provider", "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:9/v1",
    })
    assert provider.status_code in (200, 201), provider.text
    created = client.post("/api/v1/models", json={
        "id": model_id, "provider": "m3-provider", "capabilities": {},
    })
    assert created.status_code in (200, 201), created.text
    published = client.post(f"/api/v1/models/{model_id}/publish")
    assert published.status_code in (200, 201), published.text
    return model_id


def _runner_probe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from motte_benchmark.harbor.adapter import ADAPTER_ID, HarborJobAdapter
    from motte_benchmark.registry import register_adapter, unregister_adapter

    unregister_adapter(ADAPTER_ID)  # 同一进程内其余用例可能已注册
    register_adapter(ADAPTER_ID, lambda: HarborJobAdapter(argv=["/nonexistent/harbor-entry"]))
    report_path = Path(str(client.app.state.__dict__.get("tmp", "/tmp"))) / "preflight.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "available": True, "server_version": "27.4.0", "platform": "linux/arm64",
    }), encoding="utf-8")
    monkeypatch.setenv("MOTTE_HARBOR_PREFLIGHT_REPORT", str(report_path))


def _assert_not_persisted(root: Path, sentinel: str) -> None:
    """明文凭据不得落进任何持久化文件（DB / WAL / 工件 / 旁路 JSON）。"""
    needle = sentinel.encode("utf-8")
    for path in sorted(root.rglob("*")):
        if path.is_file() and needle in path.read_bytes():
            raise AssertionError(f"synthetic secret persisted in {path}")


# --------------------------------------------------------------- R2-06

def test_create_run_accepts_credential_refs_for_the_real_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公共创建必须能传凭据引用，并把它冻结进 Profile 与原生 Agent 段。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)

    created = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1, "credentials": {"provider": {"ref": CREDENTIAL_REF}},
    })
    assert created.status_code == 202, created.text

    run = client.get(f"/api/v1/runs/{created.json()['id']}").json()
    external = run["manifest"]["external_benchmark"]
    profile = external["profile"]
    # 冻结配置里只有引用（平台侧唯一的嵌套引用形状），没有值。
    assert profile["credentials"] == {"provider": {"ref": CREDENTIAL_REF}}
    for value in profile["credentials"].values():
        assert set(value) == {"ref"} and value["ref"].startswith("env:")
    assert set(profile["model"]) == {"provider", "model"}
    assert profile["model"]["provider"] == "m3-provider"
    assert profile["model"]["model"], "模型身份必须真实落到冻结 Profile 里"

    agent = external["runner_config"]["harbor"]["job"]["agents"][0]
    from motte_benchmark.harbor.config import AGENT_SPECS

    spec = AGENT_SPECS["claude-code"]
    assert agent["name"] == spec["harbor_name"]
    assert agent["model_name"] == f"{profile['model']['provider']}/{profile['model']['model']}"
    assert agent["kwargs"] == {"version": "2.0.30"}
    assert external["runner_config"]["harbor"]["credentials"] == {
        "provider": CREDENTIAL_REF,
    }


def test_credential_ref_under_a_key_like_name_is_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """引用本身是合法输入：名字像环境变量也不能被通用键名规则误伤。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)
    created = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1,
        "credentials": {"anthropic_api_key": {"ref": CREDENTIAL_REF}},
    })
    assert created.status_code == 202, created.text
    run = client.get(f"/api/v1/runs/{created.json()['id']}").json()
    profile = run["manifest"]["external_benchmark"]["profile"]
    assert profile["credentials"] == {"anthropic_api_key": {"ref": CREDENTIAL_REF}}


def test_missing_credential_ref_still_fails_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不放宽凭据要求：没有引用仍然是具名 422，且不创建 Run。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)
    missing = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1,
    })
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "HARBOR_AGENT_CREDENTIAL_REF_MISSING"
    assert client.get("/api/v1/runs").json()["total"] == 0


@pytest.mark.parametrize(
    ("credentials", "code"),
    [
        ({"provider": SYNTHETIC_KEY}, "SECRET_VALUE_IN_CREDENTIALS"),
        ({"provider": {"value": SYNTHETIC_KEY}}, "SECRET_VALUE_IN_CREDENTIALS"),
        ({"provider": {"ref": f"file:{SYNTHETIC_KEY}"}}, "CREDENTIAL_REF_REQUIRED"),
        ({"provider": {"ref": "env:"}}, "CREDENTIAL_REF_REQUIRED"),
        ({"provider": ["env:ANTHROPIC_API_KEY"]}, "CREDENTIAL_REF_REQUIRED"),
        # 键名本身像凭据也必须走同一语义，不能只得到笼统的 CREDENTIALS_REJECTED。
        ({"anthropic_api_key": SYNTHETIC_KEY}, "SECRET_VALUE_IN_CREDENTIALS"),
    ],
)
def test_credential_values_are_refused_and_never_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    credentials: dict, code: str,
) -> None:
    """任何非引用形式都必须 422，且明文值不得出现在响应或任何持久化里。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)
    response = client.post("/api/v1/benchmarks/terminal-bench/runs", json={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1, "credentials": credentials,
    })
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert SYNTHETIC_KEY not in response.text, "错误信息不得回显凭据值"
    assert client.get("/api/v1/runs").json()["total"] == 0
    _assert_not_persisted(tmp_path, SYNTHETIC_KEY)


def test_preflight_accepts_credential_refs_and_passes_for_the_real_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预检与创建同一入口：引用齐全的真实 Agent 必须给出 ok=true。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)

    report = client.get("/api/v1/benchmarks/terminal-bench/preflight", params={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1, "credential_refs": f"provider={CREDENTIAL_REF}",
    })
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["ok"] is True, payload
    assert payload["reasons"] == [], payload["reasons"]

    missing = client.get("/api/v1/benchmarks/terminal-bench/preflight", params={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "n_trials": 1,
    }).json()
    assert missing["ok"] is False
    assert "AGENT_CREDENTIAL_REF_MISSING" in missing["reasons"]


def test_preflight_refuses_credential_values_in_the_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询参数里的凭据同样只能引用；非法形式是 4xx 而不是 500。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    model_id = _published_model(client)

    plaintext = client.get("/api/v1/benchmarks/terminal-bench/preflight", params={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "credential_refs": f"provider={SYNTHETIC_KEY}",
    })
    assert plaintext.status_code == 422, plaintext.text
    assert plaintext.json()["error"]["code"] == "SECRET_VALUE_IN_CREDENTIALS"
    assert SYNTHETIC_KEY not in plaintext.text

    malformed = client.get("/api/v1/benchmarks/terminal-bench/preflight", params={
        "model": model_id, "agent_id": "claude-code", "agent_version": "2.0.30",
        "credential_refs": CREDENTIAL_REF,
    })
    assert malformed.status_code == 422, malformed.text
    assert malformed.json()["error"]["code"] == "CREDENTIAL_REF_REQUIRED"


# --------------------------------------------------------------- R2-07

def _trial_with_artifacts(
    client: TestClient, *, text: str, binary: bytes,
) -> tuple[str, str, str, str]:
    """写一个 Trial：文本工件与二进制工件都含合成哨兵，证据冻结在工作区外。"""
    import os

    from motte_sdk import terminalbench as tb
    from motte_sdk.service import build_run_service
    from motte_storage.artifacts import ArtifactStore

    service = build_run_service(None)
    record = tb.prepared_dataset(service.store)
    inputs = tb.build_run_inputs(
        record=record, run_id="run-bytes", job_id="job-bytes",
        profile=tb.terminal_bench_profile(n_trials=1),
    )
    service.create_run(
        tb.SCENARIO_VERSION, inputs["manifest"], inputs["case_ids"], run_id="run-bytes",
    )
    trials = inputs["trials"]
    service.store.trials.create_plans([dict(trial) for trial in trials])
    trial_id = str(trials[0]["trial_id"])
    artifacts = ArtifactStore(os.environ["ARTIFACT_ROOT"])
    text_id = "external-jobs/run-bytes/job-bytes/evidence/trial.log"
    binary_id = "external-jobs/run-bytes/job-bytes/evidence/capture.bin"
    text_blob = artifacts.put_bytes(text_id, text.encode("utf-8"))
    binary_blob = artifacts.put_bytes(binary_id, binary)
    service.store.trials.put_result(
        trial_id,
        {
            "trial_id": trial_id,
            "disposition": "succeeded",
            "termination": {"reason": "exit", "detail": "done"},
            "verifier_observation": {"status": "scored", "reward": 1.0},
            "artifact_refs": [
                {"artifact_id": text_id, "kind": "harbor-trial-log",
                 "sha256": "sha256:" + text_blob.sha256,
                 "size_bytes": len(text.encode("utf-8")),
                 "media_type": "text/plain", "complete": True, "truncated": False},
                {"artifact_id": binary_id, "kind": "harbor-capture",
                 "sha256": "sha256:" + binary_blob.sha256,
                 "size_bytes": len(binary),
                 "media_type": "application/octet-stream",
                 "complete": True, "truncated": False},
            ],
            "usage": {"cost_usd": None},
            "coverage": {"items": {"reward": "available"}},
            "parser_version": "harbor-terminal-bench-parser@1",
        },
        source_hash="sha256:bytes-case", parser_version="harbor-terminal-bench-parser@1",
    )
    return ("run-bytes", trial_id, text_id, binary_id)


def test_bytes_route_returns_redacted_text_with_the_source_hash(
    client: TestClient,
) -> None:
    """文本工件的 /bytes 必须是脱敏后的字节，原 hash 只作为来源标注。"""
    _prepared(client)
    run_id, trial_id, text_id, _binary_id = _trial_with_artifacts(
        client,
        text=f"claude: using {SYNTHETIC_KEY}\nstep 2 ok\n",
        binary=b"\x00\xff" + SYNTHETIC_KEY.encode() + b"\xfe\x00",
    )
    response = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{text_id}/bytes",
    )
    assert response.status_code == 200, response.text
    assert SYNTHETIC_KEY not in response.text, "原始字节路径不得绕过脱敏"
    assert "[REDACTED-SECRET]" in response.text
    # 身份不被改写：来源 hash 指向冻结证据本身，响应如实标注已脱敏。
    frozen_sha = "sha256:" + hashlib.sha256(
        f"claude: using {SYNTHETIC_KEY}\nstep 2 ok\n".encode(),
    ).hexdigest()
    assert response.headers["X-Motte-Artifact-Source-Sha256"] == frozen_sha
    assert response.headers["X-Motte-Artifact-Redacted"] == "true"

    # 普通内容接口与 /bytes 同一保护边界。
    content = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{text_id}",
    )
    assert content.status_code == 200, content.text
    assert SYNTHETIC_KEY not in content.text


def test_binary_artifact_export_is_refused_by_default(client: TestClient) -> None:
    """无法扫描的二进制默认拒绝导出，且拒绝响应本身不含原始字节。"""
    _prepared(client)
    run_id, trial_id, _text_id, binary_id = _trial_with_artifacts(
        client,
        text="ok\n",
        binary=b"\x00\xff" + SYNTHETIC_KEY.encode() + b"\xfe\x00",
    )
    response = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{binary_id}/bytes",
    )
    assert 400 <= response.status_code < 500, response.text
    assert response.json()["error"]["code"] == "ARTIFACT_RAW_EXPORT_DISABLED"
    assert SYNTHETIC_KEY not in response.text
    assert "MOTTE_ALLOW_RAW_ARTIFACT_EXPORT" in response.json()["error"]["message"]

    # 二进制元数据走普通内容接口：只给身份与不可读原因，不产出内容。
    meta = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{binary_id}",
    )
    assert meta.status_code == 200, meta.text
    payload = meta.json()
    assert payload["encoding"] == "binary"
    assert payload["text"] is None
    assert payload["sha256"].startswith("sha256:")
    assert payload["size_bytes"] == len(b"\x00\xff" + SYNTHETIC_KEY.encode() + b"\xfe\x00")
    assert SYNTHETIC_KEY not in meta.text


def test_raw_export_requires_explicit_operator_opt_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有操作员显式开启才允许原始字节，且响应如实标注未脱敏。"""
    _prepared(client)
    raw = b"\x00\xff" + SYNTHETIC_KEY.encode() + b"\xfe\x00"
    run_id, trial_id, _text_id, binary_id = _trial_with_artifacts(
        client, text="ok\n", binary=raw,
    )
    monkeypatch.setenv("MOTTE_ALLOW_RAW_ARTIFACT_EXPORT", "1")
    response = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{binary_id}/bytes",
    )
    assert response.status_code == 200, response.text
    assert response.content == raw
    assert response.headers["X-Motte-Artifact-Redacted"] == "false"
    assert response.headers["X-Motte-Artifact-Source-Sha256"] == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )


def test_artifact_display_notes_are_redacted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """note 是内容字段：读取失败原因里出现秘密形状的文本也必须脱敏。"""
    _prepared(client)
    run_id, trial_id, text_id, _binary_id = _trial_with_artifacts(
        client, text="ok\n", binary=b"\x00\xff\xfe",
    )

    from motte_sdk import terminalbench as tb

    def _boom(self, artifact_id):  # noqa: ANN001, ANN202 - 模拟底层读失败
        raise ValueError(f"reader failed with {SYNTHETIC_KEY}")

    monkeypatch.setattr(tb.FrozenEvidenceReader, "read_bytes", _boom)
    response = client.get(
        f"/api/v1/runs/{run_id}/trials/{trial_id}/artifacts/{text_id}",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["note"], payload
    assert SYNTHETIC_KEY not in response.text
    assert "[REDACTED-SECRET]" in payload["note"]
    # 身份/hash 字段逐字保留。
    assert payload["artifact_id"] == text_id
    assert payload["sha256"].startswith("sha256:")
    assert payload["size_bytes"] == len("ok\n")


# --------------------------------------------------------------- R2-12

@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dataset_revision", {"bad": "type"}),
        ("dataset_revision", ["bad"]),
        ("model", {"bad": "type"}),
        ("model", ["bad"]),
        ("n_trials", "oops"),
        ("n_trials", 1.5),
        ("task_keys", "task-key"),
        ("task_keys", [1, 2]),
        ("timeouts", ["bad"]),
        ("resources", "bad"),
        ("credentials", ["bad"]),
    ],
)
def test_run_dto_types_are_validated_before_any_repository_access(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    field: str, value: object,
) -> None:
    """非法 DTO 一律结构化 422，且不产生任何 Run/Job（review R2-12）。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)
    body: dict = {"n_trials": 1}
    body[field] = value
    response = client.post("/api/v1/benchmarks/terminal-bench/runs", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"].startswith("REQUEST_FIELD")
    assert client.get("/api/v1/runs").json()["total"] == 0, "拒绝请求不得留下 Run"


def test_dataset_revision_of_a_prepared_revision_is_still_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验不能误伤合法输入：字符串 revision 与 null 都照常工作。"""
    prepared = _prepared(client)
    _runner_probe(client, monkeypatch)
    for revision in (prepared["dataset_revision"], None, ""):
        body: dict = {"n_trials": 1}
        if revision is not None:
            body["dataset_revision"] = revision
        created = client.post("/api/v1/benchmarks/terminal-bench/runs", json=body)
        assert created.status_code == 202, created.text


def test_preflight_query_types_are_4xx_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预检的同类遗漏：非法查询参数是 4xx，不是 500。"""
    _prepared(client)
    _runner_probe(client, monkeypatch)

    bad_trials = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight", params={"n_trials": "oops"},
    )
    assert 400 <= bad_trials.status_code < 500, bad_trials.text

    zero_trials = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight", params={"n_trials": 0},
    )
    assert zero_trials.status_code == 422, zero_trials.text
    assert zero_trials.json()["error"]["code"] == "REQUEST_FIELD_OUT_OF_RANGE"

    deep_task_keys = client.get(
        "/api/v1/benchmarks/terminal-bench/preflight",
        params={"task_keys": "no-such-task"},
    )
    assert 400 <= deep_task_keys.status_code < 500, deep_task_keys.text
