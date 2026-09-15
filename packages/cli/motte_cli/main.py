"""`python -m motte_cli` 入口：doctor / run / replay / live-smoke。"""
import argparse
import asyncio
import json


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

    smoke = sub.add_parser("live-smoke", help="显式发起一次真实 Provider 调用（会产生费用）")
    smoke.add_argument("--provider", required=True, choices=["openai-compatible"])
    smoke.add_argument("--model", required=True)
    smoke.add_argument("--base-url", required=True)
    smoke.add_argument("--api-key-env", default="OPENAI_API_KEY")
    smoke.add_argument("--prompt", default="Reply with the single word: ok")
    smoke.add_argument("--price-table", help="价格表 JSON 或 @文件：{version, input_per_million, output_per_million}")
    smoke.add_argument("--report", help="把脱敏报告写入 JSON 文件")
    smoke.add_argument("--record", help="把结果小节追加到 markdown 记录（如 docs/operations/live-smoke-log.md）")
    smoke.add_argument("--timeout", type=float, default=30.0)
    smoke.add_argument("--max-retries", type=int, default=2)
    return parser


def _service(args):
    from motte_sdk.service import RunService, build_run_service
    from motte_storage.run_store import SQLiteRunStore

    return RunService(SQLiteRunStore(args.db)) if args.db else build_run_service()


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
        spec = _load_json(args.spec)
        service = _service(args)
        run = service.create_run(
            spec.get("scenario_version", "default@1"),
            spec.get("manifest", {}),
            spec.get("case_ids", []),
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
        from motte_provider.pricing import parse_price_table

        api_key = resolve_api_key(args.api_key_env)
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

    return 0


if __name__ == "__main__":
    main()
