"""`python -m motte_cli` 入口：doctor / run / replay / live-smoke。

M7（协议 docs/protocols/sdk-and-migration.md §3）起支持 local/server 模式路由：
server 模式只经 MotteClient HTTP（见 motte_cli.remote），local 模式与既有行为
完全一致。
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from motte_cli import ops, remote, runops


def _runtime_catalog():
    from motte_harness.compatibility import backend_ids, probed_readiness
    from motte_sdk.runtime_backends import CANONICAL_RUNTIME_VERSIONS

    items = []
    for backend_id in backend_ids():
        resource = CANONICAL_RUNTIME_VERSIONS.get(backend_id) or {}
        definition = resource.get("definition") or {}
        items.append({
            "name": backend_id,
            "version": resource.get('version'),
            "kind": definition.get("kind"),
            "transport": definition.get("transport"),
            "upstream_version": definition.get("upstream_version"),
            "model_control": definition.get("model_control"),
            "interactive": bool(definition.get("interactive")),
            # 真实零成本探测（bridge probe / --version，M4 review R19）。
            "readiness": probed_readiness(backend_id),
        })
    return items


def _cmd_runtime(args):
    command = getattr(args, "runtime_command", None) or "list"
    as_json = getattr(args, "json", False)
    if command == "list":
        items = _runtime_catalog()
        if as_json:
            print(json.dumps({"items": items}, ensure_ascii=False))
        else:
            for item in items:
                readiness = item["readiness"]
                flags = "/".join(
                    "Y" if readiness[level] else "N"
                    for level in ("installed", "protocol_ready", "execution_ready")
                )
                print(
                    f"  {item['name']}@{item['version']} [{item['kind']}/{item['transport']}] "
                    f"installed/protocol/execution={flags} upstream={item['upstream_version']}"
                )
                for level, reason in sorted(readiness["reasons"].items()):
                    print(f"    {level}: {reason}")
        return 0
    if command == "readiness":
        from motte_harness.compatibility import CompatibilityError, probed_readiness

        try:
            state = probed_readiness(args.name)
        except CompatibilityError as error:
            print(f"runtime: {error}")
            return 2
        print(json.dumps(state, ensure_ascii=False) if as_json else state)
        return 0
    if command == "publish":
        from motte_storage.factory import create_resource_store

        from motte_sdk.runtime_backends import publish_canonical_runtime_versions

        published = publish_canonical_runtime_versions(create_resource_store())
        names = sorted(published)
        if as_json:
            print(json.dumps({"published": names}, ensure_ascii=False))
        else:
            print("published runtime versions: " + ", ".join(names))
        return 0
    print(f"runtime: unknown subcommand {command!r}")
    return 2


def _load_json(raw: str):
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(raw)


def _harness_installations() -> dict:
    """Claude / Codex / Pi bridge 的本地安装检测（缺失不是错误，如实报告）。"""
    from motte_harness.claude import ClaudeHarness
    from motte_harness.codex import CodexHarness

    async def collect():
        return {
            "claude": await ClaudeHarness().inspect(),
            "codex": await CodexHarness().inspect(),
        }

    reports = asyncio.run(collect())
    try:
        from motte_agent.pi import PiAgentRuntime

        runtime = PiAgentRuntime()
        transport_available = runtime.available()
        probe = runtime.probe() if transport_available else None
        reports["pi-bridge"] = {
            "name": "pi-bridge",
            "installed": bool(probe and probe["execution_ready"]),
            "execution_ready": bool(probe and probe["execution_ready"]),
            "transport_available": transport_available,
            "detail": f"node={runtime._node is not None}, bridge={runtime.bridge_path}",
        }
    except Exception as error:  # agent 包异常时 doctor 不失败
        reports["pi-bridge"] = {"name": "pi-bridge", "installed": False, "error": str(error)}
    return reports


def _load_external_files(items: list[str]) -> tuple[dict[str, bytes] | None, str | None]:
    files: dict[str, bytes] = {}
    for item in items:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            return (None, "--files 需要 logical_name=path 形式：" + item)
        files[name] = open(path, "rb").read()  # noqa: SIM115 - CLI 短命令
    return (files, None)


def _handle_external_benchmark(args: argparse.Namespace, benchmark_id: str) -> int:
    """外部基准（ceval/cmmlu）：与 API 共用 catalog/校验/构造服务。

    准备结果持久化到同一存储（review R13）；run 入队前跑同一校验服务
    （review R14）；adapter 从同一受控配置加载（review R01）。
    """
    import json as _json

    from motte_benchmark.registry import registered_adapter_ids
    from motte_benchmark.runner_config import ensure_builtin_adapters
    from motte_sdk.benchmark_catalog import (
        BENCHMARK_DESCRIPTORS,
        BenchmarkCatalog,
        prepare_external_dataset,
    )

    ensure_builtin_adapters()
    descriptor = BENCHMARK_DESCRIPTORS[benchmark_id]
    adapter_connected = descriptor.adapter_id in registered_adapter_ids()
    from motte_storage.factory import create_run_store

    db_path = getattr(args, "db", None) or os.environ.get("MOTTE_DB_PATH") or "var/runs.db"
    store = create_run_store(db_path)
    catalog = BenchmarkCatalog(getattr(store, "benchmark_datasets", None))
    catalog.register(benchmark_id, benchmark_version=descriptor.benchmark_version)

    command = (
        getattr(args, "ceval_command", None)
        or getattr(args, "cmmlu_command", None)
        or getattr(args, "benchmark_command", None)
    )

    if command == "prepare":
        files, usage_error = _load_external_files(args.files)
        if usage_error:
            print(usage_error, file=sys.stderr)
            return 2
        prepared = prepare_external_dataset(
            files=files, dataset_revision=args.revision,
            provenance_target=args.provenance,
            benchmark_id=benchmark_id,
        )
        catalog.update_dataset(benchmark_id, prepared)
        catalog.mark_profile_validated(benchmark_id, prepared.state == "ready")
        catalog.mark_runner_connected(benchmark_id, adapter_connected)
        print(_json.dumps({
            "state": prepared.state,
            "provenance": prepared.provenance,
            "revision": prepared.dataset_revision,
            "rows": prepared.row_count,
            "gold_rows": prepared.gold_count,
            "unscored": prepared.unscored,
            "reasons": list(prepared.reasons),
            "注意": "合成/本地数据为 user-supplied；official 需受信核验与许可证据（API 侧）",
        }, ensure_ascii=False))
        return 0 if prepared.state == "ready" else 1

    if command == "preflight":
        # 预检与创建共用同一校验服务与同一输入（review R14），不再固定
        # state=unprepared。
        from motte_sdk.benchmark_catalog import validate_external_run_request

        dataset = catalog.dataset(benchmark_id)
        if dataset is None:
            from motte_sdk.benchmark_catalog import PreparedBenchmarkDataset

            dataset = PreparedBenchmarkDataset(
                benchmark_id=benchmark_id,
                benchmark_version=descriptor.benchmark_version,
                dataset_revision="unprepared",
                state="unprepared",
            )
        resources = _resources(args) if hasattr(args, "db") else None
        model_record = None
        model_id = getattr(args, "model", None)
        if model_id and resources is not None:
            try:
                model_record = resources.models.get(model_id)
            except Exception:  # noqa: BLE001 - 未知模型给原因而不是崩溃
                model_record = None
        reasons = validate_external_run_request(
            dataset,
            benchmark_id=benchmark_id,
            model_record=model_record,
            scope=getattr(args, "scope", "custom-subset") or "custom-subset",
            split=getattr(args, "split", None),
            few_shot=int(getattr(args, "few_shot", 0) or 0),
            few_shot_split=getattr(args, "few_shot_split", None),
            runner_connected=adapter_connected,
        )
        print(_json.dumps({
            "ok": not reasons,
            "reasons": reasons,
            "runner_connected": adapter_connected,
            "dataset_state": dataset.state,
        }, ensure_ascii=False))
        return 0 if not reasons else 1

    if command == "run":
        from motte_sdk.benchmark_catalog import prepare_external_run_inputs

        files, usage_error = _load_external_files(args.files)
        if usage_error:
            print(usage_error, file=sys.stderr)
            return 2
        prepared = prepare_external_dataset(
            files=files, dataset_revision=args.revision,
            benchmark_id=benchmark_id,
        )
        if prepared.state != "ready":
            print(_json.dumps({"error": "DATASET_PREPARE_FAILED",
                               "reasons": list(prepared.reasons)}, ensure_ascii=False),
                  file=sys.stderr)
            return 1
        if not adapter_connected:
            print(
                f"RUNNER_NOT_CONNECTED: {descriptor.adapter_id} adapter 未注册"
                "（配置 MOTTE_RUNNER_CONFIG 或部署固定环境 wrapper）",
                file=sys.stderr,
            )
            return 1
        resources = _resources(args)
        model_record = resources.models.get(args.model)
        try:
            inputs = prepare_external_run_inputs(
                prepared,
                benchmark_id=benchmark_id,
                model_id=args.model,
                model_record=model_record,
                few_shot=int(args.few_shot or 0),
                seed=int(args.seed or 0),
                scope=args.scope,
                split=getattr(args, "split", None),
                few_shot_split=getattr(args, "few_shot_split", None),
                resources=resources,
            )
        except ValueError as error:
            print(f"RUN_REQUEST_INVALID: {error}", file=sys.stderr)
            return 1
        from motte_sdk.execution_backends import resolve_execution
        from motte_sdk.service import RunService

        service = RunService(store)
        # 入队前再过一次后端契约（版本钉齐/runner_config 存在），失败即退出。
        resolve_execution(inputs["scenario_version"], inputs["manifest"])
        run = service.create_run(
            inputs["scenario_version"], inputs["manifest"], inputs["case_ids"],
            requested_manifest={"model": args.model, "scope": args.scope},
        )
        print(_json.dumps({"id": run["id"], "status": run["status"],
                           "scope": args.scope,
                           "提示": "Worker 执行：uv run python -m apps.worker.motte_worker --once"},
                          ensure_ascii=False))
        return 0
    return 2


def _preflight_report() -> dict:
    """CLI 侧读取 Runner 探测报告；缺失/畸形按"未上报"处理，预检随之失败关闭。"""
    path = os.environ.get("MOTTE_HARBOR_PREFLIGHT_REPORT")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tb_model_mapping(args: argparse.Namespace) -> dict | None:
    """``--model`` → 冻结完整模型执行配置，再统一映射或拒绝。

    只接受**已发布**的 ModelProfile id：CLI 指定一个不存在的模型时必须在入队
    前失败，而不是"成功 queued、跑的时候才发现没有模型"。oracle 可以不带模型。
    """
    model_id = getattr(args, "model", None)
    if not model_id:
        return None
    from motte_storage.factory import create_resource_store

    resources = create_resource_store(getattr(args, "db", None))
    record = resources.models.get(model_id)
    if record is None:
        raise ValueError(f"unknown model profile: {model_id}")
    if str(record.get("lifecycle") or record.get("status") or "") != "published":
        raise ValueError(f"model profile is not published: {model_id}")
    from motte_sdk.terminalbench import published_model_config

    return published_model_config(record)


def _tb_profile_from_args(args: argparse.Namespace) -> dict:
    """CLI → 冻结 Profile：字段名与 API DTO 一致（``timeouts`` 是唯一来源）。"""
    from motte_sdk import terminalbench as tb

    timeouts = {
        "agent_sec": getattr(args, "agent_timeout_sec", None),
        "verifier_sec": getattr(args, "verifier_timeout_sec", None),
        "agent_setup_sec": getattr(args, "agent_setup_timeout_sec", None),
        "job_sec": getattr(args, "job_timeout_sec", None),
    }
    credentials = {}
    for item in getattr(args, "credential_ref", None) or []:
        name, _, ref = str(item).partition("=")
        if not name or not ref:
            raise ValueError("--credential-ref must be NAME=env:VAR")
        credentials[name] = {"ref": ref}
    return tb.terminal_bench_profile(
        agent_id=getattr(args, "agent_id", "oracle"),
        agent_version=getattr(args, "agent_version", "1.0.0"),
        n_trials=max(getattr(args, "n_trials", 1), 1),
        aggregation=getattr(args, "aggregation", "first-trial"),
        model=_tb_model_mapping(args),
        timeouts={key: value for key, value in timeouts.items() if value is not None},
        credentials=credentials,
    )


def _handle_terminal_bench(args: argparse.Namespace) -> int:
    """Terminal-Bench（Harbor）CLI：prepare / tasks / preflight / run / status / trials。

    与 API 共用 ``motte_sdk.terminalbench`` 门面，因此任务身份、预检原因码与
    Trial 视图在两端一致；执行同样只入队，不在本进程跑任何任务。
    """
    from uuid import uuid4

    import json as _json

    from motte_sdk import terminalbench as tb
    from motte_sdk.execution_backends import resolve_execution
    from motte_sdk.service import build_run_service

    service = build_run_service(args.db)
    store = service.store
    command = args.terminal_bench_command

    if command == "prepare":
        try:
            record = tb.prepare_terminal_bench_dataset(
                store,
                task_root=args.task_root,
                source_id=args.source_id,
                dataset_revision=args.revision,
                license_id=args.license_id,
                license_evidence=args.license_evidence,
                source_kind="pinned-source" if args.pinned_source else "local",
            )
        except Exception as error:  # noqa: BLE001 - 合同错误以结构化错误返回
            return _error(getattr(error, "code", "TASK_PREPARE_FAILED"), str(error))
        manifest = record.get("manifest") or {}
        print(json.dumps({
            "state": record.get("state"),
            "dataset_revision": record.get("dataset_revision"),
            "source_id": manifest.get("source_id"),
            "manifest_hash": record.get("manifest_hash"),
            "tasks": len(tb.task_view(record)),
            "invalid_tasks": manifest.get("invalid_tasks") or [],
            "license_id": record.get("license_id"),
        }, ensure_ascii=False))
        return 0

    if command == "tasks":
        record = tb.prepared_dataset(store, dataset_revision=args.dataset_revision)
        if record is None:
            return _error("DATASET_UNPREPARED", "no prepared Terminal-Bench task set")
        items = tb.task_view(record)
        if args.json:
            print(_json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
            return 0
        for item in items:
            print(
                f"{item['task_key']}  {item['normalized_relative_path']}  "
                f"tests={item['has_tests']} solution={item['has_solution']}",
            )
        return 0

    if command == "preflight":
        record = tb.prepared_dataset(store, dataset_revision=args.dataset_revision)
        if record is None:
            return _error("DATASET_UNPREPARED", "no prepared Terminal-Bench task set")
        try:
            profile = _tb_profile_from_args(args)
            report = tb.preflight_terminal_bench(
                record=record, profile=profile,
                task_keys=[item for item in (args.task_keys or "").split(",") if item] or None,
                docker=_preflight_report(),
            )
        except Exception as error:  # noqa: BLE001 - 能力/任务集问题不是崩溃
            return _error(getattr(error, "code", "PREFLIGHT_INVALID"), str(error))
        print(_json.dumps({
            "ok": report["allowed"],
            "reasons": report["reason_codes"],
            "messages": report["messages"],
            "checks": report["checks"],
            "profile_fingerprint": report["profile_fingerprint"],
            "platform_custom_profile": report["platform_custom_profile"],
        }, ensure_ascii=False))
        return 0 if report["allowed"] else 1

    if command == "run":
        from motte_benchmark.registry import registered_adapter_ids

        record = tb.prepared_dataset(store, dataset_revision=args.dataset_revision)
        if record is None:
            return _error("DATASET_UNPREPARED", "no prepared Terminal-Bench task set")
        if tb.ADAPTER_ID not in registered_adapter_ids():
            return _error(
                "RUNNER_NOT_CONNECTED",
                "the Harbor adapter is not registered; deploy the pinned runner "
                "environment (scripts/runner/install-harbor) first",
            )
        selected = [item for item in (args.task_keys or "").split(",") if item]
        try:
            profile = _tb_profile_from_args(args)
            report = tb.preflight_terminal_bench(
                record=record, profile=profile, task_keys=selected or None,
                docker=_preflight_report(),
            )
        except Exception as error:  # noqa: BLE001 - 创建前拒绝，不产生 queued Run
            return _error(getattr(error, "code", "RUN_REQUEST_INVALID"), str(error))
        if not report["allowed"]:
            print(_json.dumps({"error": {
                "code": "PREFLIGHT_FAILED",
                "message": "Terminal-Bench preflight did not pass",
                "details": {"reasons": report["reason_codes"],
                            "messages": report["messages"]},
            }}, ensure_ascii=False), file=sys.stderr)
            return 1
        planned_run_id = f"run-{uuid4().hex}"
        try:
            inputs = tb.build_run_inputs(
                record=record, run_id=planned_run_id, job_id=f"job-{uuid4().hex}",
                profile=profile, task_keys=selected or None,
            )
            resolved = resolve_execution(tb.SCENARIO_VERSION, inputs["manifest"])
            run = service.create_run(
                tb.SCENARIO_VERSION, resolved, inputs["case_ids"],
                requested_manifest={"benchmark": tb.BENCHMARK_ID},
                run_id=planned_run_id,
            )
        except Exception as error:  # noqa: BLE001 - 配置/任务集问题一律结构化失败
            return _error(getattr(error, "code", "RUN_CREATE_FAILED"), str(error))
        print(_json.dumps({
            "id": run["id"], "status": run["status"], "scenario": tb.SCENARIO_VERSION,
            "execution": resolved.get("execution") or {},
            "trials": len(inputs["trials"]),
            "提示": "由 Worker 执行：uv run python -m apps.worker.motte_worker --once",
        }, ensure_ascii=False))
        return 0

    if command == "status":
        # 冻结 manifest 必须一起传给视图：否则"计划里有、结果里没有"的 Task
        # 会从分母里消失，CLI 的覆盖率就会比 API/报告虚高（review R17）。
        try:
            run = service.get_run(args.run_id)
        except KeyError:
            return _error("RUN_NOT_FOUND", args.run_id)
        rows = tb.task_rows(store, args.run_id, manifest=run.get("manifest") or {})
        if args.json:
            print(_json.dumps({
                "run_id": args.run_id, "status": run.get("status"),
                "items": rows, "total": len(rows),
            }, ensure_ascii=False))
            return 0
        for row in rows:
            print(
                f"{row['task_key']}  planned={row['planned_trials']} "
                f"valid={row['valid_trials']} pass={row['task_pass']}",
            )
        aggregate = rows[0]["aggregate"] if rows else {}
        print(_json.dumps({
            "status": run.get("status"),
            "aggregation": aggregate.get("aggregation"),
            "selected_trials": aggregate.get("selected_trials"),
            "valid_trial_pass_rate": aggregate.get("valid_trial_pass_rate"),
            "valid_trial_coverage": aggregate.get("valid_trial_coverage"),
            "gate": rows[0]["gate"] if rows else None,
        }, ensure_ascii=False))
        return 0

    if command == "trials":
        items = tb.trial_rows(store, args.run_id, args.task_key)
        print(_json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
        return 0

    if command == "trial":
        detail = tb.trial_detail(store, args.run_id, args.trial_id)
        if detail is None:
            return _error("TRIAL_NOT_FOUND", f"{args.trial_id} is not in run {args.run_id}")
        if args.artifact:
            # 内容读取链路（review R19）：按引用读冻结证据，有界 + 脱敏。
            content = tb.trial_artifact_content(
                store, args.run_id, args.trial_id, args.artifact,
            )
            if content is None:
                return _error(
                    "ARTIFACT_NOT_FOUND",
                    f"{args.artifact} is not referenced by trial {args.trial_id}",
                )
            if args.text and content.get("text") is not None:
                print(content["text"])
                return 0
            print(_json.dumps(content, ensure_ascii=False))
            return 0
        print(_json.dumps(detail, ensure_ascii=False))
        return 0

    return 2


def _handle_ceval(args: argparse.Namespace) -> int:
    """C-Eval 外部基准：真实实现见 _handle_external_benchmark。"""
    return _handle_external_benchmark(args, "ceval")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motte")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--jsonl", action="store_true")

    inspect_import = sub.add_parser(
        "inspect-import", help="Inspect eval-log 只读导入（不执行日志内容）",
    )
    inspect_import.add_argument("file", help="Inspect .json EvalLog 文件路径（--full 完整导出；二进制 .eval 不支持）")
    inspect_import.add_argument("--name", help="导入名称（审计用）")
    inspect_import.add_argument("--json", action="store_true")
    inspect_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    runtime = sub.add_parser("runtime", help="M4 外部 runtime：目录 / 分层就绪 / 发布规范版本")
    runtime_sub = runtime.add_subparsers(dest="runtime_command")
    runtime_sub.add_parser("list", help="列出 runtime 目录与分层就绪（零模型调用）")
    runtime_readiness = runtime_sub.add_parser("readiness", help="单个 runtime 的分层就绪详情")
    runtime_readiness.add_argument("name", help="backend 名（pi-agent / claude-cli / codex-cli / codex-app-server）")
    runtime_sub.add_parser("publish", help="发布规范 runtime 版本到资源仓库（幂等）")
    runtime.add_argument("--json", action="store_true")

    scenario = sub.add_parser(
        "scenario", help="逐步骤 Scenario：Workflow 文档预检 / 目标能力 / 创建运行",
    )
    scenario_sub = scenario.add_subparsers(dest="scenario_command", required=True)
    scenario_validate = scenario_sub.add_parser(
        "validate",
        help="纯预检 WorkflowVersion（不发布、不执行、零模型调用；JSON 文件或 name@version）",
    )
    scenario_validate.add_argument(
        "document", help="WorkflowVersion JSON 文件，或已发布引用 name@version",
    )
    scenario_validate.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    scenario_targets = scenario_sub.add_parser(
        "targets", help="列出可用 scenario target 及其真实能力（零模型调用）",
    )
    scenario_targets.add_argument("--json", action="store_true")
    scenario_run = scenario_sub.add_parser(
        "run", help="创建逐步骤 Scenario queued Run（与 run/API 同一条创建路径）",
    )
    scenario_run.add_argument(
        "--spec", help="run 定义 JSON 或 @文件：{scenario_version, manifest, case_ids}",
    )
    scenario_run.add_argument("--scenario", help="已发布 ScenarioVersion 引用，如 order-cancel@1")
    scenario_run.add_argument(
        "--workflow", help="已发布 Workflow 引用，如 order-cancel-confirmed@1",
    )
    scenario_run.add_argument(
        "--target-runtime", dest="target_runtime",
        help="目标 runtime 引用（写进 manifest.runtime，如 codex-cli@1）",
    )
    scenario_run.add_argument(
        "--target-agent", dest="target_agent",
        help="目标内置 Agent 引用（写进 manifest.agent，如 builtin-agent@1）",
    )
    scenario_run.add_argument(
        "--case-ids", dest="case_ids", help="只跑指定 case：逗号分隔 case id",
    )
    scenario_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    judge = sub.add_parser(
        "judge", help="Judge 评分作业：预检 / 提交 / 查询 / 历史 / 取消（与 API 同一编译器）",
    )
    judge_sub = judge.add_subparsers(dest="judge_command", required=True)
    judge_preflight = judge_sub.add_parser(
        "preflight", help="零费用预检：服务端解析资源与证据，不落作业、不调用模型",
    )
    judge_preflight.add_argument(
        "--spec", required=True,
        help="judge 请求 JSON 或 @文件：{run_id, mode, spec{model,budget,...}, case_ids, authorisation}",
    )
    judge_preflight.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    judge_submit = judge_sub.add_parser(
        "submit", help="持久提交 Judge 作业（由 Worker 在执行锁内领取执行）",
    )
    judge_submit.add_argument("--spec", required=True, help="同 preflight 的请求 JSON 或 @文件")
    judge_submit.add_argument(
        "--request-key", dest="request_key", help="幂等键；缺省时取 spec.request_key",
    )
    judge_submit.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    judge_status = judge_sub.add_parser("status", help="读取一个 Judge 作业（零模型调用）")
    judge_status.add_argument("job_id")
    judge_status.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    judge_history = judge_sub.add_parser(
        "history", help="列出某个 Run 的 Judge 作业历史（零模型调用）",
    )
    judge_history.add_argument("--run", required=True, help="subject Run id")
    judge_history.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    judge_cancel = judge_sub.add_parser(
        "cancel", help="幂等取消 Judge 作业（已发出的请求只中断，不宣称未计费）",
    )
    judge_cancel.add_argument("job_id")
    judge_cancel.add_argument("--reason", default="operator request")
    judge_cancel.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    workflow = sub.add_parser(        "workflow", help="Workflow 版本资源：发布 / 列表 / 旧 DSL 只读转换报告",
    )
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    wf_publish = workflow_sub.add_parser(
        "publish", help="发布不可变 WorkflowVersion（与 API 同一校验；同内容幂等）",
    )
    wf_publish.add_argument("file", help="WorkflowVersion JSON 文件")
    wf_publish.add_argument("--json", action="store_true")
    wf_publish.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    wf_list = workflow_sub.add_parser("list", help="列出已发布 Workflow 版本（只读）")
    wf_list.add_argument("--json", action="store_true")
    wf_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    wf_convert = workflow_sub.add_parser(
        "convert-legacy", help="旧 DSL 只读转换报告（诊断 + 映射；不发布、不执行）",
    )
    wf_convert.add_argument("file", help="旧 DSL 场景 JSON 文件")
    wf_convert.add_argument(
        "--workflow-id", dest="workflow_id", help="目标 workflow_id（缺省取旧文档 id）",
    )
    wf_convert.add_argument("--version", help="目标版本（缺省取旧文档 version）")
    wf_convert.add_argument(
        "--published-at", dest="published_at",
        help="发布时刻（只用于生成候选引用；缺省取当前 UTC）",
    )
    skill = sub.add_parser(
        "skill", help="Skill 版本资源：发布 / 列表（与 API 同一校验；不执行入口）",
    )
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    sk_publish = skill_sub.add_parser(
        "publish", help="发布不可变 SkillVersion（同内容幂等、异内容冲突）",
    )
    sk_publish.add_argument("file", help="SkillVersion JSON 文件")
    sk_publish.add_argument("--json", action="store_true")
    sk_publish.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    sk_list = skill_sub.add_parser("list", help="列出已发布 Skill 版本（只读）")
    sk_list.add_argument("--json", action="store_true")
    sk_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    fixture = sub.add_parser(
        "fixture", help="Fixture 版本资源：发布 / 列表（与 API 同一校验；不执行 fixture）",
    )
    fixture_sub = fixture.add_subparsers(dest="fixture_command", required=True)
    fx_publish = fixture_sub.add_parser(
        "publish", help="发布不可变 FixtureSpec 版本（同内容幂等、异内容冲突）",
    )
    fx_publish.add_argument("file", help="FixtureSpec JSON 文件")
    fx_publish.add_argument("--json", action="store_true")
    fx_publish.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    fx_list = fixture_sub.add_parser("list", help="列出已发布 Fixture 版本（只读）")
    fx_list.add_argument("--json", action="store_true")
    fx_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    run = sub.add_parser("run", help="创建 queued Run（由 Worker 异步执行）")
    run.add_argument("--spec", required=True, help="run 定义 JSON 或 @文件：{scenario_version, manifest, case_ids}")
    run.add_argument(
        "--request-key", dest="request_key",
        help="幂等创建键（协议 §1.3：同键同 body 重放同一 Run；同键异 body 拒绝）",
    )
    run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    remote.add_mode_arguments(run)

    replay = sub.add_parser("replay", help="创建并同步执行 replay Run")
    replay.add_argument("--scenario", default="replay@1")
    replay.add_argument("--fixture", required=True, help="replay fixture：JSON 字符串或 @文件路径")
    replay.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    replay.add_argument("--json", action="store_true")

    try:
        from motte_provider.config import smoke_kinds
        smoke_kind_hint = ", ".join(smoke_kinds())
    except ModuleNotFoundError:
        # 干净安装（protocol §8：cli 闭包 = sdk + storage）下 provider 不在场，
        # --help 不应因此失败；真正执行 live-smoke 时命令处理器会再按需导入。
        smoke_kind_hint = "需安装 motte-provider"

    smoke = sub.add_parser("live-smoke", help="显式发起一次真实 Provider 调用（会产生费用）")
    smoke.add_argument("--provider", required=True, help=f"provider kind：{smoke_kind_hint}（连字符拼写兼容）")
    smoke.add_argument("--model", required=True)
    smoke.add_argument("--base-url", required=True)
    smoke.add_argument("--credentials", default=None, help="凭据文件 profile 名（~/.motte/credentials.toml），优先于环境变量")
    smoke.add_argument("--api-key-env", default="OPENAI_API_KEY", help="回退用的密钥环境变量名")
    smoke.add_argument("--prompt", default="Reply with the single word: ok")
    smoke.add_argument("--price-table", help="价格表 JSON 或 @文件：{version, input_per_million, output_per_million}")
    smoke.add_argument("--report", help="把脱敏报告写入 JSON 文件")
    smoke.add_argument("--record", help="把结果小节追加到 markdown 记录（如 docs/operations/live-smoke-log.md）")
    smoke.add_argument("--timeout", type=float, default=30.0)
    smoke.add_argument("--max-retries", type=int, default=2)

    credentials = sub.add_parser("credentials", help="管理本地凭据文件（~/.motte/credentials.toml）")
    credentials_sub = credentials.add_subparsers(dest="credentials_command", required=True)
    credentials_set = credentials_sub.add_parser("set", help="为 profile 设置 api key（交互输入，无回显）")
    credentials_set.add_argument("profile", help="凭据 profile 名，通常与 provider 连接同名")
    credentials_sub.add_parser("list", help="列出 profile 与掩码密钥")
    credentials_remove = credentials_sub.add_parser("remove", help="删除一个 profile")
    credentials_remove.add_argument("profile")

    # backup / restore 的解析器与处理器都在 motte_cli.ops（M7 起支持 --consistent /
    # --staging；flag-less 行为保持 legacy 兼容）。

    benchmark = sub.add_parser("benchmark", help="GSM8K 基准（下载 / 导入数据集，创建运行）")
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)
    from motte_contracts.gsm8k import SCOPES

    scope_help = "full=源文件全部题目（默认）；smoke=源文件前 20 题"
    bench_download = benchmark_sub.add_parser(
        "download", help="下载官方 test split 并导入：默认解析官方最新 commit + 全量题目")
    bench_download.add_argument("--revision", help="官方仓库 40 位 commit hash（省略=解析官方最新）")
    bench_download.add_argument("--name", default="gsm8k-test", help="数据集名（默认 gsm8k-test）")
    bench_download.add_argument("--version", help="数据集版本（省略=自动：同内容复用，否则下一个空号）")
    bench_download.add_argument("--license", dest="license_id", default="MIT")
    bench_download.add_argument("--scope", choices=SCOPES, default="full", help=scope_help)
    bench_download.add_argument("--split", default="test", choices=("test",), help="官方 split（契约当前仅 test）")
    bench_download.add_argument("--source-dir", help="源文件落盘目录，默认 MOTTE_DATASET_DIR 或 var/datasets/gsm8k")
    bench_download.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    bench_import = benchmark_sub.add_parser("import", help="导入本地 official-format GSM8K JSONL（question/answer 两列）")
    bench_import.add_argument("--file", required=True, help="official-format test JSONL 路径")
    bench_import.add_argument("--name", default="gsm8k-test", help="数据集名（默认 gsm8k-test）")
    bench_import.add_argument("--version", help="数据集版本（省略=自动：同内容复用，否则下一个空号）")
    bench_import.add_argument("--revision", required=True, help="官方数据 pinned commit hash")
    bench_import.add_argument("--license", dest="license_id", required=True)
    bench_import.add_argument("--scope", choices=SCOPES, default="full", help=scope_help)
    bench_import.add_argument("--synthetic", action="store_true", help="标记为合成冒烟数据（跳过 commit hash 校验）")
    bench_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    bench_run = benchmark_sub.add_parser("run", help="创建 benchmark queued Run（由 Worker 执行）")
    bench_run.add_argument("--scenario", required=True, help="benchmark scenario 引用，如 gsm8k-test-smoke@1 / gsm8k-test-full@1")
    selection = bench_run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--provider", help="provider connection name or provider object JSON/@file (not a manifest)")
    selection.add_argument("--model", help="model profile resource id")
    subset = bench_run.add_mutually_exclusive_group()
    subset.add_argument("--case-ids", help="只跑指定题目：逗号分隔的 case id，如 gsm8k-test-0000,gsm8k-test-0042")
    subset.add_argument("--random", type=int, dest="random_count", metavar="N",
                        help="随机抽 N 题（种子写进运行快照，可复现）")
    bench_run.add_argument("--seed", help="随机种子（8-64 位十六进制，省略则生成并记录）")
    bench_run.add_argument("--reasoning-level", help="该模型的思考强度等级（需模型档案声明支持）")
    bench_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    bench_run.add_argument("--json", action="store_true")

    def _add_external_benchmark_subcommands(
        group: argparse.ArgumentParser, dest: str,
    ) -> None:
        subparsers = group.add_subparsers(dest=dest, required=True)
        prepare = subparsers.add_parser("prepare", help="校验并准备本地 JSONL 数据（来源治理门禁；结果持久化）")
        prepare.add_argument("--files", required=True, nargs="+",
                             help="logical_name=path 形式的 JSONL 文件（如 logic_val=./logic.jsonl）")
        prepare.add_argument("--revision", required=True, help="数据集 revision（非占位）")
        prepare.add_argument("--provenance", choices=("user-supplied", "verified-official"),
                             default="user-supplied")
        preflight = subparsers.add_parser("preflight", help="静态预检（与创建同一校验服务，不触发模型调用）")
        preflight.add_argument("--model", help="模型档案 id")
        preflight.add_argument("--scope", choices=("smoke", "custom-subset", "full"), default="custom-subset")
        preflight.add_argument("--split", help="评测分区（缺省取基准默认）")
        preflight.add_argument("--few-shot", type=int, default=0)
        run = subparsers.add_parser("run", help="创建 job-based queued Run（由 Worker 执行，一次一个外部 Job）")
        run.add_argument("--model", required=True, help="已发布模型档案 id")
        run.add_argument("--files", required=True, nargs="+",
                         help="logical_name=path 形式的 JSONL 文件（与 prepare 同一校验服务）")
        run.add_argument("--revision", required=True, help="数据集 revision")
        run.add_argument("--few-shot", type=int, default=0)
        run.add_argument("--few-shot-split", dest="few_shot_split", help="few-shot 示例来源分区")
        run.add_argument("--split", help="评测分区（缺省取基准默认）")
        run.add_argument("--seed", type=int, default=0)
        run.add_argument("--scope", choices=("smoke", "custom-subset", "full"), default="custom-subset")
        run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
        for command in (prepare, preflight):
            command.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    ceval = sub.add_parser("ceval", help="C-Eval 外部基准（job-based；数据准备/预检/创建运行）")
    _add_external_benchmark_subcommands(ceval, "ceval_command")

    cmmlu = sub.add_parser("cmmlu", help="CMMLU 外部基准（独立身份；数据准备/预检/创建运行）")
    _add_external_benchmark_subcommands(cmmlu, "cmmlu_command")

    terminal_bench = sub.add_parser(
        "terminal-bench", help="Terminal-Bench（Harbor 外部基准；任务准备/预检/Trial 查看）",
    )
    tb_sub = terminal_bench.add_subparsers(dest="terminal_bench_command", required=True)
    tb_prepare = tb_sub.add_parser("prepare", help="只读准备受控任务根目录（不执行任务包脚本）")
    tb_prepare.add_argument("--task-root", required=True, help="受控任务根目录")
    tb_prepare.add_argument("--source-id", required=True, help="任务来源标识")
    tb_prepare.add_argument("--revision", required=True, help="数据集 revision（非占位）")
    tb_prepare.add_argument("--license-id", help="任务集许可证标识")
    tb_prepare.add_argument("--license-evidence", help="许可证据说明")
    tb_prepare.add_argument("--pinned-source", action="store_true", help="来源为固定 revision")
    tb_prepare.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_tasks = tb_sub.add_parser("tasks", help="列出已准备任务")
    tb_tasks.add_argument("--dataset-revision", help="指定 revision（缺省取最新）")
    tb_tasks.add_argument("--json", action="store_true")
    tb_tasks.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_preflight = tb_sub.add_parser("preflight", help="只读预检（零模型调用/零任务启动）")
    tb_preflight.add_argument("--agent-id", default="oracle")
    tb_preflight.add_argument("--agent-version", default="1.0.0")
    tb_preflight.add_argument("--model", help="已发布 ModelProfile id（真实 Agent 必需）")
    tb_preflight.add_argument(
        "--credential-ref", action="append", dest="credential_ref",
        help="凭据引用 NAME=env:VAR（可重复；值永不进入平台）",
    )
    tb_preflight.add_argument("--n-trials", type=int, default=1, dest="n_trials")
    tb_preflight.add_argument("--task-keys", help="逗号分隔的 task_key 子集")
    tb_preflight.add_argument("--dataset-revision", help="指定 revision（缺省取最新）")
    tb_preflight.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_run = tb_sub.add_parser("run", help="创建 job-based queued Run（由 Worker 执行）")
    tb_run.add_argument("--model", help="已发布 ModelProfile id（真实 Agent 必需）")
    tb_run.add_argument("--agent-id", default="oracle")
    tb_run.add_argument("--agent-version", default="1.0.0")
    tb_run.add_argument(
        "--credential-ref", action="append", dest="credential_ref",
        help="凭据引用 NAME=env:VAR（可重复；值永不进入平台）",
    )
    tb_run.add_argument("--n-trials", type=int, default=1, dest="n_trials", help="计划重复数")
    tb_run.add_argument("--aggregation", choices=("first-trial", "mean-success"),
                        default="first-trial", help="Task 层聚合规则（事前固定）")
    tb_run.add_argument("--task-keys", help="逗号分隔的 task_key 子集")
    tb_run.add_argument("--dataset-revision", help="指定 revision（缺省取最新）")
    tb_run.add_argument("--agent-timeout-sec", type=float, dest="agent_timeout_sec")
    tb_run.add_argument("--verifier-timeout-sec", type=float, dest="verifier_timeout_sec")
    tb_run.add_argument(
        "--agent-setup-timeout-sec", type=float, dest="agent_setup_timeout_sec",
        help="Agent 安装/准备阶段期限（映射到 Harbor override_setup_timeout_sec）",
    )
    tb_run.add_argument(
        "--job-timeout-sec", type=float, dest="job_timeout_sec",
        help="Job 总期限（映射到 Supervisor max_wall_seconds，超时中断并保留部分证据）",
    )
    tb_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_status = tb_sub.add_parser("status", help="Task 层结果与覆盖门禁")
    tb_status.add_argument("--run-id", required=True)
    tb_status.add_argument("--json", action="store_true")
    tb_status.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_trials = tb_sub.add_parser("trials", help="某 Task 的计划 Trial 列表")
    tb_trials.add_argument("--run-id", required=True)
    tb_trials.add_argument("--task-key", required=True)
    tb_trials.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    tb_trial = tb_sub.add_parser("trial", help="单 Trial 详情（终止/Verifier/证据引用）")
    tb_trial.add_argument("--run-id", required=True)
    tb_trial.add_argument("--trial-id", required=True)
    tb_trial.add_argument("--artifact", help="读取该 Trial 的某个证据引用内容")
    tb_trial.add_argument("--text", action="store_true", help="只打印文本内容")
    tb_trial.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    direct = sub.add_parser("direct-llm", help="Direct LLM 通用直连评测（导入 JSONL 数据集，创建运行）")
    direct_sub = direct.add_subparsers(dest="direct_command", required=True)
    direct_builtins = direct_sub.add_parser("builtins", help="列出仓库内置的样例数据集")
    direct_builtins.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    direct_list = direct_sub.add_parser("list", help="列出已导入的 Direct LLM 数据集")
    direct_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    direct_sources = direct_sub.add_parser(
        "sources",
        help="静态来源登记、安全缓存、转换与发布",
        description=("Direct LLM 静态来源登记与受控处理；支持 list、inspect、fetch、verify、"
                     "convert、import、prepare。"),
    )
    source_sub = direct_sources.add_subparsers(dest="source_command", required=True)
    source_list = source_sub.add_parser("list", help="列出静态来源登记；只读本地 JSON，不联网")
    source_inspect = source_sub.add_parser("inspect", help="查看一个静态来源登记；不联网")
    source_inspect.add_argument("--source", required=True, help="来源 id，如 mmlu-pro")
    source_fetch = source_sub.add_parser(
        "fetch", help="由 CLI 显式下载并校验 required artifacts")
    source_fetch.add_argument("--source", required=True, help="来源 id")
    source_fetch.add_argument("--revision", help="不可变 revision；省略时使用登记值")
    source_fetch.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数")
    source_fetch.add_argument(
        "--max-bytes", type=int,
        help="单 artifact 下载上限；省略时使用登记的 max_bytes")
    source_verify = source_sub.add_parser(
        "verify", help="只复验 required artifact 缓存的大小与 SHA-256；绝不联网")
    source_verify.add_argument("--source", required=True, help="来源 id")
    source_verify.add_argument("--revision", help="不可变 revision；省略时使用登记值")
    source_convert = source_sub.add_parser(
        "convert", help="从 verified cache 转换为 Direct LLM v2 JSON；绝不联网")
    source_convert.add_argument("--source", required=True, help="来源 id")
    source_convert.add_argument("--revision", required=True, help="不可变 revision")
    source_convert.add_argument("--config", required=True, help="converter config JSON 或 @文件")
    source_convert.add_argument("--output", required=True, help="原子写入 dataset+receipt JSON")
    source_convert.add_argument(
        "--allow-experimental", action="store_true",
        help="允许 pending/restricted 仅本地转换；结果不可发布")
    source_import = source_sub.add_parser(
        "import", help="从 verified cache 重新转换并原子发布；绝不联网")
    source_import.add_argument("--source", required=True, help="来源 id")
    source_import.add_argument("--revision", required=True, help="不可变 revision")
    source_import.add_argument("--config", required=True, help="converter config JSON 或 @文件")
    source_import.add_argument("--actor", required=True, help="发布操作者")
    source_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    source_prepare = source_sub.add_parser(
        "prepare", help="显式 fetch、转换并原子发布 approved 来源")
    source_prepare.add_argument("--source", required=True, help="来源 id")
    source_prepare.add_argument("--revision", required=True, help="不可变 revision")
    source_prepare.add_argument("--config", required=True, help="prepare config JSON 或 @文件")
    source_prepare.add_argument("--actor", required=True, help="发布操作者")
    source_prepare.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    for source_parser in (
        source_list, source_inspect, source_fetch, source_verify,
        source_convert, source_import, source_prepare,
    ):
        source_parser.add_argument(
            "--registry-dir", help="静态来源 JSON 目录（默认 datasets/direct-llm/sources）")
        source_parser.add_argument(
            "--cache-dir", help="artifact 缓存根目录（默认 var/datasets/direct-llm）")
    for source_parser in (source_fetch, source_verify):
        source_parser.add_argument("--override-actor", help="restricted 单次 override 操作者")
        source_parser.add_argument("--override-purpose", help="restricted 单次 override 用途")
        source_parser.add_argument("--override-ticket", help="restricted 单次 override 工单/审批号")
    direct_import = direct_sub.add_parser("import", help="导入一份 Direct LLM JSONL（内置样例或本地文件）")
    origin = direct_import.add_mutually_exclusive_group(required=True)
    origin.add_argument("--builtin", help="内置样例 id，见 `direct-llm builtins`")
    origin.add_argument("--file", help="本地 JSONL 路径")
    direct_import.add_argument("--name", help="数据集名（默认：内置 id，或 local-jsonl 导入的 direct-llm-custom）")
    direct_import.add_argument("--version", help="数据集版本（省略=自动：同内容复用，否则下一个空号）")
    direct_import.add_argument("--license", dest="license_id", default="internal-sample")
    direct_import.add_argument("--scorer", help="数据集默认评分器：exact / contains / regex（默认 exact）")
    direct_import.add_argument("--source", help="来源标记，写进 provenance（默认跟 --builtin / local-jsonl）")
    direct_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    direct_run = direct_sub.add_parser("run", help="创建 Direct LLM queued Run（由 Worker 执行）")
    direct_run.add_argument("--scenario", required=True,
                            help="数据集场景引用，如 direct-llm-classify@1（场景名=数据集名）")
    direct_selection = direct_run.add_mutually_exclusive_group(required=True)
    direct_selection.add_argument("--provider", help="provider connection name or provider object JSON/@file")
    direct_selection.add_argument("--model", help="model profile resource id")
    direct_subset = direct_run.add_mutually_exclusive_group()
    direct_subset.add_argument("--case-ids", help="只跑指定题目：逗号分隔的 case id")
    direct_subset.add_argument("--random", type=int, dest="random_count", metavar="N",
                               help="随机抽 N 题（种子写进运行快照，可复现）")
    direct_subset.add_argument(
        "--profile", help="运行 v2 数据集的固定 profile，如 smoke / regression / full"
    )
    direct_run.add_argument("--seed", help="随机种子（8-64 位十六进制，省略则生成并记录）")
    direct_run.add_argument("--temperature", type=float, help="采样温度（省略=模型档案默认值）")
    direct_run.add_argument("--max-output-tokens", type=int, dest="max_output_tokens",
                            help="本次输出上限，省略=数据集预设 1024，且不得超模型上限")
    direct_run.add_argument("--reasoning-level", help="该模型的思考强度等级（需模型档案声明支持）")
    direct_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    agent = sub.add_parser("agent-tasks", help="Agent 文件任务（导入任务数据集，创建 Agent Run）")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_list = agent_sub.add_parser("list", help="列出已导入的 Agent 任务数据集")
    agent_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    agent_import = agent_sub.add_parser("import", help="导入一份 Agent 任务数据集（JSON 数组文件）")
    agent_import.add_argument("--file", required=True, help="JSON 数组文件路径（每项一个任务 case）")
    agent_import.add_argument("--name", required=True, help="数据集名（场景名与之一致）")
    agent_import.add_argument("--version", help="数据集版本（省略=自动：同内容复用，否则下一个空号）")
    agent_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    agent_run = agent_sub.add_parser("run", help="创建 Agent queued Run（由 Worker 执行）")
    agent_run.add_argument("--scenario", required=True, help="任务场景引用，如 file-report@1")
    agent_run.add_argument("--model", required=True, help="已发布模型档案 id")
    agent_run.add_argument("--mode", choices=("native-tool", "legacy-json"), default="native-tool",
                           help="工具模式（native-tool 需模型 supports_tools=true）")
    agent_run.add_argument("--max-steps", type=int, help="步数预算（默认 8）")
    agent_run.add_argument("--max-tool-calls", type=int, help="工具次数预算（默认 16）")
    agent_run.add_argument("--wall-time-sec", type=float, help="时长预算秒（默认 120）")
    agent_run.add_argument("--case-ids", help="只跑指定任务：逗号分隔 case id")
    agent_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    # restore 的解析器在 motte_cli.ops（M7 起支持 --staging staging 恢复）。

    cleanup = sub.add_parser(
        "cleanup-artifacts",
        help="按 TTL 清理 artifact（默认 dry-run；legacy 简单清理，M7 起优先使用 motte gc）",
    )
    cleanup.add_argument("--older-than-days", type=float, required=True)
    cleanup.add_argument("--artifacts-root", default=None, help="artifact 根目录，默认 ARTIFACT_ROOT")
    cleanup.add_argument("--apply", action="store_true", help="真正删除（缺省仅报告）")
    remote.add_mode_arguments(cleanup)

    experiment = sub.add_parser(
        "experiment",
        help="M6 实验编排：预览 / 创建分配 / 状态 / 取消 / 单元重试（执行主权仍在 Worker）",
    )
    experiment_sub = experiment.add_subparsers(dest="experiment_command", required=True)
    exp_preview = experiment_sub.add_parser(
        "preview", help="纯预检：矩阵展开与护栏检查（零创建、零模型调用）",
    )
    exp_preview.add_argument("--spec", required=True, help="ExperimentSpec JSON 或 @文件")
    exp_preview.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(exp_preview)
    exp_create = experiment_sub.add_parser(
        "create", help="发布 spec（幂等）并为每 cell 分配恰好一个 initial Run",
    )
    exp_create.add_argument("--spec", required=True, help="ExperimentSpec JSON 或 @文件")
    exp_create.add_argument("--request-key", dest="request_key", help="幂等键（同键异内容拒绝）")
    exp_create.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(exp_create)
    exp_status = experiment_sub.add_parser("status", help="读取实验状态与 cell 进度（只读）")
    exp_status.add_argument("experiment_id")
    exp_status.add_argument("--version", help="指定版本（省略取最新）")
    exp_status.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(exp_status)
    exp_cancel = experiment_sub.add_parser(
        "cancel", help="取消实验（只作用于本实验拥有的 cell 与 Run）",
    )
    exp_cancel.add_argument("experiment_id")
    exp_cancel.add_argument("--version", help="指定版本（省略取最新）")
    exp_cancel.add_argument("--reason", default="operator request")
    exp_cancel.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(exp_cancel)
    exp_retry = experiment_sub.add_parser(
        "retry-cell", help="显式重试：superseding 子 Run，原结果不消失（local-only）",
    )
    exp_retry.add_argument("cell_id")
    exp_retry.add_argument("--reason", default="operator retry")
    exp_retry.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")

    compare = sub.add_parser(
        "compare", help="M6 两份固定报告的三级可比性结论（只读，零 Provider/Judge 调用）",
    )
    compare.add_argument("--baseline", required=True, help="基线 run id")
    compare.add_argument("--candidate", required=True, help="候选 run id")
    compare.add_argument("--factors", default="model", help="允许因子，逗号分隔（默认 model）")
    compare.add_argument("--baseline-pass", dest="baseline_pass",
                         help="固定基线 scoring pass id（缺省 current）")
    compare.add_argument("--candidate-pass", dest="candidate_pass",
                         help="固定候选 scoring pass id（缺省 current）")
    compare.add_argument("--json", dest="json_out", help="把比较 JSON 写入文件")
    compare.add_argument("--statistics", action="store_true",
                         help="附带固定 Case/Task 配对统计与资格（只读）")
    compare.add_argument("--k", type=int, default=1,
                         help="Terminal-Bench 的事前计划 Trial pass@k（默认 1）")
    compare.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(compare)

    baseline = sub.add_parser(
        "baseline", help="M6 BaselineSnapshot：创建 / 列表 / 默认指针（指针是 CAS 操作）",
    )
    baseline_sub = baseline.add_subparsers(dest="baseline_command", required=True)
    bl_create = baseline_sub.add_parser(
        "create", help="固定 RunReportRef 集合 → 不可变 baseline（引用不完整拒绝创建）",
    )
    bl_create.add_argument("--id", required=True, help="baseline_id")
    bl_create.add_argument(
        "--entries", required=True,
        help='JSON 或 @文件：[{"cell_key"?, "run_id", "scoring_pass_id"}]',
    )
    bl_create.add_argument(
        "--policy", help='比较政策 JSON 或 @文件（默认 {"allowed_factors": ["model"]}）',
    )
    bl_create.add_argument("--by", required=True, help="创建操作者（审计）")
    bl_create.add_argument("--reason", required=True, help="创建原因（审计）")
    bl_create.add_argument("--source-note", dest="source_note", help="来源备注（审计）")
    bl_create.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(bl_create)
    bl_list = baseline_sub.add_parser("list", help="列出 baseline 快照（只读）")
    bl_list.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(bl_list)
    bl_select = baseline_sub.add_parser("select", help="设置 scope 默认 baseline 指针（CAS）")
    bl_select.add_argument("--scope", required=True)
    bl_select.add_argument("--baseline", required=True, help="baseline_id")
    bl_select.add_argument(
        "--expected-current", dest="expected_current",
        help="CAS 期望当前值（指针已存在时移动必须给出）",
    )
    bl_select.add_argument("--by", required=True, help="操作者（审计）")
    bl_select.add_argument("--reason", required=True, help="原因（审计）")
    bl_select.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(bl_select)
    bl_default = baseline_sub.add_parser("default", help="读取 scope 当前默认 baseline 指针")
    bl_default.add_argument("--scope", required=True)
    bl_default.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(bl_default)

    gate = sub.add_parser(
        "gate", help="M6 版本化 Gate：政策发布 / 固定报告求值 / 结果读取与导出（零调用）",
    )
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_publish = gate_sub.add_parser(
        "policy-publish", help="发布不可变 GatePolicyVersion（同 id@version 同内容幂等）",
    )
    gate_publish.add_argument("--policy", required=True, help="GatePolicyVersion JSON 或 @文件")
    gate_publish.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(gate_publish)
    gate_evaluate = gate_sub.add_parser(
        "evaluate",
        help="固定报告求值（只读）；退出码=决策映射（协议 §7）：pass=0 quality_fail=1 "
             "execution_error=3 insufficient/not_comparable=5 safety_block=6",
    )
    gate_evaluate.add_argument("--policy", help="政策引用 id@version")
    gate_evaluate.add_argument("--policy-id", dest="policy_id", help="政策 id（与 --version 连用）")
    gate_evaluate.add_argument("--version", dest="policy_version", help="政策版本（与 --policy-id 连用）")
    gate_evaluate.add_argument("--run", required=True, help="候选 run id（必须终态）")
    gate_evaluate.add_argument("--pass", dest="scoring_pass",
                               help="固定 scoring pass id（缺省 current）")
    gate_evaluate.add_argument("--baseline", help="BaselineSnapshot id（基线比较）")
    gate_evaluate.add_argument("--json", dest="json_out", help="把导出 JSON 写入文件")
    gate_evaluate.add_argument("--junit", dest="junit_out", help="把 JUnit XML 写入文件")
    gate_evaluate.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(gate_evaluate)
    gate_result = gate_sub.add_parser("result", help="读取一个 GateResult（只读）")
    gate_result.add_argument("gate_result_id")
    gate_result.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(gate_result)
    gate_export = gate_sub.add_parser("export", help="导出 GateResult（exporter v1：json/junit）")
    gate_export.add_argument("--result", required=True, help="gate_result_id")
    gate_export.add_argument("--format", choices=("json", "junit"), default="json")
    gate_export.add_argument("--out", help="写文件（缺省打印 stdout）")
    gate_export.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(gate_export)

    regression = sub.add_parser(
        "regression", help="M6 两份固定报告的逐 case 回归分类（只读；added/removed 独立列出）",
    )
    regression.add_argument("--baseline", required=True, help="基线 run id")
    regression.add_argument("--candidate", required=True, help="候选 run id")
    regression.add_argument("--baseline-pass", dest="baseline_pass",
                            help="固定基线 scoring pass id（缺省 current）")
    regression.add_argument("--candidate-pass", dest="candidate_pass",
                            help="固定候选 scoring pass id（缺省 current）")
    regression.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    remote.add_mode_arguments(regression)

    # M7 新增：run 生命周期子命令与运维命令（local/server 路由见各模块）。
    runops.add_run_lifecycle_parsers(sub)
    ops.add_ops_parsers(sub)
    return parser


def _service(args):
    from motte_sdk.service import RunService, build_run_service
    from motte_storage.run_store import SQLiteRunStore

    return RunService(SQLiteRunStore(args.db)) if args.db else build_run_service()


def _error(code: str, message: str, **extra) -> int:
    payload = {"code": code, "message": message, **extra}
    print(json.dumps({"error": payload}, ensure_ascii=False), file=sys.stderr)
    return 2


def _source_payload(source) -> dict:
    return {
        "id": source.id,
        "label": source.label,
        "description": source.description,
        "status": source.governance.status,
        "tier": source.tier,
        "blockers": list(source.blockers),
        "revision": source.upstream.revision.model_dump(mode="json"),
        "artifacts": [item.model_dump(mode="json") for item in source.upstream.artifacts],
        "conversion": source.conversion.model_dump(mode="json"),
        "safety": source.safety.model_dump(mode="json"),
        "links": source.links.model_dump(mode="json"),
        "license": source.license.model_dump(mode="json"),
        "official_comparability": source.official_comparability.model_dump(mode="json"),
    }


def _source_override(args):
    from motte_sdk.dataset_sources import SourcePipelineError

    values = (args.override_actor, args.override_purpose, args.override_ticket)
    if not any(values):
        return None
    if not all(values):
        raise SourcePipelineError(
            "CONTRACT_INVALID",
            "restricted override requires --override-actor, --override-purpose, and --override-ticket together",
        )
    # These are untrusted request attributes, not approval evidence. This CLI
    # deliberately has no ApprovalVerifier capability.
    return {"actor": values[0], "purpose": values[1], "ticket": values[2]}


def _source_error(error) -> int:
    print(json.dumps({"error": error.to_dict()}, ensure_ascii=False), file=sys.stderr)
    return 2


_SOURCE_CONFIG_KEYS = frozenset({
    "converter_id",
    "converter_version",
    "expected_rows",
    "expected_test_rows",
    "expected_validation_rows",
    "input_count",
    "max_bytes",
    "profile_counts",
    "profile_seed",
    "profile_targets",
    "runner_revision",
    "seed",
    "split",
    "test_artifact",
    "timeout",
    "validation_artifact",
    "version",
})


def _source_config(raw: str) -> dict:
    from motte_sdk.dataset_sources import SourcePipelineError, parse_json
    from motte_sdk.resolve import find_secret_paths

    try:
        payload = Path(raw[1:]).read_bytes() if raw.startswith("@") else raw.encode("utf-8")
    except OSError as error:
        raise SourcePipelineError(
            "SOURCE_CONFIG_INVALID", f"cannot read source config: {error}") from error
    value = parse_json(payload)
    if not isinstance(value, dict):
        raise SourcePipelineError("SOURCE_CONFIG_INVALID", "source config must be a JSON object")
    secret_paths = find_secret_paths(value)
    if secret_paths:
        raise SourcePipelineError(
            "CREDENTIALS_REJECTED",
            "source config cannot contain credential fields",
            details={"paths": sorted(secret_paths)},
        )
    unknown = sorted(set(value) - _SOURCE_CONFIG_KEYS)
    if unknown:
        raise SourcePipelineError(
            "SOURCE_CONFIG_INVALID",
            "source config contains unsupported fields",
            details={"fields": unknown},
        )
    return value


def _require_managed_actions(source, actions) -> None:
    from motte_contracts.dataset_sources import SourceActionBlockedError, require_source_action
    from motte_sdk.dataset_sources import SourcePipelineError

    for action in actions:
        try:
            require_source_action(source, action)
        except SourceActionBlockedError as error:
            decision = error.decision
            raise SourcePipelineError(
                decision.code or "SOURCE_ACTION_BLOCKED",
                "; ".join(decision.reasons),
                details={"source_id": source.id, "action": action},
            ) from error


def _atomic_source_output(path: str, payload: dict) -> None:
    from motte_sdk.dataset_sources import SourcePipelineError

    target = Path(path)
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n").encode("utf-8")
    temporary_path = None
    try:
        if target.is_symlink():
            raise SourcePipelineError(
                "SOURCE_OUTPUT_UNSAFE", f"output path cannot be a symlink: {target}")
        if target.exists():
            if target.read_bytes() == encoded:
                return
            raise SourcePipelineError(
                "SOURCE_OUTPUT_CONFLICT", f"output already exists with different content: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, target)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != encoded:
                raise SourcePipelineError(
                    "SOURCE_OUTPUT_CONFLICT",
                    f"output concurrently appeared with different content: {target}",
                )
    except SourcePipelineError:
        raise
    except (OSError, ValueError) as error:
        raise SourcePipelineError(
            "SOURCE_OUTPUT_IO", f"cannot atomically write source output: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _managed_source_summary(result: dict, *, output: str | None = None) -> dict:
    summary = {
        "source_id": result.get("source_id"),
        "revision": result.get("revision"),
        "dataset_fingerprint": result.get("dataset_fingerprint"),
        "profile_hashes": result.get("profile_hashes", {}),
        "publishable": result.get(
            "publishable", result.get("governance", {}).get("publishable", False)),
    }
    if output is not None:
        summary["output"] = output
    if result.get("publication_id") is not None:
        summary["publication_id"] = result["publication_id"]
    return summary


def _direct_llm_sources_command(args) -> int:
    """Static source catalog and CLI-only artifact cache operations."""
    from motte_sdk.dataset_sources import (
        SafeFetcher,
        SourcePipelineError,
        artifact_cache_path,
        inspect_source,
        list_sources,
        load_registry,
        read_cached_artifact,
        resolve_revision,
    )
    from motte_sdk.managed_sources import (
        ManagedSourceError,
        convert_cached_source,
        import_cached_source,
        prepare_source,
    )

    try:
        if args.source_command == "list":
            items = [_source_payload(source) for source in list_sources(args.registry_dir)]
            print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
            return 0

        source = inspect_source(args.source, args.registry_dir)
        if args.source_command == "inspect":
            print(json.dumps(_source_payload(source), ensure_ascii=False))
            return 0

        if args.source_command in {"convert", "import", "prepare"}:
            config = _source_config(args.config)
            registry = load_registry(args.registry_dir)
            if args.source_command == "convert":
                result = convert_cached_source(
                    source.id,
                    args.revision,
                    config,
                    args.allow_experimental,
                    registry=registry,
                    cache_root=args.cache_dir,
                )
                dataset = result["dataset"]
                receipt = {key: value for key, value in result.items() if key != "dataset"}
                output = str(Path(args.output))
                _atomic_source_output(output, {"dataset": dataset, "receipt": receipt})
                print(json.dumps(
                    _managed_source_summary(result, output=output), ensure_ascii=False))
                return 0

            actions = ("import", "publish")
            if args.source_command == "prepare":
                actions = ("fetch", "import", "publish")
            _require_managed_actions(source, actions)
            resources = _resources(args)
            if args.source_command == "import":
                result = import_cached_source(
                    source.id,
                    args.revision,
                    config,
                    resources=resources,
                    actor=args.actor,
                    registry=registry,
                    cache_root=args.cache_dir,
                )
            else:
                result = prepare_source(
                    source.id,
                    args.revision,
                    config,
                    resources=resources,
                    actor=args.actor,
                    registry=registry,
                    cache_root=args.cache_dir,
                )
            print(json.dumps(_managed_source_summary(result), ensure_ascii=False))
            return 0

        override = _source_override(args)
        revision = args.revision or source.upstream.revision.value or ""
        required = [artifact for artifact in source.upstream.artifacts if artifact.required]
        if args.source_command == "fetch":
            fetcher = SafeFetcher(cache_root=args.cache_dir)
            receipts = []
            for artifact in required:
                limit = args.max_bytes if args.max_bytes is not None else artifact.max_bytes or 1
                receipt = fetcher.fetch(
                    source,
                    revision,
                    artifact.logical_name,
                    entrypoint="cli",
                    timeout=args.timeout,
                    max_bytes=limit,
                    override=override,
                )
                receipts.append(receipt.to_dict())
            effective_revision = receipts[0]["revision"] if receipts else revision
            print(json.dumps({
                "source_id": source.id,
                "revision": effective_revision,
                "artifacts": receipts,
                "total": len(receipts),
            }, ensure_ascii=False))
            return 0

        resolved = resolve_revision(source, revision)
        verified = []
        for artifact in required:
            raw = read_cached_artifact(
                source,
                resolved,
                artifact.logical_name,
                cache_root=args.cache_dir,
                override=override,
            )
            verified.append({
                "logical_name": artifact.logical_name,
                "path": str(artifact_cache_path(
                    source, resolved, artifact.logical_name, cache_root=args.cache_dir)),
                "sha256": artifact.sha256,
                "bytes": len(raw),
                "verified": True,
            })
        print(json.dumps({
            "source_id": source.id,
            "revision": resolved,
            "artifacts": verified,
            "total": len(verified),
        }, ensure_ascii=False))
        return 0
    except (SourcePipelineError, ManagedSourceError) as error:
        return _source_error(error)
    except OSError as error:
        return _error("SOURCE_UNAVAILABLE", str(error))
    except ValueError as error:
        return _error("CONTRACT_INVALID", str(error))


def _agent_tasks_command(args) -> int:
    """`motte agent-tasks`：任务数据集导入、清单与 Agent Run 创建（stdout 始终是 JSON）。"""
    from motte_contracts import suites as contract_suites
    from motte_contracts.agent_tasks import SUITE
    from motte_sdk.agent_tasks import persist_agent_tasks_dataset
    from motte_storage.resource_store import ResourceConflictError

    if args.agent_command == "list":
        resources = _resources(args)
        items = []
        for scenario in sorted(resources.scenarios.list(), key=lambda s: str(s.get("name"))):
            if scenario.get("suite") != SUITE:
                continue
            name, _, version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(name, version)
            if dataset is None:
                continue
            items.append({
                "scenario": f"{scenario['name']}@{scenario['version']}",
                "dataset": scenario["dataset"],
                "suite": SUITE,
                "cases": len(dataset.get("cases") or ()),
                "dataset_fingerprint": dataset.get("dataset_fingerprint"),
            })
        print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
        return 0

    if args.agent_command == "import":
        import json as _json

        try:
            with open(args.file, encoding="utf-8") as handle:
                cases = _json.load(handle)
            receipt = persist_agent_tasks_dataset(
                {"name": args.name, "version": args.version or "1", "cases": cases},
                _resources(args), version=args.version,
            )
        except OSError as error:
            return _error("SOURCE_UNAVAILABLE", f"cannot read source file: {error}")
        except ValueError as error:
            code = "RESOURCE_CONFLICT" if isinstance(error, ResourceConflictError) else "CONTRACT_INVALID"
            return _error(code, str(error))
        print(json.dumps(receipt, ensure_ascii=False))
        return 0

    from motte_sdk.resolve import ManifestResolutionError, prepare_run

    name, _, version = args.scenario.rpartition("@")
    scenario = _resources(args).scenarios.get(name, version)
    if scenario is None:
        return _error("SCENARIO_NOT_FOUND", f"scenario not found: {args.scenario}")
    if scenario.get("suite") != SUITE:
        actual = contract_suites.suite_of(scenario)
        return _error(
            "SUITE_MISMATCH",
            f"resource {args.scenario} belongs to suite {actual!r}, expected {SUITE!r}",
        )
    requested: dict = {"model": args.model, "agent": {"mode": args.mode}}
    budget = {}
    if args.max_steps is not None:
        budget["max_steps"] = args.max_steps
    if args.max_tool_calls is not None:
        budget["max_tool_calls"] = args.max_tool_calls
    if args.wall_time_sec is not None:
        budget["wall_time_sec"] = args.wall_time_sec
    if budget:
        requested["agent"]["budget"] = budget
    if args.case_ids is not None:
        ids = [item.strip() for item in args.case_ids.replace("，", ",").split(",") if item.strip()]
        if not ids:
            return _error("CONTRACT_INVALID", "--case-ids 不能为空")
        requested["case_selection"] = {"mode": "ids", "case_ids": ids}
    try:
        manifest, case_ids = prepare_run(args.scenario, requested, [], _resources(args))
    except ManifestResolutionError as error:
        return _error(error.code, str(error))
    except ValueError as error:
        return _error("CONTRACT_INVALID", str(error))
    run = _service(args).create_run(
        args.scenario, manifest, case_ids, requested_manifest=requested
    )
    print(json.dumps(run, ensure_ascii=False))
    return 0


def _direct_llm_command(args) -> int:
    """`motte direct-llm`：内置样例 / 本地 JSONL 的导入、清单与运行创建（stdout 始终是 JSON）。"""
    if args.direct_command == "sources":
        return _direct_llm_sources_command(args)

    from motte_contracts import suites as contract_suites
    from motte_contracts.direct_llm import SUITE
    from motte_sdk.direct_llm import (BuiltinUnavailable, builtin_catalog, import_builtin_dataset,
                                      import_direct_llm_split)
    from motte_storage.resource_store import ResourceConflictError

    if args.direct_command == "builtins":
        items = builtin_catalog()
        print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
        return 0

    if args.direct_command == "list":
        resources = _resources(args)
        items = []
        for scenario in sorted(resources.scenarios.list(), key=lambda s: str(s.get("name"))):
            if contract_suites.suite_of(scenario) != SUITE:
                continue
            name, _, version = str(scenario.get("dataset", "")).rpartition("@")
            dataset = resources.datasets.get(name, version)
            if dataset is None:
                continue
            provenance = dataset.get("provenance") or {}
            profiles = [
                {"name": profile.get("name"), "count": profile.get("count")}
                for profile in dataset.get("profiles") or [] if isinstance(profile, dict)
            ]
            items.append({"scenario": f"{scenario['name']}@{scenario['version']}",
                          "dataset": scenario["dataset"],
                          "suite": SUITE,
                          "contract_version": dataset.get("contract_version", 1),
                          "dataset_fingerprint": dataset.get("dataset_fingerprint"),
                          "scorer": (dataset.get("eval") or {}).get("scorer"),
                          "cases": len(dataset.get("cases") or ()),
                          "profiles": profiles,
                          "source": provenance.get("source_id", provenance.get("source")),
                          "revision": provenance.get("upstream_revision", provenance.get("revision")),
                          "license_status": ((provenance.get("license") or {}).get("status")
                                             if isinstance(provenance.get("license"), dict)
                                             else provenance.get("license"))})
        print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
        return 0

    if args.direct_command == "import":
        try:
            if args.builtin:
                receipt = import_builtin_dataset(
                    args.builtin, resources=_resources(args), name=args.name, version=args.version,
                    license_id=args.license_id, scorer=args.scorer)
            else:
                with open(args.file, "rb") as handle:
                    raw = handle.read()
                receipt = import_direct_llm_split(
                    raw, name=args.name or "direct-llm-custom", version=args.version,
                    license_id=args.license_id, scorer=args.scorer,
                    source=args.source or "local-jsonl", resources=_resources(args))
        except BuiltinUnavailable as error:
            return _error("BUILTIN_UNAVAILABLE", str(error))
        except OSError as error:
            return _error("SOURCE_UNAVAILABLE", f"cannot read source file: {error}")
        except ValueError as error:
            code = "RESOURCE_CONFLICT" if isinstance(error, ResourceConflictError) else "CONTRACT_INVALID"
            return _error(code, str(error))
        print(json.dumps(receipt, ensure_ascii=False))
        return 0

    from motte_sdk.resolve import ManifestResolutionError, prepare_run

    name, _, version = args.scenario.rpartition("@")
    scenario = _resources(args).scenarios.get(name, version)
    if scenario is None:
        return _error("SCENARIO_NOT_FOUND", f"scenario not found: {args.scenario}")
    actual_suite = contract_suites.suite_of(scenario)
    if actual_suite != SUITE:
        return _error(
            "SUITE_MISMATCH",
            f"resource {args.scenario} belongs to suite {actual_suite!r}, expected {SUITE!r}",
        )
    if args.model:
        requested: dict = {"model": args.model}
    else:
        requested = {"provider": _load_json(args.provider) if args.provider.startswith(("{", "@"))
                     else args.provider}
    try:
        if args.case_ids is not None:
            ids = [item.strip() for item in args.case_ids.replace("，", ",").split(",") if item.strip()]
            if not ids:
                raise ValueError("--case-ids 不能为空")
            requested["case_selection"] = {"mode": "ids", "case_ids": ids}
        elif args.random_count is not None:
            requested["case_selection"] = {"mode": "random", "count": args.random_count}
            if args.seed:
                requested["case_selection"]["seed"] = args.seed
        elif args.profile is not None:
            if not args.profile.strip():
                raise ValueError("--profile 不能为空")
            if args.seed:
                raise ValueError("--seed 只与 --random 搭配使用")
            requested["case_selection"] = {"mode": "profile", "profile": args.profile.strip()}
        elif args.seed:
            raise ValueError("--seed 只与 --random 搭配使用")
        parameters = {}
        if args.temperature is not None:
            parameters["temperature"] = args.temperature
        if args.max_output_tokens is not None:
            parameters["max_output_tokens"] = args.max_output_tokens
        if parameters:
            requested["parameters"] = parameters
        if args.reasoning_level:
            requested["reasoning_level"] = args.reasoning_level
        manifest, case_ids = prepare_run(args.scenario, requested, [], _resources(args))
    except ManifestResolutionError as error:
        return _error(error.code, str(error))
    except ValueError as error:
        return _error("CONTRACT_INVALID", str(error))
    run = _service(args).create_run(
        args.scenario, manifest, case_ids, requested_manifest=requested
    )
    print(json.dumps(run, ensure_ascii=False))
    return 0


def _resources(args):
    from motte_storage.factory import create_resource_store
    from motte_storage.resource_store import SQLiteResourceStore

    from motte_storage.factory import default_content_store

    if args.db:
        # 与 create_resource_store() 同一装配：API 与 Worker 必须指向同一个内容目录
        return SQLiteResourceStore(args.db, content_store=default_content_store())
    return create_resource_store()


def _skill_command(args) -> int:
    """skill publish/list：与 API 同一契约、同一 content_hash。

    发布只写不可变版本资源；不执行入口、不注入、不创建 Run、不调用模型。
    """
    from motte_cli import skills as sk
    from motte_storage.resource_store import ResourceConflictError

    command = args.skill_command
    if command == "list":
        items = _resources(args).skills.list()
        if getattr(args, "json", False):
            print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
            return 0
        if not items:
            print("（没有已发布的 Skill 版本）")
        for item in items:
            print(
                f"  {item.get('skill_id')}@{item.get('version')}  "
                f"kind={item.get('kind')} hash={item.get('content_hash')}"
            )
        return 0

    if command == "publish":
        try:
            document = sk.load_document(args.file)
            stored = sk.publish_skill(_resources(args), document)
        except sk.SkillDocumentError as error:
            return _error(error.code, str(error), **({"fields": error.fields} if error.fields else {}))
        except ResourceConflictError as error:
            return _error("RESOURCE_CONFLICT", str(error))
        except ValueError as error:
            return _error(type(error).__name__.upper(), str(error))
        if getattr(args, "json", False):
            print(json.dumps(stored, ensure_ascii=False))
        else:
            print(
                f"published {stored.get('skill_id')}@{stored.get('version')} "
                f"kind={stored.get('kind')} content_hash={stored.get('content_hash')}"
            )
        return 0

    return _error("CONTRACT_INVALID", f"unknown skill command: {command}")


def _fixture_command(args) -> int:
    """fixture publish/list：与 API 同一契约、同一 content_hash。

    发布只写不可变版本资源；不初始化 fixture、不执行工具、不创建 Run、不调用模型。
    """
    from motte_cli import fixtures as fx
    from motte_storage.resource_store import ResourceConflictError

    command = args.fixture_command
    if command == "list":
        items = _resources(args).fixtures.list()
        if getattr(args, "json", False):
            print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
            return 0
        if not items:
            print("（没有已发布的 Fixture 版本）")
        for item in items:
            print(
                f"  {item.get('fixture_id')}@{item.get('version')}  "
                f"hash={item.get('content_hash')}"
            )
        return 0

    if command == "publish":
        try:
            document = fx.load_document(args.file)
            stored = fx.publish_fixture(_resources(args), document)
        except fx.FixtureDocumentError as error:
            return _error(error.code, str(error), **({"fields": error.fields} if error.fields else {}))
        except ResourceConflictError as error:
            return _error("RESOURCE_CONFLICT", str(error))
        except ValueError as error:
            return _error("CONTRACT_INVALID", str(error))
        if getattr(args, "json", False):
            print(json.dumps(stored, ensure_ascii=False))
        else:
            print(
                f"published {stored.get('fixture_id')}@{stored.get('version')} "
                f"content_hash={stored.get('content_hash')}"
            )
        return 0

    return _error("CONTRACT_INVALID", f"unknown fixture command: {command}")


def _workflow_command(args) -> int:
    """workflow publish/list/convert-legacy：与 API 同一契约、同一编译器。

    预检与转换都不执行步骤、不创建 Run、不调用模型；发布只写不可变版本资源。
    """
    from motte_cli import workflows as wf
    from motte_storage.resource_store import ResourceConflictError

    command = args.workflow_command
    if command == "list":
        items = _resources(args).workflows.list()
        if getattr(args, "json", False):
            print(json.dumps({"items": items, "total": len(items)}, ensure_ascii=False))
            return 0
        if not items:
            print("（没有已发布的 Workflow 版本）")
        for item in items:
            print(
                f"  {item.get('workflow_id')}@{item.get('version')}  "
                f"hash={item.get('content_hash')}"
            )
        return 0

    if command == "publish":
        try:
            document = wf.load_document(args.file)
            stored = wf.publish_workflow(_resources(args), document)
        except wf.WorkflowDocumentError as error:
            return _error(error.code, str(error), **({"fields": error.fields} if error.fields else {}))
        except ResourceConflictError as error:
            return _error("RESOURCE_CONFLICT", str(error))
        except ValueError as error:
            return _error("CONTRACT_INVALID", str(error))
        if getattr(args, "json", False):
            print(json.dumps(stored, ensure_ascii=False))
        else:
            print(
                f"published {stored.get('workflow_id')}@{stored.get('version')} "
                f"content_hash={stored.get('content_hash')}"
            )
        return 0

    if command == "convert-legacy":
        try:
            legacy = wf.load_document(args.file)
            report = wf.conversion_report(
                legacy, workflow_id=args.workflow_id, version=args.version,
                published_at=args.published_at,
            )
        except wf.WorkflowDocumentError as error:
            return _error(error.code, str(error), **({"fields": error.fields} if error.fields else {}))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["publishable"] else 1

    return _error("CONTRACT_INVALID", f"unknown workflow subcommand: {command}")


def _scenario_run(args) -> int:
    """创建逐步骤 Scenario Run：与 CLI run / API POST /runs 共用 prepare_run+create_run。"""
    from motte_sdk.resolve import ManifestResolutionError, prepare_run

    if args.spec:
        if args.scenario or args.workflow or args.target_runtime or args.target_agent:
            return _error(
                "CONTRACT_INVALID",
                "--spec 与 --scenario/--workflow/--target-runtime/--target-agent 不能同时使用",
            )
        try:
            spec = _load_json(args.spec)
        except (OSError, json.JSONDecodeError) as error:
            return _error("RUN_SPEC_INVALID", f"--spec 不是可解析的 JSON：{error}")
        if not isinstance(spec, dict):
            return _error(
                "RUN_SPEC_INVALID",
                "--spec 必须是 JSON 对象：{scenario_version, manifest, case_ids}",
            )
    else:
        if not args.scenario or not args.workflow:
            return _error("CONTRACT_INVALID", "需要 --spec，或同时提供 --scenario 与 --workflow")
        if args.target_runtime and args.target_agent:
            return _error("CONTRACT_INVALID", "--target-runtime 与 --target-agent 只能选一个")
        manifest: dict = {"workflow": args.workflow}
        if args.target_runtime:
            manifest["runtime"] = args.target_runtime
        if args.target_agent:
            manifest["agent"] = args.target_agent
        case_ids = [
            item.strip()
            for item in (args.case_ids or "").replace("，", ",").split(",")
            if item.strip()
        ]
        spec = {"scenario_version": args.scenario, "manifest": manifest, "case_ids": case_ids}

    scenario_version = spec.get("scenario_version") or ""
    manifest = spec.get("manifest") or {}
    case_ids = spec.get("case_ids") or []
    if not isinstance(scenario_version, str) or not scenario_version:
        return _error("SCENARIO_REQUIRED", "scenario_version is required")
    if not isinstance(manifest, dict):
        return _error("RUN_CONFIG_INVALID", "manifest must be an object")
    try:
        prepared, resolved_case_ids = prepare_run(
            scenario_version, manifest, case_ids, _resources(args)
        )
    except ManifestResolutionError as error:
        return _error(error.code, str(error))
    except ValueError as error:
        return _error("CONTRACT_INVALID", str(error))
    try:
        run = _service(args).create_run(
            scenario_version, prepared, resolved_case_ids, requested_manifest=manifest
        )
    except ValueError as error:
        return _error("RUN_CREATE_FAILED", str(error))
    print(json.dumps({
        "id": run["id"],
        "status": run["status"],
        "scenario": scenario_version,
        "workflow": prepared.get("workflow"),
        "execution": prepared.get("execution") or {},
        "cases": len(resolved_case_ids),
        "提示": "由 Worker 执行：uv run python -m apps.worker.motte_worker --once",
    }, ensure_ascii=False))
    return 0


def _scenario_command(args) -> int:
    """scenario validate/targets/run：预检纯只读，运行走同一条创建路径。"""
    from motte_cli import workflows as wf

    command = args.scenario_command
    if command == "targets":
        items = wf.scenario_targets()
        if args.json:
            print(json.dumps(
                {"items": items, "total": len(items), "available": bool(items)},
                ensure_ascii=False,
            ))
            return 0
        if not items:
            print("（没有注册的 scenario target adapter；逐步骤 Scenario Run 会在创建期被拒绝）")
        for item in items:
            flags = "/".join(
                "Y" if item.get(flag) else "N"
                for flag in ("multi_turn", "interrupt", "skill_injection")
            )
            print(
                f"  {item.get('kind')} available={item.get('available')} "
                f"multi_turn/interrupt/skill={flags} "
                f"tool_modes={','.join(item.get('tool_modes') or [])}"
            )
        return 0

    if command == "validate":
        try:
            resources = _resources(args)
            if "@" in args.document and not Path(args.document).exists():
                document = wf.published_document(resources, args.document)
            else:
                document = wf.load_document(args.document)
            report = wf.preflight_report(document, resources=resources)
        except wf.WorkflowDocumentError as error:
            # 预检失败是**结果**不是崩溃：稳定 JSON + 非 0 退出码（与 API 同一 code）。
            print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False))
            return 1
        print(json.dumps(report, ensure_ascii=False))
        return 0

    if command == "run":
        return _scenario_run(args)

    return _error("CONTRACT_INVALID", f"unknown scenario subcommand: {command}")


def _judge_command(args) -> int:
    """judge preflight/submit/status/history/cancel：与 API 共用同一编译器。

    预检与读取零模型调用；提交只落持久作业，执行永远在 Worker 的执行锁内。
    """
    from motte_eval.judge import JudgeBudgetError, JudgeError, JudgeInputError, JudgeNotAuthorised
    from motte_sdk.resolve import ManifestResolutionError
    from motte_sdk.scoring_jobs import (
        FrozenProviderFactory,
        JudgeEvidenceError,
        JudgeProviderSnapshotError,
        ScoringJobService,
        build_judge_submission,
    )
    from motte_storage.scoring_jobs import ScoringJobConflict, ScoringJobError

    command = args.judge_command
    service = _service(args)
    try:
        judge = ScoringJobService(service.store, provider_factory=FrozenProviderFactory())
    except ScoringJobError as error:
        return _error("JUDGE_UNAVAILABLE", str(error))

    if command in ("preflight", "submit"):
        try:
            document = _load_json(args.spec)
        except (OSError, json.JSONDecodeError) as error:
            return _error("JUDGE_SPEC_INVALID", f"--spec 不是可解析的 JSON：{error}")
        if not isinstance(document, dict):
            return _error("JUDGE_SPEC_INVALID", "--spec 必须是 JSON 对象")
        request_key = (
            getattr(args, "request_key", None) or document.get("request_key") or ""
        )
        if command == "submit" and not request_key:
            return _error(
                "JUDGE_SPEC_INVALID", "submit 需要 --request-key 或 spec.request_key"
            )
        try:
            request = build_judge_submission(
                store=service.store,
                resources=_resources(args),
                request_key=request_key or "judge-preflight",
                run_id=str(document.get("run_id") or ""),
                mode=str(document.get("mode") or "single"),
                spec_request=document.get("spec") or {},
                case_ids=document.get("case_ids"),
                source_pass_id=document.get("source_pass_id"),
                authorisation=document.get("authorisation"),
                publish_policy=str(document.get("publish_policy") or "all_scored"),
                repeats=int(document.get("repeats") or 1),
                presentation_orders=document.get("presentation_orders"),
                price_table_version=document.get("price_table_version"),
            )
        except ManifestResolutionError as error:
            return _error(error.code, str(error))
        except JudgeEvidenceError as error:
            return _error(error.code, str(error))
        except JudgeProviderSnapshotError as error:
            return _error(error.code, str(error))
        except JudgeNotAuthorised as error:
            return _error("JUDGE_NOT_AUTHORISED", str(error))
        except JudgeBudgetError as error:
            return _error("JUDGE_BUDGET_NOT_EXECUTABLE", str(error))
        except (JudgeInputError, ValueError, TypeError) as error:
            return _error("JUDGE_INPUT_INVALID", str(error))
        if command == "preflight":
            print(json.dumps(judge.preflight(request), ensure_ascii=False))
            return 0
        try:
            result = judge.submit(request)
        except JudgeNotAuthorised as error:
            return _error("JUDGE_NOT_AUTHORISED", str(error))
        except JudgeBudgetError as error:
            return _error("JUDGE_BUDGET_NOT_EXECUTABLE", str(error))
        except ScoringJobConflict as error:
            return _error("SCORING_JOB_CONFLICT", str(error))
        except JudgeError as error:
            return _error("JUDGE_PROVIDER_UNAVAILABLE", str(error))
        stored = judge.jobs.get(result["job_id"])
        view = judge.public_view(stored) if stored is not None else result
        view["reused"] = result.get("reused")
        view["preflight"] = result.get("preflight")
        print(json.dumps(view, ensure_ascii=False))
        return 0

    if command == "status":
        try:
            print(json.dumps(judge.get_public(args.job_id), ensure_ascii=False))
        except KeyError:
            return _error("JUDGE_NOT_FOUND", f"scoring job not found: {args.job_id}")
        return 0

    if command == "history":
        try:
            service.get_run(args.run)
        except KeyError:
            return _error("RUN_NOT_FOUND", f"run not found: {args.run}")
        print(json.dumps({
            "run_id": args.run, "items": judge.history_public(args.run),
        }, ensure_ascii=False))
        return 0

    if command == "cancel":
        if judge.jobs.get(args.job_id) is None:
            return _error("JUDGE_NOT_FOUND", f"scoring job not found: {args.job_id}")
        try:
            outcome = judge.cancel(args.job_id, actor="cli", reason=args.reason)
        except ScoringJobError as error:
            return _error("SCORING_JOB_CONFLICT", str(error))
        print(json.dumps(outcome, ensure_ascii=False))
        return 0

    return _error("CONTRACT_INVALID", f"unknown judge subcommand: {command}")


# ------------------------------------------------------- M6：实验 / 比较 / 门禁


def _m6_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _write_text(path: str, content: str) -> None:
    target = Path(path)
    if target.parent != Path(""):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _m6_store(args):
    from motte_storage.factory import create_run_store

    return create_run_store(getattr(args, "db", None))


def _comparison_service(args):
    """与 API 同一装配：run store + baseline 仓库（只读消费）。"""
    from motte_sdk.comparisons import ComparisonService

    store = _m6_store(args)
    return ComparisonService(store, baselines=getattr(store, "baselines", None))


def _experiment_service(args):
    from motte_sdk.experiments import ExperimentService
    from motte_sdk.service import RunService

    store = _m6_store(args)
    # 与 API 同一装配：Cell 的 Run 走 prepare_run 解析链（需要资源仓库）。
    return ExperimentService(store, RunService(store), resources=_resources(args))


def _load_m6_document(raw: str, code: str, *, kind=dict):
    """--spec/--policy/--entries 共用：@file 或内联 JSON + 顶层类型校验。"""
    try:
        document = _load_json(raw)
    except (OSError, json.JSONDecodeError) as error:
        return None, f"{code}: 输入不是可解析的 JSON：{error}"
    if not isinstance(document, kind) or (
        kind is dict and not document
    ) or (kind is list and not document):
        return None, f"{code}: 输入必须是{' JSON 对象' if kind is dict else '非空 JSON 数组'}"
    return document, None


def _experiment_command(args) -> int:
    """experiment preview/create/status/cancel/retry-cell。

    preview 纯只读（零创建零调用）；violations 非空按"请求不合法"族退出 2。
    create 只发布 spec 并铺 cell/分配 Run，执行仍在 Worker 的执行锁内。
    server 模式（协议 §3）只覆盖 preview/create/status/cancel；retry-cell 无
    远端入口，local-only。
    """
    from pydantic import ValidationError

    from motte_sdk.experiments import ExperimentError

    command = args.experiment_command
    if command not in ("preview", "create", "status", "cancel", "retry-cell"):
        return _error("CONTRACT_INVALID", f"unknown experiment subcommand: {command}")

    if remote.is_server(args):
        if command not in ("preview", "create", "status", "cancel"):
            return remote.cli_error(
                "LOCAL_ONLY_COMMAND",
                f"experiment {command} has no remote endpoint; run it with --mode local",
            )
        document = None
        if command in ("preview", "create"):
            document, usage_error = _load_m6_document(args.spec, "EXPERIMENT_SPEC_INVALID")
            if usage_error:
                return _error("EXPERIMENT_SPEC_INVALID", usage_error)

        def invoke(client):
            if command == "preview":
                return client.experiment_preview(document)
            if command == "create":
                return client.experiment_create(
                    document, request_key=getattr(args, "request_key", None),
                )
            if command == "status":
                return client.experiment_status(
                    args.experiment_id, getattr(args, "version", None),
                ).raw
            return client.experiment_cancel(
                args.experiment_id, getattr(args, "version", None),
                reason=getattr(args, "reason", None),
            )

        outcome = remote.call_remote(args, invoke, default_code="EXPERIMENT_NOT_FOUND")
        if isinstance(outcome, remote.RemoteOk):
            print(_m6_json(outcome.payload))
            if command == "preview":
                return 2 if outcome.payload.get("violations") else 0
            return 0
        return outcome

    if command in ("preview", "create"):
        document, usage_error = _load_m6_document(
            args.spec, "EXPERIMENT_SPEC_INVALID",
        )
        if usage_error:
            return _error("EXPERIMENT_SPEC_INVALID", usage_error)
        service = _experiment_service(args)
        try:
            if command == "preview":
                view = service.preview(document)
            else:
                outcome = service.create(
                    document, request_key=getattr(args, "request_key", None),
                )
        except ValidationError as error:
            return _error("EXPERIMENT_INVALID", str(error))
        except ExperimentError as error:
            return _error(error.code, error.message)
        except ValueError as error:  # 存储层 cell/spec 内容冲突等
            return _error("EXPERIMENT_INVALID", str(error))
        if command == "preview":
            print(_m6_json(view))
            return 2 if view.get("violations") else 0
        print(_m6_json({key: outcome.get(key) for key in (
            "experiment_id", "version", "created", "allocated",
            "skipped_existing", "failed", "cells",
        )}))
        return 0

    service = _experiment_service(args)
    try:
        if command == "status":
            view = service.status(args.experiment_id, getattr(args, "version", None))
        elif command == "cancel":
            view = service.cancel(
                args.experiment_id, getattr(args, "version", None),
                reason=args.reason,
            )
        else:
            view = service.retry_cell(args.cell_id, reason=args.reason)
    except ExperimentError as error:
        return _error(error.code, error.message)
    except ValueError as error:
        return _error("EXPERIMENT_INVALID", str(error))
    print(_m6_json(view))
    return 0


def _compare_command(args) -> int:
    """compare：三级结论（comparable/partially/not_comparable），只读。

    比较本身成功时退出 0；not_comparable 是"结论"不是崩溃，按协议 §7 退出 5。
    """
    from motte_sdk.comparisons import ComparisonError
    from motte_sdk.export import comparison_to_json

    if args.statistics and args.k < 1:
        return _error("POLICY_INVALID", "k must be a positive integer")
    factors = [item.strip() for item in (args.factors or "model").split(",") if item.strip()]
    if remote.is_server(args):
        def invoke(client):
            payload = client.compare(
                args.baseline, args.candidate,
                factors=",".join(factors),
                baseline_pass=args.baseline_pass, candidate_pass=args.candidate_pass,
            ).raw
            if args.statistics:
                payload["statistics"] = client.compare_statistics(
                    args.baseline, args.candidate, factors=",".join(factors),
                    baseline_pass=args.baseline_pass, candidate_pass=args.candidate_pass,
                    k=args.k,
                )
            return payload

        outcome = remote.call_remote(args, invoke, default_code="RUN_NOT_FOUND")
        if isinstance(outcome, remote.RemoteOk):
            payload = outcome.payload
            if args.json_out:
                _write_text(args.json_out, _m6_json(payload))
            print(_m6_json(payload))
            return 5 if payload.get("level") == "not_comparable" else 0
        return outcome
    service = _comparison_service(args)
    try:
        result = service.compare(
            args.baseline, args.candidate, allowed_factors=factors,
            baseline_pass_id=args.baseline_pass, candidate_pass_id=args.candidate_pass,
        )
    except KeyError as error:
        return _error("RUN_NOT_FOUND", f"run not found: {error}")
    except ComparisonError as error:
        return _error(error.code, str(error))
    except ValueError as error:  # 未知允许因子等政策错误
        return _error("POLICY_INVALID", str(error))
    payload = comparison_to_json({
        "level": result.level.value,
        "eligible": result.eligible,
        "structural_reasons": list(result.structural_reasons),
        "metric_reasons": list(result.metric_reasons),
        "metric_eligibility": dict(result.metric_eligibility),
        "case_diff": {key: list(value) for key, value in result.case_diff.items()},
        "allowed_differences": list(result.allowed_differences),
    })
    # exporter 视图不含聚合 reasons；补上保持"全部事实"完整（协议 §6）。
    payload["reasons"] = list(result.reasons)
    if args.statistics:
        payload["statistics"] = service.paired_statistics(
            args.baseline, args.candidate, allowed_factors=factors,
            baseline_pass_id=args.baseline_pass, candidate_pass_id=args.candidate_pass,
            k=args.k,
        )
    if args.json_out:
        _write_text(args.json_out, _m6_json(payload))
    print(_m6_json(payload))
    return 5 if result.level.value == "not_comparable" else 0


def _baseline_command(args) -> int:
    """baseline create/list/select/default：快照不可变，默认值是指针 CAS 操作。"""
    from motte_storage.baseline_store import BaselineConflict
    from motte_sdk.comparisons import ComparisonError

    command = args.baseline_command
    if command not in ("create", "list", "select", "default"):
        return _error("CONTRACT_INVALID", f"unknown baseline subcommand: {command}")

    if remote.is_server(args):
        invoke = None
        if command == "create":
            entries, usage_error = _load_m6_document(args.entries, "ENTRIES_INVALID", kind=list)
            if usage_error:
                return _error("ENTRIES_INVALID", usage_error)
            if any(not isinstance(item, dict) for item in entries):
                return _error(
                    "ENTRIES_INVALID",
                    '--entries 每项必须是对象：{"cell_key"?, "run_id", "scoring_pass_id"}',
                )
            policy = None
            if args.policy:
                loaded, policy_error = _load_m6_document(
                    args.policy, "COMPARISON_POLICY_INVALID",
                )
                if policy_error:
                    return _error("COMPARISON_POLICY_INVALID", policy_error)
                policy = loaded

            def invoke(client):
                return client.create_baseline(
                    args.id, entries, policy=policy, created_by=args.by,
                    reason=args.reason, source_note=args.source_note,
                ).raw
        elif command == "list":
            def invoke(client):
                items = [snapshot.raw for snapshot in client.list_baselines()]
                return {"items": items, "total": len(items)}
        elif command == "select":
            def invoke(client):
                return client.set_default_baseline(
                    args.scope, args.baseline, updated_by=args.by, reason=args.reason,
                    expected_current=args.expected_current,
                )
        else:
            def invoke(client):
                # 读取指针：client 未暴露便捷方法，仍只经 MotteClient（GET /baselines/default）。
                return client._get(  # noqa: SLF001 - 协议 §3：远端数据只经 SDK client
                    "/api/v1/baselines/default", params={"scope": args.scope},
                )

        outcome = remote.call_remote(args, invoke, default_code="BASELINE_NOT_FOUND")
        if isinstance(outcome, remote.RemoteOk):
            print(_m6_json(outcome.payload))
            return 0
        return outcome

    service = _comparison_service(args)

    if command == "create":
        entries, usage_error = _load_m6_document(args.entries, "ENTRIES_INVALID", kind=list)
        if usage_error:
            return _error("ENTRIES_INVALID", usage_error)
        if any(not isinstance(item, dict) for item in entries):
            return _error(
                "ENTRIES_INVALID",
                '--entries 每项必须是对象：{"cell_key"?, "run_id", "scoring_pass_id"}',
            )
        policy = {"allowed_factors": ["model"]}
        if args.policy:
            loaded, policy_error = _load_m6_document(
                args.policy, "COMPARISON_POLICY_INVALID",
            )
            if policy_error:
                return _error("COMPARISON_POLICY_INVALID", policy_error)
            policy = loaded
        try:
            stored = service.create_baseline(
                args.id, entries, policy=policy, created_by=args.by,
                reason=args.reason, source_note=args.source_note,
            )
        except ComparisonError as error:
            return _error(error.code, str(error))
        except BaselineConflict as error:
            return _error(error.code, str(error))
        except ValueError as error:
            return _error("CONTRACT_INVALID", str(error))
        print(_m6_json(stored))
        return 0

    if command == "list":
        items = service.list_baselines()
        print(_m6_json({"items": items, "total": len(items)}))
        return 0

    if command == "select":
        try:
            pointer = service.set_default_baseline(
                args.scope, args.baseline, updated_by=args.by, reason=args.reason,
                expected_current=args.expected_current,
            )
        except ComparisonError as error:
            return _error(error.code, str(error))
        except BaselineConflict as error:
            return _error(error.code, str(error))
        except ValueError as error:
            return _error("CONTRACT_INVALID", str(error))
        print(_m6_json(pointer))
        return 0

    if command == "default":
        print(_m6_json({
            "scope": args.scope,
            "pointer": service.get_default_baseline(args.scope),
        }))
        return 0

    return _error("CONTRACT_INVALID", f"unknown baseline subcommand: {command}")


def _gate_policy_ref(args) -> tuple[tuple[str, str], None] | tuple[None, str]:
    """--policy id@version 或 --policy-id/--version → (policy_id, version)。"""
    policy_ref = getattr(args, "policy", None)
    policy_id = getattr(args, "policy_id", None)
    policy_version = getattr(args, "policy_version", None)
    if policy_ref:
        if policy_id or policy_version:
            return None, "--policy 与 --policy-id/--version 不能同时使用"
        name, _, version = policy_ref.partition("@")
        if not name or not version:
            return None, "--policy 需要 id@version 形式（如 my-policy@1）"
        return (name, version), None
    if not policy_id or not policy_version:
        return None, "需要 --policy id@version，或同时提供 --policy-id 与 --version"
    return (policy_id, policy_version), None


def _gate_command_remote(args, command: str) -> int:
    """gate 的 server 模式：求值/读取/导出都只经 MotteClient（协议 §3）。

    junit 导出端点返回 XML（非 JSON），因此走 get_gate_result + 本地
    result_to_junit 纯函数格式化——仍是零模型/零 Judge 调用。
    """
    from motte_sdk.export import result_to_json, result_to_junit

    if command == "policy-publish":
        document, usage_error = _load_m6_document(args.policy, "GATE_POLICY_INVALID")
        if usage_error:
            return _error("GATE_POLICY_INVALID", usage_error)

        def invoke(client):
            return client.publish_gate_policy(document)
    elif command == "evaluate":
        ref, usage_error = _gate_policy_ref(args)
        if usage_error:
            return _error("POLICY_REF_INVALID", usage_error)

        def invoke(client):
            return client.evaluate_gate_versioned(
                args.run, ref[0], ref[1],
                scoring_pass_id=args.scoring_pass, baseline_id=args.baseline,
            ).raw
    elif command == "result":
        def invoke(client):
            return client.get_gate_result(args.gate_result_id).raw
    else:  # export
        def invoke(client):
            if args.format == "junit":
                stored = client.get_gate_result(args.result).raw
                return ("junit", result_to_junit(stored))
            return ("json", client.export_gate(args.result, format="json"))

    outcome = remote.call_remote(args, invoke, default_code="GATE_RESULT_NOT_FOUND")
    if not isinstance(outcome, remote.RemoteOk):
        return outcome
    payload = outcome.payload

    if command == "policy-publish":
        print(_m6_json(payload))
        return 0
    if command == "evaluate":
        if args.json_out:
            _write_text(args.json_out, _m6_json(result_to_json(payload)))
        if args.junit_out:
            _write_text(args.junit_out, result_to_junit(payload) + "\n")
        print(_m6_json(payload))
        exit_code = payload.get("exit_code")
        if exit_code is None:
            from motte_eval.gates import gate_exit_code

            exit_code = gate_exit_code(payload.get("decision"))
        return int(exit_code)
    if command == "result":
        print(_m6_json(payload))
        return 0
    kind, content = payload
    formatted = content + "\n" if kind == "junit" else _m6_json(content)
    if args.out:
        _write_text(args.out, formatted)
    else:
        sys.stdout.write(formatted)
    return 0


def _gate_command(args) -> int:
    """gate policy-publish/evaluate/result/export（协议 §6–§7）。

    evaluate 只求值固定引用：退出码 = 决策映射（pass=0 / quality_fail=1 /
    execution_error=3 / insufficient|not_comparable=5 / safety_block=6）；
    输入/配置不合法在求值前拒绝并退出 2。
    """
    from motte_sdk.comparisons import ComparisonError
    from motte_sdk.export import result_to_json, result_to_junit

    command = args.gate_command
    if command not in ("policy-publish", "evaluate", "result", "export"):
        return _error("CONTRACT_INVALID", f"unknown gate subcommand: {command}")

    if remote.is_server(args):
        return _gate_command_remote(args, command)

    service = _comparison_service(args)

    if command == "policy-publish":
        document, usage_error = _load_m6_document(args.policy, "GATE_POLICY_INVALID")
        if usage_error:
            return _error("GATE_POLICY_INVALID", usage_error)
        try:
            stored = service.publish_gate_policy(document)
        except ComparisonError as error:
            return _error(error.code, str(error))
        except ValueError as error:  # 同 id@version 异内容等存储冲突
            return _error("GATE_POLICY_INVALID", str(error))
        print(_m6_json(stored))
        return 0

    if command == "evaluate":
        ref, usage_error = _gate_policy_ref(args)
        if usage_error:
            return _error("POLICY_REF_INVALID", usage_error)
        try:
            result = service.evaluate_gate_versioned(
                policy_id=ref[0], policy_version=ref[1], run_id=args.run,
                scoring_pass_id=args.scoring_pass, baseline_id=args.baseline,
            )
        except KeyError as error:
            return _error("RUN_NOT_FOUND", f"run not found: {error}")
        except ComparisonError as error:
            return _error(error.code, str(error))
        except ValueError as error:
            return _error("GATE_EVALUATE_FAILED", str(error))
        if args.json_out:
            _write_text(args.json_out, _m6_json(result_to_json(result)))
        if args.junit_out:
            _write_text(args.junit_out, result_to_junit(result) + "\n")
        print(_m6_json(result))
        exit_code = result.get("exit_code")
        if exit_code is None:
            from motte_eval.gates import gate_exit_code

            exit_code = gate_exit_code(result.get("decision"))
        return int(exit_code)

    gate_store = getattr(service.store, "gate_store", None)
    if gate_store is None:
        return _error("GATE_STORE_MISSING", "this store has no gate store wired")
    gate_result_id = (
        args.gate_result_id if command == "result" else args.result
    )
    stored = gate_store.get_result(gate_result_id)
    if stored is None:
        return _error(
            "GATE_RESULT_NOT_FOUND", f"gate result not found: {gate_result_id}",
        )
    if command == "result":
        print(_m6_json(stored))
        return 0
    if args.format == "junit":
        content = result_to_junit(stored) + "\n"
    else:
        content = _m6_json(result_to_json(stored))
    if args.out:
        _write_text(args.out, content)
    else:
        sys.stdout.write(content)
    return 0


def _regression_command(args) -> int:
    """regression：两份固定报告的逐 case 回归分类（A10/G14，只读）。"""
    from motte_sdk.comparisons import ComparisonError

    if remote.is_server(args):
        def invoke(client):
            return client.classify_regression(
                args.baseline, args.candidate,
                baseline_pass=args.baseline_pass, candidate_pass=args.candidate_pass,
            )

        outcome = remote.call_remote(args, invoke, default_code="RUN_NOT_FOUND")
        if isinstance(outcome, remote.RemoteOk):
            report = outcome.payload
            classification = report.get("classification") or {}
            report["new_failure_cases"] = sorted(
                case_id for case_id, result in classification.items()
                if result == "new_failure"
            )
            report["fixed_cases"] = sorted(
                case_id for case_id, result in classification.items()
                if result == "fixed"
            )
            print(_m6_json(report))
            return 0
        return outcome
    service = _comparison_service(args)
    try:
        report = service.classify_regression(
            args.baseline, args.candidate,
            baseline_pass_id=args.baseline_pass, candidate_pass_id=args.candidate_pass,
        )
    except KeyError as error:
        return _error("RUN_NOT_FOUND", f"run not found: {error}")
    except ComparisonError as error:
        return _error(error.code, str(error))
    classification = report.get("classification") or {}
    report["new_failure_cases"] = sorted(
        case_id for case_id, outcome in classification.items()
        if outcome == "new_failure"
    )
    report["fixed_cases"] = sorted(
        case_id for case_id, outcome in classification.items()
        if outcome == "fixed"
    )
    print(_m6_json(report))
    return 0


#: 与 apps/api/app/main.py 的 BUILTIN_SCENARIOS 同一集合（SCENARIO_NOT_FOUND parity）。
_BUILTIN_SCENARIOS = {"replay@1", "json_extract@1", "direct-llm@1", "vision@1"}


def _scenario_missing(scenario: str, resources) -> str | None:
    """镜像 API 的场景存在性预检：local/server 同输入产出同错误码（协议 §3）。"""
    if scenario in _BUILTIN_SCENARIOS:
        return None
    try:
        scenario_name, scenario_version = scenario.rsplit("@", 1)
    except ValueError:
        scenario_name, scenario_version = scenario, ""
    if (
        not scenario_name
        or resources.scenarios.get(scenario_name, scenario_version) is None
    ):
        return f"scenario not found: {scenario}"
    return None


def _run_create_remote(args) -> int:
    """`motte run` 的 server 模式：POST /runs（可选 request_key 幂等，协议 §1.3）。"""
    try:
        spec = _load_json(args.spec)
    except (OSError, json.JSONDecodeError) as error:
        return remote.cli_error("RUN_SPEC_INVALID", f"--spec 不是可解析的 JSON：{error}")
    if not isinstance(spec, dict):
        return remote.cli_error(
            "RUN_SPEC_INVALID", "--spec 必须是 JSON 对象：{scenario_version, manifest, case_ids}",
        )

    def invoke(client):
        return client.create_run(
            spec.get("scenario_version", "default@1"),
            spec.get("manifest") or {},
            spec.get("case_ids") or [],
            request_key=getattr(args, "request_key", None),
        ).raw

    outcome = remote.call_remote(args, invoke)
    if isinstance(outcome, remote.RemoteOk):
        print(json.dumps(outcome.payload, ensure_ascii=False))
        return 0
    return outcome


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        remote.resolved_mode(args)
    except ValueError as error:
        return remote.cli_error("MODE_CONFIG_INVALID", str(error))

    if args.command in (None, "doctor"):
        result = {"status": "ok", "checks": {"python": "ok", "harnesses": _harness_installations()}}
        as_json = getattr(args, "json", False) or getattr(args, "jsonl", False)
        if as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("doctor: ok")
            for name, report in result["checks"]["harnesses"].items():
                if not report.get("installed"):
                    print(f"  {name}: not installed")
                elif "version" in report:
                    print(f"  {name}: installed ({report.get('source')}, v{report.get('version')}) @ {report.get('path')}")
                else:
                    print(f"  {name}: installed ({report.get('detail')})")
        return 0

    if args.command == "runtime":
        return _cmd_runtime(args)

    if args.command == "scenario":
        return _scenario_command(args)

    if args.command == "judge":
        return _judge_command(args)

    if args.command == "experiment":
        return _experiment_command(args)
    if args.command == "compare":
        return _compare_command(args)
    if args.command == "baseline":
        return _baseline_command(args)
    if args.command == "gate":
        return _gate_command(args)
    if args.command == "regression":
        return _regression_command(args)

    if args.command == "skill":
        return _skill_command(args)
    if args.command == "fixture":
        return _fixture_command(args)
    if args.command == "workflow":
        return _workflow_command(args)

    # M7：run 生命周期子命令与运维命令（local/server 路由见 motte_cli.runops / ops）。
    if args.command in (
        "run-list", "run-get", "run-cancel", "run-retry",
        "run-events", "run-wait", "run-report",
    ):
        return {
            "run-list": runops.run_list_command,
            "run-get": runops.run_get_command,
            "run-cancel": runops.run_cancel_command,
            "run-retry": runops.run_retry_command,
            "run-events": runops.run_events_command,
            "run-wait": runops.run_wait_command,
            "run-report": runops.run_report_command,
        }[args.command](args)
    if args.command == "backup":
        return ops.backup_command(args)
    if args.command == "restore":
        return ops.restore_command(args)
    if args.command == "restore-guard":
        return ops.restore_guard_command(args)
    if args.command == "maintenance":
        return ops.maintenance_command(args)
    if args.command == "gc":
        return ops.gc_command(args)
    if args.command == "import":
        return ops.import_command(args)

    if args.command == "inspect-import":
        from pathlib import Path as _Path

        from motte_harness.inspect import InspectLogError
        from motte_sdk.inspect_import import import_inspect_run

        content = _Path(args.file).read_bytes().decode("utf-8")
        try:
            report = import_inspect_run(_service(args), content, name=args.name)
        except InspectLogError as error:
            print(f"inspect-import: {error.code}: {error}")
            return 2
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False))
        else:
            print(
                f"inspect-import: {report['import_id']} samples={report['sample_count']} "
                f"schema={report['schema']} idempotent={report['idempotent']}"
            )
        return 0

    if args.command == "run":
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        if remote.is_server(args):
            return _run_create_remote(args)
        try:
            spec = _load_json(args.spec)
        except (OSError, json.JSONDecodeError) as error:
            return remote.cli_error("RUN_SPEC_INVALID", f"invalid run spec: {error}")
        if not isinstance(spec, dict):
            return remote.cli_error("RUN_SPEC_INVALID", "run spec must be a JSON object")
        service = _service(args)
        requested_manifest = spec.get("manifest", {})
        manifest = requested_manifest
        missing_scenario = _scenario_missing(
            spec.get("scenario_version", "default@1"), _resources(args))
        if missing_scenario is not None:
            print(json.dumps({"error": {"code": "SCENARIO_NOT_FOUND",
                                        "message": missing_scenario}},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        try:
            manifest, case_ids = prepare_run(spec.get("scenario_version", "default@1"), manifest, spec.get("case_ids", []), _resources(args))
        except ManifestResolutionError as error:
            print(json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False), file=sys.stderr)
            return 2
        request_key = getattr(args, "request_key", None)
        if request_key:
            # local 幂等创建（协议 §1.3）：与 API 同一 canonical-hash + 注册表语义。
            from motte_cli.idempotent_create import create_run_idempotent
            from motte_storage.platform import RequestConflict

            try:
                run = create_run_idempotent(
                    service,
                    scenario_version=spec.get("scenario_version", "default@1"),
                    manifest=manifest,
                    case_ids=case_ids,
                    requested_manifest=requested_manifest,
                    raw_case_ids=spec.get("case_ids", []),
                    request_key=request_key,
                )
            except RequestConflict as error:
                return _error(getattr(error, "code", "REQUEST_KEY_CONFLICT"), str(error))
        else:
            run = service.create_run(
                spec.get("scenario_version", "default@1"),
                manifest,
                case_ids,
                requested_manifest=requested_manifest,
            )
        print(json.dumps(run, ensure_ascii=False))
        return 0

    if args.command == "replay":
        fixture = _load_json(args.fixture)
        from motte_sdk.dispatcher import RunDispatcher
        from motte_sdk.execution_lock import WorkerAlreadyRunning, worker_execution_lock
        from motte_sdk.resolve import find_secret_paths

        if find_secret_paths(fixture):
            return _error("CREDENTIALS_REJECTED", "replay fixtures cannot contain credential fields")
        if not isinstance(fixture, dict) or not fixture or any(
            not isinstance(case_id, str) or not case_id
            or not isinstance(item, dict) or "output" not in item
            for case_id, item in fixture.items()
        ):
            return _error(
                "REPLAY_FIXTURE_INVALID",
                "replay fixture must contain object cases with output fields",
            )
        service = _service(args)
        manifest = {
            "provider": {"kind": "replay", "fixture": fixture},
            "execution": {
                "backend_id": "replay",
                "backend_version": "1",
                "capabilities": {"interactive": False, "safe_to_repeat": True},
            },
        }
        try:
            with worker_execution_lock(args.db):
                run = service.create_run(
                    args.scenario, manifest, case_ids=list(fixture), requested_manifest=manifest
                )
                result = RunDispatcher(service).dispatch(run["id"])
        except WorkerAlreadyRunning as error:
            return _error("EXECUTOR_LOCKED", str(error))
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "live-smoke":
        from motte_cli.smoke import execute_smoke, record_live_smoke, resolve_api_key
        from motte_provider.config import smoke_kinds
        from motte_provider.pricing import parse_price_table

        kind = args.provider.replace("-", "_")
        if kind not in smoke_kinds():
            print(f"--provider 仅支持：{', '.join(smoke_kinds())}", file=sys.stderr)
            return 2
        api_key = resolve_api_key(args.api_key_env, profile=args.credentials)
        price_table = parse_price_table(_load_json(args.price_table)) if args.price_table else None
        report, exit_code = execute_smoke(
            args.provider,
            args.model,
            base_url=args.base_url,
            api_key=api_key,
            prompt=args.prompt,
            price_table=price_table,
            timeout=args.timeout,
            max_retries=args.max_retries,
        )
        if args.report:
            with open(args.report, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
        if args.record:
            record_live_smoke(report, args.record)
        print(json.dumps(report, ensure_ascii=False))
        return exit_code

    if args.command == "terminal-bench":
        return _handle_terminal_bench(args)

    if args.command == "ceval":
        return _handle_ceval(args)

    if args.command == "cmmlu":
        return _handle_external_benchmark(args, "cmmlu")

    if args.command == "benchmark":
        from motte_sdk.benchmark import import_benchmark_split
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        if args.benchmark_command in ("import", "download"):
            revision = (args.revision or "").strip()
            if args.benchmark_command == "download":
                from motte_sdk.gsm8k_source import (SourceUnavailable, fetch_official_jsonl,
                                                    latest_revision, store_source_file)

                try:
                    revision = revision or latest_revision(split=args.split)
                    raw = fetch_official_jsonl(revision, split=args.split)
                except ValueError as error:
                    print(json.dumps({"error": {"code": "CONTRACT_INVALID", "message": str(error)}},
                                     ensure_ascii=False), file=sys.stderr)
                    return 2
                except SourceUnavailable as error:
                    print(json.dumps({"error": {"code": "SOURCE_UNAVAILABLE", "message": str(error)}},
                                     ensure_ascii=False), file=sys.stderr)
                    return 2
                saved = store_source_file(raw, revision=revision, split=args.split,
                                          directory=args.source_dir)
                print(f"已下载 {args.split} split @ {revision[:7]}（{len(raw)} 字节）→ {saved}",
                      file=sys.stderr)
            else:
                with open(args.file, "rb") as handle:
                    raw = handle.read()
            from motte_storage.resource_store import ResourceConflictError

            try:
                receipt = import_benchmark_split(
                    raw, name=args.name, version=args.version, revision=revision,
                    license_id=args.license_id, scope=args.scope, resources=_resources(args),
                    synthetic=getattr(args, "synthetic", False),
                )
            except ValueError as error:
                code = "RESOURCE_CONFLICT" if isinstance(error, ResourceConflictError) else "CONTRACT_INVALID"
                print(json.dumps({"error": {"code": code, "message": str(error)}},
                                 ensure_ascii=False), file=sys.stderr)
                return 2
            print(json.dumps(receipt, ensure_ascii=False))
            return 0
        name, _, version = args.scenario.rpartition("@")
        scenario = _resources(args).scenarios.get(name, version)
        if scenario is None:
            print(json.dumps({"error": {"code": "SCENARIO_NOT_FOUND",
                                        "message": f"scenario not found: {args.scenario}"}},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        if args.model:
            requested = {"model": args.model}
        else:
            provider_ref = _load_json(args.provider) if args.provider.startswith(("{", "@")) else args.provider
            requested = {"provider": provider_ref}
        try:
            if args.case_ids is not None:
                ids = [item.strip() for item in args.case_ids.replace("，", ",").split(",") if item.strip()]
                if not ids:
                    raise ValueError("--case-ids 不能为空")
                requested["case_selection"] = {"mode": "ids", "case_ids": ids}
            elif args.random_count is not None:
                requested["case_selection"] = {"mode": "random", "count": args.random_count}
                if args.seed:
                    requested["case_selection"]["seed"] = args.seed
            elif args.seed:
                raise ValueError("--seed 只与 --random 搭配使用")
            if args.reasoning_level:
                requested["reasoning_level"] = args.reasoning_level
            manifest, case_ids = prepare_run(args.scenario, requested, [], _resources(args))
        except ManifestResolutionError as error:
            print(json.dumps({"error": {"code": error.code, "message": str(error)}},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        except ValueError as error:
            print(json.dumps({"error": {"code": "CONTRACT_INVALID", "message": str(error)}},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        run = _service(args).create_run(
            args.scenario, manifest, case_ids, requested_manifest=requested
        )
        print(json.dumps(run, ensure_ascii=False))
        return 0

    if args.command == "agent-tasks":
        return _agent_tasks_command(args)
    if args.command == "direct-llm":
        return _direct_llm_command(args)

    if args.command == "credentials":
        from motte_provider import credentials as store

        if args.credentials_command == "set":
            import getpass

            key = getpass.getpass(f"api key for {args.profile}: ").strip()
            if not key:
                print("空密钥，未写入", file=sys.stderr)
                return 2
            path = store.save_api_key(args.profile, key)
            print(f"已写入 {path}（0600）")
            return 0
        if args.credentials_command == "list":
            profiles = store.load_credentials()
            if not profiles:
                print(f"（无凭据；文件位置：{store.credentials_path()}）")
            for name in sorted(profiles):
                key = profiles[name].get("api_key")
                print(f"{name}  {store.mask(key) if isinstance(key, str) and key else '未配置'}")
            return 0
        if not store.remove_profile(args.profile):
            print(f"profile 不存在：{args.profile}", file=sys.stderr)
            return 1
        print(f"已删除 profile {args.profile}")
        return 0

    if args.command == "cleanup-artifacts":
        # local-only（涉及宿主文件；协议 §3）；M7 起优先使用 motte gc。
        blocked = remote.local_only_error(args, "cleanup-artifacts")
        if blocked is not None:
            return blocked
        from motte_storage.maintenance import cleanup_artifacts

        artifacts_root = getattr(args, "artifacts_root", None) or os.environ.get("ARTIFACT_ROOT")
        if not artifacts_root:
            print("cleanup-artifacts 需要 --artifacts-root 或 ARTIFACT_ROOT", file=sys.stderr)
            return 2
        report = cleanup_artifacts(artifacts_root, older_than_days=args.older_than_days, dry_run=not args.apply)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    main()
