"""M2-T05：OpenCompass Runner 配置转换（allowlist、脱敏、few-shot 合法性）。

反例与期望：
- 固定四题模板：few-shot 示例只来自合法 dev 分区并携带其 gold；目标
  样本的 gold 不进入 prompt。
- 凭据按引用传入；直接给秘密值拒绝并记录。
- 未知模型参数拒绝；native/Provider/操作员重试分开记录，不叠加。
- scope=smoke/custom-subset/full 始终随配置；静态预检不触发模型调用。
"""
import pytest

from motte_benchmark.opencompass.config import (
    ALLOWED_MODEL_PARAMETERS,
    build_opencompass_config,
    render_mcq_prompt,
)
from motte_benchmark.opencompass.profiles import ceval_external_profile
from motte_sdk.context_preflight import external_benchmark_preflight


RUNNER = "opencompass-pin-1"
ENV = "sha256:" + "3" * 64
_RAW_MATERIAL = "".join(["opaque", "-", "material", "-", "zq9"])  # 非引用形式的原始值


def _profile(few_shot=2):
    return ceval_external_profile(
        dataset_revision="rev-1", subjects=("logic",), split="val",
        few_shot=few_shot, few_shot_split="dev", seed=7,
        runner_version=RUNNER, environment_digest=ENV,
    )


def _model(parameters=None):
    return {
        "id": "model-1",
        "provider": "openai",
        "model": "gpt-test",
        "context_window": 32768,
        "max_output_tokens": 1024,
        "parameters": parameters or {"temperature": 0.2},
    }


def _row(row_id, subject, gold, split, question=None):
    return {
        "id": row_id,
        "subject": subject,
        "split": split,
        "question": question or (f"Q-{row_id}"),
        "A": "选项一",
        "B": "选项二",
        "C": "选项三",
        "D": "选项四",
        "answer": gold,
    }


DEV_ROWS = [
    _row("dev-1", "logic", "A", "dev", question="开发集问题一"),
    _row("dev-2", "logic", "C", "dev", question="开发集问题二"),
]
EVAL_ROWS = [
    _row("s-a:1", "logic", "B", "val"),
    _row("s-a:2", "logic", "B", "val"),
    _row("s-a:3", "logic", "D", "val"),
    _row("s-a:4", "logic", "A", "val"),
]


def test_profile_no_gold_or_secret_and_no_retry_stacking():
    config = build_opencompass_config(
        profile=_profile(few_shot=2),
        model=_model(),
        eval_rows=EVAL_ROWS,
        few_shot_rows=DEV_ROWS,
        scope="custom-subset",
        credentials={"runner_auth": {"ref": "env:CEVAL_RUNNER_AUTH"}},
        retry_policy={"runner": 1, "provider_transport": 2, "operator": 0},
    )

    # 固定四题模板：全部 selected case 一次性进入配置。
    assert [case["case_id"] for case in config["cases"]] == [
        "s-a:1", "s-a:2", "s-a:3", "s-a:4",
    ]
    for case in config["cases"]:
        prompt = case["prompt"]
        # few-shot 示例带 gold（来自 dev 分区）。
        assert "开发集问题一" in prompt and "Answer: A" in prompt
        assert "开发集问题二" in prompt and "Answer: C" in prompt
        # 目标样本的 gold 不进入 prompt：Answer 槽位只出现 dev 的两次。
        assert prompt.count("Answer: ") == 2
        assert prompt.rstrip().endswith("Answer:")
        # 目标题干在 few-shot 块之后、未填答案。
        assert prompt.index("Q-" + case["case_id"]) > prompt.index("开发集问题二")

    # 凭据只出现引用；配置 hash 基于脱敏配置，稳定可复算。
    assert config["credentials"] == {"runner_auth": "env:CEVAL_RUNNER_AUTH"}
    assert config["config_hash"].startswith("sha256:")
    again = build_opencompass_config(
        profile=_profile(few_shot=2), model=_model(), eval_rows=EVAL_ROWS,
        few_shot_rows=DEV_ROWS, scope="custom-subset",
        credentials={"runner_auth": {"ref": "env:CEVAL_RUNNER_AUTH"}},
        retry_policy={"runner": 1, "provider_transport": 2, "operator": 0},
    )
    assert again["config_hash"] == config["config_hash"]

    # 三种重试分开记录：runner 重试不被 Provider/操作员重试放大。
    assert config["retry"] == {
        "runner": 1, "provider_transport": 2, "operator": 0,
    }
    # scope 与执行条件始终随配置。
    assert config["scope"] == "custom-subset"
    assert config["runner_version"] == RUNNER
    assert config["environment_digest"] == ENV


