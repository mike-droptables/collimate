"""The update path (``definition_updates``): a Capabilities cell follows its
upstream, shows an update when the upstream's copy changes, and Review and
replace brings that copy in as a new version of the same cell, keeping the
cell's runners.

The in-app counterpart of the developer reset it replaces (a matched cell
re-admitted on the same id with its own runners, the others left alone,
matching by provenance only), for each followed upstream: the app's
content after an app update, a republished catalog entry, and a copied
cell's source. Synthetic trees and cells only.
"""
from __future__ import annotations

import pytest
import yaml

from capability_cells import capability_cells, edit, generator_cell, library_cell, place, staged
from suggested_trees import build_tree, create_suggested, provenance, write


@pytest.fixture
def content(client, tmp_path, monkeypatch):
    """``(client, root)``: the app's content is the synthetic tree at ``root``."""
    import collimate_server.genesis as genesis
    root = build_tree(tmp_path / "content")
    real = genesis.bundled_dir
    monkeypatch.setattr(genesis, "bundled_dir", lambda tree: str(root) if tree == "content" else real(tree))
    return client, root


def _seeded(client, name="Seeded"):
    """A channel from the synthetic suggestion, its complete ``ready`` runner draft kept."""
    offered = client.get("/api/suggested").json()["suggestions"][0]["runners"]
    ready = [item["draft"] for item in offered if item["draft"]["runner"]["id"] == "ready"]
    response = create_suggested(client, name, tutorial=False, runners=ready)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _updates(client, channel):
    response = client.get(f"/api/channels/{channel}/capabilities/updates")
    assert response.status_code == 200, response.text
    names = {str(c.id): name for name, c in capability_cells(channel).items()}
    return {names[row["cell_id"]]: row for row in response.json()["cells"]}


def _review(client, channel, cell):
    return client.post(f"/api/channels/{channel}/capabilities/cells/{cell.id}/replace/review")


def _replace(client, channel, cell):
    review = _review(client, channel, cell)
    assert review.status_code == 200, review.text
    return review.json(), client.post(f"/api/channels/{channel}/capabilities/cells/{cell.id}/replace",
                                      json={"token": review.json()["token"]})


def _versions(channel, cell_id) -> int:
    from collimate_server.cell_files import walk_versions
    from collimate_server.database import get_db_connection
    conn = get_db_connection(channel)
    try:
        return len(walk_versions(conn, str(cell_id)))
    finally:
        conn.close()


def test_an_app_update_shows_and_review_and_replace_keeps_the_runners(content):
    client, root = content
    channel = _seeded(client)
    assert {name: row["status"] for name, row in _updates(client, channel).items()} == {
        "First": "current", "Second": "current", "Views": "current"}
    # An app update changes the content cell First.
    write(root / "generators" / "first" / "alpha" / "alpha.py", "print('alpha, updated')\n")
    write(root / "generators" / "first" / "alpha" / "notes.md", "New in this release.\n")
    rows = _updates(client, channel)
    assert {name: row["status"] for name, row in rows.items()} == {
        "First": "update", "Second": "current", "Views": "current"}
    assert rows["First"]["upstream"] == {"kind": "app", "id": "", "label": "the app"}
    assert rows["First"]["edited"] is False
    first = capability_cells(channel)["First"]
    before_versions = _versions(channel, first.id)
    runner_record = next(r for r in provenance(channel, first.id) if r["package_kind"] == "runner")
    review, replaced = _replace(client, channel, first)
    assert replaced.status_code == 200, replaced.text
    assert [(p["kind"], p["id"], p["change"]) for p in review["packages"]] == [("generator", "alpha", "changed")]
    assert review["runners"] == ["Ready"] and review["edited"] is False
    # The same cell, a new version, the content's files and its own runner.
    after = capability_cells(channel)["First"]
    assert after.id == first.id and after.name == "First"
    files = {f.name: f for f in after.files}
    assert files["alpha/alpha.py"].content == "print('alpha, updated')\n" and "alpha/notes.md" in files
    assert any(name.endswith("/ready.runner") for name in files)
    assert not any(name.startswith("needs/") for name in files)
    assert _versions(channel, first.id) == before_versions + 1
    records = provenance(channel, first.id)
    assert next(r for r in records if r["package_kind"] == "runner") == runner_record
    assert {r["upstream_kind"] for r in records} == {"app"}
    assert _updates(client, channel)["First"]["status"] == "current"
    # The runner still runs the replaced generator.
    assert {row["id"] for row in client.get("/api/runners").json()} == {"ready"}
    assert staged(channel, "alpha")["alpha/alpha.py"] == "print('alpha, updated')\n"


