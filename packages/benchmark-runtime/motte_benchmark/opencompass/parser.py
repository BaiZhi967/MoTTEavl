"""C-Eval/OpenCompass 结果解析（自旧适配器 b661bcdf 迁移）。

迁移来源：``llm_agent__evaluation_platform @ b661bcdf83e1c3dfb8d6062ee78817d249e86a4c``
的 ``opencompass.py``（同作者私有项目）。解析算法逐函数保留原语义：
两段式答案提取（结论优先）、C-Eval 官方 52 学科 → 四大类 + Hard 聚合、
学科宏平均。删除控制面依赖（旧 schemas/CLI 基类），并把指标拆成两个
命名空间：

- ``native.*``：OpenCompass 原始聚合与对账指标（accuracy、空预测率、
  结论覆盖数、明细来源），不被诊断重算覆盖；
- ``diagnostic.*``：平台按样本明细重算的口径（学科/大类/宏平均）。

仅聚合（无 details/predictions）的学科保留 native 聚合并标记
``aggregate_only_native_only``，不伪造样本。输入安全：文件须在受控根
内（symlink/逃逸拒绝）、大小受限、半写 JSON 拒绝。fixture 对照见
``tests/fixtures/benchmarks/ceval/manifest.json``。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PARSER_VERSION = "ceval-opencompass-parser@1"
MIGRATED_FROM = (
    "llm_agent__evaluation_platform@b661bcdf83e1c3dfb8d6062ee78817d249e86a4c "
    "packages/benchmark-adapters/src/evalstudio_benchmark_adapters/opencompass.py"
)
# 旧适配器钉定的上游 OpenCompass 版本（升级另开兼容任务，不切 latest）。
RUNNER_VERSION_PIN = "0.4.2"

EMPTY_PREDICTION_WARN_RATIO = 0.10

ANSWER_EXTRACTION_NOTE = (
    "答案提取(两段式,结论优先):(1) 对齐 C-Eval 官方评测代码口径,先在正文结论句式"
    "(答案[:：]是?为?选?/选项X是正确的/正确的一项是X,取最后一次出现)提取 A-D;"
    "(2) 无结论时回退 OpenCompass 官方提取(first_option_postprocess 模式链+裸字母兜底)。"
    "样本级对错与学科/大类聚合按两段式结果重算;OpenCompass 原始 accuracy 保留在 native "
    "指标(<dataset>_<subject>/accuracy)对账。全部失败则标记为未作答(计 0 分)。"
)

# C-Eval 官方 52 学科 → 四大类聚合（来源：hkust-nlp/ceval subject_mapping；
# STEM 20 / SocialScience 10 / Humanities 11 / Other 11，合计严格 52）。
CEVAL_SUBJECT_CATEGORIES: dict[str, str] = {
    "computer_network": "STEM",
    "operating_system": "STEM",
    "computer_architecture": "STEM",
    "college_programming": "STEM",
    "college_physics": "STEM",
    "college_chemistry": "STEM",
    "advanced_mathematics": "STEM",
    "probability_and_statistics": "STEM",
    "discrete_mathematics": "STEM",
    "electrical_engineer": "STEM",
    "metrology_engineer": "STEM",
    "high_school_mathematics": "STEM",
    "high_school_physics": "STEM",
    "high_school_chemistry": "STEM",
    "high_school_biology": "STEM",
    "middle_school_mathematics": "STEM",
    "middle_school_biology": "STEM",
    "middle_school_physics": "STEM",
    "middle_school_chemistry": "STEM",
    "veterinary_medicine": "STEM",
    "college_economics": "SocialScience",
    "business_administration": "SocialScience",
    "marxism": "SocialScience",
    "mao_zedong_thought": "SocialScience",
    "education_science": "SocialScience",
    "teacher_qualification": "SocialScience",
    "high_school_politics": "SocialScience",
    "high_school_geography": "SocialScience",
    "middle_school_politics": "SocialScience",
    "middle_school_geography": "SocialScience",
    "modern_chinese_history": "Humanities",
    "ideological_and_moral_cultivation": "Humanities",
    "logic": "Humanities",
    "law": "Humanities",
    "chinese_language_and_literature": "Humanities",
    "art_studies": "Humanities",
    "professional_tour_guide": "Humanities",
    "legal_professional": "Humanities",
    "high_school_chinese": "Humanities",
    "high_school_history": "Humanities",
    "middle_school_history": "Humanities",
    "civil_servant": "Other",
    "sports_science": "Other",
    "plant_protection": "Other",
    "basic_medicine": "Other",
    "clinical_medicine": "Other",
    "urban_and_rural_planner": "Other",
    "accountant": "Other",
    "fire_engineer": "Other",
    "environmental_impact_assessment_engineer": "Other",
    "tax_accountant": "Other",
    "physician": "Other",
}
CEVAL_CATEGORY_NAMES = ("STEM", "SocialScience", "Humanities", "Other")
CEVAL_HARD_SUBJECTS = [
    "advanced_mathematics",
    "discrete_mathematics",
    "probability_and_statistics",
    "college_chemistry",
    "college_physics",
    "high_school_mathematics",
    "high_school_chemistry",
    "high_school_physics",
]
CEVAL_ALL_SUBJECTS = sorted(CEVAL_SUBJECT_CATEGORIES)
assert len(CEVAL_ALL_SUBJECTS) == 52, "C-Eval 官方学科全集必须严格为 52"

_ANSWER_EXPLICIT_RE = re.compile(r"答案[:：]?\s*\(?([A-D])\)?")
_ANSWER_LINE_RE = re.compile(r"(?:^|\n|。|,|,|;|;)\s*\(?([A-D])\)?[.。、\s]")
_ANSWER_ANY_RE = re.compile(r"\(?([A-D])\)?")

_CONCLUSION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"答案(?:应该?|就)?(?:是|为|选)?\s*[:：]?\s*\**\s*(?:选项\s*)?\(?([A-D])\)?(?![A-Za-z0-9])"
    ),
    re.compile(r"选项?\s*\(?([A-D])\)?\s*(?:是|为)?正确的"),
    re.compile(r"正确的?一项[^\n]{0,6}?([A-D])(?![A-Za-z0-9])"),
)
_CONCLUSION_NEGATIVE_RE = re.compile(r"答案(?:不|没|无|未)")


class CevalParserError(ValueError):
    """结果解析失败；message 含类别（escape/size/parse/missing/unknown）。"""


def _extract_conclusion(text: str) -> str | None:
    """结论句式提取（旧实现原样迁移：模式优先级 + 最后一次出现）。"""
    text = str(text or "")
    if not text.strip():
        return None
    for pattern in _CONCLUSION_PATTERNS:
        found = [
            match for match in pattern.finditer(text)
            if _CONCLUSION_NEGATIVE_RE.match(text, match.start()) is None
        ]
        if found:
            return found[-1].group(1)
    return None


def _extract_option(prediction: str) -> str | None:
    """OpenCompass 式 regex 链兜底提取（旧实现原样迁移）。"""
    text = str(prediction or "")
    if not text.strip():
        return None
    for pattern in (_ANSWER_EXPLICIT_RE, _ANSWER_LINE_RE, _ANSWER_ANY_RE):
        found = pattern.findall(text)
        if found:
            return found[-1] if pattern is _ANSWER_ANY_RE else found[0]
    return None


def _resolve_prediction(
    raw_output: object, official_pred: object,
) -> tuple[object | None, str, object | None]:
    """两段式：结论优先，官方提取兜底（旧实现原样迁移）。"""
    conclusion = _extract_conclusion(str(raw_output or ""))
    if conclusion is not None:
        official = (
            official_pred.strip() if isinstance(official_pred, str) else official_pred
        )
        differs = conclusion != official
        return conclusion, "conclusion", (official if differs else None)
    return (official_pred or None), ("official" if official_pred else "fallback_regex"), None


def _category_of(subject: str) -> str:
    category = CEVAL_SUBJECT_CATEGORIES.get(subject)
    if category is None:
        raise CevalParserError(
            f"unknown-subject: 未知 C-Eval 学科 {subject!r}，不在官方 52 学科清单，拒绝静默聚合"
        )
    return category


def _aggregate_subjects(per_subject: dict[str, float]) -> dict[str, float]:
    """非 C-Eval 数据集（如 CMMLU）：学科宏平均 + subject 条目，无大类。"""
    aggregate: dict[str, float] = {}
    if per_subject:
        aggregate["accuracy"] = round(sum(per_subject.values()) / len(per_subject), 6)
    for subject, acc in per_subject.items():
        aggregate[f"subject:{subject}"] = acc
    return aggregate


def _aggregate_for(dataset: str, per_subject: dict[str, float]) -> dict[str, float]:
    if dataset == "ceval":
        return _aggregate_ceval(per_subject)
    return _aggregate_subjects(per_subject)


def _aggregate_ceval(per_subject: dict[str, float]) -> dict[str, float]:
    """C-Eval 四大类 + Hard 聚合（旧实现原样迁移：学科宏平均）。"""
    aggregate: dict[str, float] = {}
    if per_subject:
        aggregate["accuracy"] = round(sum(per_subject.values()) / len(per_subject), 6)
    groups: dict[str, list[float]] = {name: [] for name in CEVAL_CATEGORY_NAMES}
    for subject, acc in per_subject.items():
        groups[_category_of(subject)].append(acc)
    for group, values in groups.items():
        if values:
            aggregate[f"ceval_{group.lower()}"] = round(sum(values) / len(values), 6)
    hard = [per_subject[subject] for subject in CEVAL_HARD_SUBJECTS if subject in per_subject]
    if hard:
        aggregate["ceval_hard"] = round(sum(hard) / len(hard), 6)
    for subject, acc in per_subject.items():
        aggregate[f"subject:{subject}"] = acc
    return aggregate


def _sample_rows_from_details(details: object) -> list[dict]:
    """details 归一化（旧实现原样迁移：dict 索引序 / list 形态）。"""
    rows: list[dict] = []
    if isinstance(details, dict):

        def _order(key: str) -> int:
            try:
                return int(key)
            except ValueError:
                return 0

        for idx in sorted(details, key=_order):
            item = details[idx] or {}
            if not isinstance(item, dict):
                continue
            raw = item.get("origin_prediction", item.get("prediction"))
            gold = item.get("references", item.get("gold"))
            pred, extraction, official_differs = _resolve_prediction(
                raw, item.get("predictions", item.get("prediction")),
            )
            if gold is not None:
                normalized = str(pred).strip().upper() if pred is not None else None
                correct = (
                    normalized == str(gold).strip().upper() if normalized else False
                )
            else:
                correct = bool(item.get("correct", item.get("is_correct", False)))
            rows.append({
                "idx": idx,
                "prompt": item.get("prompt", item.get("origin_prompt")),
                "raw_output": raw,
                "prediction": pred,
                "official_prediction": official_differs,
                "extraction": extraction,
                "gold": gold,
                "correct": correct,
            })
    elif isinstance(details, list):
        for idx, item in enumerate(details):
            if not isinstance(item, dict):
                continue
            rows.append({
                "idx": idx,
                "prompt": item.get("origin_prompt") or item.get("prompt"),
                "raw_output": item.get("origin_prediction", item.get("prediction")),
                "prediction": item.get("prediction", item.get("predictions")),
                "gold": item.get("gold", item.get("references")),
                "correct": bool(item.get("is_correct", item.get("correct", False))),
            })
    return rows


def _safe_result_files(root: Path, pattern: str, dataset: str, max_bytes: int) -> list[Path]:
    root_real = root.resolve()
    files = sorted(set(root.rglob(pattern)))
    safe: list[Path] = []
    for file in files:
        if file.is_symlink():
            raise CevalParserError(f"path-escape: symlink result rejected: {file.name}")
        try:
            real = file.resolve()
        except OSError as error:
            raise CevalParserError(f"path-escape: {file.name}: {error}") from error
        if root_real != real and root_real not in real.parents:
            raise CevalParserError(f"path-escape: resolved path escapes root: {file.name}")
        if not file.is_file():
            continue
        if file.stat().st_size > max_bytes:
            raise CevalParserError(
                f"file-size: {file.name} is {file.stat().st_size} bytes > limit {max_bytes}"
            )
        safe.append(file)
    return safe


def _load_json(file: Path) -> Any:
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CevalParserError(
            f"json-parse: 半写或非法 JSON 被拒绝: {file.name}: {error}",
        ) from error


def parse_opencompass_results(
    root: str | Path,
    *,
    dataset: str = "ceval",
    max_file_bytes: int = 32 * 1024 * 1024,
) -> dict[str, Any]:
    """解析 OpenCompass 输出目录 → native/diagnostic 双口径结果。

    返回：samples（逐样本：预测/提取来源/结论覆盖/gold/对错/明细来源）、
    diagnostic（per_subject + aggregate[宏平均] + detail_source）、
    native（OpenCompass 原始聚合与对账指标）、parser_version、
    migrated_from、runner_version_pin。
    """
    outputs = Path(root)
    if not outputs.is_dir():
        raise CevalParserError(f"missing-output-root: {outputs} 不是目录")

    subject_files = [
        *_safe_result_files(outputs, f"results/*/{dataset}-*.json", dataset, max_file_bytes),
        *_safe_result_files(outputs, f"results/*/{dataset}_*.json", dataset, max_file_bytes),
    ]
    if not subject_files:
        raise CevalParserError(
            f"missing-results: 未找到 OpenCompass 结果文件: results/*/{dataset}-*.json"
        )
    prediction_files = {}
    for file in [
        *_safe_result_files(outputs, f"predictions/*/{dataset}-*.json", dataset, max_file_bytes),
        *_safe_result_files(outputs, f"predictions/*/{dataset}_*.json", dataset, max_file_bytes),
    ]:
        stem = file.stem
        subject = (
            stem[len(f"{dataset}_"):] if stem.startswith(f"{dataset}_")
            else stem[len(f"{dataset}-"):]
        )
        doc = _load_json(file)
        if isinstance(doc, dict):
            prediction_files.setdefault(subject, file)

    def _subject_of(file: Path) -> str:
        stem = file.stem
        return (
            stem[len(f"{dataset}_"):] if stem.startswith(f"{dataset}_")
            else stem[len(f"{dataset}-"):]
        )

    samples: list[dict[str, Any]] = []
    per_subject: dict[str, float] = {}
    detail_source: dict[str, str] = {}
    native: dict[str, Any] = {}
    conclusion_overrides = 0
    for file in sorted(subject_files, key=lambda item: _subject_of(item)):
        subject = _subject_of(file)
        doc = _load_json(file)
        if not isinstance(doc, dict):
            raise CevalParserError(f"json-parse: 结果文件必须是对象: {file.name}")
        acc = float(doc.get("accuracy", 0.0)) / 100.0
        native[f"{dataset}_{subject}/accuracy"] = acc
        rows = _sample_rows_from_details(doc.get("details"))
        source_kind = "results_details" if rows else None
        if not rows:
            pred_file = prediction_files.get(subject)
            if pred_file is not None:
                doc_pred = _load_json(pred_file)
                for idx in sorted(
                    doc_pred, key=lambda key: int(key) if str(key).isdigit() else 0,
                ):
                    item = doc_pred[idx] or {}
                    if not isinstance(item, dict):
                        continue
                    gold = item.get("gold")
                    raw = str(item.get("prediction", ""))
                    conclusion = _extract_conclusion(raw)
                    pred = conclusion if conclusion is not None else _extract_option(raw)
                    rows.append({
                        "idx": idx,
                        "prompt": item.get("origin_prompt"),
                        "raw_output": item.get("prediction"),
                        "prediction": pred,
                        "extraction": "conclusion" if conclusion is not None else "fallback_regex",
                        "gold": gold,
                        "correct": bool(
                            pred is not None and gold is not None
                            and str(pred).upper() == str(gold).strip().upper()
                        ),
                    })
                source_kind = "predictions_fallback"
        if rows:
            per_subject[subject] = round(
                sum(1 for row in rows if row["correct"]) / len(rows), 6,
            )
            detail_source[subject] = source_kind or "results_details"
        else:
            # 仅聚合：保留 native 聚合与缺失标记，不伪造样本（M2-A03）。
            per_subject[subject] = acc
            detail_source[subject] = "aggregate_only_native_only"
        conclusion_overrides += sum(
            1 for row in rows if row.get("official_prediction") is not None
        )
        for row in rows:
            samples.append({
                "sample_id": f"{subject}-{row['idx']}",
                "subject": subject,
                "prediction": row.get("prediction"),
                "extraction": row.get("extraction"),
                "official_prediction": row.get("official_prediction"),
                "gold": row.get("gold"),
                "correct": row.get("correct"),
                "raw_output": row.get("raw_output"),
                "prompt": row.get("prompt"),
                "detail_source": detail_source[subject],
            })

    if samples:
        empty = sum(
            1 for sample in samples if not str(sample["prediction"] or "").strip()
        )
        ratio = round(empty / len(samples), 4)
        native["empty_prediction_ratio"] = ratio
        if ratio > EMPTY_PREDICTION_WARN_RATIO:
            native["empty_prediction_warning"] = (
                f"{empty}/{len(samples)} 样本({ratio:.0%})预测为空、按 0 分计。"
                "常见原因:思考型模型在 max_tokens 预算内只产出思维链而正文为空,或答案提取失败。"
            )
    native["conclusion_override_count"] = conclusion_overrides
    native["answer_extraction"] = ANSWER_EXTRACTION_NOTE
    native["sample_detail_source"] = sorted(set(detail_source.values()))

    return {
        "parser_version": PARSER_VERSION,
        "migrated_from": MIGRATED_FROM,
        "runner_version_pin": RUNNER_VERSION_PIN,
        "dataset": dataset,
        "samples": samples,
        "native": native,
        "diagnostic": {
            "per_subject": per_subject,
            "aggregate": _aggregate_for(dataset, per_subject),
            "detail_source": detail_source,
        },
    }
