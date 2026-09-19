import pytest

from motte_contracts.run import EvaluationDescriptor
from motte_sdk.benchmark_plugins import (
    BenchmarkPlugin,
    aggregate_with_plugin,
    prepare_with_plugin,
    register_benchmark_plugin,
    registered_benchmark_plugins,
    score_with_plugin,
    suite_for_run,
    unregister_benchmark_plugin,
)


def test_builtin_plugins_are_registered_without_service_branches():
    assert [(item.suite_id, item.contract_version) for item in registered_benchmark_plugins()] == [
        ("agent-tasks", "1"),
        ("ceval-external", "1"),
        ("direct-llm", "1"),
        ("direct-llm", "2"),
        ("gsm8k", "1"),
    ]


def test_explicit_unknown_suite_never_falls_back_to_gsm8k():
    run = {"manifest": {"benchmark_provenance": {"suite": "unknown", "plugin_version": "1"}}}
    with pytest.raises(ValueError, match="unsupported benchmark plugin"):
        suite_for_run(run)


def test_missing_suite_is_the_only_legacy_gsm8k_fallback():
    run = {"manifest": {"benchmark_provenance": {"selected_count": 1}}}
    assert suite_for_run(run) == ("gsm8k", "1")


def test_tiny_benchmark_extension_needs_only_plugin_registration():
    def prepare(scenario, manifest, resources):
        return {
            **manifest,
            "benchmark_provenance": {
                "id": "tiny",
                "version": "test-v1",
                "scorer_version": "tiny-score-v1",
                "selected_count": 1,
                "run_selection": {"mode": "all", "selected_count": 1},
            },
        }

    plugin = BenchmarkPlugin(
        suite_id="tiny-test",
        contract_version="1",
        prepare_manifest=prepare,
        score=lambda run, results: [{"case_id": "tiny-1", "passed": results[0]["result"] == 1}],
        aggregate=lambda run, scores: {"accuracy": float(scores[0]["passed"])},
        adapter_id="tiny-memory",
        adapter_version="1",
    )
    register_benchmark_plugin(plugin)
    try:
        manifest = prepare_with_plugin("tiny-test", {"name": "tiny"}, {}, object())
        run = {"manifest": manifest, "case_ids": ["tiny-1"]}
        scores = score_with_plugin(run, [{"case_id": "tiny-1", "result": 1}])
        assert scores == [{"case_id": "tiny-1", "passed": True}]
        assert aggregate_with_plugin(run, scores) == {"accuracy": 1.0}
        assert manifest["evaluation"]["adapter_id"] == "tiny-memory"
        assert EvaluationDescriptor.model_validate(manifest["evaluation"]).adapter_version == "1"
    finally:
        unregister_benchmark_plugin("tiny-test", "1")
