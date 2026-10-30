"""The Removed shelf, restore and remove by id (O04). Synthetic cells only."""
import json
from uuid import uuid4

import pytest

from test_pytest_options_identity import CHANNEL as CH, client as client, accept_file


def add_cell(name, files=None, **metadata):
    from models import Cell, Stack, VirtualFile
    from state import get_active_channel
    ch = get_active_channel()
    cell = Cell(name=name, files=[VirtualFile(name=k, content=v) for k, v in (files or {}).items()], **metadata)
    stack = Stack(name="Project", cells=[cell])
    ch.main_stacks.append(stack)
    from persistence import initialize_files
    from database import get_db_connection
    with get_db_connection(CH) as conn:
        initialize_files(conn, str(cell.id), cell.files)
        conn.commit()
    return ch, stack, cell


def remove(client, cell):
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/remove")
    assert response.status_code == 200, response.text
    return response.json()


def shelf(client, **params):
    response = client.get(f"/api/channels/{CH}/cells/removed", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def lineage_events(monkeypatch):
    import events
    sent = []
    original = events.broadcast

    def record(channel, event):
        if event.get("type") == "lineage_changed":
            sent.append(event["cell_ids"])
        return original(channel, event)

    monkeypatch.setattr(events, "broadcast", record)
    return sent


def test_shelf_entry_carries_tip_author_files_and_pinned_preview_O04(client):
    from database import get_db_connection
    from versions import find_tip
    from models import Cell
    ch, stack, cell = add_cell("Questions", {"b.md": "# Questions\nfor Dana", "a.md": "first", "_runs/x/y.log": "run"},
                               icon="BookOpen", pinned="b.md")
    stack.cells.append(Cell(name="Neighbour"))
    remove(client, cell)
    [entry] = shelf(client)
    conn = get_db_connection(CH)
    tip = find_tip(conn, str(cell.id))
    conn.close()
    assert entry["cell_id"] == str(cell.id) and entry["tip_version_id"] == tip
    assert (entry["name"], entry["icon"], entry["stack_name"]) == ("Questions", "BookOpen", "Project")
    assert entry["stack_cell_count"] == 2 and entry["reason"] == "removed"
    assert entry["made_by"] == {"kind": "user", "runner_id": None, "runner_name": None}
    assert entry["files"] == ["a.md", "b.md"] and entry["file_count"] == 2
    assert entry["preview"] == {"path": "b.md", "text": "# Questions\nfor Dana"}
    for key in ("created_at", "edited_at", "removed_at"):
        assert entry[key].endswith("+00:00"), key


def test_preview_is_the_first_text_file_and_bounded_O04(client):
    from cas import hash_and_store_bytes
    import cell_catalog
    _, stack, cell = add_cell("Bounded", {"z.md": "later"})
    digest = hash_and_store_bytes(b"\x00\xffPNG")
    accept_file(str(cell.id), "a.png", blob=digest)
    # A 3-byte character straddles the 4 KB cut: it is dropped, not mangled.
    body = "x" * (cell_catalog.PREVIEW_BYTES - 1) + "€" + "tail"
    accept_file(str(cell.id), "m.md", body)
    remove(client, cell)
    [entry] = shelf(client)
    assert entry["files"] == ["a.png", "m.md", "z.md"]
    assert entry["preview"] == {"path": "m.md", "text": "x" * (cell_catalog.PREVIEW_BYTES - 1)}


def test_made_by_runner_for_a_run_in_a_new_cell_and_a_generator_root_O04(client):
    import cell_moves
    from database import get_db_connection
    from versions import synthesize_genesis_row
    _, _, source = add_cell("Frame")
    _, _, clone = add_cell("Sown", {"idea.md": "lww"})
    _, _, emitted = add_cell("Emitted", {"out.md": "o"})
    conn = get_db_connection(CH)
    runs = {}
    for cell, runner in ((clone, "sower"), (emitted, "probe")):
        run_id = str(uuid4())
        conn.execute("INSERT INTO runs(id, prompt, status, kind, target_cell_id, runner_id, runner_snapshot) "
                     "VALUES(?, '', 'completed', 'generator', ?, ?, ?)",
                     (run_id, str(cell.id), runner, json.dumps({"name": runner.title()})))
        runs[runner] = run_id
    # The clone's genesis is filed under its run's id but is a genesis row:
    # the move, not the run id, says a runner made it.
    synthesize_genesis_row(conn, str(clone.id), runs["sower"], 1, clone.files)
    cell_moves.record_move(conn, op="run_cell", op_id=runs["sower"], from_cell=str(source.id),
                           from_version=None, to_cell=str(clone.id))
    synthesize_genesis_row(conn, str(emitted.id), runs["probe"], 1, emitted.files)
    conn.execute("UPDATE cells SET source_type='generator' WHERE id=?", (str(emitted.id),))
    conn.commit()
    conn.close()
    for cell in (source, clone, emitted):
        remove(client, cell)
    made = {e["name"]: e["made_by"] for e in shelf(client)}
    assert made["Sown"] == {"kind": "runner", "runner_id": "sower", "runner_name": "Sower"}
    assert made["Emitted"] == {"kind": "runner", "runner_id": "probe", "runner_name": "Probe"}
    assert made["Frame"]["kind"] == "user"
    assert [e["name"] for e in shelf(client, q="sower")] == ["Sown"]


def test_a_genesis_filed_under_a_generator_run_is_still_the_users_make_O04(client):
    """The run-start flush (and the run's own genesis guard) files a
    never-versioned cell's genesis row under the generator run. The root's
    source_type, not that run id, says who made the cell: the shelf and the
    journal both name the user."""
    from database import get_db_connection
    from versions import synthesize_genesis_row
    _, _, cell = add_cell("Hand written", {"plan.md": "steps"})
    conn = get_db_connection(CH)
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id, prompt, status, kind, target_cell_id, runner_id, runner_snapshot) "
                 "VALUES(?, '', 'completed', 'generator', ?, 'probe', ?)",
                 (run_id, str(cell.id), json.dumps({"name": "Probe"})))
    synthesize_genesis_row(conn, str(cell.id), run_id, 1, cell.files)
    assert tuple(conn.execute("SELECT source_type, run_id FROM cells WHERE id=?",
                              (str(cell.id),)).fetchone()) == ("genesis", run_id)
    conn.commit()
    conn.close()
    remove(client, cell)
    [entry] = shelf(client)
    assert entry["made_by"] == {"kind": "user", "runner_id": None, "runner_name": None}
    response = client.get(f"/api/channels/{CH}/cells/{cell.id}/journal")
    assert response.status_code == 200, response.text
    [make] = [e for e in response.json() if e["kind"] == "make"]
    assert make["by"] == {"kind": "user", "runner_id": None, "runner_name": None}


