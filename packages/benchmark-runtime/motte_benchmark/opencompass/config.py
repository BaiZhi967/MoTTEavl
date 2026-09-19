"""OpenCompass 运行配置转换（M2-T05）。

把冻结的 C-Eval 外部 Profile、已发布 ModelProfile 快照与样本行转换成
Runner 配置：

- 模型参数经 allowlist 转换，未知参数拒绝；
- 凭据按引用（``{"ref": "env:NAME"}``）传入，任何原始值拒绝并报
  ``SECRET_VALUE_IN_CREDENTIALS``；配置只含引用，hash 基于脱敏配置；
- few-shot 示例只允许来自与评测分区不同的合法分区，并携带该分区 gold；
  目标样本 gold 不进入 prompt；
- native/Provider transport/操作员三种重试分开记录，不叠加；
- ``scope``（smoke/custom-subset/full）始终随配置。

Runner 的原生调用路径标注 ``transport_owner`` 与观测缺口；可核验的
Provider hook 由 T07/T10 的桥接接入。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

ALLOWED_MODEL_PARAMETERS: frozenset[str] = frozenset(
    {"temperature", "top_p", "max_output_tokens"},
)
_SCOPES = ("smoke", "custom-subset", "full")
_ANSWER_LABELS = ("A", "B", "C", "D")


class OpenCompassConfigError(ValueError):
    """配置转换失败；code 前缀进入证据。"""


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OpenCompassConfigError(f"CONFIG_INVALID:{label} must be an object")
    return value


def render_mcq_prompt(
    *,
    subject: str,
    question: str,
    options: Mapping[str, str],
    few_shot: Sequence[Mapping[str, Any]],
) -> str:
    """渲染单选题 prompt：few-shot 示例带 gold，目标问题只留 Answer 槽位。"""
    lines: list[str] = [f"以下是关于{subject}的单项选择题，请选出唯一正确选项。"]
    for example in few_shot:
        lines.append("")
        lines.append(f"题目：{example.get('question', '')}")
        for label in _ANSWER_LABELS:
            lines.append(f"{label}. {example.get(label, '')}")
        lines.append(f"Answer: {example.get('answer', '')}")
    lines.append("")
    lines.append(f"题目：{question}")
    for label in _ANSWER_LABELS:
        lines.append(f"{label}. {options.get(label, '')}")
    lines.append("Answer:")
    return "\n".join(lines)


def _credential_refs(credentials: Mapping[str, Any] | None) -> dict[str, str]:
    if credentials is None:
        return {}
    refs: dict[str, str] = {}
    for name, value in credentials.items():
        if isinstance(value, Mapping) and set(value) == {"ref"} and isinstance(
            value.get("ref"), str,
        ) and value.get("ref", "").startswith("env:"):
            refs[str(name)] = str(value["ref"])
            continue
        # 任何非引用形式（原始值、嵌套对象）都按秘密处理：拒绝并要求改引用。
        raise OpenCompassConfigError(
            f"SECRET_VALUE_IN_CREDENTIALS:{name}: pass credentials by reference "
            "({'ref': 'env:NAME'}), never as values"
        )
    return refs


def _model_section(model: Mapping[str, Any]) -> dict[str, Any]:
    parameters = model.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise OpenCompassConfigError("CONFIG_INVALID:model.parameters must be an object")
    unknown = sorted(set(parameters) - ALLOWED_MODEL_PARAMETERS)
    if unknown:
        raise OpenCompassConfigError(
            "UNKNOWN_MODEL_PARAMETER:" + ",".join(unknown)
            + f" (allowed: {','.join(sorted(ALLOWED_MODEL_PARAMETERS))})",
        )
    allowed = {key: parameters[key] for key in sorted(parameters)}
    ceiling = model.get("max_output_tokens")
    if ceiling is not None and "max_output_tokens" in allowed:
        requested = allowed["max_output_tokens"]
        if (
            isinstance(requested, int) and isinstance(ceiling, int)
            and requested > ceiling
        ):
            raise OpenCompassConfigError(
                f"MODEL_LIMIT_EXCEEDED:max_output_tokens {requested} > ceiling {ceiling}",
            )
    return {
        "provider": model.get("provider"),
        "model": model.get("model") or model.get("id"),
        "parameters": allowed,
        "max_output_tokens_ceiling": ceiling,
    }


def build_opencompass_config(
    *,
    profile: Mapping[str, Any],
    model: Mapping[str, Any],
    eval_rows: Sequence[Mapping[str, Any]],
    few_shot_rows: Sequence[Mapping[str, Any]],
    scope: str,
    credentials: Mapping[str, Any] | None = None,
    retry_policy: Mapping[str, int] | None = None,
    transport_owner: str = "runner-native",
) -> dict[str, Any]:
    profile = _as_mapping(profile, "profile")
    model_section = _model_section(_as_mapping(model, "model"))
    if scope not in _SCOPES:
        raise OpenCompassConfigError(f"CONFIG_INVALID:scope must be one of {_SCOPES}")

    split = str(profile.get("split") or "")
    few_shot_cfg = profile.get("few_shot") or {}
    few_shot_count = int(few_shot_cfg.get("count", 0)) if isinstance(
        few_shot_cfg, Mapping,
    ) else 0
    few_shot_split = str(few_shot_cfg.get("source_split", "dev")) if isinstance(
        few_shot_cfg, Mapping,
    ) else "dev"
    if len(few_shot_rows) != few_shot_count:
        raise OpenCompassConfigError(
            f"CONFIG_INVALID:few-shot rows ({len(few_shot_rows)}) must match the "
            f"profile count ({few_shot_count})",
        )
    for example in few_shot_rows:
        if str(example.get("split") or "") != few_shot_split:
            raise OpenCompassConfigError(
                "CONFIG_INVALID:few-shot example must come from the declared "
                f"partition {few_shot_split!r}, got {example.get('split')!r}",
            )
    if few_shot_count and few_shot_split == split:
        raise OpenCompassConfigError(
            "CONFIG_INVALID:few-shot partition must differ from the evaluated split",
        )

    cases: list[dict[str, Any]] = []
    for row in eval_rows:
        row = _as_mapping(row, "eval row")
        if str(row.get("split") or "") != split:
            raise OpenCompassConfigError(
                f"CONFIG_INVALID:eval row split {row.get('split')!r} does not "
                f"match profile split {split!r}",
            )
        options = {label: str(row.get(label, "")) for label in _ANSWER_LABELS}
        cases.append({
            "case_id": str(row.get("id") or ""),
            "subject": str(row.get("subject") or ""),
            # 渲染后的完整 prompt（含合法 few-shot）是 Runner 的实际输入；
            # 结构化字段供桥接导出本地数据集（review R3-02），两者同源。
            "prompt": render_mcq_prompt(
                subject=str(row.get("subject") or ""),
                question=str(row.get("question", "")),
                options=options,
                few_shot=list(few_shot_rows),
            ),
            "question": str(row.get("question", "")),
            "options": options,
        })
    # few-shot 示例内容随配置冻结（review R3-02）：桥接按学科导出为数据集
    # 的示例来源；目标样本 gold 只保留在 dataset 侧，不进入 prompt。
    few_shot_examples: list[dict[str, Any]] = [
        {
            "question": str(example.get("question", "")),
            "options": {label: str(example.get(label, "")) for label in _ANSWER_LABELS},
            "answer": example.get("answer") or "",
        }
        for example in few_shot_rows
    ]

    retry = {
        "runner": int((retry_policy or {}).get("runner", 0)),
        "provider_transport": int((retry_policy or {}).get("provider_transport", 0)),
        "operator": int((retry_policy or {}).get("operator", 0)),
    }
    credential_refs = _credential_refs(credentials)

    config = {
        "benchmark_id": profile.get("benchmark_id"),
        "benchmark_version": profile.get("benchmark_version"),
        "dataset_revision": profile.get("dataset_revision"),
        "split": split,
        "selected_subjects": list(profile.get("selected_subjects") or ()),
        "few_shot": {"count": few_shot_count, "source_split": few_shot_split},
        "seed": profile.get("seed"),
        "prompt_template_version": profile.get("prompt_template_version"),
        "answer_extractor": profile.get("answer_extractor"),
        "extractor_version": profile.get("extractor_version"),
        "aggregation": profile.get("aggregation"),
        "aggregation_version": profile.get("aggregation_version"),
        "runner_version": profile.get("runner_version"),
        "environment_digest": profile.get("environment_digest"),
        "max_output_tokens": profile.get("max_output_tokens"),
        "scope": scope,
        "model": model_section,
        "retry": retry,
        "credentials": credential_refs,
        "few_shot_examples": few_shot_examples,
        "transport_owner": transport_owner,
        # runner-native 调用路径的观测缺口：未观测的请求身份/费用记 unknown，
        # 不补填为已核验（T07/T10 的 Provider 桥接可消除）。
        "observation_gaps": (
            ["request-identity", "usage-and-cost"] if transport_owner == "runner-native" else []
        ),
        "cases": cases,
    }
    config["config_hash"] = "sha256:" + hashlib.sha256(json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return config
