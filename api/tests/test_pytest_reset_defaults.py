"""build/reset_defaults.py, the developer reset: a channel's Capabilities
cells that admission recorded as packages of a content tree take that tree's
current copy; everything else in the channel stays.

The channel is created through the app from a synthetic content folder (so
its cells carry real provenance records), the server stopped, and the tool
run against the data dir with ``--source`` naming the folder. Covers:

  * a matched cell whose content changed gets a new version holding the
    content's files (its build runners left out) and its own runners, on its
    own chain, keeping its id, name and slot; its content records are the
    new admission's; a capability cell without provenance, a standard stack,
    the channel's identity and selection are untouched, and a runner in
    another cell still resolves against the replaced generator
  * a content cell the channel never held is not added
  * a fresh channel is current, and a second run changes nothing
  * matching is by provenance only: a renamed cell is matched, a cell whose
    records are gone (an older channel's) is never matched, a cell moved off
    Capabilities is left alone
  * an admission that holds or refuses a package leaves the cell as it was
  * a running server (any launcher) refuses the reset and a reset refuses a
    server, through the data dir's server lock; a crashed holder leaves no
    lock; no data is a no-op; the CLI runs as a script with PYTHONPATH=api
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cell_placement import capabilities_stack, capabilities_stack_cells
from models import Cell, Stack, VirtualFile
from reset_tool import PATH, load
from suggested_trees import create_suggested, folder_hash, use_source

# The tool's functions import the app's modules as they run, so one import
# of it serves every test.
tool = load()

ALPHA_MANIFEST = "id: alpha\nname: Alpha\nsummary: Reset fixture\nentrypoint: alpha.py\nmain_config: settings.set_yaml\n"
ALPHA_SCHEMA = "fields:\n  - key: message\n    type: text\n    default: hello\n"
CONTENT_ALPHA = {"alpha/choice.generator", "alpha/settings.set_yaml", "alpha/alpha.py", "cell.yaml"}


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _runner(rid: str, generator: str = "alpha") -> str:
    """A complete runner definition targeting ``generator``."""
    return yaml.safe_dump({
        "id": rid, "name": rid.title(), "description": "", "tags": [], "generator": generator,
        "prompt": "go", "settings": {"settings.set_yaml": {"message": "hi"}},
        "folders": [], "accounts": {}, "permissions": {}, "packages": {},
    }, sort_keys=False)


def _renderer(rid: str, version: int) -> tuple[str, str]:
    return (f"id: {rid}\nname: {rid.title()}\nsummary: Reset fixture\nentrypoint: main.tsx\nembed: native\n"
            f"extensions: [.{rid}]\n",
            "import { defineRenderer } from '/api/sdk/react.js';\n"
            "const Component = () => null;\n"
            "export default defineRenderer({ component: Component });\n"
            f"// v{version}\n")


def _content(root: Path, *, version: int, gamma: bool = False) -> None:
    """The content tree at ``version``: Alpha (the ``alpha`` generator and a
    build runner) and Widget (the ``widget`` renderer), with Gamma from
    ``gamma``; one suggestion holds Alpha and Widget."""
    alpha = root / "generators" / "alpha_cell"
    _write(alpha / "alpha" / "choice.generator", ALPHA_MANIFEST)
    _write(alpha / "alpha" / "settings.set_yaml", ALPHA_SCHEMA)
    _write(alpha / "alpha" / "alpha.py", f"print('v{version}')\n")
    _write(alpha / "saved" / "shipped" / "preset.runner", _runner("shipped"))
    _write(alpha / "cell.yaml", yaml.safe_dump({"name": "Alpha", "icon": "bot", "order": 10}))
    if gamma:
        cell = root / "generators" / "gamma_cell"
        _write(cell / "gamma" / "choice.generator", ALPHA_MANIFEST.replace("alpha", "gamma"))
        _write(cell / "gamma" / "settings.set_yaml", ALPHA_SCHEMA)
        _write(cell / "gamma" / "gamma.py", "print('gamma')\n")
        _write(cell / "cell.yaml", yaml.safe_dump({"name": "Gamma", "order": 20}))
    manifest, body = _renderer("widget", version)
    _write(root / "renderers" / "widget_cell" / "widget" / "view.renderer", manifest)
    _write(root / "renderers" / "widget_cell" / "widget" / "main.tsx", body)
    _write(root / "renderers" / "widget_cell" / "cell.yaml", yaml.safe_dump({"name": "Widget"}))
    _write(root / "suggested" / "basic.json", json.dumps({
        "name": "Basic", "description": "Reset fixture",
        "generators": [["Alpha", "generators/alpha_cell"]], "renderers": [["Widget", "renderers/widget_cell"]],
        "tutorial": [], "quick_fields": {}}))


def _files(cell) -> dict[str, str]:
    return {f.name: f.content for f in cell.files}


def _named(ch, name):
    return next(c for c in capabilities_stack_cells(ch) if c.name == name)


def _load_state():
    import genesis
    restored = genesis.parse_saved_app_state(genesis.load_global_ui_state())
    genesis.reconcile_restored_cell_files(restored)
    return restored


def _save(state) -> None:
    import genesis
    genesis.save_global_ui_state(genesis.snapshot_app_state(state))


def _records(ws, cell_id) -> list[dict]:
    from suggested_trees import provenance
    return provenance(ws.slug, cell_id)


def _reset(ws, out=None) -> dict:
    return tool.reset_defaults(str(ws.root), source_dir=str(ws.content), out=out or (lambda *_: None))


def _add_capability_cell(ws, state, cell) -> None:
    """Put ``cell`` (never admitted: no provenance) on the saved channel's
    Capabilities stack with its files as its accepted content."""
    from database import get_db_connection
    from persistence import initialize_files
    capabilities_stack(state.channels[0]).cells.append(cell)
    conn = get_db_connection(ws.slug)
    initialize_files(conn, str(cell.id), cell.files)
    conn.commit()
    conn.close()
    _save(state)


@pytest.fixture
def workspace(api_app, fresh_cwd):
    """A stopped data dir with one channel created from the content folder at
    version 1, plus what a user adds on top: a runner of their own inside
    Alpha, a capability cell of their own (no provenance) holding a runner
    that targets Alpha's generator, and a standard stack."""
    import runners
    from fastapi.testclient import TestClient
    from conftest import add_capabilities_cell
    from database import get_db_connection
    from persistence import initialize_files
    from state import flush_persist_state, persist_state, require_channel

    content = fresh_cwd / "content"
    _content(content, version=1)
    with TestClient(api_app) as client:
        use_source(client, content)
        response = create_suggested(client, "test", tutorial=False)
        assert response.status_code == 200, response.text
        slug = response.json()["id"]
        ch = require_channel(slug)
        alpha = _named(ch, "Alpha")
        assert set(_files(alpha)) == CONTENT_ALPHA
        runners.commit_cell_files(ch, alpha, alpha.files + [
            VirtualFile(name="saved/mine/preset.runner", content=_runner("mine"), language="yaml"),
        ], "user runner")
        add_capabilities_cell(ch, Cell(name="Mine", files=[
            VirtualFile(name="saved/keeper/preset.runner", content=_runner("keeper"), language="yaml"),
        ]))
        doc = Cell(name="Doc", files=[VirtualFile(name="a.md", content="hi\n")])
        ch.main_stacks.append(Stack(name="Main", stack_type="standard", cells=[doc]))
        ch.main_selection_id = ch.main_stacks[0].id
        conn = get_db_connection(slug)
        initialize_files(conn, str(doc.id), doc.files)
        conn.commit()
        conn.close()
        persist_state()
        flush_persist_state()
        widget_id = str(_named(ch, "Widget").id)
        alpha_id = str(alpha.id)
    return SimpleNamespace(root=fresh_cwd, content=content, slug=slug, alpha_id=alpha_id, widget_id=widget_id,
                           before=_load_state())