def test_search_matches_name_author_file_names_and_preview_text_O04(client):
    for name, files in (("Server locks", {"probe.md": "offline edits wait"}),
                        ("Merge screen", {"merge-screen.html": "<h1>both versions</h1>"}),
                        ("Scratch", {"scratch.md": "merge screen only on conflict?"})):
        remove(client, add_cell(name, files)[2])

    def names(q):
        return sorted(e["name"] for e in shelf(client, q=q))

    assert names("LOCKS") == ["Server locks"]
    assert names("merge-screen.html") == ["Merge screen"]
    assert names("conflict") == ["Scratch"]
    assert names("merge screen") == ["Merge screen", "Scratch"]
    assert names("you offline") == ["Server locks"]
    assert names("nothing like this") == []
    assert len(shelf(client)) == 3


def test_shelf_sorts_by_created_or_edited_in_either_direction_O04(client):
    from datetime import datetime, timedelta
    now = datetime.now()
    cells = {}
    for name, created, edited in (("old-made", 30, 1), ("mid", 20, 20), ("new-made", 10, 5)):
        _, _, cell = add_cell(name, created_at=now - timedelta(days=created),
                              updated_at=now - timedelta(days=edited))
        cells[name] = cell
        remove(client, cell)

    def order(**params):
        return [e["name"] for e in shelf(client, **params)]

    assert order() == ["old-made", "new-made", "mid"]  # edited, newest first
    assert order(sort="edited", dir="asc") == ["mid", "new-made", "old-made"]
    assert order(sort="created", dir="desc") == ["new-made", "mid", "old-made"]
    assert order(sort="created", dir="asc") == ["old-made", "mid", "new-made"]
    assert client.get(f"/api/channels/{CH}/cells/removed", params={"sort": "name"}).status_code == 422


