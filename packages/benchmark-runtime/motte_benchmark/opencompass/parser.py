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
from pathlib import Path, PurePosixPath
from typing import Any

from motte_benchmark.trusted import TrustedDir

# v2 accepts upstream infer-only artifacts without inventing native accuracy,
# and refuses multi-model bundles instead of silently selecting a model.
PARSER_VERSION = "ceval-opencompass-parser@2"
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


class _TrustedDir(TrustedDir):
    """受信目录读取（review R06）：异常类型保持 ``CevalParserError``。

    实现已抽到 ``motte_benchmark.trusted.TrustedDir``，与 Harbor 任务准备
    共用同一套 fd 锚定与 ``O_NOFOLLOW`` 语义。
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path, error_factory=CevalParserError)


EXPERIMENT_POINTER = "experiment.json"
_KNOWN_TOP_LEVEL = {"results", "predictions", EXPERIMENT_POINTER}


def _stem_matches_dataset(name: str, dataset: str) -> bool:
    """``{dataset}-*.json`` / ``{dataset}_*.json``（stem 含学科后缀）。"""
    if not name.endswith(".json"):
        return False
    stem = name[: -len(".json")]
    return stem.startswith(f"{dataset}-") or stem.startswith(f"{dataset}_")


def _split_layout(
    rels: list[str], base_parts: tuple[str, ...], dataset: str,
) -> tuple[str | None, dict[str, list[str]], dict[str, list[str]]]:
    """把受信清单按布局归类（review R2-02）。

    支持两种固定形态（不多不少的层级，绝不无边界递归）：

    - legacy：``{base}/results/<model>/{dataset}-*.json``
    - timestamp：``{base}/<experiment>/results/<model>/{dataset}-*.json``
      （OpenCompass 0.4.2 固定版 CLI 在 --work-dir 下按时间戳建实验目录）

    返回 ``(experiment, results_by_exp, predictions_by_exp)``；legacy 记在
    ``experiment=None``。``{base}/experiment.json`` 指针文件单独返回候选。
    """
    results: dict[str, list[str]] = {}
    predictions: dict[str, list[str]] = {}
    for rel in rels:
        parts = PurePosixPath(rel).parts
        if len(parts) <= len(base_parts) or parts[:len(base_parts)] != base_parts:
            continue
        rest = parts[len(base_parts):]
        if len(rest) == 3 and rest[0] in ("results", "predictions"):
            experiment = None
            kind, name = rest[0], rest[2]
        elif (
            len(rest) == 4
            and rest[0] not in _KNOWN_TOP_LEVEL
            and rest[1] in ("results", "predictions")
        ):
            experiment = rest[0]
            kind, name = rest[1], rest[3]
        else:
            continue
        if not _stem_matches_dataset(name, dataset):
            continue
        bucket = results if kind == "results" else predictions
        bucket.setdefault(experiment or "", []).append(rel)
    results_by_exp = {key or None: value for key, value in results.items()}
    predictions_by_exp = {key or None: value for key, value in predictions.items()}
    return (None, results_by_exp, predictions_by_exp)


def _resolve_experiment(
    results_by_exp: dict[str | None, list[str]],
    experiment: str | None,
) -> str | None:
    """确定本 Job 的实验目录（review R2-02：不递归混入其他实验）。

    优先级：显式参数（含桥接 ``experiment.json`` 指针读出的值）> 唯一
    候选自动发现；legacy 与 timestamp 并存或多候选 → 明确报错。
    """
    candidates = [key for key in results_by_exp if key is not None]
    has_legacy = None in results_by_exp
    if experiment is not None:
        if experiment not in candidates:
            raise CevalParserError(
                f"missing-results: declared experiment {experiment!r} has no "
                f"results under the output root (found: {candidates or 'none'})"
            )
        return experiment
    if has_legacy and candidates:
        raise CevalParserError(
            "ambiguous-experiment: output root mixes legacy results and "
            f"experiment dirs {sorted(candidates)}; refuse to merge runs"
        )
    if len(candidates) > 1:
        raise CevalParserError(
            "ambiguous-experiment: multiple experiment dirs "
            f"{sorted(candidates)}; pass the explicit experiment or the "
            "bridge-written experiment.json pointer"
        )
    if candidates:
        return candidates[0]
    return None


def _reject_symlinks_under(symlinks: list[str], base_parts: tuple[str, ...]) -> None:
    """输出边界（base_parts，如 ``outputs/``）内的任何 symlink 都拒绝。

    静默排除会让 symlink 指向的结果文件无声消失、聚合被篡改（review
    R06）；边界之外的 symlink 与解析无关，不在此约束。
    """
    if not symlinks:
        return
    boundary = "/".join(base_parts)
    for rel in symlinks:
        inside = (
            not boundary
            or rel == boundary
            or rel.startswith(boundary + "/")
        )
        if inside:
            raise CevalParserError(
                f"path-escape: symlink inside output boundary rejected: {rel}",
            )


def _load_json_bytes(data: bytes, name: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CevalParserError(
            f"json-parse: 半写或非法 JSON 被拒绝: {name}: {error}",
        ) from error


def parse_opencompass_files(
    files: dict[str, str],
    *,
    dataset: str = "ceval",
    experiment: str | None = None,
    base_parts: tuple[str, ...] = (),
) -> dict[str, Any]:
    """纯内存解析：``{rel: text}`` → native/diagnostic 双口径结果。

    ``base_parts`` 是 rel 的公共前缀（如 ``("outputs",)``；受信根即 outputs
    时为空）。与目录形态无关（legacy / ``<experiment>/`` 前缀都可），供
    ``parse_opencompass_results``（受信目录读取）与“先冻结后解析”的
    证据 bundle（review R2-06）共用同一实现。实验目录选择与目录版一致
    （``_split_layout`` + ``_resolve_experiment``），混入多个实验目录时
    拒绝，绝不合并不同运行的结果。
    """
    rels = sorted(files)
    _, results_by_exp, predictions_by_exp = _split_layout(rels, base_parts, dataset)
    resolved_experiment = _resolve_experiment({**predictions_by_exp, **results_by_exp}, experiment)
    results_rels = sorted(results_by_exp.get(resolved_experiment) or [])
    prediction_rels = sorted(predictions_by_exp.get(resolved_experiment) or [])
    if not results_rels and not prediction_rels:
        raise CevalParserError(
            f"missing-results: 未找到 OpenCompass 结果文件: results/*/{dataset}-*.json"
        )
    model_dirs = {PurePosixPath(rel).parent.name for rel in results_rels + prediction_rels}
    if len(model_dirs) > 1:
        raise CevalParserError(
            "ambiguous-model: a single-model Job cannot merge output models: "
            + ", ".join(sorted(model_dirs)),
        )

    def _subject_of(rel: str) -> str:
        stem = PurePosixPath(rel).stem
        return (
            stem[len(f"{dataset}_"):] if stem.startswith(f"{dataset}_")
            else stem[len(f"{dataset}-"):]
        )

    def _read(rel: str) -> Any:
        return _load_json_bytes(files[rel].encode("utf-8"), PurePosixPath(rel).name)

    prediction_files: dict[str, str] = {}
    for rel in prediction_rels:
        if isinstance(_read(rel), dict):
            subject = _subject_of(rel)
            if subject in prediction_files:
                raise CevalParserError(f"ambiguous-model: duplicate predictions for {subject}")
            prediction_files[subject] = rel

    samples: list[dict[str, Any]] = []
    per_subject: dict[str, float] = {}
    detail_source: dict[str, str] = {}
    native: dict[str, Any] = {}
    conclusion_overrides = 0
    result_files = {_subject_of(rel): rel for rel in results_rels}
    if len(result_files) != len(results_rels):
        raise CevalParserError("ambiguous-model: duplicate results for the same subject")
    for subject in sorted(set(result_files) | set(prediction_files)):
        rel = result_files.get(subject)
        doc = _read(rel) if rel is not None else {}
        if not isinstance(doc, dict):
            raise CevalParserError(
                f"json-parse: 结果文件必须是对象: {PurePosixPath(rel).name}"
            )
        acc = float(doc["accuracy"]) / 100.0 if "accuracy" in doc else None
        if acc is not None:
            native[f"{dataset}_{subject}/accuracy"] = acc
        rows = _sample_rows_from_details(doc.get("details"))
        source_kind = "results_details" if rows else None
        if not rows:
            pred_rel = prediction_files.get(subject)
            if pred_rel is not None:
                doc_pred = _read(pred_rel)
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
            # Infer-only output intentionally has no target gold. Do not
            # manufacture native or diagnostic accuracy before platform scoring.
            if all(row.get("gold") is not None for row in rows):
                per_subject[subject] = round(
                    sum(1 for row in rows if row["correct"]) / len(rows), 6,
                )
            detail_source[subject] = source_kind or "results_details"
        else:
            # 仅聚合：保留 native 聚合与缺失标记，不伪造样本（M2-A03）。
            if acc is not None:
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
        "parser_version": PARSER_VERSION.replace("ceval-", f"{dataset}-", 1),
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


def parse_opencompass_results(
    root: str | Path,
    *,
    dataset: str = "ceval",
    max_file_bytes: int = 32 * 1024 * 1024,
    trusted_root: str | Path | None = None,
    experiment: str | None = None,
) -> dict[str, Any]:
    """解析 OpenCompass 输出目录 → native/diagnostic 双口径结果。

    信任边界（review R06）：``trusted_root`` 固定为原 Job 工作目录（缺省时
    为 ``root`` 自身）；``root`` 只做词法包含检查、不做 ``resolve()``。
    读取沿目录描述符逐组件 ``O_NOFOLLOW`` 打开，单文件与总量都受限。

    实验目录（review R2-02）：固定版 OpenCompass 0.4.2 CLI 在 --work-dir
    下按时间戳建实验目录（``outputs/<timestamp>/results/<model>/...``）。
    只接受恰好一层实验目录：优先 ``experiment`` 显式参数（桥接会把
    ``outputs/experiment.json`` 指针读出后传入），否则在唯一候选时自动
    发现；legacy 直排与实验目录并存、或多个实验目录 → 拒绝，绝不把
    其他实验的结果混进本 Job。

    实际解析委托 ``parse_opencompass_files``（纯内存实现，供"先冻结后
    解析"的证据 bundle 复用，review R2-06）。
    """
    outputs = Path(root)
    trusted = Path(trusted_root if trusted_root is not None else root)
    import os as _os

    if outputs.is_absolute():
        try:
            rel_base = outputs.relative_to(trusted)
        except ValueError as error:
            raise CevalParserError(
                f"path-escape: outputs {outputs} is not inside trusted root {trusted}",
            ) from error
    else:
        trusted = Path(_os.path.abspath(trusted))
        try:
            rel_base = Path(_os.path.abspath(outputs)).relative_to(trusted)
        except ValueError as error:
            raise CevalParserError(
                f"path-escape: outputs {outputs} is not inside trusted root {trusted}",
            ) from error
    base_parts = tuple(part for part in rel_base.parts if part not in ("", "."))
    if not Path(root).is_dir():
        raise CevalParserError(f"missing-output-root: {root} 不是目录")

    max_total_bytes = max_file_bytes * 8
    with _TrustedDir(trusted) as trusted_dir:
        files, symlinks = trusted_dir.list_files()
        _reject_symlinks_under(symlinks, base_parts)
        _, results_by_exp, predictions_by_exp = _split_layout(files, base_parts, dataset)
        pointer_rel = "/".join((*base_parts, EXPERIMENT_POINTER))
        if experiment is None and pointer_rel in files:
            pointer = _load_json_bytes(
                trusted_dir.read_bytes(pointer_rel, max_bytes=4096),
                EXPERIMENT_POINTER,
            )
            if isinstance(pointer, dict) and isinstance(pointer.get("experiment"), str):
                experiment = pointer["experiment"]
        resolved_experiment = _resolve_experiment(
            {**predictions_by_exp, **results_by_exp}, experiment,
        )
        wanted = sorted(set(results_by_exp.get(resolved_experiment) or []))
        wanted += sorted(set(predictions_by_exp.get(resolved_experiment) or []))
        if not wanted:
            raise CevalParserError(
                f"missing-results: 未找到 OpenCompass 结果文件: results/*/{dataset}-*.json"
            )
        frozen: dict[str, str] = {}
        total_read = 0
        for rel in wanted:
            data = trusted_dir.read_bytes(rel, max_bytes=max_file_bytes)
            total_read += len(data)
            if total_read > max_total_bytes:
                raise CevalParserError(
                    f"file-size: total output budget exceeded "
                    f"({total_read} > {max_total_bytes})"
                )
            frozen[rel] = data.decode("utf-8")
    parsed = parse_opencompass_files(
        frozen, dataset=dataset, base_parts=base_parts,
    )
    parsed["experiment"] = resolved_experiment
    return parsed
