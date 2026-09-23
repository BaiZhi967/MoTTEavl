"""Build the M8 crosswalk from the archived 137-goal assessment.

Historical labels are retained as provenance, never promoted to current verification.
Run from the repository root; the output is deterministic and reviewable in Git.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSESSMENT = (
    ROOT / "docs/review/2026-09-22-m1-m7-assessment/"
    "MoTTEavl_M1-M7_Assessment_2026-09-22.md"
)
OUTPUT = ROOT / "docs/verification/m8-evidence-index.json"
BASE_SHA = "0ad766fd8020e852b61b88ef82e92d010c553584"
REVIEW_SHA = "fc1e7d56c47d36952fb338bf9cb3b1f8c0fbfb3d"
GOAL_ROW = re.compile(r"^\| (M[1-7]-G\d{2}) \| (.*?) \| ([EIVRO]) \| (.*?) \|$")

ROADMAP = {
    1: "M1-native-agent-and-evaluation.md",
    2: "M2-llm-benchmarks-and-ceval.md",
    3: "M3-harbor-and-terminal-bench.md",
    4: "M4-pi-and-external-harnesses.md",
    5: "M5-scenarios-skills-and-judges.md",
    6: "M6-experiments-comparison-and-gates.md",
    7: "M7-sdk-migration-and-release.md",
}
HISTORICAL_RECEIPT = {
    1: "docs/verification/M1.md",
    2: "docs/verification/M2-R4-fixes-2026-09-20.md",
    3: "docs/verification/M3.md",
    4: "docs/verification/M4-completion-review-2026-09-21.md",
    5: "docs/verification/M1-M5-acceptance-report-2026-09-21.md",
    6: "docs/verification/M6.md",
    7: "docs/verification/M7.md",
}

CURRENT_EVIDENCE = {
    "M3-G04": (
        ["packages/sdk-python/motte_sdk/comparisons.py",
         "packages/contracts/motte_contracts/trial.py"],
        ["tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_aggregate_only_fixed_planned_valid_trials",
         "tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_reject_missing_trial_and_retry_rows"],
        "partial_m8; fixed-pass statistical consumer excludes unplanned retry rows; full Trial lifecycle remains historical evidence",
    ),
    "M3-G15": (
        ["packages/sdk-python/motte_sdk/comparisons.py",
         "packages/evaluators/motte_eval/statistics.py"],
        ["tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_aggregate_only_fixed_planned_valid_trials",
         "tests/integration/test_experiment_gate_slice.py::test_terminal_trial_statistics_http_uses_fixed_pass_and_k"],
        "partial_m8; fixed-pass Task/Trial denominator and k consumer verified offline; live Profile coverage pending",
    ),
    "M6-G01": (
        ["packages/sdk-python/motte_sdk/experiments.py", "packages/sdk-python/motte_sdk/resolve.py"],
        ["tests/api/test_m8_experiment_contract.py::test_a03_ten_cases_two_cells_cap_five_has_zero_executable_side_effects",
         "tests/sdk/test_m6_experiments.py::test_gsm8k_experiment_matches_standalone_preparation",
         "tests/sdk/test_m6_experiments.py::test_unassembled_suite_preview_and_create_fail_closed",
         "tests/sdk/test_m6_experiments.py::test_agent_runtime_factor_cannot_silently_use_legacy_json"],
        "partial_m8; other suites and real execution pending",
    ),
    "M6-G02": (
        ["packages/sdk-python/motte_sdk/experiments.py",
         "packages/sdk-python/motte_sdk/execution_lock.py",
         "packages/storage/motte_storage/platform.py"],
        ["tests/api/test_m8_experiment_contract.py::test_a05_request_key_conflict_is_http_409_after_app_rebuild",
         "tests/sdk/test_m6_experiments.py::test_active_sqlite_allocation_is_not_reset_by_second_service",
         "tests/sdk/test_m6_experiments.py::test_resource_ceiling_drift_after_preview_rejects_before_persistence"],
        "partial_m8; cross-process lock and drift verified on SQLite; PostgreSQL process test pending CI receipt",
    ),
    "M6-G18": (
        ["packages/sdk-python/motte_sdk/experiments.py", "packages/sdk-python/motte_sdk/resolve.py"],
        ["tests/sdk/test_m6_experiments.py::test_gsm8k_experiment_matches_standalone_preparation",
         "tests/sdk/test_m6_experiments.py::test_agent_tasks_experiment_matches_standalone_and_counts_steps"],
        "partial_m8; Direct/GSM8K/Agent controlled factors verified, runtime/Judge factor coverage pending",
    ),
    **{
        goal_id: (
            ["packages/sdk-python/motte_sdk/comparisons.py",
             "apps/web/src/evalTypes/suiteComparison.ts"],
            ["tests/integration/test_experiment_gate_slice.py::test_comparison_http_uses_fixed_pass_and_detects_case_and_scorer_changes",
             "apps/web/tests/evalTypes.test.tsx::DirectLlmCompare"],
            "partial_m8; fixed-pass suite pages verified offline",
        )
        for goal_id in ("M6-G04", "M6-G05", "M6-G07", "M6-G08", "M6-G09",
                        "M6-G12", "M6-G19", "M6-G21")
    },
    "M6-G10": (
        ["packages/evaluators/motte_eval/statistics.py",
         "packages/sdk-python/motte_sdk/comparisons.py", "apps/api/app/main.py",
         "packages/cli/motte_cli/main.py", "apps/web/src/pages/m6/ComparePage.tsx"],
        ["tests/sdk/test_m6_comparison_service.py::test_paired_statistics_uses_fixed_pass_and_keeps_missing_visible",
         "tests/integration/test_experiment_gate_slice.py::test_comparison_http_uses_fixed_pass_and_detects_case_and_scorer_changes",
         "tests/cli/test_m6_cli.py::test_compare_statistics_cli_pins_pass_and_records_method",
         "tests/cli/test_m6_cli.py::test_compare_statistics_cli_exports_terminal_trial_qualification",
         "tests/cli/test_m6_cli.py::test_compare_statistics_cli_rejects_invalid_k_as_request_error",
         "apps/web/tests/M6.test.tsx::ComparePage"],
        "partial_m8; fixed-pass Case and Trial statistics consumed by SDK/HTTP/CLI JSON/Web; persisted published report pending",
    ),
    "M6-G11": (
        ["packages/evaluators/motte_eval/statistics.py",
         "packages/sdk-python/motte_sdk/comparisons.py", "apps/api/app/main.py",
         "packages/cli/motte_cli/main.py", "apps/web/src/pages/m6/ComparePage.tsx"],
        ["tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_aggregate_only_fixed_planned_valid_trials",
         "tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_reject_missing_trial_and_retry_rows",
         "tests/sdk/test_m6_comparison_service.py::test_terminal_statistics_reject_reused_upstream_trial_identity",
         "tests/integration/test_experiment_gate_slice.py::test_terminal_trial_statistics_http_uses_fixed_pass_and_k",
         "tests/cli/test_m6_cli.py::test_compare_statistics_cli_exports_terminal_trial_qualification",
         "apps/web/tests/M6.test.tsx::ComparePage"],
        "partial_m8; planned valid Trial pass@k and retry distinction have fixed-pass consumers; live independence not verified",
    ),
    "M7-G10": (
        ["packages/sdk-python/motte_sdk/migration/plan.py",
         "packages/sdk-python/motte_sdk/migration/apply.py"],
        ["tests/migration/test_import_plan.py::test_plan_is_a_pure_dry_run",
         "tests/migration/test_legacy_import.py::test_second_apply_of_same_source_is_all_reused"],
        "partial_m8; synthetic dry-run/apply idempotency verified; authorized private export pending",
    ),
    "M7-G14": (
        ["packages/sdk-python/motte_sdk/migration/apply.py",
         "packages/sdk-python/motte_sdk/migration/rollback.py"],
        ["tests/migration/test_import_resume_rollback.py::test_crash_mid_apply_resumes_without_duplicates",
         "tests/migration/test_import_resume_rollback.py::test_rollback_deletes_only_own_unreferenced_objects"],
        "partial_m8; synthetic interruption/rollback verified; private export and PG restore pending",
    ),
    "M7-G15": (
        ["packages/storage/motte_storage/maintenance.py"],
        ["tests/integration/test_backup_restore_consistency.py::test_manifest_v2_roundtrip_counts_hashes_and_extra_file_warning",
         "tests/integration/test_backup_restore_consistency.py::test_staging_restore_verifies_and_guards_worker_claims"],
        "partial_m8; SQLite DB+Artifact backup/restore verified; isolated PG dump/restore awaits CI",
    ),
    "M7-G16": (
        ["packages/storage/motte_storage/maintenance.py",
         "packages/storage/motte_storage/migrations.py"],
        ["tests/integration/test_release_smoke.py::test_release_upgrade_old_shape_database_gets_platform_tables",
         "tests/integration/test_release_smoke.py::test_release_recovery_path_from_backup"],
        "partial_m8; synthetic old-shape SQLite upgrade/recovery verified; supported PG upgrade/rollback pending",
    ),
    "M7-G21": (
        ["tests/integration/test_cutover_readiness.py"],
        ["tests/integration/test_cutover_readiness.py::test_cutover_scenario_1_model_evaluation_two_runs",
         "tests/integration/test_cutover_readiness.py::test_cutover_scenario_2_agent_benchmark_offline_evidence",
         "tests/integration/test_cutover_readiness.py::test_cutover_scenario_3_business_regression_offline_evidence"],
        "partial_m8; three synthetic scenarios checked, real replacement acceptance and cutover pending",
    ),
}


def packages(goal_id: str) -> list[str]:
    stage = int(goal_id[1])
    suffix = int(goal_id[-2:])
    result = ["T00"]
    if stage == 6:
        if suffix in {1, 2, 3, 18}:
            result += ["T02", "T03", "T05"]
        if suffix in {4, 5, 7, 8, 9, 12, 19, 21}:
            result += ["T04"]
        if suffix in {10, 11, 14, 15, 16, 17}:
            result += ["T06"]
    if stage in {1, 2, 3, 4}:
        result += ["T07"]
    if stage == 5:
        result += ["T08" if suffix in {15, 16, 17, 18, 19} else "T07"]
    if stage == 7:
        if suffix in {14, 15, 16, 18}:
            result += ["T09"]
        if suffix in {10, 11, 12, 13, 14}:
            result += ["T10"]
        result += ["T11", "T12"]
    if goal_id in {"M1-G17", "M5-G22", "M7-G08"}:
        result += ["T01"]
    return list(dict.fromkeys(result))


def main() -> None:
    goals: list[dict[str, object]] = []
    for line in ASSESSMENT.read_text(encoding="utf-8").splitlines():
        match = GOAL_ROW.fullmatch(line)
        if match is None:
            continue
        goal_id, summary, historical_status, historical_note = match.groups()
        stage = int(goal_id[1])
        evidence = CURRENT_EVIDENCE.get(goal_id)
        current = {
            "baseline_sha": BASE_SHA,
            "implementation_status": "gap_recheck" if historical_status == "I" else "not_reverified",
            "consumer_paths": [],
            "test_nodes": [],
            "environment": None,
            "verification_layer": "not_reverified",
            "receipt_ref": None,
            "artifact_hash": None,
            "supersedes": None,
        }
        if evidence is not None:
            paths, nodes, status = evidence
            current.update({
                "implementation_status": status,
                "consumer_paths": paths,
                "test_nodes": nodes,
                "environment": "Windows 11 / Python 3.12 / Node 24; synthetic offline",
                "verification_layer": "offline_component_or_http",
                "receipt_ref": "docs/verification/M8.md",
                "supersedes": "assessment@fc1e7d56 (current slice only)",
            })
        goals.append({
            "goal_id": goal_id,
            "original_goal": summary,
            "original_roadmap": "docs/roadmap/" + ROADMAP[stage],
            "historical_assessment": {
                "sha": REVIEW_SHA,
                "status": historical_status,
                "note": historical_note,
                "receipt_ref": HISTORICAL_RECEIPT[stage],
            },
            "m8_work_packages": packages(goal_id),
            "current": current,
        })
    if len(goals) != 137 or len({item["goal_id"] for item in goals}) != 137:
        raise SystemExit(f"expected 137 unique goals, got {len(goals)}")
    for goal in goals:
        current = goal["current"]
        for path in [*current["consumer_paths"], *current["test_nodes"]]:
            source = ROOT / path.split("::", 1)[0]
            if not source.is_file():
                raise SystemExit(f"{goal['goal_id']}: missing evidence path {path}")
    OUTPUT.write_text(
        json.dumps({"schema_version": 1, "assessment": ASSESSMENT.relative_to(ROOT).as_posix(),
                    "goals": goals}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
