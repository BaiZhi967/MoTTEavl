"""M4-T11：Inspect EvalLog 只读导入（官方 .json 完整格式；不执行日志内容）。

主断言 test_inspect_import_never_executes_log：日志里的命令/路径只是
数据；未知 schema 拒绝；同身份同内容重导幂等；同身份异内容冲突；
aggregate/summary 不制造样本；原始内容冻结可复核（R28）。
"""
from __future__ import annotations

import json

import pytest

from motte_harness.inspect import (
    InspectHarness,
    InspectLogError,
    import_inspect_log,
    load_import_raw,
)
from motte_harness.parsers.inspect import parse_inspect_log


def _eval_log(*, samples=None, scores=None, created="2026-09-20T00:00:00Z",
              model="test-model", task="hello-task", status="success",
              extra=None) -> str:
    payload = {
        "version": 2,
        "status": status,
        "eval": {"eval_id": f"eval-{created}", "run_id": f"run-{created}",
                 "created": created, "model": model, "task": task},
        "plan": {"steps": [], "config": {}, "separators": []},
        "results": {"total_samples": len(samples or [])},
    }
    if samples is not None:
        payload["samples"] = samples
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


SAMPLES = [
    {
        "id": "sample-1", "epoch": 1,
        "scores": {"exact": {"name": "exact", "value": 1.0, "answer": "V1"}},
        "transcript": "text transcript",
    },
    {
        "id": "sample-2", "epoch": 1,
        "scores": {"exact": {"name": "exact", "value": 0.0, "answer": "V2"}},
    },
]
SAMPLE_LOG = _eval_log(samples=SAMPLES)


class TestParser:
    def test_parses_official_object_with_samples(self):
        parsed = parse_inspect_log(SAMPLE_LOG)
        assert parsed["schema"] == "inspect-eval-log-v2"
        assert parsed["header"]["model"] == "test-model"
        assert [s["id"] for s in parsed["samples"]] == ["sample-1", "sample-2"]
        assert parsed["samples"][0]["scores"]["exact"]["value"] == 1.0
        assert parsed["samples"][0]["scores"]["exact"]["answer"] == "V1"
        assert parsed["coverage"] == "complete"
        assert parsed["identity"]["eval_id"] == "eval-2026-09-20T00:00:00Z"

    def test_unknown_schema_rejected(self):
        with pytest.raises(InspectLogError) as raised:
            parse_inspect_log(json.dumps({"weird": "shape"}))
        assert raised.value.code == "UNKNOWN_SCHEMA"

    def test_old_jsonl_header_shape_is_rejected_not_guessed(self):
        with pytest.raises(InspectLogError) as raised:
            parse_inspect_log("\n".join([
                json.dumps({"model": "m"}),
                json.dumps({"sample": {"id": "s1", "scores": {}}}),
            ]))
        assert raised.value.code == "MALFORMED_JSON"

    def test_aggregate_only_does_not_fabricate_samples(self):
        log = _eval_log()  # 无 samples 字段（summary-only 导出）
        with pytest.raises(InspectLogError) as raised:
            parse_inspect_log(log)
        assert raised.value.code == "AGGREGATE_ONLY"
        assert "--full" in str(raised.value)

    def test_size_limit(self):
        with pytest.raises(InspectLogError) as raised:
            parse_inspect_log("x" * 65_000_000)
        assert raised.value.code == "SIZE_LIMIT"

    def test_non_object_rejected(self):
        with pytest.raises(InspectLogError) as raised:
            parse_inspect_log("[]")
        assert raised.value.code == "MALFORMED_JSON"


class TestImport:
    def test_inspect_import_never_executes_log(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # 日志含"命令/路径"内容：导入只把它当数据（无执行路径可观察，
        # 断言导入成功且内容原样保留在样本证据里，进程无副作用）
        dangerous = _eval_log(samples=[{
            "id": "s1", "epoch": 1,
            "scores": {"exact": {"name": "exact", "value": 1}},
            "transcript": "ran `rm -rf /tmp/x`; wrote /etc/passwd-like path",
        }])
        report = import_inspect_log(dangerous, name="danger-probe")
        assert report["imported"] is True
        assert report["sample_count"] == 1
        assert not (tmp_path / "etc").exists()
        assert not (tmp_path / "tmp").exists()

    def test_same_source_reimport_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        first = import_inspect_log(SAMPLE_LOG, name="probe")
        second = import_inspect_log(SAMPLE_LOG, name="probe")
        assert first["import_id"] == second["import_id"]
        assert first["idempotent"] is False
        assert second["idempotent"] is True
        # 原生分数标记 imported（与平台重评区分）
        assert first["score_source"] == "inspect-native"
        assert first["samples"][0]["scores"]["exact"]["value"] == 1.0
        # R28：原始内容冻结并可只读复核。
        raw = load_import_raw(first["import_id"])
        assert raw is not None and json.loads(raw)["eval"]["model"] == "test-model"
        assert first["raw_frozen"].endswith(".source")

    def test_same_identity_different_content_conflicts(self, tmp_path, monkeypatch):
        """R28：改分后的同一来源是身份冲突，不是新导入。"""
        monkeypatch.chdir(tmp_path)
        first = import_inspect_log(SAMPLE_LOG, name="probe")
        tampered = _eval_log(samples=[
            {**SAMPLES[0], "scores": {"exact": {"name": "exact", "value": 0.0}}},
            SAMPLES[1],
        ])
        with pytest.raises(InspectLogError) as raised:
            import_inspect_log(tampered, name="probe")
        assert raised.value.code == "IMPORT_IDENTITY_CONFLICT"
        # 不同来源（不同 created）是独立导入，不冲突。
        other = import_inspect_log(
            _eval_log(samples=SAMPLES, created="2026-09-21T00:00:00Z"),
            name="other-run",
        )
        assert other["import_id"] != first["import_id"]
        assert other["idempotent"] is False

    def test_inspect_execution_stays_unsupported(self):
        with pytest.raises(InspectLogError) as raised:
            InspectHarness().run("anything")
        assert raised.value.code == "INSPECT_EXECUTION_UNSUPPORTED"


def test_api_import_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient

    from apps.api.app.main import create_app
    from motte_storage.run_store import InMemoryRunStore

    client = TestClient(create_app(store=InMemoryRunStore()))
    response = client.post(
        "/api/v1/inspect/import",
        json={"name": "api-probe", "content": SAMPLE_LOG},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] is True
    assert body["sample_count"] == 2

    bad = client.post(
        "/api/v1/inspect/import",
        json={"name": "bad", "content": "not-json"},
    )
    assert bad.status_code == 422
