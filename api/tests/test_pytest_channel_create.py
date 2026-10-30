"""``POST /api/channels``: the create body, the suggested path and the empty
channel.

A suggested channel's cells of both asset trees pass one package admission
into its Capabilities stack (there before admission), each under a fresh id
with a provenance record of its source per package, with the runner drafts
the user kept written into their generator cell first, so every cell starts
with one version; its tutorial cells start on the canvas. Most cases read a synthetic
tree (tests/suggested_trees.py); one creates the Standard suggestion from the
bundled ``content/``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from suggested_trees import PNG, build_tree, create_suggested, draft, folder_hash, provenance, use_source


@pytest.fixture
def app(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


@pytest.fixture
def basic(app, tmp_path):
    """A client whose suggested source is the synthetic tree; yields
    ``(client, root)``."""
    root = build_tree(tmp_path / "sdk")
    use_source(app, root)
    return app, root


def _channel(channel_id):
    from state import state
    return next(c for c in state.channels if str(c.id) == channel_id)


def _capability_cells(channel):
    from cell_placement import capabilities_stack_cells
    return capabilities_stack_cells(channel)


def _versions(channel_id, cell_id) -> list[str]:
    from cell_files import walk_versions
    from database import get_db_connection
    conn = get_db_connection(channel_id)
    try:
        return walk_versions(conn, str(cell_id))
    finally:
        conn.close()


def _no_channel_files() -> bool:
    return not list(Path(".collimate/channels").glob("*.db*"))


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [None, {}, {"config": {}}, {"config": {
    "packages": None, "settings": None, "main_cells": None, "history_cells": None}}])
def test_no_selection_creates_an_empty_channel_with_its_capabilities_stack(app, body):
    response = app.post("/api/channels", params={"name": "Empty"}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    channel = _channel(response.json()["id"])
    assert channel.main_stacks == [] and channel.main_selection_id is None
    assert [(s.name, s.stack_type, [c.name for c in s.cells if not c.builtin]) for s in channel.nav_stacks] == [
        ("Capabilities", "capabilities", [])]
    assert all(c.builtin for c in channel.nav_stacks[0].cells) and channel.nav_stacks[0].cells
    settings = app.get(f"/api/channels/{channel.id}/settings").json()
    from channel_settings import default_settings
    assert {k: v for k, v in settings.items() if k != "renderer_availability"} == {
        k: v for k, v in default_settings().items() if k != "renderer_availability"}


@pytest.mark.parametrize("body", [
    {"suggested": {"id": "basic"}, "config": {}},
    {"runners": []}, {"name": "x"},
    {"suggested": {"id": "basic", "configs": []}},
    {"config": {"packages": {"channel_id": "x", "tag": "y"}}},
    {"config": {"packages": {}}},
    {"config": {"settings": {"doc": {}, "extra": 1}}},
    {"config": {"history_cells": {"channel_id": "x"}}},
])
def test_a_body_naming_both_sources_or_unknown_keys_is_422(app, body):
    response = app.post("/api/channels", params={"name": "Refused"}, json=body)
    assert response.status_code == 422, response.text
    assert app.get("/api/channels").json()["channels"] == []


def test_a_failed_seed_leaves_no_channel_or_database__F0349(basic, monkeypatch):
    import suggested
    from fastapi import HTTPException
    client, _root = basic
    real = suggested.seed_suggested

    def seed(ch, selection):
        real(ch, selection.model_copy(update={"runners": []}))
        raise HTTPException(400, "synthetic failure after the database was written")

    monkeypatch.setattr(suggested, "seed_suggested", seed)
    response = create_suggested(client, "Rejected")
    assert response.status_code == 400, response.text
    assert client.get("/api/channels").json()["channels"] == []
    assert _no_channel_files()


# ---------------------------------------------------------------------------
# A suggestion
# ---------------------------------------------------------------------------

def test_a_suggestion_seeds_its_cells_through_admission_with_their_provenance(basic):
    from collimate._host.packaging import read_suggested_cell, read_suggestions
    from app_version import sdk_version
    from cas import read_blob
    client, root = basic
    response = create_suggested(client, "Seeded", tutorial=False)
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    channel_id = response.json()["id"]
    channel = _channel(channel_id)
    assert channel.main_stacks == []
    (suggestion,) = read_suggestions(root)
    (stack,) = channel.nav_stacks
    assert (stack.name, stack.stack_type) == ("Capabilities", "capabilities")
    # Both trees' cells share the one stack, each under a fresh id.
    expected = [(spec.name, spec.icon or None)
                for kind, refs in (("generator", suggestion.generators), ("renderer", suggestion.renderers))
                for spec in (read_suggested_cell(root, ref, kind) for ref in refs)]
    cells = _capability_cells(channel)
    assert [(c.name, c.icon) for c in cells] == expected
    assert all(c.id.version == 4 for c in cells)
    for cell in cells:
        assert len(_versions(channel_id, cell.id)) == 1
    # One record per admitted package: the folder source, the package's
    # identity and the SDK it was admitted under (a library is no package).
    folders = {"First": root / "generators" / "first", "Second": root / "generators" / "second",
               "Views": root / "renderers" / "views"}
    packages = {"First": [("alpha", "generator", "alpha")], "Second": [("beta", "generator", "beta")],
                "Views": [("widget", "renderer", "widget")]}
    for cell in cells:
        records = provenance(channel_id, cell.id)
        assert [(r["folder"], r["package_kind"], r["package_id"]) for r in records] == packages[cell.name]
        for record in records:
            assert (record["source_kind"], record["source_id"]) == ("folder", str(root))
            assert record["content_hash"] == folder_hash(folders[cell.name] / record["folder"])
            assert record["package_version"] is None and record["sdk_version"] == sdk_version()
    first = cells[0]
    files = {f.name: f for f in first.files}
    # The cell.yaml travels with the cell (test_pytest_shared_helpers covers
    # the helpers it names).
    assert files["cell.yaml"].content == (root / "generators" / "first" / "cell.yaml").read_text()
    assert read_blob(files["alpha/image.png"].blob_hash) == PNG
    # The cell's own runner packages are offered as drafts, never seeded.
    assert not any(name.endswith(".runner") for name in files)
    assert client.get("/api/runners").json() == []


def test_one_admission_sees_both_trees_on_the_ensured_stack(basic, monkeypatch):
    """The candidate holds its Capabilities stack before the one admission
    call, which gets the generator and renderer cells together."""
    import package_import
    from cell_placement import capabilities_stack
    client, _root = basic
    real = package_import.prepare_packages
    calls = []

    def spy(channel, incoming, *, source, source_state=None):
        calls.append(([c.name for c in incoming], capabilities_stack(source_state[0]).name))
        return real(channel, incoming, source=source, source_state=source_state)

    monkeypatch.setattr(package_import, "prepare_packages", spy)
    assert create_suggested(client, "Once").status_code == 200
    assert calls == [(["First", "Second", "Views"], "Capabilities")]


def test_ids_are_fresh_in_every_channel(basic):
    client, _root = basic
    first = create_suggested(client, "One").json()["id"]
    second = create_suggested(client, "Two").json()["id"]
    ids = [{str(c.id) for c in _capability_cells(_channel(channel))} for channel in (first, second)]
    assert len(ids[0]) == 3 and not ids[0] & ids[1]


def test_the_tutorial_starts_on_the_canvas_pinned_to_its_one_file(basic):
    client, _root = basic
    channel_id = create_suggested(client, "Tour").json()["id"]
    channel = _channel(channel_id)
    (stack,) = channel.main_stacks
    assert channel.main_selection_id == stack.id
    (cell,) = stack.cells
    assert (stack.name, stack.stack_type, cell.name, cell.pinned) == ("Tour", "standard", "Tour", "tour.slides")
    assert [(f.name, f.content) for f in cell.files] == [("tour.slides", "# Tour\n\nSynthetic slides.\n")]
    assert len(_versions(channel_id, cell.id)) == 1


def test_kept_drafts_land_in_their_generator_cell_with_one_version(basic):
    import runners
    client, _root = basic
    catalog = client.get("/api/suggested").json()
    ready = next(row["draft"] for row in catalog["suggestions"][0]["runners"]
                 if row["draft"]["runner"]["id"] == "ready")
    response = create_suggested(client, "Drafts", runners=[ready, draft("second", generator="beta")])
    assert response.status_code == 200, response.text
    channel_id = response.json()["id"]
    channel = _channel(channel_id)
    catalog = {row["id"]: row for row in client.get("/api/runners").json()}
    assert set(catalog) == {"ready", "second"}
    assert all("invalid" not in row for row in catalog.values())
    first, second, _views = _capability_cells(channel)
    assert catalog["ready"]["cell_id"] == str(first.id) and catalog["second"]["cell_id"] == str(second.id)
    assert "runner-ready/ready.runner" in {f.name for f in first.files}
    for cell in (first, second):
        assert len(_versions(channel_id, cell.id)) == 1
    editor = runners.build_editor(channel, "ready")
    assert editor["runner"] == ready["runner"]


@pytest.mark.parametrize("drafts, detail", [
    ([draft("needs", prompt="")], "Runner is incomplete: Prompt"),
    ([draft("ghost", generator="gamma")], "not included in the suggestion"),
    ([draft("twin"), draft("twin")], "unique"),
    ([{"runner": {}, "files": [], "cell_id": "x"}], "Invalid runner draft"),
])
def test_an_unready_or_invalid_draft_refuses_the_creation(basic, drafts, detail):
    client, _root = basic
    response = create_suggested(client, "Refused", runners=drafts)
    assert response.status_code == 400, response.text
    assert detail in response.json()["detail"]
    assert client.get("/api/channels").json()["channels"] == []
    assert _no_channel_files()


def test_an_unknown_suggestion_is_refused(basic):
    client, _root = basic
    response = create_suggested(client, "Unknown", "missing")
    assert response.status_code == 400 and "Unknown suggestion" in response.json()["detail"]


def test_a_package_admission_refuses_the_whole_creation(basic):
    client, root = basic
    (root / "renderers" / "views" / "widget" / "main.tsx").write_text("export default {{{ broken\n")
    response = create_suggested(client, "Broken")
    assert response.status_code == 400, response.text
    assert client.get("/api/channels").json()["channels"] == []
    assert _no_channel_files()


def test_the_bundled_standard_suggestion_creates_with_its_ready_drafts(app):
    catalog = app.get("/api/suggested").json()
    assert catalog["source"]["kind"] == "bundled" and catalog["error"] is None
    standard = next(s for s in catalog["suggestions"] if s["id"] == "standard")
    drafts = {row["draft"]["runner"]["id"]: row["draft"] for row in standard["runners"]}
    ready = [d for d in drafts.values() if d["runner"]["accounts"] or not any(
        g["account_requirements"] for g in standard["runner_generators"] if g["id"] == d["runner"]["generator"])]
    assert ready
    response = create_suggested(app, "Standard", "standard", runners=ready)
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    channel = _channel(response.json()["id"])
    assert [c.name for c in _capability_cells(channel)] == [
        r["name"] for r in standard["generators"] + standard["renderers"]]
    # Every capability cell records the bundled tree it was read from.
    for cell in _capability_cells(channel):
        records = provenance(channel.id, cell.id)
        assert records and {(r["source_kind"], r["source_id"]) for r in records} == {
            ("bundled", catalog["source"]["path"])}
    assert [s.name for s in channel.main_stacks] == [r["name"] for r in standard["tutorial"]]
    assert {row["id"] for row in app.get("/api/runners").json()} == {d["runner"]["id"] for d in ready}