def test_shelf_sorts_by_last_removed_and_re_removal_restamps_TG04(client, monkeypatch):
    import itertools
    import cell_catalog
    stamps = (f"2040-01-01T12:00:{s:02d}+00:00" for s in itertools.count())
    monkeypatch.setattr(cell_catalog, "now_iso", lambda: next(stamps))
    cells = {name: add_cell(name)[2] for name in ("first", "second", "third")}
    for name in ("first", "second", "third"):
        remove(client, cells[name])

    def order(direction):
        return [e["name"] for e in shelf(client, sort="removed", dir=direction)]

    assert order("desc") == ["third", "second", "first"]
    assert order("asc") == ["first", "second", "third"]
    assert client.post(f"/api/channels/{CH}/cells/{cells['first'].id}/restore").status_code == 200
    remove(client, cells["first"])
    assert order("desc") == ["first", "third", "second"]
    assert order("asc") == ["second", "third", "first"]
    [entry] = [e for e in shelf(client) if e["name"] == "first"]
    assert entry["removed_at"] == "2040-01-01T12:00:03+00:00"


def test_shelf_entries_carry_tags_and_search_matches_them_TG04(client):
    remove(client, add_cell("Alpha", {"a.md": "nothing here"}, tags=["Client Work", "q3"])[2])
    remove(client, add_cell("Beta", {"b.md": "client notes"})[2])
    remove(client, add_cell("Gamma")[2])
    tags = {e["name"]: e["tags"] for e in shelf(client)}
    assert tags == {"Alpha": ["Client Work", "q3"], "Beta": [], "Gamma": []}

    def names(q):
        return sorted(e["name"] for e in shelf(client, q=q))

    assert names("Q3") == ["Alpha"]
    assert names("client") == ["Alpha", "Beta"]
    assert names("client work") == ["Alpha"]


def test_remove_by_id_takes_an_optional_tag_action_TG03(client, lineage_events):
    _, _, plain = add_cell("Plain", tags=["kept"])
    _, _, tagged = add_cell("Tagged", tags=["kept"])
    _, _, empty = add_cell("Empty body")
    remove(client, plain)
    response = client.post(f"/api/channels/{CH}/cells/{tagged.id}/remove",
                           json={"add_tags": ["Later"], "remove_tags": ["KEPT"]})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/channels/{CH}/cells/{empty.id}/remove", json={})
    assert response.status_code == 200, response.text
    assert {e["name"]: e["tags"] for e in shelf(client)} == {
        "Plain": ["kept"], "Tagged": ["Later"], "Empty body": []}
    assert lineage_events == [[str(plain.id)], [str(tagged.id)], [str(empty.id)]]


def test_restore_appends_at_the_right_end_selected_O04(client, lineage_events):
    ch, first, cell = add_cell("Comes back", {"n.md": "n"})
    _, last, _ = add_cell("Last")
    remove(client, cell)
    assert lineage_events == [[str(cell.id)]]
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/restore")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cell_id"] == str(cell.id)
    assert str(ch.main_stacks[-1].id) == body["stack_id"] and ch.main_selection_id == ch.main_stacks[-1].id
    assert [c.id for c in ch.main_stacks[-1].cells] == [cell.id]
    assert lineage_events[-1] == [str(cell.id)]
    assert shelf(client) == []
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/restore").status_code == 409

    remove(client, cell)
    ch.main_selection_id = ch.main_stacks[0].id  # a selection is not an anchor
    response = client.post(f"/api/channels/{CH}/cells/{cell.id}/restore")
    assert response.status_code == 200, response.text
    assert str(ch.main_stacks[-1].id) == response.json()["stack_id"] == str(ch.main_selection_id)
    assert [c.id for c in ch.main_stacks[-1].cells] == [cell.id]
    assert client.post(f"/api/channels/{CH}/cells/{uuid4()}/restore").status_code == 404


