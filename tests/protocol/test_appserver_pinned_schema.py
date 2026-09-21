import hashlib
import json
from pathlib import Path

from jsonschema import Draft7Validator


def test_synthetic_transcript_validates_against_generated_pinned_schema():
    root = Path(__file__).resolve().parents[1] / 'fixtures' / 'harness'
    data = (root / 'codex-app-server-0.155.1.schema.json').read_bytes()
    schema = json.loads(data)
    provenance = json.loads((root / 'codex-app-server-0.155.1.provenance.json').read_text())
    assert hashlib.sha256(data).hexdigest() == provenance['subset_sha256']
    assert provenance['live_capture'] is False
    for line in (root / 'codex-app-server-rpc-v2.jsonl').read_text().splitlines():
        frame = json.loads(line)
        Draft7Validator({**schema, '$ref': '#/definitions/' + frame['schema']}).validate(frame['payload'])
