"""`python -m motte_cli` 入口：doctor / run / replay / live-smoke。"""
import argparse
import asyncio
import json
import sys


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
        reports["pi-bridge"] = {
            "name": "pi-bridge",
            "installed": runtime.available(),
            "detail": f"node={runtime._node is not None}, bridge={runtime.bridge_path}",
        }
    except Exception as error:  # agent 包异常时 doctor 不失败
        reports["pi-bridge"] = {"name": "pi-bridge", "installed": False, "error": str(error)}
    return reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motte")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="环境自检")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--jsonl", action="store_true")

    run = sub.add_parser("run", help="创建 queued Run（由 Worker 异步执行）")
    run.add_argument("--spec", required=True, help="run 定义 JSON 或 @文件：{scenario_version, manifest, case_ids}")
    run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")

    replay = sub.add_parser("replay", help="创建并同步执行 replay Run")
    replay.add_argument("--scenario", default="replay@1")
    replay.add_argument("--fixture", required=True, help="replay fixture：JSON 字符串或 @文件路径")
    replay.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    replay.add_argument("--json", action="store_true")

    from motte_provider.config import smoke_kinds

    smoke = sub.add_parser("live-smoke", help="显式发起一次真实 Provider 调用（会产生费用）")
    smoke.add_argument("--provider", required=True, help=f"provider kind：{', '.join(smoke_kinds())}（连字符拼写兼容）")
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

    backup = sub.add_parser("backup", help="在线备份 SQLite 与 artifacts")
    backup.add_argument("--target", required=True, help="备份目录")
    backup.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    backup.add_argument("--artifacts-root", default=None, help="artifact 根目录（默认不备份工件）")

    benchmark = sub.add_parser("benchmark", help="GSM8K-20 冒烟基准（导入数据集 / 创建运行）")
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)
    bench_import = benchmark_sub.add_parser("import", help="导入本地 official-format GSM8K JSONL（question/answer 两列）")
    bench_import.add_argument("--file", required=True, help="official-format test JSONL 路径")
    bench_import.add_argument("--name", required=True)
    bench_import.add_argument("--version", required=True)
    bench_import.add_argument("--revision", required=True, help="官方数据 pinned commit hash")
    bench_import.add_argument("--license", dest="license_id", required=True)
    bench_import.add_argument("--synthetic", action="store_true", help="标记为合成冒烟数据（跳过 commit hash 校验）")
    bench_import.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    bench_run = benchmark_sub.add_parser("run", help="创建 benchmark queued Run（由 Worker 执行）")
    bench_run.add_argument("--scenario", required=True, help="benchmark scenario 引用，如 gsm8k-20-smoke@1")
    selection = bench_run.add_mutually_exclusive_group(required=True)
    selection.add_argument("--provider", help="provider connection name or provider object JSON/@file (not a manifest)")
    selection.add_argument("--model", help="model profile resource id")
    bench_run.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    bench_run.add_argument("--json", action="store_true")


    restore = sub.add_parser("restore", help="从最新备份恢复（先停 API 与 Worker）")
    restore.add_argument("--source", required=True, help="备份目录")
    restore.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH")
    restore.add_argument("--artifacts-root", default=None, help="artifact 根目录（备份含工件时恢复）")

    cleanup = sub.add_parser("cleanup-artifacts", help="按 TTL 清理 artifact（默认 dry-run）")
    cleanup.add_argument("--older-than-days", type=float, required=True)
    cleanup.add_argument("--artifacts-root", default=None, help="artifact 根目录，默认 ARTIFACT_ROOT")
    cleanup.add_argument("--apply", action="store_true", help="真正删除（缺省仅报告）")
    return parser


def _service(args):
    from motte_sdk.service import RunService, build_run_service
    from motte_storage.run_store import SQLiteRunStore

    return RunService(SQLiteRunStore(args.db)) if args.db else build_run_service()


def _resources(args):
    from motte_storage.factory import create_resource_store
    from motte_storage.resource_store import SQLiteResourceStore

    return SQLiteResourceStore(args.db) if args.db else create_resource_store()


def main(argv=None):
    args = _build_parser().parse_args(argv)

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

    if args.command == "run":
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        spec = _load_json(args.spec)
        service = _service(args)
        manifest = spec.get("manifest", {})
        try:
            manifest, case_ids = prepare_run(spec.get("scenario_version", "default@1"), manifest, spec.get("case_ids", []), _resources(args))
        except ManifestResolutionError as error:
            print(json.dumps({"error": {"code": error.code, "message": str(error)}}, ensure_ascii=False), file=sys.stderr)
            return 2
        run = service.create_run(
            spec.get("scenario_version", "default@1"),
            manifest,
            case_ids,
        )
        print(json.dumps(run, ensure_ascii=False))
        return 0

    if args.command == "replay":
        fixture = _load_json(args.fixture)
        from motte_sdk.replay_run import ReplayProvider

        service = _service(args)
        run = service.create_run(args.scenario, {}, case_ids=list(fixture))
        result = service.execute(run["id"], provider=ReplayProvider(fixture).invoke)
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

    if args.command == "benchmark":
        from motte_contracts.gsm8k import import_official_jsonl
        from motte_sdk.resolve import ManifestResolutionError, prepare_run

        if args.benchmark_command == "import":
            with open(args.file, "rb") as handle:
                raw = handle.read()
            record = import_official_jsonl(
                raw, name=args.name, version=args.version, revision=args.revision,
                license_id=args.license_id, synthetic=args.synthetic,
            )
            scenario = {
                "name": f"{args.name}-smoke", "version": args.version,
                "mode": "direct-llm", "benchmark": record["benchmark"],
                "dataset": f"{args.name}@{args.version}",
            }
            resources = _resources(args)
            resources.datasets.put(record)
            resources.scenarios.put(scenario)
            print(json.dumps({"imported": f"{args.name}@{args.version}",
                              "scenario": f"{args.name}-smoke@{args.version}",
                              "cases": len(record["cases"]),
                              "source_sha256": record["provenance"]["source_sha256"],
                              "cases_sha256": record["cases_sha256"]}, ensure_ascii=False))
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
            manifest, case_ids = prepare_run(args.scenario, requested, [], _resources(args))
        except ManifestResolutionError as error:
            print(json.dumps({"error": {"code": error.code, "message": str(error)}},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        run = _service(args).create_run(args.scenario, manifest, case_ids)
        print(json.dumps(run, ensure_ascii=False))
        return 0

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

    if args.command in ("backup", "restore", "cleanup-artifacts"):
        import os

        from motte_storage.maintenance import backup_sqlite, cleanup_artifacts, restore_sqlite

        db_path = args.db or os.environ.get("MOTTE_DB_PATH", "var/runs.db")
        artifacts_root = getattr(args, "artifacts_root", None) or os.environ.get("ARTIFACT_ROOT")

        if args.command == "backup":
            manifest = backup_sqlite(db_path, args.target, artifacts_root=artifacts_root)
            print(json.dumps(manifest, ensure_ascii=False))
            return 0
        if args.command == "restore":
            restored = restore_sqlite(args.source, db_path, artifacts_root=artifacts_root)
            print(json.dumps(restored, ensure_ascii=False))
            return 0
        if not artifacts_root:
            print("cleanup-artifacts 需要 --artifacts-root 或 ARTIFACT_ROOT", file=sys.stderr)
            return 2
        report = cleanup_artifacts(artifacts_root, older_than_days=args.older_than_days, dry_run=not args.apply)
        print(json.dumps(report, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    main()
