"""Actual Inspect runner output with its official mock model; never a live-model test."""
import hashlib
import json
from pathlib import Path

from motte_harness.inspect import load_import_raw
from motte_sdk.inspect_import import import_inspect_run
from motte_sdk.service import RunService
from motte_storage.run_store import SQLiteRunStore

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'harness'
SOURCE = FIXTURES / 'inspect-ai-0.3.266-native-mock.json'
PROVENANCE = FIXTURES / 'inspect-ai-0.3.266-native-mock.provenance.json'
SOURCE_SHA256 = '9dec3bb4c91175688bd5f93e066232da81cfdc321999a6753c2febb756479aea'


def test_native_mock_fixture_persists_reopens_and_reimports_exactly(tmp_path, monkeypatch):
    raw = SOURCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256
    provenance = json.loads(PROVENANCE.read_text(encoding='utf-8'))
    assert provenance['source_sha256'] == SOURCE_SHA256
    assert provenance['source_bytes'] == len(raw) == 24546
    assert provenance['version'] == '0.3.266'
    assert provenance['capture_kind'] == 'actual-native-output'
    assert provenance['model_kind'] == 'official-mockllm'
    assert provenance['live_model'] is False
    assert provenance['external_model_calls'] == 0
    for field in ('original_capture_script', 'reproduction_script'):
        script = FIXTURES / provenance[field]
        assert hashlib.sha256(script.read_bytes()).hexdigest() == provenance[field + '_sha256']
    original = json.loads(raw)
    assert original['eval']['model'] == 'mockllm/model'
    assert original['eval']['metadata']['live_model'] is False

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    database = tmp_path / 'import.db'
    service = RunService(SQLiteRunStore(database))
    first = import_inspect_run(service, raw.decode('utf-8'), name='actual-native-mock')
    run_id = first['run_id']
    assert first['sample_count'] == 2
    assert first['source_identity'] == {
        key: original['eval'][key] for key in ('eval_id', 'run_id')}

    reopened = RunService(SQLiteRunStore(database))
    run = reopened.get_run(run_id)
    assert run['status'] == 'completed'
    assert len(reopened.store.case_runs.list_for_run(run_id)) == 2
    scoring = reopened.store.scoring_passes.current(run_id)
    assert scoring['id'] == first['scoring_pass_id']
    assert scoring['source'] == 'inspect-native'
    scores = reopened.store.score_sets.list_for_pass(scoring['id'])
    assert len(scores) == 2
    assert sorted(score['value'] for score in scores) == [0.0, 1.0]
    assert {score['metric_id'] for score in scores} == {'native.deterministic_exact'}
    assert all(score['metric_status'] == 'scored' for score in scores)
    again = import_inspect_run(reopened, raw.decode('utf-8'))
    assert again['idempotent'] is True
    assert again['run_id'] == run_id
    assert again['scoring_pass_id'] == scoring['id']
    assert reopened.store.attempts.list_for_run(run_id) == []
    assert len(reopened.store.runs.list()) == 1
    assert load_import_raw(first['import_id']).encode('utf-8') == raw
    assert Path(first['raw_frozen']).read_bytes() == raw
    source_artifact = tmp_path / 'artifacts' / 'imports' / first['import_id'] / 'source.json'
    assert source_artifact.read_bytes() == raw
