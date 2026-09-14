import argparse, json


def _load_fixture(raw: str) -> dict:
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(raw)


def main(argv=None):
    p = argparse.ArgumentParser(prog="motte")
    p.add_argument("command", nargs="?", default="doctor", choices=["doctor", "replay"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--jsonl", action="store_true")
    p.add_argument("--scenario", default="replay@1", help="replay 场景版本标识")
    p.add_argument("--fixture", help="replay fixture：JSON 字符串或 @文件路径")
    p.add_argument("--db", help="SQLite 路径，默认 MOTTE_DB_PATH（var/runs.db）")
    args = p.parse_args(argv)

    if args.command == "doctor":
        result = {"status": "ok", "checks": {"python": "ok"}}
        print(json.dumps(result) if args.json or args.jsonl else "doctor: ok")
        return 0

    if args.command == "replay":
        if not args.fixture:
            p.error("--fixture is required for replay")
        fixture = _load_fixture(args.fixture)

        from motte_sdk.replay_run import ReplayProvider
        from motte_sdk.service import RunService, build_run_service
        from motte_storage.run_store import SQLiteRunStore

        service = RunService(SQLiteRunStore(args.db)) if args.db else build_run_service()
        run = service.create_run(args.scenario, {}, case_ids=list(fixture))
        result = service.execute(run["id"], provider=ReplayProvider(fixture).invoke)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return 0


if __name__ == "__main__":
    main()