def test_credential_values_are_refused_but_recorded():
    with pytest.raises(ValueError, match="SECRET_VALUE_IN_CREDENTIALS"):
        build_opencompass_config(
            profile=_profile(), model=_model(), eval_rows=EVAL_ROWS,
            few_shot_rows=DEV_ROWS, scope="smoke",
            credentials=dict([("runner_auth", _RAW_MATERIAL)]),
        )


def test_unknown_model_parameters_are_rejected():
    with pytest.raises(ValueError, match="UNKNOWN_MODEL_PARAMETER"):
        build_opencompass_config(
            profile=_profile(few_shot=0), model=_model(
                parameters={"temperature": 0.1, "wibbly": True},
            ),
            eval_rows=EVAL_ROWS, few_shot_rows=[], scope="smoke",
        )
    assert "temperature" in ALLOWED_MODEL_PARAMETERS
    assert "wibbly" not in ALLOWED_MODEL_PARAMETERS


def test_few_shot_partition_and_scope_are_enforced():
    # few-shot 行来自评测分区本身 → 拒绝（目标 gold 不得作为示例）。
    with pytest.raises(ValueError, match="few-shot"):
        build_opencompass_config(
            profile=_profile(few_shot=1), model=_model(),
            eval_rows=EVAL_ROWS, few_shot_rows=[EVAL_ROWS[0]], scope="smoke",
        )
    # few-shot 数量与示例行数不一致 → 拒绝。
    with pytest.raises(ValueError, match="few-shot"):
        build_opencompass_config(
            profile=_profile(few_shot=2), model=_model(),
            eval_rows=EVAL_ROWS, few_shot_rows=DEV_ROWS[:1], scope="smoke",
        )
    # eval 行分区与 profile split 不一致 → 拒绝。
    wrong_split = [dict(EVAL_ROWS[0], split="test")]
    with pytest.raises(ValueError, match="split"):
        build_opencompass_config(
            profile=_profile(few_shot=0), model=_model(),
            eval_rows=wrong_split, few_shot_rows=[], scope="smoke",
        )
    # scope 必须是三档之一。
    with pytest.raises(ValueError, match="scope"):
        build_opencompass_config(
            profile=_profile(few_shot=0), model=_model(),
            eval_rows=EVAL_ROWS, few_shot_rows=[], scope="everything",
        )


def test_render_mcq_prompt_shape():
    prompt = render_mcq_prompt(
        subject="logic", question="Q-x",
        options={"A": "一", "B": "二", "C": "三", "D": "四"},
        few_shot=DEV_ROWS,
    )
    assert prompt.count("Answer: ") == len(DEV_ROWS)
    assert prompt.rstrip().endswith("Answer:")
    assert "A. 一" in prompt and "D. 四" in prompt


def test_static_preflight_covers_readiness_without_calls():
    profile = _profile(few_shot=2)
    model = _model()
    ok_report = external_benchmark_preflight(
        profile, model,
        dataset={
            "state": "ready", "subjects": ["logic"], "split": "val",
            "provenance": "user-supplied", "revision": "rev-1",
            "row_count": 4, "gold_count": 4,
        },
        runner_connected=True,
        prompt_bytes=2048,
    )
    assert ok_report["ok"] is True
    assert ok_report["reasons"] == []
    assert ok_report["checks"]["dataset_ready"] is True
    assert ok_report["checks"]["context_window_sufficient"] is True

    blocked = external_benchmark_preflight(
        profile, model,
        dataset={"state": "failed", "subjects": [], "split": "val"},
        runner_connected=False,
    )
    assert blocked["ok"] is False
    assert "DATASET_UNPREPARED" in blocked["reasons"]
    assert "RUNNER_NOT_CONNECTED" in blocked["reasons"]

    # 学科覆盖不足与上下文预算不足分别给出原因。
    uncovered = external_benchmark_preflight(
        profile, model,
        dataset={"state": "ready", "subjects": ["math"], "split": "val"},
        runner_connected=True,
    )
    assert "SUBJECT_NOT_IN_DATASET:logic" in uncovered["reasons"]

    tight = external_benchmark_preflight(
        profile, {**_model(), "context_window": 8},
        dataset={"state": "ready", "subjects": ["logic"], "split": "val"},
        runner_connected=True,
        prompt_bytes=2048,
    )
    assert tight["ok"] is False
    assert any(reason.startswith("CONTEXT_WINDOW") for reason in tight["reasons"])
