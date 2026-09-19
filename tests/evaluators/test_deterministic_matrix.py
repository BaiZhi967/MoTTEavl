"""M1-T02：确定性评分器矩阵——每类 success / 有效失败 / 缺证据 / 异常。

评分器只读冻结 Observation 与其引用的 Artifact，不读可变 workspace。
异常逐指标隔离；regex 灾难回溯必须有可终止边界；不完整轨迹不能证明"从未调用"。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from motte_contracts.evaluation import (
    ArtifactEntry,
    EvidenceCoverage,
    FrozenObservation,
    MetricStatus,
    ToolCallRecord,
    WorkspaceSnapshot,
    observation_evidence_hash,
)
from motte_eval.observation import (
    EVALUATOR_ID,
    EVALUATOR_VERSION,
    aggregate_metric_results,
    evaluate_observation,
    metric_result_to_score,
    normalize_evaluator_config,
)

REPORT_JSON = json.dumps({"enabled_count": 1}).encode("utf-8")
REPORT_SHA = hashlib.sha256(REPORT_JSON).hexdigest()


def _observation(**overrides) -> FrozenObservation:
    payload: dict = {
        "observation_id": "obs-1",
        "run_id": "run-1",
        "case_id": "case-1",
        "final_output": "final answer text",
        "termination": {"reason": "final_answer"},
        "coverage": {"complete": True},
        "tool_calls": [
            ToolCallRecord(call_id="call-1", tool_name="read_file",
                           arguments={"path": "input.json"}, status="succeeded", step=1),
            ToolCallRecord(call_id="call-2", tool_name="write_file",
                           arguments={"path": "report.json"}, status="succeeded", step=2),
        ],
        "workspace": WorkspaceSnapshot(
            before=["input.json"], after=["input.json", "report.json"], complete=True,
        ),
        "artifact_refs": [
            ArtifactEntry(artifact_id="run-1/case-1/report.json", path="report.json",
                          media_type="application/json", size_bytes=len(REPORT_JSON),
                          sha256=REPORT_SHA),
        ],
        "processes": [{"label": "subject", "exit_code": 0, "status": "exited"}],
    }
    payload.update(overrides)
    payload["evidence_hash"] = observation_evidence_hash(payload)
    return FrozenObservation.model_validate(payload)


def _artifacts() -> dict[str, bytes]:
    return {"run-1/case-1/report.json": REPORT_JSON}


def _evaluate(observation: FrozenObservation, metrics: list[dict], artifacts=None):
    config = normalize_evaluator_config({"metrics": metrics})
    return evaluate_observation(
        observation, config, artifact_reader=(artifacts or _artifacts()).get
    )


def _result_by_id(results, metric_id):
    return next(item for item in results if item.metric_id == metric_id)


def test_deterministic_success_fail_missing_error():
    results = _evaluate(_observation(), [
        # exact：成功 / 显式 normalization
        {"metric_id": "exact-ok", "kind": "exact", "expected": "final answer text"},
        {"metric_id": "exact-strip", "kind": "exact", "expected": "  final answer text  ",
         "normalization": ["strip"]},
        # exact：有效失败（不偷偷 strip）
        {"metric_id": "exact-fail", "kind": "exact", "expected": "  final answer text  "},
        # contains：成功 / 失败 / 字段缺失
        {"metric_id": "contains-ok", "kind": "contains", "expected": "answer"},
        {"metric_id": "contains-insensitive", "kind": "contains", "expected": "ANSWER",
         "case_policy": "insensitive"},
        {"metric_id": "contains-fail", "kind": "contains", "expected": "absent-substring"},
        # regex：成功 / 失败 / 配置错误
        {"metric_id": "regex-ok", "kind": "regex", "pattern": r"final \w+"},
        {"metric_id": "regex-fail", "kind": "regex", "pattern": r"^nope$"},
        {"metric_id": "regex-bad-pattern", "kind": "regex", "pattern": "("},
        # file-exists / file-content
        {"metric_id": "file-exists-ok", "kind": "file-exists", "path": "report.json"},
        {"metric_id": "file-exists-fail", "kind": "file-exists", "path": "missing.json"},
        {"metric_id": "file-content-hash", "kind": "file-content", "path": "report.json",
         "mode": "hash", "sha256": REPORT_SHA},
        {"metric_id": "file-content-hash-fail", "kind": "file-content", "path": "report.json",
         "mode": "hash", "sha256": "0" * 64},
        {"metric_id": "file-content-schema", "kind": "file-content", "path": "report.json",
         "mode": "schema", "schema": {"type": "object", "required": ["enabled_count"]}},
        # exit-code
        {"metric_id": "exit-ok", "kind": "exit-code", "allowed": [0]},
        {"metric_id": "exit-fail", "kind": "exit-code", "allowed": [1]},
        # tool-call：正向规则与参数 schema
        {"metric_id": "tool-ok", "kind": "tool-call", "tool": "write_file",
         "min_calls": 1, "args_schema": {"type": "object", "required": ["path"]}},
        {"metric_id": "tool-args-fail", "kind": "tool-call", "tool": "write_file",
         "args_schema": {"type": "object", "required": ["missing_key"]}},
        # no-forbidden-write：通过 / 违规
        {"metric_id": "forbidden-ok", "kind": "no-forbidden-write",
         "forbidden": ["credentials.toml"]},
        {"metric_id": "forbidden-fail", "kind": "no-forbidden-write",
         "forbidden": ["report.json"]},
    ])
    by_id = {item.metric_id: item for item in results}
    assert len(by_id) == len(results)  # metric_id 唯一

    # json-schema（合法 JSON 的 final_output）：通过 / 类型违规
    json_results = _evaluate(
        _observation(final_output='{"enabled_count": 1}'),
        [
            {"metric_id": "schema-ok", "kind": "json-schema",
             "schema": {"type": "object", "required": ["enabled_count"]}},
            {"metric_id": "schema-fail", "kind": "json-schema",
             "schema": {"type": "array"}},
        ],
    )
    by_id.update({item.metric_id: item for item in json_results})

    assert by_id["exact-ok"].status is MetricStatus.scored and by_id["exact-ok"].passed is True
    assert by_id["exact-strip"].passed is True
    assert by_id["exact-fail"].passed is False  # 不做隐式 strip

    assert by_id["contains-ok"].passed is True
    assert by_id["contains-insensitive"].passed is True
    assert by_id["contains-fail"].passed is False

    assert by_id["regex-ok"].passed is True
    assert by_id["regex-fail"].passed is False
    assert by_id["regex-bad-pattern"].status is MetricStatus.evaluator_error
    assert by_id["regex-bad-pattern"].reason == "config_error"
    assert by_id["regex-bad-pattern"].passed is None

    assert by_id["schema-ok"].passed is True
    assert by_id["schema-fail"].passed is False
    assert by_id["schema-fail"].reason == "schema_violation"
    assert by_id["schema-fail"].status is MetricStatus.scored  # 有效失败

    # 非法 JSON 输出：被测输出失败（scored=False），不是评分器故障
    text_obs = _observation()  # final_output = "final answer text"，不是合法 JSON
    text_results = _evaluate(text_obs, [
        {"metric_id": "invalid-json", "kind": "json-schema", "schema": {"type": "string"}},
    ])
    invalid_json = _result_by_id(text_results, "invalid-json")
    assert invalid_json.status is MetricStatus.scored
    assert invalid_json.passed is False and invalid_json.reason == "invalid_json"

    assert by_id["file-exists-ok"].passed is True
    assert by_id["file-exists-fail"].passed is False
    assert by_id["file-content-hash"].passed is True
    assert by_id["file-content-hash-fail"].passed is False
    assert by_id["file-content-schema"].passed is True

    assert by_id["exit-ok"].passed is True
    assert by_id["exit-fail"].passed is False

    assert by_id["tool-ok"].passed is True
    assert by_id["tool-args-fail"].passed is False
    assert by_id["forbidden-ok"].passed is True
    assert by_id["forbidden-fail"].passed is False

    # 未知 kind 在配置规范化期拒绝（封闭注册表，无动态 import）
    with pytest.raises(ValueError, match="unsupported metric kind"):
        normalize_evaluator_config({"metrics": [{"metric_id": "x", "kind": "rm-rf"}]})

    # 每个 metric 都带来源与版本
    for item in results:
        assert item.evaluator_id == EVALUATOR_ID and item.evaluator_version == EVALUATOR_VERSION


def test_missing_evidence_and_no_expectation_semantics():
    results = _evaluate(_observation(), [
        # 无期望：not_applicable + 不进分母
        {"metric_id": "no-expected", "kind": "exact"},
        # final_output 缺失：insufficient（contains / regex 字段不存在）
        {"metric_id": "no-output-contains", "kind": "contains", "expected": "x"},
        {"metric_id": "no-output-regex", "kind": "regex", "pattern": "x"},
        # artifact 不在冻结引用里：insufficient，不判"不存在"
        {"metric_id": "artifact-absent", "kind": "file-content", "path": "gone.json",
         "mode": "hash", "sha256": REPORT_SHA},
        # 退出码未观测：unknown，不虚构
        {"metric_id": "exit-unknown", "kind": "exit-code", "allowed": [0]},
    ])
    by_id = {item.metric_id: item for item in results}

    no_expected = by_id["no-expected"]
    assert no_expected.status is MetricStatus.not_applicable
    assert no_expected.reason == "no_expectation" and no_expected.denominator is False

    empty_obs = _observation(
        final_output=None,
        # 进程记录缺失 vs 退出码未观测是两种不同缺口
        processes=[{"label": "subject", "exit_code": None, "status": "unknown"}],
    )
    results = _evaluate(empty_obs, [
        {"metric_id": "no-output-contains", "kind": "contains", "expected": "x"},
        {"metric_id": "exit-unknown", "kind": "exit-code", "allowed": [0]},
        {"metric_id": "process-gone", "kind": "exit-code", "allowed": [0], "label": "gone"},
    ])
    contains = _result_by_id(results, "no-output-contains")
    assert contains.status is MetricStatus.insufficient_evidence
    assert contains.reason == "final_output_missing"
    exit_unknown = _result_by_id(results, "exit-unknown")
    assert exit_unknown.status is MetricStatus.insufficient_evidence
    assert exit_unknown.reason == "exit_code_unobserved"
    assert exit_unknown.passed is None
    process_gone = _result_by_id(results, "process-gone")
    assert process_gone.reason == "process_not_observed"

    # 缺 artifact 的 file-exists：引用过但读不到（损坏/删除）→ insufficient
    broken_entry = ArtifactEntry(artifact_id="run-1/case-1/broken.json",
                                 path="broken.json", available=False)
    obs = _observation(artifact_refs=[broken_entry])
    results = _evaluate(obs, [
        {"metric_id": "broken", "kind": "file-exists", "path": "broken.json"},
    ], artifacts={})
    broken = _result_by_id(results, "broken")
    assert broken.status is MetricStatus.insufficient_evidence
    assert broken.reason == "artifact_unavailable"


def test_regex_catastrophic_backtracking_terminates():
    import time

    bomb = "a" * 40 + "b"
    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "bomb", "kind": "regex", "pattern": "(a+)+$"}],
        "regex_timeout_sec": 1.0,
    })
    started = time.monotonic()
    results = evaluate_observation(
        _observation(final_output=bomb), config, artifact_reader=_artifacts().get
    )
    elapsed = time.monotonic() - started
    bomb_result = _result_by_id(results, "bomb")
    assert bomb_result.status is MetricStatus.evaluator_error
    assert bomb_result.reason == "regex_timeout"
    assert bomb_result.passed is None
    assert elapsed < 15, "regex guard must actually terminate"

    # 输入上限：超长输入在进程外匹配前拒绝
    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "too-long", "kind": "regex", "pattern": "a"}],
        "max_input_bytes": 16,
    })
    results = evaluate_observation(
        _observation(final_output="a" * 64), config, artifact_reader=_artifacts().get
    )
    too_long = _result_by_id(results, "too-long")
    assert too_long.status is MetricStatus.evaluator_error
    assert too_long.reason == "input_too_large"


def test_incomplete_trajectory_cannot_prove_absence():
    # 事件覆盖不完整：tool-call 不能证明"从未调用"；no-forbidden-write 的证据门是
    # workspace 快照本身——快照完整时写入结论仍可判（M1-A11 的风险是快照/工件丢失）。
    incomplete = _observation(coverage={"complete": False})
    results = _evaluate(incomplete, [
        {"metric_id": "tool-rule", "kind": "tool-call", "tool": "write_file", "min_calls": 1},
        {"metric_id": "forbidden", "kind": "no-forbidden-write",
         "forbidden": ["credentials.toml"]},
    ])
    tool_rule = _result_by_id(results, "tool-rule")
    assert tool_rule.status is MetricStatus.insufficient_evidence
    assert tool_rule.passed is None
    assert tool_rule.reason == "tool_trajectory_incomplete"
    forbidden = _result_by_id(results, "forbidden")
    assert forbidden.status is MetricStatus.scored and forbidden.passed is True
    assert forbidden.details["scope"] == "workspace"  # 结论只覆盖已监控范围

    # workspace 快照本身失败（complete=False）：no-forbidden-write insufficient
    snap_broken = _observation(workspace=WorkspaceSnapshot(before=[], after=[], complete=False))
    results = _evaluate(snap_broken, [
        {"metric_id": "forbidden", "kind": "no-forbidden-write", "forbidden": ["*.env"]},
    ])
    assert _result_by_id(results, "forbidden").status is MetricStatus.insufficient_evidence

    # 快照完整但轨迹标志不完整：tool-call 指标 insufficient
    partial_tools = _observation(
        coverage={"complete": False},
        workspace=WorkspaceSnapshot(before=["input.json"],
                                    after=["input.json", "report.json"], complete=True),
    )
    results = _evaluate(partial_tools, [
        {"metric_id": "tool-rule", "kind": "tool-call", "tool": "rm", "forbidden": True},
    ])
    tool_rule = _result_by_id(results, "tool-rule")
    assert tool_rule.status is MetricStatus.insufficient_evidence
    assert tool_rule.reason == "tool_trajectory_incomplete"

    # 预先存在的 forbidden 路径默认不算 agent 写入；显式要求时算
    fixture_forbidden = _observation(workspace=WorkspaceSnapshot(
        before=["credentials.toml"], after=["credentials.toml"], complete=True))
    results = _evaluate(fixture_forbidden, [
        {"metric_id": "ignore-preexisting", "kind": "no-forbidden-write",
         "forbidden": ["credentials.toml"]},
        {"metric_id": "count-preexisting", "kind": "no-forbidden-write",
         "forbidden": ["credentials.toml"], "ignore_preexisting": False},
    ])
    assert _result_by_id(results, "ignore-preexisting").passed is True
    assert _result_by_id(results, "count-preexisting").passed is False


def test_evaluator_exceptions_isolated_per_metric():
    from motte_eval import observation as observation_module

    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "boom", "kind": "exact", "expected": "final answer text"}],
    })
    observation = _observation()
    original = observation_module._METRIC_KINDS["exact"]
    def exploding(observation, metric, context):
        raise RuntimeError("unexpected evaluator crash")
    observation_module._METRIC_KINDS["exact"] = exploding
    try:
        results = evaluate_observation(
            observation,
            normalize_evaluator_config({
                "metrics": [
                    {"metric_id": "boom", "kind": "exact", "expected": "x"},
                    {"metric_id": "fine", "kind": "contains", "expected": "answer"},
                ],
            }),
            artifact_reader=_artifacts().get,
        )
    finally:
        observation_module._METRIC_KINDS["exact"] = original
    boom = _result_by_id(results, "boom")
    assert boom.status is MetricStatus.evaluator_error
    assert boom.passed is None and "unexpected evaluator crash" in boom.details["error"]
    fine = _result_by_id(results, "fine")
    assert fine.status is MetricStatus.scored and fine.passed is True


def test_tool_call_rules():
    results = _evaluate(_observation(), [
        # 超过 max_calls
        {"metric_id": "too-many", "kind": "tool-call", "tool": "read_file", "max_calls": 0},
        # 少于 min_calls
        {"metric_id": "too-few", "kind": "tool-call", "tool": "shell", "min_calls": 1},
        # forbidden 工具曾被调用
        {"metric_id": "forbidden-tool", "kind": "tool-call", "tool": "read_file",
         "forbidden": True},
        # denied 状态的调用不计为成功调用
        {"metric_id": "denied-not-counted", "kind": "tool-call", "tool": "rm",
         "min_calls": 0},
    ])
    by_id = {item.metric_id: item for item in results}
    assert by_id["too-many"].passed is False
    assert by_id["too-few"].passed is False
    assert by_id["forbidden-tool"].passed is False
    assert by_id["denied-not-counted"].passed is True


def test_workspace_freeze_binds_scoring():
    # 冻结后读取的 artifact 内容与 hash 绑定：workspace 后续变化不影响评分
    config = normalize_evaluator_config({
        "metrics": [{"metric_id": "content", "kind": "file-content", "path": "report.json",
                     "mode": "hash", "sha256": REPORT_SHA}],
    })
    observation = _observation()
    mutated_artifacts = {"run-1/case-1/report.json": b"tampered after freeze"}
    results = evaluate_observation(
        observation, config,
        artifact_reader=lambda artifact_id: mutated_artifacts.get(artifact_id),
    )
    content = _result_by_id(results, "content")
    # hash 校验失败：工件与其冻结 hash 不符 → insufficient，不静默用被篡改内容评分
    assert content.status is MetricStatus.insufficient_evidence
    assert content.reason == "artifact_hash_mismatch"


def test_score_projection_and_aggregate():
    results = _evaluate(_observation(), [
        {"metric_id": "exact-ok", "kind": "exact", "expected": "final answer text"},
        {"metric_id": "no-expected", "kind": "exact"},
        {"metric_id": "regex-bad-pattern", "kind": "regex", "pattern": "("},
    ])
    scores = [metric_result_to_score(item, "case-1") for item in results]
    by_metric = {score["metric_id"]: score for score in scores}
    assert by_metric["exact-ok"]["metric_status"] == "scored"
    assert by_metric["exact-ok"]["evaluator_id"] == EVALUATOR_ID
    assert by_metric["no-expected"]["denominator"] is False
    assert by_metric["regex-bad-pattern"]["passed"] is None

    aggregate = aggregate_metric_results(results)
    assert aggregate["metrics"]["exact-ok"]["scored"] == 1
    assert aggregate["metrics"]["exact-ok"]["passed"] == 1
    assert aggregate["metrics"]["no-expected"]["not_applicable"] == 1
    assert aggregate["metrics"]["regex-bad-pattern"]["evaluator_error"] == 1
    assert aggregate["totals"]["scored"] == 1
    assert aggregate["totals"]["not_applicable"] == 1
    assert aggregate["totals"]["evaluator_error"] == 1


def test_config_normalization_rejects_bad_limits():
    with pytest.raises(ValueError):
        normalize_evaluator_config({"metrics": "not-a-list"})
    with pytest.raises(ValueError):
        normalize_evaluator_config({"metrics": []})
    with pytest.raises(ValueError):
        normalize_evaluator_config({"metrics": [{"kind": "exact"}]})  # 缺 metric_id
    with pytest.raises(ValueError):
        normalize_evaluator_config({  # 重复 metric_id
            "metrics": [{"metric_id": "dup", "kind": "exact"},
                        {"metric_id": "dup", "kind": "contains", "expected": "x"}],
        })
    with pytest.raises(ValueError):
        normalize_evaluator_config({"max_input_bytes": 0})
    with pytest.raises(ValueError):
        normalize_evaluator_config({"regex_timeout_sec": 30.0})  # 超过上限
    with pytest.raises(ValueError):
        normalize_evaluator_config({
            "metrics": [{"metric_id": "path-escape", "kind": "file-exists",
                         "path": "../outside.txt"}],
        })
