"""The selected file: the host writes its cell-relative path to
``.collrun/selected_file``; the run reads it as ``gen.selected_file`` and
reads the content itself (GN-01)."""
import os

import pytest

import gen
from collimate import native
from collimate._host.bridge import HostBridge, _read_selected_file
from collimate.lint import lint_generator_manifest_data
from collimate.testing import make_test_bridge, run_generator, run_generator_callable


def test_host_bridge_reads_the_selected_file_beside_the_prompt_GN01(tmp_path):
    run_dir = tmp_path / ".collrun"
    run_dir.mkdir()
    assert _read_selected_file(str(run_dir)) is None
    (run_dir / "selected_file").write_text("notes/draft.md", encoding="utf-8")
    assert _read_selected_file(str(run_dir)) == "notes/draft.md"
    (run_dir / "selected_file").write_text("", encoding="utf-8")
    assert _read_selected_file(str(run_dir)) is None


def test_a_hosted_bridge_exposes_the_selected_file_GN01(tmp_path, monkeypatch):
    run_dir = tmp_path / ".collrun"
    run_dir.mkdir()
    (run_dir / "file_revisions.json").write_text("{}")
    (run_dir / "prompt.md").write_text("go", encoding="utf-8")
    (run_dir / "selected_file").write_text("docs/a.md", encoding="utf-8")
    monkeypatch.delenv("COLLIMATE_IPC_PORT", raising=False)
    monkeypatch.delenv("COLLIMATE_RUN_DIR", raising=False)
    monkeypatch.setenv("COLLIMATE_WORKSPACE", str(tmp_path))
    bridge = HostBridge()
    try:
        assert bridge.prompt == "go"
        assert bridge.selected_file == "docs/a.md"
    finally:
        bridge.finish(success=True)


def test_gen_selected_file_is_a_dynamic_attribute_GN01(tmp_path):
    assert "selected_file" in native.DYNAMIC_ATTRS
    bridge = make_test_bridge(tmp_path, selected_file="a/b.txt")
    prev = native.current_bridge()
    native.set_bridge(bridge)
    try:
        assert gen.selected_file == "a/b.txt"
    finally:
        native.set_bridge(prev)
        bridge.close()
    bridge = make_test_bridge(tmp_path)
    native.set_bridge(bridge)
    try:
        assert gen.selected_file is None
    finally:
        native.set_bridge(prev)
        bridge.close()


def test_the_harness_seeds_and_reads_the_selected_file_GN01(tmp_path):
    seen = {}

    def run():
        seen["path"] = gen.selected_file
        seen["text"] = gen.read_file(gen.selected_file)
        seen["file"] = (tmp_path / ".collrun" / "selected_file").read_text(encoding="utf-8")

    result = run_generator_callable(run, inputs={"notes/a.md": "# A\n"},
                                    selected_file="notes/a.md", workspace=tmp_path)
    assert result.ok, result.error
    assert seen == {"path": "notes/a.md", "text": "# A\n", "file": "notes/a.md"}


def test_the_harness_runs_with_no_selection_GN01(tmp_path):
    entry = tmp_path / "gen.py"
    entry.write_text(
        "import gen\n"
        "def run():\n"
        "    gen.update_file('out.txt', repr(gen.selected_file))\n"
        "if __name__ == '__main__':\n"
        "    gen.main(run)\n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = run_generator(entry, workspace=workspace)
    assert result.ok, result.error
    assert result.output_text("out.txt") == "None"
    assert not os.path.exists(workspace / ".collrun" / "selected_file")


def test_forwarded_children_receive_the_selected_file_GN01(native_bridge):
    from pathlib import Path
    from collimate.testing.forward import Forwarder, prepare_child

    root = Path(native_bridge.workspace)
    (root / "pkg").mkdir()
    (root / "pkg/demo.generator").write_text("entrypoint: main.py\n")
    (root / "pkg/main.py").write_text("pass\n")
    relay = Forwarder(native_bridge)
    relay.port = 1234
    child = prepare_child(relay, "pkg/demo.generator", selected_file="notes/a.md")
    try:
        assert Path(child.host_run_dir, "selected_file").read_text() == "notes/a.md"
    finally:
        child.close()
    child = prepare_child(relay, "pkg/demo.generator")
    try:
        assert not Path(child.host_run_dir, "selected_file").exists()
    finally:
        child.close()
    with pytest.raises(ValueError):
        prepare_child(relay, "pkg/demo.generator", selected_file="_runs/x-00000000/a.chat")


def _manifest(**extra):
    return {"id": "g", "name": "G", "summary": "s", "entrypoint": "main.py",
            "main_config": "settings.set_yaml", **extra}


_SETTINGS = ("fields:\n"
             "  - {key: first, type: checkbox, default: false}\n"
             "  - {key: mode, type: dropdown, options: [a, b], default: a}\n"
             "  - {key: note, type: text, default: x}\n")


def _codes(manifest):
    return {i.code for i in lint_generator_manifest_data(
        manifest, files_present={"main.py", "settings.set_yaml"},
        file_contents={"settings.set_yaml": _SETTINGS})}


def test_lint_takes_selected_file_keys_GN01():
    assert _codes(_manifest(takes_selected_file=True)) == set()
    assert "bad-type" in _codes(_manifest(takes_selected_file="yes"))
    assert _codes(_manifest(takes_selected_file_if={"first": True})) == set()
    assert _codes(_manifest(takes_selected_file_if={"not": {"mode": "b"}})) == set()
    assert "bad-type" in _codes(_manifest(takes_selected_file_if={}))
    assert "manifest-condition-unknown-field" in _codes(_manifest(takes_selected_file_if={"absent": True}))
    assert "manifest-condition-referent-type" in _codes(_manifest(takes_selected_file_if={"note": "x"}))
    assert "manifest-condition-value" in _codes(_manifest(takes_selected_file_if={"mode": "c"}))
    assert "manifest-condition-redundant" in _codes(
        _manifest(takes_selected_file=True, takes_selected_file_if={"first": True}))

