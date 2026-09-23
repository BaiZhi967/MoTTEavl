"""M6 CLI 行为测试：experiment / compare / baseline / gate / regression。

全部走 `motte_cli.main.main()` 入口（与既有 CLI 测试同一模式），用 tmp SQLite
库手工铺 Run + ScoringPass + ScoreSet 合成固定证据；断言的对象是**退出码**
（协议 §7 冻结表）与 stdout/stderr 的 JSON 形状。成功路径不打印密钥/prompt：
本套件根本不注入任何凭据。
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from motte_cli.main import main
from motte_storage.factory import create_run_store

RUN_BASE = "run-m6-base"
RUN_SUB = "run-m6-subset"
RUN_CAND = "run-m6-candidate"
RUN_FAILED = "run-m6-failed"
TIMESTAMP = "2026-09-21T00:00:00+00:00"


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def seed_run(store, run_id: str, cases: list[str], *, status: str = "completed") -> None:
    store.runs.create({
        "id": run_id,
        "schema_version": 2,
        "revision": 1,
        "scenario_version": "replay@1",
        "status": status,
        "manifest": {"evaluation": {"scorer_id": "deterministic", "scorer_version": "1"}},
        "requested_manifest": {},
        "case_ids": list(cases),
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    }, event={"run_id": run_id, "type": "queued", "status": status})


def seed_pass(store, run_id: str, pass_id: str, case_values: dict[str, bool]) -> None:
    """单指标 accuracy pass：与所选 pass 的身份由 scorer_id/version 固定。"""
    scores = [{
        "case_id": case_id,
        "metric_id": "accuracy",
        "evaluator_id": "deterministic",
        "evaluator_version": "1",
        "metric_status": "scored",
        "value": 1.0 if passed else 0.0,
        "passed": passed,
        "denominator": True,
        "details": {},
    } for case_id, passed in case_values.items()]
    store.scoring_passes.append({
        "id": pass_id,
        "run_id": run_id,
        "scorer_id": "deterministic",
        "scorer_version": "1",
        "created_at": TIMESTAMP,
        "source": "initial",
        "source_run_revision": 1,
        "summary": {},
    }, scores)


@pytest.fixture()
def db(tmp_path):
    """合成固定证据库：4 个终态 Run + 各一条 current pass。"""
    path = tmp_path / "m6-cli.db"
    store = create_run_store(str(path))
    seed_run(store, RUN_BASE, ["c1", "c2"])
    seed_pass(store, RUN_BASE, "pass-base", {"c1": True, "c2": True})
    seed_run(store, RUN_SUB, ["c1"])
    seed_pass(store, RUN_SUB, "pass-sub", {"c1": True})
    seed_run(store, RUN_CAND, ["c1", "c2", "c3"])
    seed_pass(store, RUN_CAND, "pass-cand", {"c1": False, "c2": True, "c3": True})
    seed_run(store, RUN_FAILED, ["c1"], status="failed")
    seed_pass(store, RUN_FAILED, "pass-failed", {"c1": True})
    return path


def gate_policy(policy_id: str, rules: list[dict], *, version: str = "1") -> dict:
    return {
        "policy_id": policy_id,
        "version": version,
        "rules": rules,
        "created_by": "cli-tester",
        "reason": "m6 cli exit-code matrix",
    }


def threshold_rule(**overrides: object) -> dict:
    rule: dict = {
        "rule_id": "acc",
        "kind": "metric_threshold",
        "metric_id": "accuracy",
        "operator": "gte",
        "threshold": 0.5,
    }
    rule.update(overrides)
    return rule


def experiment_spec(**overrides: object) -> dict:
    payload: dict = {
        "experiment_id": "exp-cli",
        "version": "1",
        "task_ref": {"suite": "direct-llm",
                     "scenario_version": "direct-llm-exact-answer@1"},
        "factors": {"model_profile": ["probe"]},
        "repeats": 2,
        "budget_policy": {"max_total_calls": 100},
        "created_by": "cli-tester",
        "reason": "m6 cli test",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def exp_db(tmp_path, capsys):
    """experiment create 的可分配环境：真实 direct-llm 数据集 + provider/模型档案。

    Cell 的 Run 走与普通 Run 同一条 prepare_run 解析链，因此场景与模型必须
    真实存在于资源仓库（与 tests/cli/test_direct_llm_cli.py 同一播种方式）。
    """
    path = tmp_path / "m6-exp.db"
    code, out, err = run_cli(
        capsys, "direct-llm", "import", "--builtin", "direct-llm-exact-answer",
        "--db", str(path))
    assert code == 0, err
    from motte_storage.factory import default_content_store
    from motte_storage.resource_store import SQLiteResourceStore

    resources = SQLiteResourceStore(
        str(path), content_store=default_content_store(),
    )
    resources.providers.put({
        "name": "local", "kind": "openai_compatible",
        "base_url": "https://local.test/v1", "model": "unused",
    })
    resources.models.put({
        "id": "probe", "provider": "local", "model": "probe-1",
        "capabilities": {}, "max_output_tokens": 4096,
    })
    return path


# ------------------------------------------------------------------ 退出码矩阵


def test_gate_evaluate_exit_code_matrix(db, capsys):
    """协议 §7：pass=0 / quality_fail=1 / execution_error=3 /
    insufficient|not_comparable=5（真实求值路径，合成固定证据）。"""
    policies = {
        "p-pass@1": [threshold_rule()],
        "p-quality@1": [threshold_rule(threshold=2.0)],
        "p-insufficient@1": [{
            "rule_id": "cost", "kind": "cost", "metric_id": "cost.total_usd",
        }],
        "p-notcomparable@1": [threshold_rule(required_comparability="comparable")],
        "p-execution@1": [threshold_rule(requires_successful_run=True)],
    }
    for policy_ref, rules in policies.items():
        policy_id, _, version = policy_ref.partition("@")
        code, out, err = run_cli(
            capsys, "gate", "policy-publish", "--db", str(db),
            "--policy", json.dumps(gate_policy(policy_id, rules, version=version)),
        )
        assert code == 0, (policy_ref, out, err)

    cases = [
        ("p-pass@1", RUN_BASE, 0, "pass"),
        ("p-quality@1", RUN_BASE, 1, "quality_fail"),
        ("p-insufficient@1", RUN_BASE, 5, "insufficient_evidence"),
        ("p-notcomparable@1", RUN_BASE, 5, "not_comparable"),
        ("p-execution@1", RUN_FAILED, 3, "execution_error"),
    ]
    for policy_ref, run_id, expected_code, expected_decision in cases:
        code, out, err = run_cli(
            capsys, "gate", "evaluate", "--policy", policy_ref,
            "--run", run_id, "--db", str(db),
        )
        assert code == expected_code, (policy_ref, out, err)
        payload = json.loads(out)
        assert payload["decision"] == expected_decision, payload
        assert payload["exit_code"] == expected_code
        assert payload["gate_result_id"].startswith("sha256:")
        assert payload["conclusion_hash"].startswith("sha256:")


def test_gate_evaluate_safety_block_exit_6(db, capsys, monkeypatch):
    """safety_block=6：SDK 版本化求值当前把副作用/安全标记投影为不可观测
    （恒 insufficient），因此这里替换服务方法返回真实形状的 safety_block
    结果，只验证 CLI 的决策→退出码映射与 JSON 输出。"""
    from motte_sdk.comparisons import ComparisonService

    def fake_evaluate(self, *, policy_id, policy_version, run_id,
                      scoring_pass_id=None, baseline_id=None,
                      evaluated_at=None, allowed_factors=("model",)):
        return {
            "gate_result_id": "sha256:" + "6" * 64,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "decision": "safety_block",
            "exit_code": 6,
            "conclusion_hash": "sha256:" + "c" * 64,
            "rule_results": [{
                "rule_id": "safety", "kind": "safety_marker", "status": "fail",
                "decision": "safety_block", "reason": "safety markers present: m-1",
            }],
        }

    monkeypatch.setattr(
        ComparisonService, "evaluate_gate_versioned", fake_evaluate,
    )
    code, out, err = run_cli(
        capsys, "gate", "evaluate", "--policy", "p-safety@1",
        "--run", RUN_BASE, "--db", str(db),
    )
    assert code == 6, (out, err)
    assert json.loads(out)["decision"] == "safety_block"


def test_gate_policy_publish_rejects_unknown_rule_kind(db, capsys):
    """配置不合法（未知规则 kind）→ 求值前拒绝，退出码 2 + 结构化错误。"""
    code, out, err = run_cli(
        capsys, "gate", "policy-publish", "--db", str(db),
        "--policy", json.dumps(gate_policy("p-bad", [{
            "rule_id": "x", "kind": "nonsense",
        }])),
    )
    assert code == 2
    assert json.loads(err)["error"]["code"] == "GATE_POLICY_INVALID"


def test_gate_policy_publish_is_idempotent(db, capsys):
    policy = json.dumps(gate_policy("p-idem", [threshold_rule()]))
    first, _, _ = run_cli(
        capsys, "gate", "policy-publish", "--db", str(db), "--policy", policy)
    second, _, _ = run_cli(
        capsys, "gate", "policy-publish", "--db", str(db), "--policy", policy)
    assert first == second == 0


def test_gate_evaluate_rejects_missing_policy_and_bad_ref(db, capsys):
    code, _, err = run_cli(
        capsys, "gate", "evaluate", "--policy", "p-missing@1",
        "--run", RUN_BASE, "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "GATE_POLICY_NOT_FOUND"

    code, _, err = run_cli(
        capsys, "gate", "evaluate", "--policy", "no-version",
        "--run", RUN_BASE, "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "POLICY_REF_INVALID"

    code, _, err = run_cli(
        capsys, "gate", "evaluate", "--policy-id", "p-pass",
        "--run", RUN_BASE, "--db", str(db))
    assert code == 2  # 缺 --version


# ------------------------------------------------------- 导出：JSON / JUnit


def test_gate_evaluate_writes_json_and_valid_junit(db, tmp_path, capsys):
    run_cli(capsys, "gate", "policy-publish", "--db", str(db),
            "--policy", json.dumps(gate_policy("p-export", [threshold_rule()])))

    json_out = tmp_path / "gate.json"
    junit_out = tmp_path / "junit.xml"
    code, out, err = run_cli(
        capsys, "gate", "evaluate", "--policy", "p-export@1", "--run", RUN_BASE,
        "--json", str(json_out), "--junit", str(junit_out), "--db", str(db))
    assert code == 0, err

    exported = json.loads(json_out.read_text(encoding="utf-8"))
    assert exported["exporter_version"] == "gate-exporter@1"
    assert exported["decision"] == "pass"
    assert exported["exit_code"] == 0

    root = ET.parse(junit_out).getroot()
    assert root.tag == "testsuite"
    assert root.attrib["tests"] == "1"
    testcases = root.findall("testcase")
    assert len(testcases) == 1
    assert testcases[0].attrib["name"] == "acc"
    properties = {
        prop.attrib["name"]: prop.attrib["value"]
        for prop in root.findall("properties/property")
    }
    assert properties["decision"] == "pass"
    assert properties["exit_code"] == "0"


def test_gate_result_and_export_roundtrip(db, capsys, tmp_path):
    run_cli(capsys, "gate", "policy-publish", "--db", str(db),
            "--policy", json.dumps(gate_policy("p-rt", [threshold_rule(threshold=2.0)])))
    code, out, _ = run_cli(
        capsys, "gate", "evaluate", "--policy", "p-rt@1",
        "--run", RUN_BASE, "--db", str(db))
    assert code == 1
    gate_result_id = json.loads(out)["gate_result_id"]

    code, out, _ = run_cli(
        capsys, "gate", "result", gate_result_id, "--db", str(db))
    assert code == 0
    assert json.loads(out)["decision"] == "quality_fail"

    code, _, err = run_cli(capsys, "gate", "result", "sha256:deadbeef", "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "GATE_RESULT_NOT_FOUND"

    junit_path = tmp_path / "rt-junit.xml"
    code, _, _ = run_cli(
        capsys, "gate", "export", "--result", gate_result_id,
        "--format", "junit", "--out", str(junit_path), "--db", str(db))
    assert code == 0
    root = ET.parse(junit_path).getroot()
    assert root.findall("testcase/failure"), "quality_fail 规则必须是 JUnit failure"

    code, out, _ = run_cli(
        capsys, "gate", "export", "--result", gate_result_id,
        "--format", "json", "--db", str(db))
    assert code == 0
    assert json.loads(out)["exporter_version"] == "gate-exporter@1"


# ------------------------------------------------------------------- compare


def test_compare_not_comparable_exits_5_and_comparable_exits_0(db, tmp_path, capsys):
    json_out = tmp_path / "compare.json"
    code, out, err = run_cli(
        capsys, "compare", "--baseline", RUN_BASE, "--candidate", RUN_SUB,
        "--json", str(json_out), "--db", str(db))
    assert code == 5, (out, err)
    payload = json.loads(out)
    assert payload["level"] == "not_comparable"
    assert any("CASE_SET_CHANGED" in reason for reason in payload["structural_reasons"])
    assert payload["case_diff"]["added"] == []
    assert payload["case_diff"]["removed"] == ["c2"]
    assert json.loads(json_out.read_text(encoding="utf-8"))["level"] == "not_comparable"

    code, out, err = run_cli(
        capsys, "compare", "--baseline", RUN_BASE, "--candidate", RUN_BASE,
        "--db", str(db))
    assert code == 0, (out, err)
    payload = json.loads(out)
    assert payload["level"] in ("comparable", "partially_comparable")
    assert payload["metric_eligibility"]["quality"] is True


def test_compare_statistics_cli_pins_pass_and_records_method(db, capsys, tmp_path):
    output = tmp_path / "paired.json"
    code, out, err = run_cli(
        capsys, "compare", "--baseline", RUN_BASE, "--candidate", RUN_BASE,
        "--baseline-pass", "pass-base", "--candidate-pass", "pass-base",
        "--statistics", "--json", str(output), "--db", str(db),
    )
    assert code == 0, err
    stats = json.loads(out)["statistics"]
    assert json.loads(out)["refs"]["baseline"]["scoring_pass_id"] == "pass-base"
    assert stats["applicable"] is True
    assert stats["n_pairs"] == 2
    assert stats["method"] == "paired_task_cluster_bootstrap"
    assert stats["statistics"]["interval"]["seed"] == 20260921
    assert json.loads(output.read_text(encoding="utf-8"))["statistics"] == stats


def test_compare_statistics_cli_exports_terminal_trial_qualification(db, capsys, tmp_path):
    store = create_run_store(str(db))
    for run_id in ("tb-base", "tb-candidate"):
        plan = [
            {"trial_id": f"{run_id}-{task}-{repeat}", "task_key": task,
             "repeat_index": repeat, "run_id": run_id,
             "agent_config_hash": "agent-a", "environment_hash": "env-a"}
            for task in ("a", "b") for repeat in range(2)
        ]
        store.runs.create({
            "id": run_id, "schema_version": 2, "revision": 1,
            "scenario_version": "terminal-bench@1", "status": "completed",
            "manifest": {"benchmark_provenance": {"suite": "terminal-bench-harbor"},
                         "task_manifest": {"trials": plan},
                         "evaluation": {"scorer_id": "harbor", "scorer_version": "1"}},
            "requested_manifest": {}, "case_ids": ["a", "b"],
            "created_at": TIMESTAMP, "updated_at": TIMESTAMP,
        })
        store.scoring_passes.append({
            "id": f"pass-{run_id}", "run_id": run_id, "scorer_id": "harbor",
            "scorer_version": "1", "created_at": TIMESTAMP, "source": "initial",
            "source_run_revision": 1, "summary": {},
        }, [{
            "case_id": task, "trial_id": f"{run_id}-{task}-{repeat}",
            "metric_id": "reward", "evaluator_id": "harbor",
            "evaluator_version": "1", "metric_status": "scored",
            "unit": "trial", "value": 1.0 if repeat == 0 else 0.0,
            "passed": repeat == 0, "denominator": True,
            "details": {"repeat_index": repeat},
        } for task in ("a", "b") for repeat in range(2)])
    output = tmp_path / "terminal-statistics.json"
    code, out, err = run_cli(
        capsys, "compare", "--baseline", "tb-base", "--candidate", "tb-candidate",
        "--baseline-pass", "pass-tb-base", "--candidate-pass", "pass-tb-candidate",
        "--statistics", "--k", "2", "--json", str(output), "--db", str(db),
    )
    assert code == 0, err
    stats = json.loads(out)["statistics"]
    assert stats["trial_aggregation"]["baseline"]["k"] == 2
    assert stats["trial_aggregation"]["baseline"]["per_task"]["a"]["pass_at_k"]["value"] == 1.0
    assert stats["refs"]["baseline"]["scoring_pass_id"] == "pass-tb-base"
    assert json.loads(output.read_text(encoding="utf-8"))["statistics"] == stats


def test_compare_statistics_cli_rejects_invalid_k_as_request_error(db, capsys):
    code, _out, err = run_cli(
        capsys, "compare", "--baseline", RUN_BASE, "--candidate", RUN_BASE,
        "--statistics", "--k", "0", "--db", str(db),
    )
    assert code == 2
    assert json.loads(err)["error"]["code"] == "POLICY_INVALID"


def test_compare_rejects_unknown_run_and_unknown_factor(db, capsys):
    code, _, err = run_cli(
        capsys, "compare", "--baseline", "run-nope", "--candidate", RUN_BASE,
        "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "RUN_NOT_FOUND"

    code, _, err = run_cli(
        capsys, "compare", "--baseline", RUN_BASE, "--candidate", RUN_BASE,
        "--factors", "bogus", "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "POLICY_INVALID"


# ----------------------------------------------------------------- baseline


def test_baseline_create_select_default_and_cas_conflict(db, capsys):
    entries = json.dumps([
        {"run_id": RUN_BASE, "scoring_pass_id": "pass-base"},
    ])
    code, out, err = run_cli(
        capsys, "baseline", "create", "--id", "blt-1", "--entries", entries,
        "--by", "cli-tester", "--reason", "m6 cli test", "--db", str(db))
    assert code == 0, err
    snapshot = json.loads(out)
    assert snapshot["baseline_id"] == "blt-1"
    assert snapshot["eligibility"] == "formal"
    assert len(snapshot["entries"]) == 1

    code, out, err = run_cli(
        capsys, "baseline", "create", "--id", "blt-2", "--entries", entries,
        "--by", "cli-tester", "--reason", "second snapshot", "--db", str(db))
    assert code == 0, err

    code, out, err = run_cli(
        capsys, "baseline", "list", "--db", str(db))
    assert code == 0
    assert json.loads(out)["total"] == 2

    # 首次设置指针不需要 expected_current。
    code, out, err = run_cli(
        capsys, "baseline", "select", "--scope", "prod", "--baseline", "blt-1",
        "--by", "cli-tester", "--reason", "initial", "--db", str(db))
    assert code == 0, err
    assert json.loads(out)["baseline_id"] == "blt-1"

    # 指针已存在：不给 expected_current / 期望不符 → CAS_CONFLICT + 退出码 2。
    code, out, err = run_cli(
        capsys, "baseline", "select", "--scope", "prod", "--baseline", "blt-2",
        "--by", "cli-tester", "--reason", "move", "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "CAS_CONFLICT"

    code, out, err = run_cli(
        capsys, "baseline", "select", "--scope", "prod", "--baseline", "blt-2",
        "--expected-current", "blt-9", "--by", "cli-tester", "--reason", "move",
        "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "CAS_CONFLICT"

    code, out, err = run_cli(
        capsys, "baseline", "select", "--scope", "prod", "--baseline", "blt-2",
        "--expected-current", "blt-1", "--by", "cli-tester", "--reason", "move",
        "--db", str(db))
    assert code == 0, err

    code, out, err = run_cli(
        capsys, "baseline", "default", "--scope", "prod", "--db", str(db))
    assert code == 0
    assert json.loads(out)["pointer"]["baseline_id"] == "blt-2"


def test_baseline_create_rejects_incomplete_reference(db, capsys):
    code, _, err = run_cli(
        capsys, "baseline", "create", "--id", "blt-bad",
        "--entries", json.dumps([{"run_id": "run-nope", "scoring_pass_id": "p"}]),
        "--by", "cli-tester", "--reason", "x", "--db", str(db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "BASELINE_NOT_FOUND"


# ---------------------------------------------------------------- experiment


def test_experiment_preview_violations_exit_2(exp_db, capsys):
    code, out, err = run_cli(
        capsys, "experiment", "preview", "--db", str(exp_db),
        "--spec", json.dumps(experiment_spec(max_cells=1)))
    assert code == 2, (out, err)
    payload = json.loads(out)
    assert payload["cell_count"] == 2
    assert [item["code"] for item in payload["violations"]] == ["MATRIX_TOO_LARGE"]


def test_experiment_preview_ok_exit_0_and_contract_error(exp_db, tmp_path, capsys):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(experiment_spec()), encoding="utf-8")
    code, out, err = run_cli(
        capsys, "experiment", "preview", "--db", str(exp_db),
        "--spec", f"@{spec_file}")
    assert code == 0, err
    payload = json.loads(out)
    assert payload["cell_count"] == 2
    assert len(payload["cells"]) == 2
    assert payload["violations"] == []

    code, out, err = run_cli(
        capsys, "experiment", "preview", "--db", str(exp_db),
        "--spec", json.dumps(experiment_spec(factors={"bogus": ["x"]})))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "EXPERIMENT_INVALID"


def test_experiment_create_status_retry_and_cancel(exp_db, capsys):
    code, out, err = run_cli(
        capsys, "experiment", "create", "--db", str(exp_db),
        "--request-key", "req-cli-1",
        "--spec", json.dumps(experiment_spec()))
    assert code == 0, err
    created = json.loads(out)
    assert created["created"] is True
    assert created["allocated"] == 2
    assert created["skipped_existing"] == 0
    assert created["failed"] == []
    assert [cell["allocation_status"] for cell in created["cells"]] == [
        "allocated", "allocated",
    ]
    cell_ids = [cell["cell_id"] for cell in created["cells"]]
    assert all(cell_id.startswith("sha256:") for cell_id in cell_ids)

    code, out, err = run_cli(
        capsys, "experiment", "status", "exp-cli", "--db", str(exp_db))
    assert code == 0, err
    status = json.loads(out)
    assert status["cell_count"] == 2
    assert status["progress"]["allocated"] == 2

    first_cell = created["cells"][0]
    code, out, err = run_cli(
        capsys, "experiment", "retry-cell", first_cell["cell_id"],
        "--reason", "flaky cell", "--db", str(exp_db))
    assert code == 0, err
    retried = json.loads(out)
    assert retried["parent_run_id"] == first_cell["run_id"]
    assert retried["run_id"].endswith("-r1")

    code, out, err = run_cli(
        capsys, "experiment", "cancel", "exp-cli", "--db", str(exp_db))
    assert code == 0, err
    cancelled = json.loads(out)
    # allocated cell 的自有 Run 被 RunService.cancel 取消（cell 记录保留
    # allocation_status=allocated：分配事实与 Run 终态分开表达）。
    assert len(cancelled["cancelled_runs"]) == 2, cancelled

    code, out, err = run_cli(
        capsys, "experiment", "status", "exp-cli", "--db", str(exp_db))
    assert code == 0

    code, _, err = run_cli(
        capsys, "experiment", "status", "exp-nope", "--db", str(exp_db))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "EXPERIMENT_NOT_FOUND"


def test_experiment_create_rejects_unsupported_suite(db, capsys):
    code, _, err = run_cli(
        capsys, "experiment", "create", "--db", str(db),
        "--spec", json.dumps(experiment_spec(
            task_ref={"suite": "harbor", "scenario_version": "harbor@1"})))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "SUITE_UNSUPPORTED"


def test_experiment_create_replay_same_request_key_is_idempotent(exp_db, capsys):
    # spec 落库走 JSON 规范化（tuple→list），sqlite 后端重放幂等。
    spec = json.dumps(experiment_spec())
    first, out, err = run_cli(
        capsys, "experiment", "create", "--db", str(exp_db),
        "--request-key", "req-replay", "--spec", spec)
    assert first == 0, err
    assert json.loads(out)["created"] is True

    second, out, err = run_cli(
        capsys, "experiment", "create", "--db", str(exp_db),
        "--request-key", "req-replay", "--spec", spec)
    assert second == 0, err
    replayed = json.loads(out)
    assert replayed["created"] is False
    assert replayed["skipped_existing"] == 2


# --------------------------------------------------------------- regression


def test_regression_classifies_new_failure_added_and_fixed(db, capsys):
    code, out, err = run_cli(
        capsys, "regression", "--baseline", RUN_BASE, "--candidate", RUN_CAND,
        "--db", str(db))
    assert code == 0, err
    report = json.loads(out)
    assert report["new_failure_cases"] == ["c1"]
    assert report["fixed_cases"] == []
    assert report["added"] == ["c3"]
    assert report["removed"] == []
    assert report["counts"]["new_failure"] == 1
    assert report["counts"]["added_case"] == 1
    assert report["classification"]["c2"] == "persistent_pass"


def test_regression_rejects_missing_scoring_evidence(db, tmp_path, capsys):
    # 没有 scoring pass 的 Run：无固定报告，比较/分类按"请求不合法"退出 2。
    path = tmp_path / "bare.db"
    store = create_run_store(str(path))
    seed_run(store, "run-bare", ["c1"])
    code, _, err = run_cli(
        capsys, "regression", "--baseline", "run-bare", "--candidate", RUN_BASE,
        "--db", str(path))
    assert code == 2
    assert json.loads(err)["error"]["code"] == "NO_SCORING_EVIDENCE"
