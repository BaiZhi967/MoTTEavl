"""M3 review R06/R13：Verifier 错误语义与完整 Task 路径归属。

R06 复现：``result.json.exception_info.exception_type=VerifierTimeoutError``
同时 structured rewards 与 ``reward.txt`` 均为 1 时，
``read_verifier_observation`` 走 reward 分支提前 return，返回
``status=scored/reward=1/error=None``——Verifier 超时被当成有效通过。

R13 复现：两任务 ``a/same``、``b/same`` 的原生结果各带不同完整
``task_id.path``，Parser 只取 basename → 两条都 not_attempted，原结果进入
``HARBOR_TASK_NAME_AMBIGUOUS``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from motte_benchmark.harbor.parser import (
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


def _trial_dirs(files: dict[str, bytes]) -> list[str]:
    return sorted({
        rel.split("/")[2] for rel in files if rel.startswith("harbor/trials/")
    })


def _set_exception(
    files: dict[str, bytes], trial: str, exception_type: str, message: str = "verifier timed out",
) -> dict[str, bytes]:
    derived = dict(files)
    rel = f"harbor/trials/{trial}/result.json"
    payload = json.loads(derived[rel])
    payload["exception_info"] = {
        "exception_type": exception_type,
        "exception_message": message,
    }
    derived[rel] = json.dumps(payload).encode("utf-8")
    return derived


def _set_exception_for_task(
    files: dict[str, bytes], prefix: str, exception_type: str, message: str = "timed out",
) -> dict[str, bytes]:
    """给该任务的全部 Trial 目录打上异常（计划只取其中一个观测，避免歧义）。"""
    derived = files
    for trial in _trial_dirs(files):
        if trial.startswith(prefix):
            derived = _set_exception(derived, trial, exception_type, message)
    return derived


def test_verifier_timeout_with_reward_is_not_a_pass() -> None:
    """R06 反例：Verifier 超时 + reward=1 必须是 verifier_error，不是 scored。"""
    files = _load_sample("pass-fail-2x2")
    pass_trial = next(t for t in _trial_dirs(files) if t.startswith("hello-pass"))
    # 该 fixture 的 reward.txt 与 result.json#verifier_result.rewards 都是 1。
    timed_out = _set_exception_for_task(files, "hello-pass", "VerifierTimeoutError")

    observation = read_verifier_observation(timed_out, pass_trial)
    assert observation["status"] == "verifier_error", observation
    assert observation["error"]["code"] == "VerifierTimeoutError"
    assert observation["error"]["details"]["phase"] == "verifier"
    # 原始 reward 证据必须保留（审计用），但绝不当作质量结论。
    assert observation["rewards"]["reward"] == 1.0
    assert observation["source_path"] in ("verifier/reward.json", "verifier/reward.txt")
    assert observation["source_hash"].startswith("sha256:")
    assert observation["error"]["details"]["raw_rewards"] == {"reward": 1.0}

    parsed = parse_harbor_files(timed_out, _plans_for(files))
    trial = next(
        result for result in parsed["results"]
        if result["verifier_observation"]["status"] == "verifier_error"
    )
    assert trial["verifier_observation"]["status"] == "verifier_error"
    assert trial["disposition"] == "indeterminate", "Verifier 超时不是质量通过/失败"
    assert trial["termination"]["failure_phase"] == "verifier"
    assert trial["verifier_observation"]["rewards"] == {"reward": 1.0}


def test_verifier_crash_without_reward_stays_a_verifier_error() -> None:
    """R06 另一类：无 reward 的 Verifier 崩溃/超时同样是 verifier_error。"""
    files = _load_sample("pass-fail-2x2")
    pass_trial = next(t for t in _trial_dirs(files) if t.startswith("hello-pass"))
    without_reward = {
        rel: data for rel, data in files.items()
        if f"harbor/trials/{pass_trial}/verifier/reward." not in rel
    }
    payload = json.loads(without_reward[f"harbor/trials/{pass_trial}/result.json"])
    payload["verifier_result"] = None
    without_reward[f"harbor/trials/{pass_trial}/result.json"] = json.dumps(payload).encode()
    crashed = _set_exception(without_reward, pass_trial, "VerifierOutputParseError")

    observation = read_verifier_observation(crashed, pass_trial)
    assert observation["status"] == "verifier_error"
    assert observation["rewards"] == {}
    assert observation["error"]["code"] == "VerifierOutputParseError"


def test_agent_timeout_with_healthy_verifier_still_scores() -> None:
    """保留有效评分路径：Agent 超时但 Verifier 正常给出 reward=0 仍是有效失败。"""
    files = _load_sample("pass-fail-2x2")
    fail_trial = next(t for t in _trial_dirs(files) if t.startswith("hello-fail"))
    timed_out = _set_exception_for_task(
        files, "hello-fail", "AgentTimeoutError", "agent hit the limit",
    )

    observation = read_verifier_observation(timed_out, fail_trial)
    assert observation["status"] == "scored"
    assert observation["rewards"] == {"reward": 0.0}
    assert observation["error"] is None

    parsed = parse_harbor_files(timed_out, _plans_for(files))
    trial = next(
        result for result in parsed["results"]
        if result["termination"].get("exception_type") == "AgentTimeoutError"
    )
    assert trial["disposition"] == "failed"
    assert trial["termination"]["failure_phase"] == "agent"


def _plans_for(files: dict[str, bytes]) -> list[dict[str, Any]]:
    plan = json.loads(files["harbor/plan.json"])
    plans: list[dict[str, Any]] = []
    for task in plan["tasks"]:
        plans.append({
            "trial_id": f"trial-{task['normalized_relative_path'].rsplit('/', 1)[-1]}",
            "run_id": "run-1",
            "task_key": task["task_key"],
            "repeat_index": 0,
            "agent_config_hash": "sha256:agent",
            "environment_hash": "sha256:env",
        })
    return plans


def _two_same_basename_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """派生 fixture：两个任务 ``a/same`` 与 ``b/same``，原生结果带完整路径。"""
    derived = dict(files)
    plan = json.loads(derived["harbor/plan.json"])
    plan["tasks"] = [
        {
            "task_key": compute_task_key(
                source_id="review-r13", dataset_revision="r13-1",
                normalized_relative_path=relative,
                task_content_hash="sha256:" + digit * 64,
            ),
            "normalized_relative_path": relative,
            "source_id": "review-r13",
        }
        for relative, digit in (("a/same", "1"), ("b/same", "2"))
    ]
    derived["harbor/plan.json"] = json.dumps(plan).encode("utf-8")

    # 两个 Trial 目录：basename 相同，只有完整 task_id.path 能区分。
    for source, target, native_path in (
        (next(t for t in _trial_dirs(files) if t.startswith("hello-pass")), "same__AAA1111",
         "/data/tasks/a/same"),
        (next(t for t in _trial_dirs(files) if t.startswith("hello-fail")), "same__BBB2222",
         "/data/tasks/b/same"),
    ):
        for rel, data in list(files.items()):
            prefix = f"harbor/trials/{source}/"
            if not rel.startswith(prefix):
                continue
            if rel.endswith("result.json"):
                payload = json.loads(data)
                payload["task_id"] = {"path": native_path}
                payload["trial_name"] = target
                data = json.dumps(payload).encode("utf-8")
            derived[prefix.replace(source, target) + rel[len(prefix):]] = data
    # 其余原始 Trial 目录清掉：本反例只保留这两个同名不同路径的 Trial。
    for rel in list(derived):
        if rel.startswith("harbor/trials/") and rel.split("/")[2] not in (
            "same__AAA1111", "same__BBB2222",
        ):
            del derived[rel]
    return derived


def test_full_task_path_distinguishes_same_basename_tasks() -> None:
    """R13：完整路径可区分时，两任务的全部 Trial 都要保留。"""
    files = _two_same_basename_files(_load_sample("pass-fail-2x2"))
    plans = _plans_for(files)
    assert len(plans) == 2

    parsed = parse_harbor_files(files, plans)
    assert parsed["unmapped"] == [], parsed["unmapped"]
    by_path = {
        task["normalized_relative_path"]: task["task_key"]
        for task in json.loads(files["harbor/plan.json"])["tasks"]
    }
    results = {result["task_key"]: result for result in parsed["results"]}
    assert set(results) == set(by_path.values())
    assert all(result["disposition"] in ("succeeded", "failed") for result in results.values())
    assert results[by_path["a/same"]]["source_directory"] == "same__AAA1111"
    assert results[by_path["b/same"]]["source_directory"] == "same__BBB2222"
    assert results[by_path["a/same"]]["termination"]["task_matching"]["method"] == "path"
    assert results[by_path["a/same"]]["termination"]["task_matching"]["native_path"] == (
        "/data/tasks/a/same"
    )


def test_basename_fallback_is_controlled_and_still_refuses_ambiguity() -> None:
    """唯一 basename 只能作为受控 fallback；歧义时仍然拒绝猜测。"""
    files = _two_same_basename_files(_load_sample("pass-fail-2x2"))
    # 去掉完整路径（原生结果被脱敏成 <HOST_PATH>）：只剩 basename，两者相同 → 歧义。
    for trial in _trial_dirs(files):
        rel = f"harbor/trials/{trial}/result.json"
        payload = json.loads(files[rel])
        payload["task_id"] = {"path": "<HOST_PATH>"}
        payload["trial_name"] = trial
        files[rel] = json.dumps(payload).encode("utf-8")
    parsed = parse_harbor_files(files, _plans_for(files))
    codes = {item["code"] for item in parsed["unmapped"]}
    assert "HARBOR_TASK_NAME_AMBIGUOUS" in codes
    assert all(
        result["disposition"] == "not_attempted" for result in parsed["results"]
    )
    assert len(parsed["results"]) == 2

    # 另一个任务路径不同名 → 原生路径末段是唯一 basename：按 basename fallback 归属。
    renamed = json.loads(files["harbor/plan.json"])
    renamed["tasks"][1]["normalized_relative_path"] = "b/other"
    renamed["tasks"][1]["task_key"] = compute_task_key(
        source_id="review-r13", dataset_revision="r13-1",
        normalized_relative_path="b/other", task_content_hash="sha256:" + "3" * 64,
    )
    unique = {rel: data for rel, data in files.items()
              if not rel.startswith("harbor/trials/same__BBB2222/")}
    unique["harbor/plan.json"] = json.dumps(renamed).encode("utf-8")
    rel = "harbor/trials/same__AAA1111/result.json"
    payload = json.loads(unique[rel])
    payload["task_id"] = {"path": "/data/elsewhere/same"}
    unique[rel] = json.dumps(payload).encode("utf-8")
    single = parse_harbor_files(unique, _plans_for(files))
    matched = [
        result for result in single["results"]
        if result["termination"].get("task_matching", {}).get("method")
    ]
    assert len(matched) == 1, single["results"]
    assert matched[0]["termination"]["task_matching"]["method"] == "basename"
    assert matched[0]["disposition"] in ("succeeded", "failed")
    # 没有观测到的另一个计划单元仍然有 disposition（不凭空消失）。
    assert sum(1 for r in single["results"] if r["disposition"] == "not_attempted") == 1

    # 原生路径完全不可用（脱敏）时，仍按 Trial 名前缀受控归属（唯一候选）。
    desensitized = dict(unique)
    payload = json.loads(desensitized[rel])
    payload["task_id"] = {"path": "<HOST_PATH>"}
    desensitized[rel] = json.dumps(payload).encode("utf-8")
    by_trial_name = parse_harbor_files(desensitized, _plans_for(files))
    fallback = [
        result for result in by_trial_name["results"]
        if result["termination"].get("task_matching", {}).get("method")
    ]
    assert len(fallback) == 1, by_trial_name["results"]
    assert fallback[0]["termination"]["task_matching"]["method"] == "trial_name"
