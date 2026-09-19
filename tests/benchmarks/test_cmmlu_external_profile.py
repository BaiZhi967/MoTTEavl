"""M2-T11：CMMLU 独立身份与证据（复用 Job 基础，不继承 C-Eval 验证）。

反例与期望：
- CMMLU 使用独立 revision/Profile/样本 hash；与 C-Eval 的准备状态互相
  隔离，不能读 C-Eval 的验证结论。
- 聚合不套用 C-Eval 四大类（未知学科不报错，按 subject 呈现）。
- 受限来源未核验时不执行（治理门禁独立生效）。
"""
import json

import pytest

from motte_benchmark.opencompass.cmmlu import (
    CMMLU_BENCHMARK_ID,
    cmmlu_external_profile,
)
from motte_benchmark.opencompass.parser import parse_opencompass_results
from motte_sdk.benchmark_catalog import BenchmarkCatalog, prepare_external_dataset


def _files(subject="agronomy"):
    row = {
        "id": "cm-1", "subject": subject, "question": "Q", "A": "1", "B": "2",
        "C": "3", "D": "4", "answer": "A",
    }
    return {"cmmlu_val.jsonl": (json.dumps(row, ensure_ascii=False) + "\n").encode()}


def test_cmmlu_has_own_identity_and_evidence():
    # 独立 Profile：benchmark id/version、revision 命名空间独立。
    profile = cmmlu_external_profile(
        dataset_revision="cmmlu-rev-1", subjects=("agronomy", "anatomy"),
        seed=3, runner_version="opencompass-0.4.2",
        environment_digest="sha256:" + "4" * 64,
    )
    assert profile["benchmark_id"] == CMMLU_BENCHMARK_ID == "cmmlu"
    assert profile["benchmark_version"] == "1"
    assert profile["answer_extractor"]  # 独立提取器版本记录
    ceval_like = dict(profile)
    ceval_like["benchmark_id"] = "ceval"
    assert profile != ceval_like  # 与 ceval Profile 不共享身份

    # 独立准备状态：CMMLU ready 不影响 ceval 条目，反之亦然。
    catalog = BenchmarkCatalog()
    catalog.register("cmmlu", benchmark_version="1")
    catalog.register("ceval", benchmark_version="1")
    prepared = prepare_external_dataset(
        benchmark_id="cmmlu", files=_files(), dataset_revision="cmmlu-rev-1",
    )
    assert prepared.state == "ready"
    catalog.update_dataset("cmmlu", prepared)
    assert catalog.status("cmmlu")["status"] == "prepared"
    assert catalog.status("ceval")["status"] == "registered"  # 未继承 ceval 数据
    assert catalog.status("ceval")["blockers"] == ["DATASET_UNPREPARED", "RUNNER_NOT_CONNECTED", "PROFILE_NOT_VALIDATED"]

    # 样本 hash 独立：同内容不同 benchmark 的 manifest 记录互不覆盖。
    ceval_prepared = prepare_external_dataset(
        benchmark_id="ceval", files=_files(), dataset_revision="cmmlu-rev-1",
    )
    assert ceval_prepared.manifest[0].case_id == prepared.manifest[0].case_id
    assert ceval_prepared.benchmark_id == "ceval"

    # 受限官方路径：无受信核验器 → 阻断（独立生效，不读 ceval 结论）。
    blocked = prepare_external_dataset(
        benchmark_id="cmmlu", files=_files(), dataset_revision="cmmlu-rev-official",
        provenance_target="verified-official",
        approval_evidence={
            "actor": "operator", "purpose": "p", "ticket": "T", "source_id": "cmmlu",
            "actions": ["import"], "approved_by": "operator",
            "issued_at": "2026-09-19T00:00:00Z", "expires_at": "2027-09-19T00:00:00Z",
        },
    )
    assert blocked.state == "failed"
    assert "OFFICIAL_APPROVAL_UNVERIFIED" in blocked.reasons


def test_cmmlu_aggregation_has_no_ceval_categories(tmp_path):
    results = tmp_path / "outputs" / "results" / "mock-model"
    results.mkdir(parents=True)
    (results / "cmmlu-agronomy.json").write_text(json.dumps({
        "accuracy": 100.0,
        "details": {
            "0": {"prompt": "Q", "origin_prediction": "答案为 A",
                  "predictions": "A", "references": "A"},
        },
    }), encoding="utf-8")
    parsed = parse_opencompass_results(tmp_path / "outputs", dataset="cmmlu")
    assert parsed["diagnostic"]["per_subject"] == {"agronomy": 1.0}
    aggregate = parsed["diagnostic"]["aggregate"]
    assert aggregate["subject:agronomy"] == 1.0
    assert aggregate["accuracy"] == 1.0
    # 不产生 C-Eval 四大类/Hard 聚合。
    assert not [key for key in aggregate if key.startswith("ceval_")]


def test_cmmlu_parser_still_validates_ceval_categories(tmp_path):
    results = tmp_path / "outputs" / "results" / "mock-model"
    results.mkdir(parents=True)
    (results / "ceval-not_a_subject.json").write_text(json.dumps({"accuracy": 50.0}), encoding="utf-8")
    with pytest.raises(Exception, match="unknown-subject"):
        parse_opencompass_results(tmp_path / "outputs", dataset="ceval")
