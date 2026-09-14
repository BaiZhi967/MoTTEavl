import json


def parse_jsonl(text):
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"error": "malformed_line", "raw": line})
    return out
