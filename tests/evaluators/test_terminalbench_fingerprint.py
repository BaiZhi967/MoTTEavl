"""review R09：Terminal-Bench 的冻结实验指纹必须参与比较资格。

反例（review 原文）：真实 ``build_run_inputs`` 构造同一任务的两份 manifest，
baseline 为 ``n_trials=1/agent_sec=5/memory_mb=256``，candidate 为
``3/105/1280``，policy 只允许 model 变化——修复前仍然 ``eligible=True``、
``metric_eligibility.quality=True``，只提示成本未知；于是"换了实验条件"的质量
差异会被当成"换了模型"的差异。

本文件用**生产 manifest 构造路径**（真实 fixture 任务 + ``build_run_inputs``）
覆盖：重复数、Agent、超时、资源、工具、环境、凭据、runner 与 environment
digest 逐项变化都必须产生对应不可比原因；显式允许的因子只记录不阻断；缺失
身份不默认相等。
"""
from __future__ import annotations

from pathlib import Path

from motte_benchmark.harbor.tasks import prepare_task_manifest
from motte_eval.comparison import compare_run_reports
from motte_sdk.terminalbench import build_run_inputs, terminal_bench_profile
from motte_contracts.comparison import ComparisonPolicy, RunReportRef

TASKS_ROOT = Path(__file__).resolve().parents[1] / "fixtures/benchmarks/harbor/tasks"
REVISION = "69671fbaac6d67a7ef0dfec016cc38a64ef7a77c"
SOURCE_ID = "motte-harbor-fixtures"


def _record() -> dict:
    manifest = prepare_task_manifest(
        TASKS_ROOT, source_id=SOURCE_ID, dataset_revision=REVISION,
    )
    manifest.pop("prepared_at", None)
    return {
        "manifest": manifest,
        "manifest_hash": manifest["manifest_hash"],
        "dataset_revision": REVISION,
        "task_root": str(TASKS_ROOT),
        "source_kind": "local",
    }


def _manifest(*, run_id: str = "run-a", **profile_overrides) -> dict:
    profile = terminal_bench_profile(**profile_overrides)
    return build_run_inputs(
        record=_record(), run_id=run_id, job_id=f"job-{run_id}", profile=profile,
    )["manifest"]


def _ref(run_id: str) -> RunReportRef:
    return RunReportRef(
        run_id=run_id, scoring_pass_id=f"pass-{run_id}", report_schema="report-v1",
        evidence_hash="sha256:" + run_id,
    )


def _compare(baseline: dict, candidate: dict, *, allowed=("model",)):
    return compare_run_reports(
        _ref("run-a"), _ref("run-b"),
        baseline_manifest=baseline, candidate_manifest=candidate,
        policy=ComparisonPolicy(allowed_factors=list(allowed)),
    )


def test_only_model_change_stays_comparable():
    """只换模型（政策允许的变量）→ 可比，且不产生阻断原因（费用未知只影响 cost）。"""
    result = _compare(
        _manifest(model={"provider": "p", "model": "m1"}),
        _manifest(model={"provider": "p", "model": "m2"}),
    )
    assert result.eligible is True, result.reasons
    assert all(reason.startswith("COST_UNKNOWN") for reason in result.reasons), result.reasons
    assert result.metric_eligibility["quality"] is True
    assert result.metric_eligibility["cost"] is False


def test_repeat_count_change_is_not_comparable():
    """n_trials 不同就是不同实验条件（review 复现的核心反例）。"""
    result = _compare(
        _manifest(n_trials=1, timeouts={"agent_sec": 5}, resources={"memory_mb": 256}),
        _manifest(n_trials=3, timeouts={"agent_sec": 105}, resources={"memory_mb": 1280}),
    )
    assert result.eligible is False
    assert result.metric_eligibility["quality"] is False
    joined = " | ".join(result.reasons)
    assert "REPEATS_CHANGED:n_trials" in joined
    assert "TIMEOUTS_CHANGED:timeouts" in joined
    assert "RESOURCES_CHANGED:resources" in joined


def test_each_frozen_condition_produces_its_own_reason():
    """逐项改变条件 → 逐项产生对应不可比原因（不只笼统失败）。"""
    baseline = _manifest(agent_id="oracle", agent_version="1.0.0")
    cases = {
        "REPEATS_CHANGED": {"n_trials": 2},
        "AGGREGATION_CHANGED": {"aggregation": "mean-success"},
        "ENVIRONMENT_CHANGED": {"environment": {"type": "docker", "delete": False}},
        "CREDENTIALS_CHANGED": {"credentials": {"provider": {"ref": "env:MOTTE_TEST_KEY_REF"}}},
        "TIMEOUTS_CHANGED": {"timeouts": {"agent_sec": 42}},
        "RESOURCES_CHANGED": {"resources": {"memory_mb": 512}},
    }
    for expected_code, overrides in cases.items():
        result = _compare(baseline, _manifest(**overrides))
        assert result.eligible is False, (expected_code, result.reasons)
        assert any(reason.startswith(expected_code) for reason in result.reasons), (
            expected_code, result.reasons,
        )
    # tools/limits 在创建路径上必须能映射到固定 Harbor Agent 的原生执行参数
    # （M3-R2-11：不支持的非空配置会被 HARBOR_PROFILE_UNSUPPORTED_FIELD 拒绝），
    # 因此这两个"未映射取值"的条件与 retries 一样直接改冻结字段——比较政策
    # 看到的仍是"冻结实验条件不同 → 不可比"这条不变量。
    for expected_code, patch in (
        ("TOOLS_CHANGED", {"tools": {"extra": True}}),
        ("LIMITS_CHANGED", {"limits": {"poll_interval_seconds": 1.0}}),
    ):
        candidate = _manifest()
        candidate["external_benchmark"]["profile"].update(patch)
        result = _compare(baseline, candidate)
        assert result.eligible is False, (expected_code, result.reasons)
        assert any(reason.startswith(expected_code) for reason in result.reasons), (
            expected_code, result.reasons,
        )
    # retries 是平台固定策略（不是创建参数），直接改冻结字段验证同一条不变量。
    retried = _manifest()
    retried["external_benchmark"]["profile"]["retries"] = {
        "runner": 1, "provider_transport": 0, "operator": 0,
    }
    result = _compare(baseline, retried)
    assert result.eligible is False
    assert any(r.startswith("RETRIES_CHANGED") for r in result.reasons)


