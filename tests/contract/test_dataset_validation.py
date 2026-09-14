import hashlib
import pytest
from motte_contracts.dataset import DatasetVersion, validate_jsonl


def test_dataset_jsonl_rejects_duplicate_case_ids():
    with pytest.raises(ValueError, match="duplicate"):
        validate_jsonl('{"case_id":"a","input":"x"}\n{"case_id":"a","input":"y"}\n')


def test_dataset_jsonl_records_hash_and_metadata():
    text = '{"case_id":"a","input":"x","metadata":{"tag":"t"}}\n'
    ds = validate_jsonl(text)
    assert ds.line_count == 1
    assert ds.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert ds.cases[0].metadata["tag"] == "t"