def test_reset_replaces_matched_cells_and_keeps_everything_else(workspace):
    from database import get_db_connection
    from versions import find_tip, tip_file_map
    ws = workspace
    conn = get_db_connection(ws.slug)
    old_tip = find_tip(conn, ws.alpha_id)
    conn.close()
    assert [r["content_hash"] for r in _records(ws, ws.alpha_id)] == [
        folder_hash(ws.content / "generators" / "alpha_cell" / "alpha")]

    _content(ws.content, version=2, gamma=True)
    lines = []
    report = _reset(ws, lines.append)
    assert report == {"test": {"matched": [("Alpha", "replaced"), ("Widget", "replaced")],
                               "unrecorded": 1, "other": 0}}
    assert lines == ["      channel 'test': Capabilities: matched by provenance: 'Alpha' replaced, "
                     "'Widget' replaced; 1 without provenance left as-is"]

    state = _load_state()
    ch = state.channels[0]
    assert str(ch.id) == ws.slug and ch.name == "test"
    assert state.active_channel_id == ws.before.active_channel_id
    assert ch.main_selection_id == ws.before.channels[0].main_selection_id
    assert [s.model_dump(mode="json") for s in ch.main_stacks] == \
        [s.model_dump(mode="json") for s in ws.before.channels[0].main_stacks]
    cells = capabilities_stack_cells(ch)
    # Gamma, never in the channel, is not added.
    assert [c.name for c in cells] == ["Alpha", "Widget", "Mine"]
    alpha = cells[0]
    assert str(alpha.id) == ws.alpha_id and alpha.icon == "bot"
    files = _files(alpha)
    # The content's files (its build runner left out) and the cell's own runner.
    assert set(files) == CONTENT_ALPHA | {"saved/mine/preset.runner"}
    assert files["alpha/alpha.py"] == "print('v2')\n"
    assert cells[2].model_dump(mode="json") == _named(ws.before.channels[0], "Mine").model_dump(mode="json")
    import runners
    catalog = {row["id"]: row for row in runners.build_catalog(ch)}
    assert catalog["keeper"]["cell_id"] == str(cells[2].id) and "invalid" not in catalog["keeper"]
    assert catalog["mine"]["cell_id"] == ws.alpha_id and "shipped" not in catalog
    resolved = runners.resolve_runner_run(ch, "keeper")
    assert (resolved.runner_cell_id, resolved.generator_id) == (str(cells[2].id), "alpha")
    assert _files(cells[1])["widget/main.tsx"].endswith("// v2\n")

    # The content's records are the new admission's; the cell's own runner
    # has none (the runner editor writes none).
    for cell_id, folder in ((ws.alpha_id, ws.content / "generators" / "alpha_cell" / "alpha"),
                            (ws.widget_id, ws.content / "renderers" / "widget_cell" / "widget")):
        (record,) = _records(ws, cell_id)
        assert (record["source_kind"], record["source_id"]) == ("folder", str(ws.content))
        assert record["content_hash"] == folder_hash(folder)

    conn = get_db_connection(ws.slug)
    new_tip = find_tip(conn, ws.alpha_id)
    assert new_tip != old_tip
    edge = conn.execute(
        "SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? "
        "AND parent_key='previous_version' ORDER BY rowid DESC LIMIT 1", (old_tip,),
    ).fetchone()
    assert edge[0] == new_tip
    row = conn.execute("SELECT version_name, generator_name FROM cells WHERE id=?", (new_tip,)).fetchone()
    assert (row[0], row[1]) == (tool.VERSION_LABEL, tool.GENERATOR_NAME)
    assert set(tip_file_map(conn, new_tip)) == set(files)
    conn.close()