def test_a_current_cell_or_one_that_follows_nothing_has_no_replace(content):
    client, _ = content
    channel = _seeded(client)
    first = capability_cells(channel)["First"]
    current = _review(client, channel, first)
    assert current.status_code == 409 and "up to date" in current.json()["detail"]
    lonely = generator_cell("lonely", shared=(), runner=False, name="Lonely")
    place(channel, lonely)
    assert "Lonely" not in _updates(client, channel)
    assert _review(client, channel, lonely).status_code == 409


def test_local_edits_are_flagged_and_discarded_by_replace(content):
    client, root = content
    channel = _seeded(client)
    second = capability_cells(channel)["Second"]
    edit(channel, second, "beta/beta.py", "print('mine')\n")
    assert _updates(client, channel)["Second"] == _updates(client, channel)["Second"] | {"status": "current", "edited": True}
    write(root / "generators" / "second" / "beta" / "beta.py", "print('beta 2')\n")
    review, replaced = _replace(client, channel, second)
    assert review["edited"] is True and replaced.status_code == 200
    assert {f.name: f.content for f in capability_cells(channel)["Second"].files}["beta/beta.py"] == "print('beta 2')\n"
    assert _updates(client, channel)["Second"]["edited"] is False


def test_a_replace_reviewed_before_a_change_is_refused(content):
    client, root = content
    channel = _seeded(client)
    write(root / "generators" / "second" / "beta" / "beta.py", "print('beta 2')\n")
    second = capability_cells(channel)["Second"]
    review = _review(client, channel, second).json()
    write(root / "generators" / "second" / "beta" / "beta.py", "print('beta 3')\n")
    stale = client.post(f"/api/channels/{channel}/capabilities/cells/{second.id}/replace", json={"token": review["token"]})
    assert stale.status_code == 409


def test_a_channel_from_before_the_upstream_link_follows_the_app(content):
    """A channel whose records were written as content-tree sources before
    channel migration 33 follows the app's content after it: its rows keep
    their hashes, so an unchanged cell is current and a changed one has an
    update."""
    import collimate_server.migrations as migrations
    from collimate_server.database import get_db_connection
    client, root = content
    channel = _seeded(client)
    conn = get_db_connection(channel)
    try:
        rows = conn.execute("SELECT cell_id, folder, package_kind, package_id, package_version, content_hash, "
                            "app_version, admitted_at FROM cell_provenance").fetchall()
        conn.execute("DROP TABLE cell_provenance")
        conn.execute("""CREATE TABLE cell_provenance (cell_id TEXT NOT NULL, folder TEXT NOT NULL,
            source_kind TEXT NOT NULL, source_id TEXT NOT NULL, package_kind TEXT NOT NULL, package_id TEXT NOT NULL,
            package_version TEXT, content_hash TEXT NOT NULL, sdk_version TEXT NOT NULL, admitted_at TEXT NOT NULL,
            PRIMARY KEY (cell_id, folder))""")
        conn.executemany("INSERT INTO cell_provenance VALUES (?,?,'folder','/old/content',?,?,?,?,?,?)",
                         [tuple(row) for row in rows])
        migrations._channel_v33_upstream_provenance(conn)
        conn.commit()
    finally:
        conn.close()
    assert {name: row["status"] for name, row in _updates(client, channel).items()} == {
        "First": "current", "Second": "current", "Views": "current"}
    write(root / "generators" / "second" / "beta" / "beta.py", "print('beta 2')\n")
    assert _updates(client, channel)["Second"]["status"] == "update"
    assert _replace(client, channel, capability_cells(channel)["Second"])[1].status_code == 200


