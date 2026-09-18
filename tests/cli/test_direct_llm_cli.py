"""CLI `direct-llm`：内置样例 / 本地 JSONL 的导入、清单与运行创建（零网络零费用）。"""
import json

import pytest

from motte_cli.main import main
from motte_sdk.direct_llm import BUILTIN_ENV
from motte_storage.resource_store import SQLiteResourceStore

CUSTOM = "\n".join([
    json.dumps({"input": "CLI one", "expected": "1"}),
    json.dumps({"input": "CLI two", "expected": "2"}),
]) + "\n"


def _db_args(tmp_path, command, **overrides):
    args = ["direct-llm", command, "--db", str(tmp_path / "runs.db")]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", value]
    return args


def _store(tmp_path):
    return SQLiteResourceStore(str(tmp_path / "runs.db"))


def test_builtins_lists_the_shipped_samples(capsys, tmp_path):
    assert main(_db_args(tmp_path, "builtins")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in payload["items"]] == [
        "direct-llm-exact-answer", "direct-llm-classify", "direct-llm-json-extract"]
    assert [item["scorer"] for item in payload["items"]] == ["exact", "contains", "regex"]
    assert [item["cases"] for item in payload["items"]] == [8, 8, 7]


def test_import_builtin_uses_registry_defaults_and_is_idempotent(capsys, tmp_path):
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-classify")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported"] == "direct-llm-classify@1"
    assert receipt["scenario"] == "direct-llm-classify@1"
    assert receipt["scorer"] == "contains" and receipt["cases"] == 8
    assert receipt["source"] == "builtin:direct-llm-classify"
    # 同内容重复导入复用版本
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-classify")) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    dataset = _store(tmp_path).datasets.get("direct-llm-classify", "1")
    assert dataset["provenance"]["license"] == "internal-sample"
    assert dataset["provenance"]["source"] == "builtin:direct-llm-classify"
    assert dataset["eval"]["scorer"] == "contains"


def test_import_local_file_with_explicit_scorer_and_name(capsys, tmp_path):
    source = tmp_path / "custom.jsonl"
    source.write_text(CUSTOM, encoding="utf-8")
    args = _db_args(tmp_path, "import", file=str(source), name="cli-set", scorer="contains",
                    license="MIT", source="manual")
    assert main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["imported"] == "cli-set@1" and receipt["cases"] == 2
    assert receipt["scorer"] == "contains" and receipt["source"] == "manual"
    assert _store(tmp_path).datasets.get("cli-set", "1")["provenance"]["license"] == "MIT"


def test_import_defaults_name_for_local_files(capsys, tmp_path):
    source = tmp_path / "custom.jsonl"
    source.write_text(CUSTOM, encoding="utf-8")
    assert main(_db_args(tmp_path, "import", file=str(source))) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == "direct-llm-custom@1"