def test_a_fresh_channel_is_current_and_a_second_run_changes_nothing(workspace):
    from database import get_db_connection
    ws = workspace
    conn = get_db_connection(ws.slug)
    rows = conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    conn.close()
    assert _reset(ws)["test"]["matched"] == [("Alpha", "current"), ("Widget", "current")]
    assert _load_state().model_dump(mode="json") == ws.before.model_dump(mode="json")

    _content(ws.content, version=2)
    _reset(ws)
    conn = get_db_connection(ws.slug)
    rows = conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    records = conn.execute("SELECT * FROM cell_provenance ORDER BY cell_id, folder").fetchall()
    conn.close()
    once = _load_state().model_dump(mode="json")
    assert _reset(ws)["test"]["matched"] == [("Alpha", "current"), ("Widget", "current")]
    conn = get_db_connection(ws.slug)
    assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == rows
    assert conn.execute("SELECT * FROM cell_provenance ORDER BY cell_id, folder").fetchall() == records
    conn.close()
    assert _load_state().model_dump(mode="json") == once


def test_a_renamed_cell_is_matched_by_its_provenance(workspace):
    ws = workspace
    state = _load_state()
    _named(state.channels[0], "Alpha").name = "Alpha (mine)"
    _save(state)
    _content(ws.content, version=2)
    assert _reset(ws)["test"]["matched"] == [("Alpha (mine)", "replaced"), ("Widget", "replaced")]
    alpha = capabilities_stack_cells(_load_state().channels[0])[0]
    assert (str(alpha.id), alpha.name) == (ws.alpha_id, "Alpha (mine)")
    assert _files(alpha)["alpha/alpha.py"] == "print('v2')\n"


