"""M2-T06：C-Eval Parser 迁移与 native/diagnostic 双口径对照。

对照依据：旧仓库 ``b661bcdf``（同作者私有项目）的 OpenCompass 适配器解析
算法原样执行产出的 golden（见 ``tests/fixtures/benchmarks/ceval/manifest.json``
的 provenance）。fixture 为合成内容，不是官方 C-Eval 数据。

反例与期望：
- 逐样本/逐学科/聚合与 golden 完全一致（新旧 Parser 对照）。
- 仅聚合的学科保留 native 聚合与缺失标记，不伪造样本。
- 路线图确定性 fixture：selected=4、attempted=3、correct=2、
  selected-case accuracy=0.5、coverage=0.75；无原生聚合不编造 native。
- 越界/超大/半写输入拒绝。
"""
import json
from pathlib import Path

import pytest

from motte_benchmark.opencompass.parser import (
    CevalParserError,
    parse_opencompass_results,
)
from motte_eval.ceval import diagnostic_metrics

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/benchmarks/ceval"


def test_parser_native_diagnostic_parity():
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest["expected"]

    parsed = parse_opencompass_results(FIXTURE / "outputs", dataset="ceval")

    # 逐样本对照（预测字母、提取来源、结论覆盖、gold、对错、明细来源）。
    assert len(parsed["samples"]) == len(expected["samples"])
    by_id = {sample["sample_id"]: sample for sample in parsed["samples"]}
    for golden in expected["samples"]:
        actual = by_id[golden["sample_id"]]
        assert actual["prediction"] == golden["prediction"]
        assert actual["extraction"] == golden["extraction"]
        assert actual["official_prediction"] == golden["official_prediction"]
        assert actual["gold"] == golden["gold"]
        assert actual["correct"] == golden["correct"]
        assert actual["detail_source"] == golden["detail_source"]

    # 逐学科与聚合对照（诊断重算）。
    assert parsed["diagnostic"]["per_subject"] == expected["per_subject"]
    assert parsed["diagnostic"]["aggregate"] == expected["aggregate"]

    # native/diagnostic 两个命名空间分开：native 保留文件里的 OpenCompass
    # 原始聚合（不被重算覆盖）；诊断重算允许与 native 不同并保留两份。
    assert parsed["native"]["ceval_logic/accuracy"] == pytest.approx(0.5)
    assert parsed["diagnostic"]["per_subject"]["logic"] == pytest.approx(0.666667)
    for subject in manifest["aggregate_only_subjects"]:
        native_key = f"ceval_{subject}/accuracy"
        assert parsed["native"][native_key] == pytest.approx(0.6667, abs=1e-4)
        assert parsed["diagnostic"]["detail_source"][subject] == "aggregate_only_native_only"
        assert not [
            sample for sample in parsed["samples"] if sample["subject"] == subject
        ]
    for subject in manifest["predictions_fallback_subjects"]:
        assert parsed["diagnostic"]["detail_source"][subject] == "predictions_fallback"
    assert parsed["native"]["conclusion_override_count"] == expected["conclusion_override_count"]
    assert parsed["native"]["empty_prediction_ratio"] == pytest.approx(
        expected["empty_prediction_ratio"],
    )
    assert parsed["parser_version"].startswith("ceval-opencompass-parser@1")
    assert "b661bcdf" in parsed["migrated_from"]


def test_parser_input_limits_and_path_safety(tmp_path):
    outputs = tmp_path / "outputs" / "results" / "mock-model"
    outputs.mkdir(parents=True)
    (outputs / "ceval-logic.json").write_text(
        json.dumps({"accuracy": 50.0, "details": {
            "0": {"prompt": "L0", "origin_prediction": "答案为 B", "predictions": "B", "references": "B"},
        }}),
        encoding="utf-8",
    )
    parsed = parse_opencompass_results(tmp_path / "outputs", dataset="ceval")
    assert parsed["diagnostic"]["per_subject"]["logic"] == 1.0

    # 越界：results 文件是指向根外文件的 symlink → 拒绝。
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"accuracy": 1.0}), encoding="utf-8")
    (outputs / "ceval-law.json").symlink_to(outside)
    with pytest.raises(CevalParserError, match="escape"):
        parse_opencompass_results(tmp_path / "outputs", dataset="ceval")

    (outputs / "ceval-law.json").unlink()
    # 半写文件（非法 JSON）→ 拒绝，不伪造记录。
    (outputs / "ceval-law.json").write_text('{"accuracy": 50.0, "det', encoding="utf-8")
    with pytest.raises(CevalParserError, match="parse"):
        parse_opencompass_results(tmp_path / "outputs", dataset="ceval")

    # 超大文件 → 拒绝。
    (outputs / "ceval-law.json").unlink()
    (outputs / "ceval-law.json").write_text(
        json.dumps({"accuracy": 50.0})[:-1] + ', "pad": "' + "x" * 200000 + '"}',
        encoding="utf-8",
    )
    with pytest.raises(CevalParserError, match="size"):
        parse_opencompass_results(tmp_path / "outputs", dataset="ceval", max_file_bytes=65536)


def test_roadmap_deterministic_fixture():
    # 需求第 7 节：selected=4、3 条回答（2 对 1 错）、Runner 非零退出。
    records = [
        {"case_id": "subject-a:1", "prediction": "A", "gold": "A"},
        {"case_id": "subject-a:2", "prediction": "B", "gold": "B"},
        {"case_id": "subject-a:3", "prediction": "C", "gold": "D"},
    ]
    metrics = diagnostic_metrics(
        selected_case_ids=["subject-a:1", "subject-a:2", "subject-a:3", "subject-a:4"],
        records=records,
        runner_exit_code=1,
    )
    assert metrics["selected"] == 4
    assert metrics["attempted"] == 3
    assert metrics["correct"] == 2
    assert metrics["wrong"] == 1
    assert metrics["not_attempted"] == 1
    assert metrics["diagnostic.selected_case_accuracy"] == 0.5
    assert metrics["diagnostic.observed_call_coverage"] == 0.75
    assert metrics["execution"]["failed"] is True
    # 无原生聚合输入 → 不编造任何 native 分数。
    assert not [key for key in metrics if key.startswith("native.")]
    assert metrics["scorer_version"].startswith("ceval-diagnostic@")


def test_unscored_without_gold():
    records = [
        {"case_id": "c:1", "prediction": "A", "gold": None},
        {"case_id": "c:2", "prediction": "B", "gold": None},
    ]
    metrics = diagnostic_metrics(selected_case_ids=["c:1", "c:2"], records=records)
    assert metrics["unscored"] is True
    assert metrics["diagnostic.selected_case_accuracy"] is None
    assert "native.accuracy" not in metrics


def test_native_discrepancy_is_recorded_not_overwritten():
    # 原始聚合与样本重算不一致：两份保留并记录差异原因。
    records = [
        {"case_id": "c:1", "prediction": "A", "gold": "A"},
    ]
    metrics = diagnostic_metrics(
        selected_case_ids=["c:1"], records=records,
        native_aggregate={"accuracy": 0.25},
    )
    assert metrics["native.accuracy"] == 0.25
    assert metrics["diagnostic.selected_case_accuracy"] == 1.0
    assert metrics["discrepancy"]["accuracy"] == {
        "native": 0.25, "diagnostic": 1.0,
        "reason": "extractor=conclusion-first@b661bcdf-migrated; "
                  "native aggregate and sample-level recomputation differ",
    }
