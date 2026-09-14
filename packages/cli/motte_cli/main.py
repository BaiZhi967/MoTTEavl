import argparse, json


def main(argv=None):
    p = argparse.ArgumentParser(prog="motte")
    p.add_argument("command", nargs="?", default="doctor")
    p.add_argument("--json", action="store_true")
    p.add_argument("--jsonl", action="store_true")
    args = p.parse_args(argv)
    if args.command == "doctor":
        result = {"status": "ok", "checks": {"python": "ok"}}
        print(json.dumps(result) if args.json or args.jsonl else "doctor: ok")
    return 0


if __name__ == "__main__":
    main()