def test_a_cell_without_provenance_is_never_matched(workspace):
    """A cell whose records are gone (as an older channel's cells have
    none) is left as it is, though it holds the content's packages; so is a
    cell of the user's own named like a content cell."""
    from database import get_db_connection
    ws = workspace
    conn = get_db_connection(ws.slug)
    conn.execute("DELETE FROM cell_provenance WHERE cell_id=?", (ws.widget_id,))
    conn.commit()
    conn.close()
    state = _load_state()
    _add_capability_cell(ws, state, Cell(name="Alpha", files=[
        VirtualFile(name="notes.md", content="mine\n", language="markdown")]))
    before = {c.name + str(i): c.model_dump(mode="json")
              for i, c in enumerate(capabilities_stack_cells(_load_state().channels[0]))}

    _content(ws.content, version=2)
    report = _reset(ws)["test"]
    assert report == {"matched": [("Alpha", "replaced")], "unrecorded": 3, "other": 0}
    after = capabilities_stack_cells(_load_state().channels[0])
    assert after[1].model_dump(mode="json") == before["Widget1"]
    assert after[3].model_dump(mode="json") == before["Alpha3"]


def test_a_cell_moved_off_capabilities_is_left_alone(workspace):
    ws = workspace
    state = _load_state()
    ch = state.channels[0]
    alpha = _named(ch, "Alpha")
    capabilities_stack(ch).cells.remove(alpha)
    ch.main_stacks.append(Stack(name="Moved", cells=[alpha]))
    moved = alpha.model_dump(mode="json")
    _save(state)
    _content(ws.content, version=2)
    assert _reset(ws)["test"]["matched"] == [("Widget", "replaced")]
    assert _load_state().channels[0].main_stacks[-1].cells[0].model_dump(mode="json") == moved