def test_import_reports_structured_errors(capsys, tmp_path):
    assert main(_db_args(tmp_path, "import", builtin="nope")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BUILTIN_UNAVAILABLE"
    assert main(_db_args(tmp_path, "import", file=str(tmp_path / "missing.jsonl"))) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "SOURCE_UNAVAILABLE"
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n", encoding="utf-8")
    assert main(_db_args(tmp_path, "import", file=str(broken))) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "CONTRACT_INVALID" and "invalid JSON" in error["message"]
    # 显式空值不会被当成默认值
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-classify", name="  ")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONTRACT_INVALID"


def test_import_missing_builtin_directory_names_the_env_var(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(BUILTIN_ENV, str(tmp_path / "nowhere"))
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-classify")) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "BUILTIN_UNAVAILABLE" and BUILTIN_ENV in error["message"]


def test_list_shows_imported_datasets_and_ignores_other_suites(capsys, tmp_path):
    assert main(_db_args(tmp_path, "list")) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "total": 0}
    store = _store(tmp_path)
    store.scenarios.put({"name": "gsm8k-test-full", "version": "1", "mode": "direct-llm",
                         "benchmark": {"id": "gsm8k-20", "version": 1, "selected_count": 20},
                         "dataset": "gsm8k-test@1"})
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-json-extract")) == 0
    capsys.readouterr()
    assert main(_db_args(tmp_path, "list")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["items"][0] == {"scenario": "direct-llm-json-extract@1",
                                   "dataset": "direct-llm-json-extract@1",
                                   "suite": "direct-llm", "scorer": "regex", "cases": 7,
                                   "source": "builtin:direct-llm-json-extract"}


def test_list_skips_scenarios_whose_dataset_is_gone(capsys, tmp_path):
    store = _store(tmp_path)
    store.scenarios.put({"name": "direct-llm-orphan", "version": "1", "mode": "direct-llm",
                         "eval": {"suite": "direct-llm", "id": "direct-llm-prompts", "version": 1,
                                  "selected_count": 3, "scorer": "exact"},
                         "dataset": "direct-llm-orphan@1"})
    assert main(_db_args(tmp_path, "list")) == 0
    assert json.loads(capsys.readouterr().out) == {"items": [], "total": 0}


def _seed_run_fixtures(tmp_path, capsys):
    """导入样例数据集，并注册一个 provider + 模型档案供 --model 引用；顺带清掉导入回执。"""
    assert main(_db_args(tmp_path, "import", builtin="direct-llm-exact-answer")) == 0
    capsys.readouterr()
    store = _store(tmp_path)
    store.providers.put({"name": "local", "kind": "openai_compatible",
                         "base_url": "https://local.test/v1", "model": "unused"})
    store.models.put({"id": "probe", "provider": "local", "model": "probe-1",
                      "capabilities": {}, "max_output_tokens": 4096})


def test_run_creates_a_queued_run_with_selection_and_parameters(capsys, tmp_path):
    _seed_run_fixtures(tmp_path, capsys)
    args = _db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1", model="probe",
                    case_ids="direct-llm-exact-answer-0003,direct-llm-exact-answer-0001",
                    temperature="0.2", max_output_tokens="128")
    assert main(args) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["status"] == "queued"
    assert run["case_ids"] == ["direct-llm-exact-answer-0001", "direct-llm-exact-answer-0003"]
    manifest = run["manifest"]
    assert manifest["provider"]["parameters"] == {"temperature": 0.2, "max_output_tokens": 128}
    assert manifest["provider"]["model"] == "probe-1"
    assert manifest["cases"]["direct-llm-exact-answer-0001"]["prompt"]
    provenance = manifest["benchmark_provenance"]
    assert provenance["suite"] == "direct-llm" and provenance["max_output_tokens"] == 128
    assert provenance["run_selection"] == {"mode": "ids", "count": 2, "seed": None}


def test_run_random_subset_requires_seed_pairing(capsys, tmp_path):
    _seed_run_fixtures(tmp_path, capsys)
    args = _db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1", model="probe",
                    random="3", seed="deadbeef")
    assert main(args) == 0
    run = json.loads(capsys.readouterr().out)
    assert len(run["case_ids"]) == 3
    assert run["manifest"]["benchmark_provenance"]["run_selection"] == {
        "mode": "random", "count": 3, "seed": "deadbeef"}
    # 只给 --seed 不给 --random 是用法错误
    assert main(_db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1", model="probe",
                         seed="deadbeef")) == 2
    assert "--seed" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_run_reports_structured_errors(capsys, tmp_path):
    _seed_run_fixtures(tmp_path, capsys)
    assert main(_db_args(tmp_path, "run", scenario="direct-llm-nope@1", model="probe")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "SCENARIO_NOT_FOUND"
    # --model / --provider 是 argparse 级必填项，缺失由 argparse 直接退出（usage 文本会污染 stderr）
    with pytest.raises(SystemExit):
        main(_db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1"))
    capsys.readouterr()
    # 未知 case id 在创建期就被拒绝（不产生任何付费调用）
    assert main(_db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1", model="probe",
                         case_ids="nope")) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "RUN_CONFIG_INVALID" and "not in dataset" in error["message"]
    # 超出模型上限的输出预算同样在创建期拒绝
    assert main(_db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1", model="probe",
                         max_output_tokens="8192")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "MODEL_CONFIG_INVALID"
    # 模型档案不存在
    assert main(_db_args(tmp_path, "run", scenario="direct-llm-exact-answer@1",
                         model="missing")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "MODEL_NOT_FOUND"
