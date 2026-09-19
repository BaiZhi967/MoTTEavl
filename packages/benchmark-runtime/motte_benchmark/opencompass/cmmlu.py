"""CMMLU 外部基准（M2-T11：与 C-Eval 共享 Job 基础，身份独立）。

学科清单与展示名迁移自旧适配器 b661bcdf（与 opencompass 官方 cmmlu 配置
对齐，67 学科）。CMMLU 不套用 C-Eval 四大类聚合：未知学科不报错，按
subject 呈现（见 parser 的 dataset 分派）。数据/Profile/验证与 C-Eval
完全隔离；受限来源治理独立生效。
"""
from __future__ import annotations

from typing import Any

CMMLU_BENCHMARK_ID = "cmmlu"
CMMLU_BENCHMARK_VERSION = "1"
CMMLU_PROMPT_TEMPLATE_VERSION = "cmmlu-zh-mcq-v1"
CMMLU_ANSWER_EXTRACTOR = "first-option"
CMMLU_EXTRACTOR_VERSION = "1"
CMMLU_AGGREGATION = "sample-weighted-only"
CMMLU_AGGREGATION_VERSION = "1"
CMMLU_SPLITS = ("test", "dev")

_PLACEHOLDER_VALUES = {"", "latest", "tbd", "todo", "placeholder", "unpinned", "unknown"}

CMMLU_SUBJECT_META: dict[str, str] = {
    "agronomy": "农学",
    "anatomy": "解剖学",
    "ancient_chinese": "古汉语",
    "arts": "艺术学",
    "astronomy": "天文学",
    "business_ethics": "商业伦理",
    "chinese_civil_service_exam": "中国公务员考试",
    "chinese_driving_rule": "中国驾驶规则",
    "chinese_food_culture": "中国饮食文化",
    "chinese_foreign_policy": "中国外交政策",
    "chinese_history": "中国历史",
    "chinese_literature": "中国文学",
    "chinese_teacher_qualification": "中国教师资格",
    "clinical_knowledge": "临床知识",
    "college_actuarial_science": "大学精算学",
    "college_education": "大学教育学",
    "college_engineering_hydrology": "大学工程水文学",
    "college_law": "大学法律",
    "college_mathematics": "大学数学",
    "college_medical_statistics": "大学医学统计",
    "college_medicine": "大学医学",
    "computer_science": "计算机科学",
    "computer_security": "计算机安全",
    "conceptual_physics": "概念物理学",
    "construction_project_management": "建设工程管理",
    "economics": "经济学",
    "education": "教育学",
    "electrical_engineering": "电气工程",
    "elementary_chinese": "小学语文",
    "elementary_commonsense": "小学常识",
    "elementary_information_and_technology": "小学信息技术",
    "elementary_mathematics": "初等数学",
    "ethnology": "民族学",
    "food_science": "食品学",
    "genetics": "遗传学",
    "global_facts": "全球事实",
    "high_school_biology": "高中生物",
    "high_school_chemistry": "高中化学",
    "high_school_geography": "高中地理",
    "high_school_mathematics": "高中数学",
    "high_school_physics": "高中物理学",
    "high_school_politics": "高中政治",
    "human_sexuality": "人类性行为",
    "international_law": "国际法学",
    "journalism": "新闻学",
    "jurisprudence": "法理学",
    "legal_and_moral_basis": "法律与道德基础",
    "logical": "逻辑学",
    "machine_learning": "机器学习",
    "management": "管理学",
    "marketing": "市场营销",
    "marxist_theory": "马克思主义理论",
    "modern_chinese": "现代汉语",
    "nutrition": "营养学",
    "philosophy": "哲学",
    "professional_accounting": "专业会计",
    "professional_law": "专业法学",
    "professional_medicine": "专业医学",
    "professional_psychology": "专业心理学",
    "public_relations": "公共关系",
    "security_study": "安全研究",
    "sociology": "社会学",
    "sports_science": "体育学",
    "traditional_chinese_medicine": "中医中药",
    "virology": "病毒学",
    "world_history": "世界历史",
    "world_religions": "世界宗教",
}


def _pinned(value: str, field: str) -> str:
    if not isinstance(value, str) or value.strip().lower() in _PLACEHOLDER_VALUES:
        raise ValueError(f"{field} must be pinned to a real value, not a placeholder: {value!r}")
    return value


def cmmlu_external_profile(
    *,
    dataset_revision: str,
    subjects: tuple[str, ...] | list[str],
    split: str = "test",
    few_shot: int = 0,
    few_shot_split: str = "dev",
    seed: int,
    runner_version: str,
    environment_digest: str,
    max_output_tokens: int = 1024,
) -> dict[str, Any]:
    """构造冻结的 CMMLU 外部 Profile（独立于 C-Eval 的身份与命名空间）。"""
    _pinned(dataset_revision, "dataset_revision")
    _pinned(runner_version, "runner_version")
    _pinned(environment_digest, "environment_digest")
    if split not in CMMLU_SPLITS:
        raise ValueError(f"unknown CMMLU split: {split!r} (known: {CMMLU_SPLITS})")
    if few_shot_split not in CMMLU_SPLITS:
        raise ValueError(f"unknown few-shot split: {few_shot_split!r}")
    if few_shot_split == split:
        raise ValueError(
            "few-shot examples must come from a different partition than the "
            f"evaluated split: both are {split!r}"
        )
    if type(few_shot) is not int or few_shot < 0 or few_shot > 32:
        raise ValueError("few_shot must be an integer in [0, 32]")
    normalized_subjects = tuple(dict.fromkeys(str(subject).strip() for subject in subjects))
    if not normalized_subjects or any(not subject for subject in normalized_subjects):
        raise ValueError("subjects must be a non-empty list of non-empty names")
    known = set(CMMLU_SUBJECT_META)
    unknown = [subject for subject in normalized_subjects if subject not in known]
    if unknown:
        raise ValueError(
            "unknown CMMLU subjects: " + ",".join(sorted(unknown))
            + f" (known: {len(known)} subjects)"
        )
    return {
        "benchmark_id": CMMLU_BENCHMARK_ID,
        "benchmark_version": CMMLU_BENCHMARK_VERSION,
        "dataset_revision": dataset_revision,
        "split": split,
        "selected_subjects": normalized_subjects,
        "few_shot": {"count": few_shot, "source_split": few_shot_split},
        "prompt_template_version": CMMLU_PROMPT_TEMPLATE_VERSION,
        "answer_extractor": CMMLU_ANSWER_EXTRACTOR,
        "extractor_version": CMMLU_EXTRACTOR_VERSION,
        "aggregation": CMMLU_AGGREGATION,
        "aggregation_version": CMMLU_AGGREGATION_VERSION,
        "runner_version": runner_version,
        "environment_digest": environment_digest,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
        "subject_labels": {
            subject: CMMLU_SUBJECT_META[subject] for subject in normalized_subjects
        },
    }