def test_a_held_or_refused_admission_leaves_the_cell(workspace):
    """The content's Widget gains a renderer another capability cell
    declares: held, and Widget stays at version 1. A content renderer that
    does not compile is refused the same way."""
    ws = workspace
    state = _load_state()
    manifest, body = _renderer("gadget", 1)
    _add_capability_cell(ws, state, Cell(name="Gadgets", files=[
        VirtualFile(name="gadget/view.renderer", content=manifest, language="yaml"),
        VirtualFile(name="gadget/main.tsx", content=body, language="typescript")]))
    widget = _named(_load_state().channels[0], "Widget").model_dump(mode="json")
    _content(ws.content, version=2)
    _write(ws.content / "renderers" / "widget_cell" / "gadget" / "view.renderer", manifest)
    _write(ws.content / "renderers" / "widget_cell" / "gadget" / "main.tsx", body)
    lines = []
    report = _reset(ws, lines.append)["test"]
    assert report["matched"][1] == (
        "Widget", "held: Definition id already exists elsewhere in Capabilities; rename first")
    assert _named(_load_state().channels[0], "Widget").model_dump(mode="json") == widget

    for gadget in (ws.content / "renderers" / "widget_cell" / "gadget").iterdir():
        gadget.unlink()
    _write(ws.content / "renderers" / "widget_cell" / "widget" / "main.tsx", "export default {{{ broken\n")
    outcome = dict(_reset(ws)["test"]["matched"])["Widget"]
    assert outcome.startswith("refused: ")
    assert _named(_load_state().channels[0], "Widget").model_dump(mode="json") == widget


