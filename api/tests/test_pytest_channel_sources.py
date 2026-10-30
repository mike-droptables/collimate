"""A config selection (channel_sources.seed_config): each part of a new
channel from an existing channel, from an uploaded file or left out.

The source channel is created from the synthetic suggestion
(tests/suggested_trees.py), gets synthetic main cells, tags and a removed
cell, and the new channel is created from it, or from its exports
round-tripped through ``POST /api/import/zip``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from import_cells import import_cell
from suggested_trees import build_tree, create_suggested, draft, use_source


@pytest.fixture
def source(api_app, tmp_path):
    """``(client, source_id, cells)``: a client whose active channel was
    created from the synthetic suggestion with the ``ready`` runner, plus
    two main stacks; ``cells`` maps names to cell ids. Notes (tagged
    ``Draft``) and Logo stay on the canvas, Gone (tagged ``draft``) is
    removed, Plain carries no tag."""
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        use_source(client, build_tree(tmp_path / "sdk"))
        created = create_suggested(client, "Source", tutorial=False, runners=[draft("ready")])
        assert created.status_code == 200, created.text
        channel_id = created.json()["id"]
        cells = {}
        for stack, name, files in (
            ("Project", "Notes", [{"name": "notes.md", "content": "# notes\n"}]),
            ("Assets", "Logo", [{"name": "logo.png", "content": b"\x89PNG\x00\xff"}]),
            ("Assets", "Gone", [{"name": "gone.md", "content": "gone\n"}]),
            ("Assets", "Plain", [{"name": "plain.txt", "content": "plain\n"}]),
        ):
            response = import_cell(client, channel_id, files, name=name, stack=stack)
            assert response.status_code == 200, response.text
            cells[name] = response.json()["cell_ids"][0]
        _merge_stacks(channel_id, "Assets")
        for name, tags in (("Notes", ["Draft", "Second"]), ("Gone", ["draft"])):
            assert client.patch(f"/api/channels/{channel_id}/cells/{cells[name]}/meta",
                                json={"tags": tags}).status_code == 200
        assert client.post(f"/api/channels/{channel_id}/cells/{cells['Gone']}/remove").status_code == 200
        assert client.put(f"/api/channels/{channel_id}/settings", json={
            "keyboard": {"overrides": {"synth": "Mod+K"}}, "editing": {"spellcheck": False}}).status_code == 200
        yield client, channel_id, cells


def _merge_stacks(channel_id: str, name: str) -> None:
    """Fold the import's one-cell stacks named ``name`` into the first one."""
    from state import persist_state, state, state_lock
    with state_lock:
        channel = next(c for c in state.channels if str(c.id) == channel_id)
        named = [s for s in channel.main_stacks if s.name == name]
        for stack in named[1:]:
            named[0].cells.extend(stack.cells)
            channel.main_stacks.remove(stack)
    persist_state()


def _channel(channel_id):
    from state import state
    return next(c for c in state.channels if str(c.id) == channel_id)


def _capability_cells(channel):
    from cell_placement import capabilities_stack_cells
    return capabilities_stack_cells(channel)


def _create(client, config: dict, name: str = "Copy"):
    return client.post("/api/channels", params={"name": name}, json={"config": config})


def _versions(channel_id, cell_id) -> list[str]:
    from cell_files import walk_versions
    from database import get_db_connection
    conn = get_db_connection(channel_id)
    try:
        return walk_versions(conn, str(cell_id))
    finally:
        conn.close()


def _expand(client, scope: str, channel_id: str, **params) -> list[dict]:
    """Export ``scope`` from the channel and expand the zip again."""
    exported = client.get(f"/api/channels/{channel_id}/export.zip", params={"scope": scope, **params})
    assert exported.status_code == 200, exported.text
    expanded = client.post("/api/import/zip", content=exported.content)
    assert expanded.status_code == 200, expanded.text
    return expanded.json()["entries"]


def _split(entries: list[dict], level: int) -> dict:
    """``{(folder, …): [{path, blob_hash}]}`` with the first ``level``
    path components as the key, as the page's splitLevels does."""
    groups: dict = {}
    for entry in entries:
        parts = entry["path"].split("/")
        groups.setdefault(tuple(parts[1:level]), []).append(
            {"path": "/".join(parts[level:]), "blob_hash": entry["blob_hash"]})
    return groups


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