def test_a_republished_catalog_entry_shows_in_the_channel_that_added_it(client):
    source = client.channel_id
    generator = generator_cell(shared=(), runner=False, body="print('one')\n")
    place(source, generator)
    review = client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/publish/review").json()
    entry = client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/publish",
                        json={"token": review["token"]}).json()
    other = client.post("/api/channels", params={"name": "Other"}).json()["id"]
    added = client.post(f"/api/channels/{other}/capabilities/catalog/entries/{entry['id']}/add")
    assert added.status_code == 200, added.text
    assert _updates(client, other)["Greeter"]["status"] == "current"
    edit(source, generator, "greeter/main.py", "print('two')\n")
    review = client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/publish/review").json()
    assert client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/publish",
                       json={"token": review["token"]}).status_code == 200
    row = _updates(client, other)["Greeter"]
    assert (row["status"], row["upstream"]) == ("update", {"kind": "catalog", "id": entry["id"],
                                                           "label": "‘Greeter’ in your catalog"})
    copy = capability_cells(other)["Greeter"]
    assert _replace(client, other, copy)[1].status_code == 200
    assert {f.name: f.content for f in capability_cells(other)["Greeter"].files}["greeter/main.py"] == "print('two')\n"
    # Removed from the catalog, the cell's upstream is gone.
    assert client.delete(f"/api/catalog/entries/{entry['id']}").status_code == 200
    assert _updates(client, other)["Greeter"]["status"] == "missing"


def test_a_copied_cell_follows_its_source_and_keeps_its_runner(client):
    source = client.channel_id
    generator, lib = generator_cell(), library_cell()
    place(source, lib, generator)
    target = client.post("/api/channels", params={"name": "Target"}).json()["id"]
    review = client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/copy/review",
                         json={"target_channel_id": target}).json()
    assert client.post(f"/api/channels/{source}/capabilities/cells/{generator.id}/copy",
                       json={"target_channel_id": target, "token": review["token"]}).status_code == 200
    assert {name: row["status"] for name, row in _updates(client, target).items()} == {
        "Greeter": "current", "Greetings": "current"}
    # A runner edit at the source is no update: runners are each channel's own.
    edit(source, generator, "hello/hello.runner", yaml.safe_dump({
        "id": "hello", "generator": "greeter", "name": "Hello there", "description": "", "tags": [], "prompt": "",
        "settings": {"settings.set_yaml": {}}, "folders": [], "accounts": {}, "permissions": {}, "packages": {}}))
    assert _updates(client, target)["Greeter"]["status"] == "current"
    edit(source, generator, "greeter/main.py", "from _greetings import NAME\nprint(NAME, 'v2')\n")
    edit(source, lib, "_greetings/__init__.py", "NAME = 'library v2'\n")
    rows = _updates(client, target)
    assert (rows["Greeter"]["status"], rows["Greetings"]["status"]) == ("update", "update")
    assert rows["Greeter"]["upstream"]["label"] == f"‘Greeter’ in the channel ‘{client.get('/api/channels').json()['channels'][0]['name']}’"
    cells = capability_cells(target)
    # Replacing the library lists the generators that name it.
    review, replaced = _replace(client, target, cells["Greetings"])
    assert review["named_by"] == [{"id": "_greetings", "cells": ["Greeter"]}]
    assert replaced.status_code == 200
    review, replaced = _replace(client, target, cells["Greeter"])
    assert review["libraries"] == [{"id": "_greetings", "resolves": "capabilities"}]
    assert review["runners"] == ["Hello"] and replaced.status_code == 200
    runner = next(f for f in capability_cells(target)["Greeter"].files if f.name.endswith(".runner"))
    assert yaml.safe_load(runner.content)["name"] == "Hello"
    assert staged(target, "greeter")["_greetings/__init__.py"] == "NAME = 'library v2'\n"