def test_the_script_runs_with_the_app_on_its_path(workspace):
    """``PYTHONPATH=api python build/reset_defaults.py``, as build_desktop.sh
    --reset runs it."""
    ws = workspace
    _content(ws.content, version=2)
    env = {**os.environ, "PYTHONPATH": str(PATH.parents[1] / "api"),
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    done = subprocess.run([sys.executable, str(PATH), "--data-dir", str(ws.root), "--source", str(ws.content)],
                          env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert "matched by provenance: 'Alpha' replaced, 'Widget' replaced" in done.stdout
    assert _files(_named(_load_state().channels[0], "Alpha"))["alpha/alpha.py"] == "print('v2')\n"


def _hold_server_lock(root) -> int:
    """What a running server holds: an exclusive flock on
    .collimate/server.lock (a separate open file, so it conflicts with the
    reset's own open even in this process)."""
    import fcntl
    fd = os.open(root / ".collimate" / "server.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.ftruncate(fd, 0)
    os.pwrite(fd, b"4242\n", 0)
    return fd


def test_reset_refuses_while_a_server_holds_the_data_dir__F0022(workspace):
    """Whatever launched it — the desktop shell, `collimate`, `python
    main.py`, uvicorn — a server holds the data dir's lock, and the reset
    refuses instead of having its layout persisted over."""
    ws = workspace
    fd = _hold_server_lock(ws.root)
    try:
        with pytest.raises(tool.ResetError, match=r"server \(pid 4242\) is running"):
            _reset(ws)
    finally:
        os.close(fd)
    assert _load_state().model_dump(mode="json") == ws.before.model_dump(mode="json")
    # The holder gone (a clean stop or a crash: the kernel drops the lock),
    # the lock file left behind is no obstacle.
    assert (ws.root / ".collimate" / "server.lock").exists()
    assert _reset(ws)["test"]


def test_reset_refuses_beside_a_running_server_and_holds_off_a_new_one__F0022(api_app, fresh_cwd):
    """End to end against the real server lifespan: the reset refuses while
    it runs; a reset in progress refuses a server start."""
    import main
    from fastapi.testclient import TestClient
    with TestClient(api_app) as c:
        assert c.post("/api/channels", params={"name": "Starting"}).status_code == 200
        with pytest.raises(tool.ResetError, match=f"pid {os.getpid()}"):
            tool.reset_defaults(str(fresh_cwd), out=lambda *_: None)
        with pytest.raises(main.StartupRefused, match="server.lock"):
            main.lock_data_dir()
    # Stopped: its lifespan released the lock.
    fd = tool.lock_data_dir(str(fresh_cwd))
    try:
        with pytest.raises(main.StartupRefused, match="a server or a reset is already running"):
            with TestClient(api_app):
                pass
    finally:
        tool.unlock_data_dir(fd)


def test_a_link_in_place_of_the_server_lock_is_refused_and_left_alone__F0022_P0F31(api_app, fresh_cwd, capsys):
    """The lock is opened O_NOFOLLOW: a symlink planted at
    .collimate/server.lock refuses a server start and a reset with a message
    naming the lock, and whatever it points at is never written."""
    import main
    from fastapi.testclient import TestClient
    with TestClient(api_app):   # a data dir with app.db, then stopped
        pass
    target = fresh_cwd / "not-the-lock.txt"
    target.write_text("keep me\n")
    lock = fresh_cwd / ".collimate" / "server.lock"
    lock.unlink()
    lock.symlink_to(target)
    with pytest.raises(main.StartupRefused, match=r"server\.lock.*plain file"):
        with TestClient(api_app):
            pass
    with pytest.raises(tool.ResetError, match=r"server\.lock.*plain file"):
        tool.reset_defaults(str(fresh_cwd), out=lambda *_: None)
    assert tool.main(["--data-dir", str(fresh_cwd)]) == 1
    assert "server.lock" in capsys.readouterr().err
    assert lock.is_symlink() and target.read_text() == "keep me\n"


def test_reset_without_data_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lines = []
    assert tool.reset_defaults(str(tmp_path / "empty"), out=lines.append) == {}
    assert lines and "nothing to reset" in lines[0]
    assert not (tmp_path / "empty").exists()


def test_cli_reports_a_live_server_as_an_error__F0022(workspace, capsys):
    ws = workspace
    fd = _hold_server_lock(ws.root)
    try:
        assert tool.main(["--data-dir", str(ws.root), "--source", str(ws.content)]) == 1
    finally:
        os.close(fd)
    assert "running" in capsys.readouterr().err


@pytest.mark.parametrize("raw", ['{"channels": 5}', "{}"])
def test_reset_refuses_a_saved_state_that_does_not_validate__F0001_F0053(workspace, raw):
    """The reset reads the saved state the way the server's startup does
    (genesis.parse_saved_app_state): a row that does not validate, or one
    without a channels list, is refused rather than read as no channels."""
    from database import get_global_db_connection
    ws = workspace
    conn = get_global_db_connection()
    conn.execute("UPDATE ui_state SET value=? WHERE key='app_state'", (raw,))
    conn.commit()
    conn.close()
    with pytest.raises(tool.ResetError, match="does not validate"):
        _reset(ws)


@pytest.mark.parametrize("kind", ["generator", "renderer"])
def test_reset_refuses_content_missing_a_tree_TR05(workspace, kind):
    """The content is read before any channel is touched: a source folder
    without one of the two asset trees stops it."""
    import shutil
    import genesis
    ws = workspace
    shutil.rmtree(ws.content / ("generators" if kind == "generator" else "renderers"))
    before = genesis.load_global_ui_state()
    with pytest.raises(tool.ResetError, match=f"the {kind} cells cannot be read"):
        _reset(ws)
    assert genesis.load_global_ui_state() == before


def test_a_reset_holds_the_data_dir_until_it_is_done__F0022(workspace, monkeypatch):
    """Not a check-then-act: from its first read to its last write, a server
    starting against the data dir is refused."""
    import collimate._host.packaging.asset_cells as asset_cells
    import genesis
    import main
    ws = workspace
    refused = []

    def server_start_refused():
        with pytest.raises(main.StartupRefused, match="server.lock"):
            main.lock_data_dir()
        refused.append(True)

    real_read, real_save = asset_cells.read_asset_cells, genesis.save_global_ui_state

    def read(asset_root, kind):
        server_start_refused()
        return real_read(asset_root, kind)

    def save(state_dict):
        real_save(state_dict)
        server_start_refused()

    monkeypatch.setattr(asset_cells, "read_asset_cells", read)
    monkeypatch.setattr(genesis, "save_global_ui_state", save)
    _content(ws.content, version=2)
    assert _reset(ws)["test"]
    # The two asset trees are read, and the one channel saved, under the lock.
    assert len(refused) == 3
    fd = main.lock_data_dir()
    main.unlock_data_dir(fd)