def test_packages_from_a_channel_copy_its_cells_and_runners_with_fresh_ids(source):
    client, source_id, _cells = source
    response = _create(client, {"packages": {"channel_id": source_id}})
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    copy_id = response.json()["id"]
    copy, original = _channel(copy_id), _channel(source_id)
    assert copy.main_stacks == []
    before, after = _capability_cells(original), _capability_cells(copy)
    assert [(c.name, c.icon) for c in after] == [(c.name, c.icon) for c in before]
    assert not {c.id for c in after} & {c.id for c in before}
    for old, new in zip(before, after):
        assert sorted(f.name for f in new.files) == sorted(f.name for f in old.files)
        assert len(_versions(copy_id, new.id)) == 1
    assert [row["id"] for row in client.get("/api/runners").json()] == ["ready"]


def test_packages_from_an_exported_zip_round_trip(source):
    client, source_id, _cells = source
    groups = _split(_expand(client, "packages", source_id), 3)
    assert {folder for folder, _cell in groups} == {"Capabilities"}
    body = {"cells": [{"name": cell, "files": files} for (_folder, cell), files in groups.items()]}
    response = _create(client, {"packages": body})
    assert response.status_code == 200, response.text
    copy = _channel(response.json()["id"])
    original = _channel(source_id)
    assert sorted(c.name for c in _capability_cells(copy)) == sorted(c.name for c in _capability_cells(original))
    assert [row["id"] for row in client.get("/api/runners").json()] == ["ready"]


def test_a_package_zip_that_fails_admission_refuses_the_creation(source):
    from import_cells import upload
    client, _source_id, _cells = source
    files = [{"path": "broken/view.renderer", "blob_hash": upload(client, (
        b"id: broken\nname: Broken\nentrypoint: main.tsx\nembed: native\nextensions: [.broken]\n"))["blob_hash"]},
             {"path": "broken/main.tsx", "blob_hash": upload(client, b"export default {{{\n")["blob_hash"]}]
    before = client.get("/api/channels").json()["channels"]
    response = _create(client, {"packages": {"cells": [{"name": "Broken", "files": files}]}})
    assert response.status_code == 400, response.text
    assert client.get("/api/channels").json()["channels"] == before
    assert len(list(Path(".collimate/channels").glob("*.db"))) == len(before)


def test_a_packages_zip_has_one_cells_list(source):
    client, _source_id, _cells = source
    for body in ({"generators": [], "renderers": []}, {"cells": [], "renderers": []}):
        assert _create(client, {"packages": body}).status_code == 422


def test_packages_from_a_channel_take_its_capability_cells_wherever_the_stack_sits(source):
    """The source's Capabilities stack open on the board copies the same,
    and a cell moved off it stays behind."""
    from cell_placement import capabilities_stack
    from state import state_lock
    client, source_id, _cells = source
    original = _channel(source_id)
    with state_lock:
        stack = capabilities_stack(original)
        original.nav_stacks.remove(stack)
        original.main_stacks.append(stack)
        views = stack.cells.pop()
        original.main_stacks.append(type(stack)(name="Off", cells=[views]))
    response = _create(client, {"packages": {"channel_id": source_id}})
    assert response.status_code == 200, response.text
    assert [c.name for c in _capability_cells(_channel(response.json()["id"]))] == ["First", "Second"]


def test_an_unknown_source_channel_is_refused(source):
    client, _source_id, _cells = source
    for config in ({"packages": {"channel_id": "00000000-0000-4000-8000-000000000000"}},
                   {"main_cells": {"channel_id": "not-a-channel"}},
                   {"history_cells": {"channel_id": "00000000-0000-4000-8000-000000000000", "tag": "x"}}):
        response = _create(client, config)
        assert response.status_code == 400, response.text


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_from_a_channel_or_a_document_replace_the_defaults(source):
    client, source_id, _cells = source
    doc = client.get(f"/api/channels/{source_id}/settings").json()
    for settings in ({"channel_id": source_id}, {"doc": doc}):
        response = _create(client, {"settings": settings})
        assert response.status_code == 200, response.text
        copied = client.get(f"/api/channels/{response.json()['id']}/settings").json()
        assert copied["keyboard"]["overrides"] == {"synth": "Mod+K"}
        assert copied["editing"] == {"spellcheck": False}


def test_a_settings_document_of_another_kind_refuses_the_creation(source):
    client, _source_id, _cells = source
    before = client.get("/api/channels").json()["channels"]
    response = _create(client, {"packages": None, "settings": {"doc": {"kind": "something-else"}}})
    assert response.status_code == 400, response.text
    assert client.get("/api/channels").json()["channels"] == before


