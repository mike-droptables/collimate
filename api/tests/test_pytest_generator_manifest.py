"""The one generator-manifest reader (``read_generator_manifest``) shared by
the run process (``ProcessManager._load_manifest``): a missing, malformed or non-mapping manifest reads as ``{}``."""
from process_manager.manager import read_generator_manifest


def test_missing_manifest_reads_empty(tmp_path):
    assert read_generator_manifest(str(tmp_path)) == {}


def test_list_manifest_reads_empty(tmp_path):
    (tmp_path / "choice.generator").write_text("- a\n- b\n", encoding="utf-8")
    assert read_generator_manifest(str(tmp_path)) == {}


def test_malformed_manifest_reads_empty(tmp_path):
    (tmp_path / "choice.generator").write_text("id: [unclosed\n", encoding="utf-8")
    assert read_generator_manifest(str(tmp_path)) == {}


def test_mapping_manifest_reads_as_dict(tmp_path):
    (tmp_path / "choice.generator").write_text("id: g\nentrypoint: g.py\n", encoding="utf-8")
    assert read_generator_manifest(str(tmp_path)) == {"id": "g", "entrypoint": "g.py"}
