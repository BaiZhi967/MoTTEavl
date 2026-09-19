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
import os
import re
import stat
from pathlib import Path, PurePosixPath
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


class _TrustedDir:
    """fd 锚定的受信目录读取（review R06）。

    信任边界固定在调用方给定的目录（原 Job 工作目录），沿目录描述符逐组件
    ``O_NOFOLLOW`` 打开；symlink 不进入受信清单（根目录本身是 symlink 也
    拒绝）。文件读取先 ``fstat`` 校验大小，再读全量。
    """

    def __init__(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._fd = os.open(path, flags)
        except OSError as error:
            raise CevalParserError(
                f"path-escape: trusted root rejected: {path}: {error}",
            ) from error

    def __enter__(self) -> _TrustedDir:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def list_files(self) -> tuple[list[str], list[str]]:
        """受信目录下的常规文件与被排除的 symlink（posix 相对路径）。

        symlink 不进入受控清单，但调用方对输出边界内的 symlink 显式拒绝
        （防止静默丢结果/篡改聚合），见 ``_reject_symlinks_under``。
        """
        out: list[str] = []
        symlinks: list[str] = []
        self._walk(self._fd, "", out, symlinks)
        return (sorted(out), sorted(symlinks))

    def _walk(
        self, dir_fd: int, prefix: str, out: list[str], symlinks: list[str],
    ) -> None:
        for name in os.listdir(dir_fd):
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(prefix + name)
                continue
            if stat.S_ISDIR(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                child = os.open(name, flags, dir_fd=dir_fd)
                try:
                    self._walk(child, prefix + name + "/", out, symlinks)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                out.append(prefix + name)

    def read_bytes(self, rel: str, *, max_bytes: int) -> bytes:
        parts = [part for part in PurePosixPath(rel).parts if part]
        if not parts:
            raise CevalParserError(f"path-escape: not a file: {rel}")
        dir_fd = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    child = os.open(part, flags, dir_fd=dir_fd)
                except OSError as error:
                    raise CevalParserError(
                        f"path-escape: component rejected: {part}: {error}",
                    ) from error
                os.close(dir_fd)
                dir_fd = child
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                file_fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
            except OSError as error:
                raise CevalParserError(
                    f"path-escape: file rejected: {parts[-1]}: {error}",
                ) from error
        finally:
            os.close(dir_fd)
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise CevalParserError(f"path-escape: not a regular file: {rel}")
            if info.st_size > max_bytes:
                raise CevalParserError(
                    f"file-size: {rel} is {info.st_size} bytes > limit {max_bytes}"
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1 << 16)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > max_bytes:
                    raise CevalParserError(
                        f"file-size: {rel} exceeds limit {max_bytes} while reading"
                    )
        finally:
            os.close(file_fd)


def _result_candidate(
    rel: str, base_parts: tuple[str, ...], kind: str, dataset: str,
) -> bool:
    """匹配 ``{base}/results/*/{dataset}[-_]*.json``（predictions 同形）。"""
    parts = PurePosixPath(rel).parts
    if len(parts) != len(base_parts) + 3:
        return False
    if parts[:len(base_parts)] != base_parts or parts[len(base_parts)] != kind:
        return False
    name = parts[-1]
    if not name.endswith(".json"):
        return False
    stem = name[: -len(".json")]
    return stem.startswith(f"{dataset}-") or stem.startswith(f"{dataset}_")


def _safe_result_files(
    trusted: _TrustedDir,
    base_parts: tuple[str, ...],
    kind: str,
    dataset: str,
    max_bytes: int,
) -> list[str]:
    """受信目录内枚举结果文件；输出边界内的 symlink 显式拒绝。"""
    files, symlinks = trusted.list_files()
    _reject_symlinks_under(symlinks, base_parts)
    return [
        rel for rel in files
        if _result_candidate(rel, base_parts, kind, dataset)
    ]


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


def parse_opencompass_results(
    root: str | Path,
    *,
    dataset: str = "ceval",
    max_file_bytes: int = 32 * 1024 * 1024,
    trusted_root: str | Path | None = None,
) -> dict[str, Any]:
    """解析 OpenCompass 输出目录 → native/diagnostic 双口径结果。

    信任边界（review R06）：``trusted_root`` 固定为原 Job 工作目录（缺省时
    为 ``root`` 自身）；``root`` 只做词法包含检查、不做 ``resolve()``——
    outputs 指向工作目录外的 symlink 不被重新认定为安全根。读取沿目录
    描述符逐组件 ``O_NOFOLLOW`` 打开，单文件与总量都受限。

    返回：samples（逐样本：预测/提取来源/结论覆盖/gold/对错/明细来源）、
    diagnostic（per_subject + aggregate[宏平均] + detail_source）、
    native（OpenCompass 原始聚合与对账指标）、parser_version、
    migrated_from、runner_version_pin。
    """
    outputs = Path(root)
    trusted = Path(trusted_root if trusted_root is not None else root)
    # 词法归一（abspath 不解析 symlink）：outputs 与 trusted 同一形态后再做
    # 包含判断，相对 work_root 下也能得到正确的边界前缀。
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
        subject_rels = sorted(set(
            _safe_result_files(trusted_dir, base_parts, "results", dataset, max_file_bytes)
        ))
        if not subject_rels:
            raise CevalParserError(
                f"missing-results: 未找到 OpenCompass 结果文件: results/*/{dataset}-*.json"
            )
        prediction_rels = sorted(set(
            _safe_result_files(trusted_dir, base_parts, "predictions", dataset, max_file_bytes)
        ))
        total_read = 0

        def _read(rel: str) -> Any:
            nonlocal total_read
            data = trusted_dir.read_bytes(rel, max_bytes=max_file_bytes)
            total_read += len(data)
            if total_read > max_total_bytes:
                raise CevalParserError(
                    f"file-size: total output budget exceeded ({total_read} > {max_total_bytes})"
                )
            return _load_json_bytes(data, PurePosixPath(rel).name)

        def _subject_of(rel: str) -> str:
            stem = PurePosixPath(rel).stem
            return (
                stem[len(f"{dataset}_"):] if stem.startswith(f"{dataset}_")
                else stem[len(f"{dataset}-"):]
            )

        prediction_files: dict[str, str] = {}
        for rel in prediction_rels:
            if isinstance(_read(rel), dict):
                prediction_files.setdefault(_subject_of(rel), rel)

        samples: list[dict[str, Any]] = []
        per_subject: dict[str, float] = {}
        detail_source: dict[str, str] = {}
        native: dict[str, Any] = {}
        conclusion_overrides = 0
        for rel in subject_rels:
            subject = _subject_of(rel)
            doc = _read(rel)
            if not isinstance(doc, dict):
                raise CevalParserError(
                    f"json-parse: 结果文件必须是对象: {PurePosixPath(rel).name}"
                )
            acc = float(doc.get("accuracy", 0.0)) / 100.0
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