# ---------------------------------------------------------------------------
# Main cells
# ---------------------------------------------------------------------------

def test_main_cells_from_a_channel_keep_the_stack_layout_latest_files_only(source):
    from cas import read_blob
    client, source_id, cells = source
    response = _create(client, {"main_cells": {"channel_id": source_id}})
    assert response.status_code == 200, response.text
    copy_id = response.json()["id"]
    copy = _channel(copy_id)
    assert copy.main_selection_id == copy.main_stacks[0].id
    assert [(s.name, s.stack_type, [c.name for c in s.cells]) for s in copy.main_stacks] == [
        ("Project", "standard", ["Notes"]), ("Assets", "standard", ["Logo", "Plain"])]
    notes = copy.main_stacks[0].cells[0]
    assert str(notes.id) != cells["Notes"] and notes.tags == ["Draft", "Second"]
    assert [(f.name, f.content) for f in notes.files] == [("notes.md", "# notes\n")]
    logo = copy.main_stacks[1].cells[0]
    assert read_blob(logo.files[0].blob_hash) == b"\x89PNG\x00\xff"
    for stack in copy.main_stacks:
        for cell in stack.cells:
            assert len(_versions(copy_id, cell.id)) == 1
    # The packages are not part of main cells: Capabilities holds only its cards.
    assert _capability_cells(copy) == []


def test_a_cell_joining_a_source_stack_during_the_freeze_stays_behind(source, monkeypatch):
    """The main cells are selected before the freeze copies the layout; a
    cell that joined a stack in between has no frozen files and is left out
    (a stack left empty goes too), as the packages are."""
    import channel_sources
    client, source_id, cells = source
    real = channel_sources._main_cells
    late = {cells["Notes"], cells["Plain"]}
    monkeypatch.setattr(channel_sources, "_main_cells", lambda ch: [i for i in real(ch) if i not in late])
    response = _create(client, {"main_cells": {"channel_id": source_id}})
    assert response.status_code == 200, response.text
    copy = _channel(response.json()["id"])
    assert [(s.name, [c.name for c in s.cells]) for s in copy.main_stacks] == [("Assets", ["Logo"])]


@pytest.mark.parametrize("part", ["packages", "main_cells", "history_cells"])
def test_a_source_changing_under_the_freeze_is_a_retryable_409(source, monkeypatch, part):
    """A selected cell that left the source (packages, main cells) or a tag
    set that moved between the listing and the freeze (history cells) is a
    409 to retry, not a 400 refusal; nothing is created."""
    import channel_sources
    import lifecycle_snapshots
    client, source_id, _cells = source
    gone = "00000000-0000-4000-8000-00000000dead"
    if part == "history_cells":
        real = lifecycle_snapshots._removed_tagged_ids
        calls = []

        def moving(conn, live_ids, key):
            calls.append(key)
            found = real(conn, live_ids, key)
            return found + [gone] if len(calls) > 1 else found

        monkeypatch.setattr(lifecycle_snapshots, "_removed_tagged_ids", moving)
        config = {"history_cells": {"channel_id": source_id, "tag": "draft"}}
    else:
        selector = "_package_cells" if part == "packages" else "_main_cells"
        real_selector = getattr(channel_sources, selector)
        monkeypatch.setattr(channel_sources, selector, lambda ch: [*real_selector(ch), gone])
        config = {part: {"channel_id": source_id}}
    before = client.get("/api/channels").json()["channels"]
    response = _create(client, config)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Source channel changed; retry"
    assert client.get("/api/channels").json()["channels"] == before


def test_main_cells_from_an_exported_zip_round_trip(source):
    client, source_id, _cells = source
    groups = _split(_expand(client, "main", source_id), 3)
    stacks: dict = {}
    for (stack, cell), files in groups.items():
        stacks.setdefault(stack, []).append({"name": cell, "files": files})
    response = _create(client, {"main_cells": {"stacks": [{"name": n, "cells": c} for n, c in stacks.items()]}})
    assert response.status_code == 200, response.text
    copy = _channel(response.json()["id"])
    assert sorted((s.name, sorted(c.name for c in s.cells)) for s in copy.main_stacks) == [
        ("Assets", ["Logo", "Plain"]), ("Project", ["Notes"])]
    empty = _create(client, {"main_cells": {"stacks": [{"name": "Empty", "cells": []}]}})
    assert empty.status_code == 400


# ---------------------------------------------------------------------------
# History cells
# ---------------------------------------------------------------------------