def test_remove_by_id_refuses_a_running_cell_and_a_cell_off_the_canvas_O04(client, monkeypatch, lineage_events):
    import run_registry
    from models import Cell
    ch, stack, cell = add_cell("Busy")
    other = Cell(name="Sibling")
    stack.cells.append(other)
    monkeypatch.setattr(run_registry, "running_cell_ids", lambda: {str(cell.id)})
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/remove").status_code == 409
    assert stack.cells == [cell, other] and shelf(client) == []
    monkeypatch.setattr(run_registry, "running_cell_ids", lambda: set())
    layout = remove(client, cell)
    assert str(cell.id) not in {c["id"] for s in layout["main_stacks"] for c in s["cells"]}
    assert stack.cells == [other]
    [entry] = shelf(client)
    assert entry["cell_id"] == str(cell.id) and entry["stack_cell_count"] == 2
    assert lineage_events == [[str(cell.id)]]
    assert client.post(f"/api/channels/{CH}/cells/{cell.id}/remove").status_code == 404
    assert client.post(f"/api/channels/{CH}/cells/{uuid4()}/remove").status_code == 404
    remove(client, other)
    assert stack not in ch.main_stacks


def test_shelf_reads_only_the_removed_cells_O04(client, monkeypatch):
    """Large cells first-class: listing never walks the channel's version
    forest, and a preview reads at most PREVIEW_BYTES of one blob."""
    import cell_catalog
    import versions
    big = "line\n" * 50_000
    _, _, kept = add_cell("Kept on canvas", {"huge.md": big})
    _, _, gone = add_cell("Gone", {"huge.md": big})
    remove(client, gone)

    def forbidden(*_a, **_k):
        raise AssertionError("the shelf walked the version forest")

    monkeypatch.setattr(versions, "iter_version_files", forbidden)
    [entry] = shelf(client)
    assert entry["cell_id"] == str(gone.id)
    assert len(entry["preview"]["text"].encode()) <= cell_catalog.PREVIEW_BYTES


def test_a_capability_cell_moved_off_and_removed_shelves_and_restores_to_the_board_O04(client):
    """Capability cells are not removed in place: one moved off Capabilities
    and removed is an ordinary shelf record, restored to a new standard
    stack on the board with its package files."""
    from cell_placement import capabilities_stack
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from state import get_active_channel
    ch = get_active_channel()
    files = {"synth-gen/choice.generator": "id: synth-gen\nname: Synth\n", "synth-gen/run.py": "print(1)\n"}
    package = Cell(name="Package", files=[VirtualFile(name=k, content=v) for k, v in files.items()])
    add_capabilities_cell(ch, package)
    stack = capabilities_stack(ch)
    ch.nav_stacks.remove(stack)
    ch.main_stacks.append(stack)
    moved = client.post(f"/api/stacks/{CH}/{stack.id}/move_cell_to/{len(ch.main_stacks)}/insert",
                        params={"cell_id": str(package.id)})
    assert moved.status_code == 200, moved.text
    remove(client, package)
    [entry] = shelf(client)
    assert (entry["cell_id"], entry["files"]) == (str(package.id), sorted(files))
    restored = client.post(f"/api/channels/{CH}/cells/{package.id}/restore")
    assert restored.status_code == 200, restored.text
    landed = ch.main_stacks[-1]
    assert str(landed.id) == restored.json()["stack_id"] and landed.stack_type == "standard"
    assert [c.id for c in landed.cells] == [package.id] and capabilities_stack(ch) is stack
    assert {f.name for f in landed.cells[0].files} == set(files)