def test_agent_identity_change_is_not_comparable():
    """Agent 身份变化同样是不可比原因（用冻结字段直接构造，绕开校验层）。"""
    baseline = _manifest()
    candidate = _manifest()
    candidate["external_benchmark"]["profile"]["agent_version"] = "1.0.1"
    result = _compare(baseline, candidate)
    assert result.eligible is False
    assert any(r.startswith("AGENT_CHANGED:agent_version") for r in result.reasons)


def test_environment_digest_is_read_from_the_external_root():
    """TB 把 environment_digest 放在 external 根，不从 profile 读取就会漏判。"""
    baseline = _manifest()
    candidate = _manifest()
    candidate["external_benchmark"]["environment_digest"] = "sha256:other"
    result = _compare(baseline, candidate)
    assert result.eligible is False
    assert any("ENVIRONMENT_CHANGED:environment_digest" in r for r in result.reasons)


def test_task_content_identity_is_frozen_and_compared():
    """任务内容 hash 逐题冻结（同 revision 字符串相同不证明内容相同）。"""
    baseline = _manifest()
    assert baseline["case_content_hashes"], "frozen manifest must carry task content hashes"
    candidate = _manifest()
    key = next(iter(candidate["case_content_hashes"]))
    candidate["case_content_hashes"][key] = "sha256:tampered"
    result = _compare(baseline, candidate)
    assert result.eligible is False
    assert any(r.startswith("CASE_CONTENT_CHANGED") for r in result.reasons)
    assert key in result.case_diff["changed"]


def test_frozen_task_files_are_part_of_the_manifest():
    """不可变执行副本所需的逐文件 hash 随 manifest 冻结（R03 的输入）。"""
    manifest = _manifest()
    plan = manifest["external_benchmark"]["runner_config"]["plan"]
    task_keys = set(manifest["task_manifest"]["task_keys"])
    assert set(plan["task_files"]) == task_keys
    for task_key, files in plan["task_files"].items():
        assert files, task_key
        assert "instruction.md" in files
        assert plan["task_content_hashes"][task_key] == manifest["case_content_hashes"][task_key]


def test_explicitly_allowed_factor_is_recorded_not_blocked():
    """政策显式允许的因子不阻断，但差异必须留下可审计记录。"""
    result = _compare(
        _manifest(n_trials=1), _manifest(n_trials=2),
        allowed=("model", "n_trials"),
    )
    assert result.eligible is True, result.reasons
    assert any(r.startswith("ALLOWED_FACTOR:n_trials") for r in result.allowed_differences)


def test_missing_identity_is_never_treated_as_equal():
    """一侧冻结了条件、另一侧没有 → 阻断（不默认相等）。"""
    baseline = _manifest()
    candidate = _manifest()
    candidate["external_benchmark"]["profile"].pop("n_trials")
    result = _compare(baseline, candidate)
    assert result.eligible is False
    assert any(r.startswith("IDENTITY_MISSING:n_trials") for r in result.reasons)


def _claude_manifest(*, model: str, run_id: str = "run-a") -> dict:
    return _manifest(
        run_id=run_id,
        agent_id="claude-code",
        agent_version="2.0.30",
        model={"provider": "anthropic", "model": model},
        credentials={"provider": {"ref": "env:ANTHROPIC_API_KEY"}},
    )


def test_model_change_is_blocked_when_the_policy_forbids_it():
    """TB 的模型身份在 ``external.profile.model``：政策不允许换模型时必须阻断。

    review R2-10 的反例：两份 manifest 只有模型不同（Claude Agent、凭据、任务与
    预算都相同），``allowed_factors`` 为空时修复前返回 ``eligible=true``——因为
    特殊分支只比较了 manifest 顶层的 ``model``。
    """
    baseline = _claude_manifest(model="claude-sonnet-4-5")
    candidate = _claude_manifest(model="claude-opus-4-1", run_id="run-b")
    strict = _compare(baseline, candidate, allowed=())
    assert strict.eligible is False
    assert any(
        r.startswith("FACTOR_NOT_ALLOWED:model") for r in strict.reasons
    ), strict.reasons
    assert strict.metric_eligibility["quality"] is False


def test_allowed_model_change_is_recorded():
    """政策显式允许换模型时可比较，但差异必须留下可审计记录。"""
    baseline = _claude_manifest(model="claude-sonnet-4-5")
    candidate = _claude_manifest(model="claude-opus-4-1", run_id="run-b")
    result = _compare(baseline, candidate, allowed=("model",))
    assert result.eligible is True, result.reasons
    assert any(
        r.startswith("ALLOWED_FACTOR:model") for r in result.allowed_differences
    ), result.allowed_differences
