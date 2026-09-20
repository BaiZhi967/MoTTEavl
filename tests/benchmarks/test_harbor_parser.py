"""M3-T04：Harbor 结果纯解析（M3-A04/A10/A11，需求第 6 节）。

fixture 全部来自真实 Harbor 0.23.0 运行（``scripts/runner/capture-harbor-fixtures``
脱敏），再按反例需要派生变体：reward=0 是有效失败、缺文件不足、畸形是协议
错误、Verifier 异常是 verifier_error、Agent 超时不覆盖有效 Verifier 结果。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from motte_benchmark.harbor.parser import (
    PARSER_VERSION,
    parse_harbor_files,
    read_verifier_observation,
)
from motte_contracts.trial import compute_task_key

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "benchmarks" / "harbor" / "samples"


def _load_sample(name: str) -> dict[str, bytes]:
    base = SAMPLES / name
    return {
        "harbor/" + path.relative_to(base).as_posix(): path.read_bytes()
        for path in base.rglob("*")
        if path.is_file() and path.name != "SOURCES.json"
    }


def _plans_for(files: dict[str, bytes], *, repeats: int = 1) -> list[dict[str, Any]]:
    plan = json.loads(files["harbor/plan.json"])
    plans: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        for repeat in range(repeats):
            plans.append({
                "trial_id": f"trial-{task['normalized_relative_path'].rsplit('/', 1)[-1]}-{repeat}",
                "run_id": "run-1",
                "task_key": task["task_key"],
                "repeat_index": repeat,
                "agent_config_hash": "sha256:agent",
                "environment_hash": "sha256:env",
            })
    return plans


def _trial_dirs(files: dict[str, bytes]) -> list[str]:
    return sorted({
        rel.split("/")[2] for rel in files if rel.startswith("harbor/trials/")
    })


def _for_trial(files: dict[str, bytes], trial: str, relative: str) -> bytes:
    return files[f"harbor/trials/{trial}/{relative}"]


def _with_reward(
    files: dict[str, bytes], trial: str, *, json_payload: Any = ..., text: str | None = None,
) -> dict[str, bytes]:
    """派生 fixture：替换/删除某个 trial 的 reward 文件。"""
    derived = dict(files)
    derived.pop(f"harbor/trials/{trial}/verifier/reward.json", None)
    derived.pop(f"harbor/trials/{trial}/verifier/reward.txt", None)
    if json_payload is not ...:
        derived[f"harbor/trials/{trial}/verifier/reward.json"] = (
            json.dumps(json_payload).encode("utf-8")
        )
    if text is not None:
        derived[f"harbor/trials/{trial}/verifier/reward.txt"] = text.encode("utf-8")
    return derived


def test_reward_zero_missing_invalid_and_verifier_error() -> None:
    """reward=0/缺文件/畸形/Verifier 异常是四种不同结果（M3-A04）。"""
    files = _load_sample("pass-fail-2x2")
    pass_trial = next(t for t in _trial_dirs(files) if t.startswith("hello-pass"))
    fail_trial = next(t for t in _trial_dirs(files) if t.startswith("hello-fail"))

    # 真实 fixture：reward=1 通过、reward=0 有效失败，且 Agent 退出 0 不覆盖它。
    observed = read_verifier_observation(files, pass_trial)
    assert observed["status"] == "scored"
    assert observed["rewards"]["reward"] == 1.0
    assert observed["source_path"] in ("verifier/reward.txt", "verifier/reward.json")
    # 原始 reward 文件不存在 = 证据不足，即使 result.json 里有汇总值也不当分数。
    without_raw = {
        rel: data for rel, data in files.items()
        if f"harbor/trials/{pass_trial}/verifier/reward." not in rel
    }
    structured_only = read_verifier_observation(without_raw, pass_trial)
    assert structured_only["status"] == "missing_verifier_evidence"
    assert structured_only["error"]["code"] == "VERIFIER_RAW_REWARD_MISSING"
    assert structured_only["detail"]["structured_rewards"] == {"reward": 1.0}
    assert observed["source_hash"].startswith("sha256:")

    failed = read_verifier_observation(files, fail_trial)
    assert failed["status"] == "scored"
    assert failed["rewards"]["reward"] == 0.0
    assert failed["error"] is None

    # 缺文件：不是 0 分，而是证据不足。
    missing = read_verifier_observation(_with_reward(files, pass_trial), pass_trial)
    assert missing["status"] == "missing_verifier_evidence"
    assert missing["rewards"] == {}

    # 畸形正文：协议错误，保留原始 hash 供审计。
    malformed = _with_reward(files, pass_trial, text="not-a-number\n")
    observation = read_verifier_observation(malformed, pass_trial)
    assert observation["status"] == "verifier_protocol_error"
    assert observation["error"]["code"] == "VERIFIER_REWARD_TYPE"
    assert observation["source_hash"] is None

    # 半写 JSON / 非对象 / bool / NaN / 字符串：一律协议错误。
    for payload, code in (
        ({"reward": "1.0"}, "VERIFIER_REWARD_TYPE"),
        ({"reward": True}, "VERIFIER_REWARD_TYPE"),
        ({"reward": None}, "VERIFIER_REWARD_TYPE"),
        ([1.0], "VERIFIER_REWARD_NOT_OBJECT"),
        (None, "VERIFIER_REWARD_EMPTY"),
    ):
        derived = _with_reward(files, pass_trial, json_payload=payload)
        derived[f"harbor/trials/{pass_trial}/verifier/reward.json"] = json.dumps(
            payload,
        ).encode("utf-8")
        result = read_verifier_observation(derived, pass_trial)
        assert result["status"] == "verifier_protocol_error", payload
        assert result["error"]["code"] == code

    derived = dict(files)
    derived[f"harbor/trials/{pass_trial}/verifier/reward.json"] = b'{"reward": 1.0'
    broken = read_verifier_observation(derived, pass_trial)
    assert broken["status"] == "missing_verifier_evidence" or (
        broken["status"] == "verifier_protocol_error"
    )

    # reward.json 与 reward.txt 一致 = 正常；冲突 = 拒绝静默挑一个。
    conflict = _with_reward(files, pass_trial, json_payload={"reward": 1.0}, text="1.0\n")
    assert read_verifier_observation(conflict, pass_trial)["status"] == "scored"
    conflict = _with_reward(files, pass_trial, json_payload={"reward": 0.0}, text="1.0\n")
    conflicted = read_verifier_observation(conflict, pass_trial)
    assert conflicted["status"] == "verifier_protocol_error"
    assert conflicted["error"]["code"] == "VERIFIER_REWARD_SOURCE_CONFLICT"
    # 结构化奖励（result.json）与 reward.txt 冲突同样拒绝。
    structured = dict(files)
    structured.pop(f"harbor/trials/{pass_trial}/verifier/reward.txt", None)
    structured[f"harbor/trials/{pass_trial}/verifier/reward.txt"] = b"0\n"
    assert read_verifier_observation(structured, pass_trial)["status"] == (
        "verifier_protocol_error"
    )


def test_verifier_error_is_not_a_quality_failure() -> None:
    """Verifier 超时 → verifier_error；Agent 超时但 Verifier 跑完 → 保留其结论。"""
    files = _load_sample("errors-timeout")
    agent_timeout = next(t for t in _trial_dirs(files) if t.startswith("agent-timeout"))
    verifier_timeout = next(t for t in _trial_dirs(files) if t.startswith("verifier-timeout"))

    agent_observation = read_verifier_observation(files, agent_timeout)
    assert agent_observation["status"] == "scored"
    assert agent_observation["rewards"]["reward"] == 0.0
    assert agent_observation["detail"]["result_json"] == "parsed"

    verifier_observation = read_verifier_observation(files, verifier_timeout)
    assert verifier_observation["status"] == "verifier_error"
    assert verifier_observation["error"]["code"] == "VerifierTimeoutError"
    assert verifier_observation["rewards"] == {}, "a crashed verifier never yields a score"
    assert verifier_observation["evidence_refs"], "raw verifier files stay as evidence"


def test_parse_keeps_every_planned_trial_and_all_dispositions() -> None:
    """多 Trial 全保留；未观测到的计划单元是 not_attempted，不挑最佳（M3-G10）。"""
    files = _load_sample("pass-fail-2x2")
    plans = _plans_for(files, repeats=3)
    parsed = parse_harbor_files(files, plans)

    assert parsed["trial_count"] == len(plans) == 6
    assert parsed["planned_trial_count"] == 6
    dispositions = [result["disposition"] for result in parsed["results"]]
    # 每个 task 只有 2 个真实 trial，第 3 个计划单元没有观测记录。
    assert sorted(dispositions) == ["failed", "failed", "not_attempted", "not_attempted",
                                    "succeeded", "succeeded"], dispositions
    per_task: dict[str, list[dict[str, Any]]] = {}
    for result in parsed["results"]:
        per_task.setdefault(result["task_key"], []).append(result)
    for results in per_task.values():
        assert [item["repeat_index"] for item in results] == [0, 1, 2]
        # 同一任务的两个真实 Trial 结论一致（校准任务确定性），第三个未观测。
        assert len({item["disposition"] for item in results} - {"not_attempted"}) == 1
        assert "not_attempted" in {item["disposition"] for item in results}


def test_repeat_index_assignment_is_deterministic_and_stable() -> None:
    """同一份冻结字节重复解析得到同一映射（幂等导入的前提）。"""
    files = _load_sample("pass-fail-2x2")
    plans = _plans_for(files, repeats=2)
    first = parse_harbor_files(files, plans)
    second = parse_harbor_files(files, plans)
    assert first == second
    assert first["parser_version"] == PARSER_VERSION
    sources = {result["source_trial_id"] for result in first["results"]}
    assert len(sources) == 4


def test_same_basename_is_never_merged() -> None:
    """同名不同路径的任务不被合并：名字歧义时拒绝映射而不是猜（M3-A02）。"""
    files = _load_sample("pass-fail-2x2")
    duplicated = json.loads(files["harbor/plan.json"])
    task = duplicated["tasks"][0]
    twin = dict(task)
    twin["normalized_relative_path"] = "other/tasks/hello-pass"
    twin["task_key"] = compute_task_key(
        source_id=task["source_id"], dataset_revision="fixtures-2026-09-20",
        normalized_relative_path=twin["normalized_relative_path"],
        task_content_hash="sha256:" + "0" * 64,
    )
    duplicated["tasks"].append(twin)
    files["harbor/plan.json"] = json.dumps(duplicated).encode("utf-8")

    parsed = parse_harbor_files(files, _plans_for(files, repeats=1))
    codes = {item["code"] for item in parsed["unmapped"]}
    assert "HARBOR_TASK_NAME_AMBIGUOUS" in codes
    ambiguous = next(
        item for item in parsed["unmapped"] if item["code"] == "HARBOR_TASK_NAME_AMBIGUOUS"
    )
    assert len(ambiguous["candidates"]) == 2
    # 被歧义挡住的计划单元仍然有 disposition，不凭空消失。
    assert parsed["planned_trial_count"] == 3
    assert len(parsed["results"]) == 3
    # 两个同名任务都被挡：它们各自的计划单元仍是 not_attempted，不混成一条。
    assert sum(
        1 for item in parsed["results"] if item["disposition"] == "not_attempted"
    ) == 2
    assert len({item["task_key"] for item in parsed["results"]}) == 3


def test_unmapped_and_unplanned_trials_are_reported() -> None:
    """未知 trial 目录与计划外的额外 trial 都显式记录，不静默丢弃。"""
    files = _load_sample("pass-fail-2x2")
    stray = {
        rel: data for rel, data in files.items()
        if rel.startswith("harbor/trials/hello-pass")
    }
    for rel, data in stray.items():
        files[rel.replace("harbor/trials/hello-pass", "harbor/trials/unknown-task")] = data

    parsed = parse_harbor_files(files, _plans_for(files, repeats=1))
    codes = {item["code"] for item in parsed["unmapped"]}
    assert "HARBOR_TRIAL_UNMAPPED" in codes

    # 计划只有 1 个 repeat，但同一 task 有 2 个真实 trial → 多出来的是 audit-only。
    parsed_extra = parse_harbor_files(files, _plans_for(files, repeats=1))
    assert any(
        item["code"] == "HARBOR_TRIAL_UNPLANNED" or item["code"] == "HARBOR_TRIAL_UNMAPPED"
        for item in parsed_extra["unmapped"]
    )


def _mapped_directory(parsed: dict[str, Any], name: str) -> str:
    """已映射到某任务的 trial 目录名（按执行顺序取第一个）。"""
    for result in parsed["results"]:
        directory = result.get("source_directory")
        if directory and str(directory).startswith(name):
            return str(directory)
    raise AssertionError(f"no mapped trial directory for {name}: {parsed['unmapped']}")


def test_evidence_completeness_degrades_honestly() -> None:
    """轨迹/终端截断或缺失时降级，不假装完整（M3-A11）。"""
    files = _load_sample("pass-fail-2x2")
    plans = _plans_for(files, repeats=1)

    parsed = parse_harbor_files(files, plans)
    trial = _mapped_directory(parsed, "hello-pass")
    complete = next(
        result for result in parsed["results"] if result["source_directory"] == trial
    )
    items = complete["coverage"]["items"]
    assert items["reward"] == "complete"
    assert items["verifier_output"] == "complete"
    assert items["trajectory"] == "unavailable", "空 agent 日志必须标 unavailable"
    assert "trajectory" in complete["coverage"]["missing"]

    # 截断终端：显式 truncated，不冒充完整。
    derived = dict(files)
    derived[f"harbor/trials/{trial}/trial.log"] = b"x" * (8 * 1024 * 1024 + 1)
    truncated = parse_harbor_files(derived, plans)
    entry = next(
        result for result in truncated["results"] if result["source_directory"] == trial
    )
    assert entry["coverage"]["items"]["terminal"] == "truncated"
    ref = next(ref for ref in entry["artifact_refs"] if ref["kind"] == "harbor-trial-log")
    assert ref["truncated"] is True and ref["complete"] is False

    # 非 UTF-8 证据：标记而非有损解码。
    derived = dict(files)
    derived[f"harbor/trials/{trial}/trial.log"] = b"\xff\xfe\x00binary"
    binary = parse_harbor_files(derived, plans)
    entry = next(
        result for result in binary["results"] if result["source_directory"] == trial
    )
    ref = next(ref for ref in entry["artifact_refs"] if ref["kind"] == "harbor-trial-log")
    assert ref["complete"] is False
    assert ref["note"] == "not valid UTF-8"


def test_terranation_keeps_agent_and_verifier_timings_separate() -> None:
    """Agent/Verifier/环境时长分别保留，不混成一个总时长（M3-G15）。"""
    files = _load_sample("pass-fail-2x2")
    parsed = parse_harbor_files(files, _plans_for(files, repeats=1))
    result = parsed["results"][0]
    timings = result["termination"]["timings"]
    assert set(timings) == {
        "environment_setup_sec", "agent_setup_sec", "agent_execution_sec",
        "verifier_sec", "total_sec",
    }
    assert all(value is None or value >= 0 for value in timings.values())
    assert result["termination"]["trial_name"]
    assert result["termination"]["task_checksum"]


def test_unknown_usage_is_unknown_not_zero() -> None:
    """不可观测的模型身份/费用保持 unknown，不填 0（M3-A12）。"""
    files = _load_sample("pass-fail-2x2")
    parsed = parse_harbor_files(files, _plans_for(files, repeats=1))
    usage = parsed["results"][0]["usage"]
    assert usage["cost_usd"] is None, "oracle agent has no provider cost; must stay null"
    assert usage["coverage"] == "unavailable"
    assert "usage" in parsed["results"][0]["coverage"]["missing"]


def test_cancelled_collection_marks_planned_trials_cancelled() -> None:
    """取消时未采集的计划单元是 cancelled，而不是 failed/通过。"""
    files = _load_sample("pass-fail-2x2")
    parsed = parse_harbor_files(files, _plans_for(files, repeats=1), cancelled=True)
    assert all(
        result["disposition"] in ("cancelled", "succeeded", "failed")
        for result in parsed["results"]
    )
    assert any(result["termination"]["cancelled"] for result in parsed["results"])


def test_candidate_evidence_hash_prevents_silent_overwrite() -> None:
    """同一份原始证据的两个不同结果必须能被区分（M3-A10 的解析侧前提）。"""
    files = _load_sample("pass-fail-2x2")
    plans = _plans_for(files, repeats=1)
    first = parse_harbor_files(files, plans)
    trial = _mapped_directory(first, "hello-fail")
    entry = next(r for r in first["results"] if r["source_directory"] == trial)
    assert entry["disposition"] == "failed"

    # 改写冻结字节里的结构化奖励：解析结果与被引用 hash 同时改变，
    # Trial 存储据此拒绝覆盖旧证据（见 tests/storage/test_trial_store.py）。
    tampered = dict(files)
    payload = json.loads(tampered[f"harbor/trials/{trial}/result.json"])
    payload["verifier_result"]["rewards"] = {"reward": 1.0}
    tampered[f"harbor/trials/{trial}/result.json"] = json.dumps(payload).encode("utf-8")
    tampered = _with_reward(tampered, trial, text="1\n")
    second = parse_harbor_files(tampered, plans)
    entry2 = next(r for r in second["results"] if r["source_directory"] == trial)
    assert entry2["disposition"] == "succeeded"
    assert entry["verifier_observation"]["source_hash"] != (
        entry2["verifier_observation"]["source_hash"]
    )


@pytest.mark.parametrize("sample", ["pass-fail-2x2", "errors-timeout"])
def test_parser_never_reads_the_filesystem(sample: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析只消费传入字节：任何文件系统访问都视为缺陷。"""
    import builtins

    files = _load_sample(sample)
    plans = _plans_for(files, repeats=1)
    real_open = builtins.open

    def _guarded(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes)) and str(file).endswith((".json", ".txt", ".log")):
            raise AssertionError(f"parser must not open {file!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded)
    parsed = parse_harbor_files(files, plans)
    assert parsed["trial_count"] == len(plans)