def _pre_v28_display(path):
    """A cell display as v27 stored it: manage tabs beside a file tab."""
    file_tab = {"id": "f", "kind": "file", "path": path, "rendererId": "text"}
    return {"version": 1,
            "root": {"type": "split", "id": "split", "dir": "row", "children": [
                {"type": "leaf", "id": "left", "tabs": [file_tab, {"id": "m", "kind": "manage"}], "activeTabId": "m"},
                {"type": "leaf", "id": "right", "tabs": [{"id": "m2", "kind": "manage"}], "activeTabId": "m2"}]},
            "activeLeafId": "left",
            "filesPane": {"collapsed": False, "side": "left", "width": None, "height": None, "viewMode": "tree",
                          "panelFilters": {"runsOnly": False, "sortByRecency": False}},
            "runsPane": {"open": False, "side": "right", "width": None, "height": None},
            "pinnedTabCompact": False, "history": None}


def test_a_generator_or_renderer_record_from_before_v28_shelves_and_restores_to_the_board_O04(client):
    """A record written when package cells lived on Generators and Renderers
    stacks (``stack_type`` 'generator' / 'renderer', a display with manage
    tabs), upgraded through v28, is an ordinary shelf entry and restores to a
    new standard stack on the board, never onto Capabilities."""
    import migrations
    from cell_display import read_display, validate_display
    from cell_placement import capabilities_stack, iter_stacks
    from database import get_db_connection
    from genesis import ensure_capabilities_stack
    from state import get_active_channel

    ch = get_active_channel()
    ensure_capabilities_stack(ch)
    gen_files = {"synth-gen/choice.generator": "id: synth-gen\nname: Synth\n", "synth-gen/run.py": "print(1)\n"}
    ren_files = {"viewer/viewer.renderer": "id: viewer\nname: Viewer\n", "viewer/index.html": "<p>v</p>\n"}
    _, _, gen = add_cell("Synth package", gen_files)
    _, _, ren = add_cell("Viewer package", ren_files)
    remove(client, gen)
    remove(client, ren)

    # Rewind both records to their v27 shape, then upgrade through v28.
    conn = get_db_connection(CH)
    try:
        conn.execute("ALTER TABLE removed_cells ADD COLUMN stack_type TEXT")
        for cell, stack_name, stack_type, path in ((gen, "Generators", "generator", "synth-gen/run.py"),
                                                   (ren, "Renderers", "renderer", "viewer/index.html")):
            conn.execute("UPDATE removed_cells SET stack_name=?, stack_type=?, cell_created_at=NULL, "
                         "display_json=? WHERE cell_id=?",
                         (stack_name, stack_type, json.dumps(_pre_v28_display(path)), str(cell.id)))
        conn.execute("DELETE FROM schema_migrations WHERE version=28")
        conn.commit()
        assert migrations.upgrade_channel_db(conn) == [28]
    finally:
        conn.close()

    entries = {e["cell_id"]: e for e in shelf(client)}
    assert set(entries) == {str(gen.id), str(ren.id)}
    assert (entries[str(gen.id)]["files"], entries[str(gen.id)]["stack_name"]) == (sorted(gen_files), "Generators")
    assert (entries[str(ren.id)]["files"], entries[str(ren.id)]["stack_name"]) == (sorted(ren_files), "Renderers")
    assert entries[str(gen.id)]["created_at"]

    caps = capabilities_stack(ch)
    for cell, files in ((gen, gen_files), (ren, ren_files)):
        restored = client.post(f"/api/channels/{CH}/cells/{cell.id}/restore")
        assert restored.status_code == 200, restored.text
        landed = ch.main_stacks[-1]
        assert str(landed.id) == restored.json()["stack_id"] and landed.stack_type == "standard"
        assert [c.id for c in landed.cells] == [cell.id] and cell.id not in {c.id for c in caps.cells}
        assert {f.name for f in landed.cells[0].files} == set(files)
        with get_db_connection(CH) as conn:
            display = read_display(conn, str(cell.id))
        validate_display(display)
        assert all(tab["kind"] != "manage" for leaf in display["root"]["children"] for tab in leaf["tabs"])
    assert shelf(client) == []
    assert [s for s in iter_stacks(ch) if s.stack_type == "capabilities"] == [caps]