def test_tagged_cells_land_on_the_removed_shelf_and_restore_with_their_files(source):
    import cas_gc
    client, source_id, cells = source
    response = _create(client, {"history_cells": {"channel_id": source_id, "tag": "DRAFT"}})
    assert response.status_code == 200, response.text
    copy_id = response.json()["id"]
    copy = _channel(copy_id)
    assert copy.main_stacks == []
    shelf = {row["name"]: row for row in client.get(f"/api/channels/{copy_id}/cells/removed").json()}
    assert set(shelf) == {"Notes", "Gone"}
    assert shelf["Notes"]["tags"] == ["Draft", "Second"] and shelf["Gone"]["tags"] == ["draft"]
    assert (shelf["Notes"]["stack_name"], shelf["Gone"]["stack_name"]) == ("Project", "Assets")
    assert shelf["Notes"]["files"] == ["notes.md"] and shelf["Gone"]["files"] == ["gone.md"]
    assert not {row["cell_id"] for row in shelf.values()} & set(cells.values())
    tags = {t["tag"].casefold(): t for t in client.get(f"/api/channels/{copy_id}/tags").json()}
    assert (tags["draft"]["live"], tags["draft"]["removed"]) == (0, 2)

    from database import get_db_connection
    from persistence import snapshot_files
    conn = get_db_connection(copy_id)
    try:
        hashes = {entry["body_hash"] for row in shelf.values()
                  for entry in _tip_entries(conn, row["cell_id"])}
        assert all(snapshot_files(conn, row["cell_id"]) == [] for row in shelf.values())
    finally:
        conn.close()
    assert hashes and hashes <= cas_gc.collect_live_blobs()

    restored = client.post(f"/api/channels/{copy_id}/cells/{shelf['Notes']['cell_id']}/restore")
    assert restored.status_code == 200, restored.text
    cell = next(c for s in copy.main_stacks for c in s.cells if str(c.id) == shelf["Notes"]["cell_id"])
    assert [(f.name, f.content) for f in cell.files] == [("notes.md", "# notes\n")]
    assert cell.tags == ["Draft", "Second"]
    assert len(_versions(copy_id, cell.id)) == 1


def _tip_entries(conn, cell_id):
    from versions import find_tip, version_file_entries
    return version_file_entries(conn, find_tip(conn, cell_id))


def test_a_tag_no_cell_carries_is_refused(source):
    client, source_id, _cells = source
    for tag in ("Missing", "", "\x00"):
        response = _create(client, {"history_cells": {"channel_id": source_id, "tag": tag}})
        assert response.status_code == 400, response.text


def test_a_history_zip_lands_on_the_shelf_under_its_tag(source):
    client, source_id, _cells = source
    groups = _split(_expand(client, "tag", source_id, tag="draft"), 2)
    assert {cell for (cell,) in groups} == {"Notes", "Gone"}
    body = {"tag": "  Imported   history ", "cells": [{"name": cell, "files": files} for (cell,), files in groups.items()]}
    response = _create(client, {"history_cells": body})
    assert response.status_code == 200, response.text
    shelf = client.get(f"/api/channels/{response.json()['id']}/cells/removed").json()
    assert sorted(row["name"] for row in shelf) == ["Gone", "Notes"]
    assert all(row["tags"] == ["Imported history"] for row in shelf)
    untagged = _create(client, {"history_cells": {**body, "tag": ""}})
    assert untagged.status_code == 200, untagged.text
    assert all(row["tags"] == [] for row in client.get(
        f"/api/channels/{untagged.json()['id']}/cells/removed").json())
    refused = _create(client, {"history_cells": {**body, "tag": "x" * 49}})
    assert refused.status_code == 400


def test_every_part_together(source):
    client, source_id, _cells = source
    response = _create(client, {"packages": {"channel_id": source_id}, "settings": {"channel_id": source_id},
                                "main_cells": {"channel_id": source_id},
                                "history_cells": {"channel_id": source_id, "tag": "second"}})
    assert response.status_code == 200, response.text
    copy_id = response.json()["id"]
    copy = _channel(copy_id)
    assert [s.name for s in copy.main_stacks] == ["Project", "Assets"]
    assert [c.name for c in _capability_cells(copy)] == ["First", "Second", "Views"]
    assert [row["name"] for row in client.get(f"/api/channels/{copy_id}/cells/removed").json()] == ["Notes"]
    assert client.get(f"/api/channels/{copy_id}/settings").json()["editing"] == {"spellcheck": False}
