"""Evidence references prove exact source nodes, never execution success by themselves."""
import pytest
from scripts.build_m8_evidence_index import validate_evidence_reference


def test_python_evidence_must_name_real_node(tmp_path):
    root = tmp_path
    (root / "test_sample.py").write_text("def test_real():\n    pass\n")
    assert validate_evidence_reference(root, "test_sample.py::test_real") == "python_node"
    with pytest.raises(ValueError, match="missing test node"):
        validate_evidence_reference(root, "test_sample.py::test_typo")


def test_typescript_label_not_promoted_to_executed_test_node(tmp_path):
    (tmp_path / "view.test.tsx").write_text('describe("ComparePage", () => {});')
    assert validate_evidence_reference(tmp_path, "view.test.tsx::ComparePage") == "source_label"
    with pytest.raises(ValueError, match="missing source label"):
        validate_evidence_reference(tmp_path, "view.test.tsx::MissingPage")


def test_source_file_only_not_promoted_to_node(tmp_path):
    (tmp_path / "source.py").write_text("pass\n")
    assert validate_evidence_reference(tmp_path, "source.py") == "source_file"
