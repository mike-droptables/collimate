"""The user catalog (``user_catalog``): Publish to catalog, Add from catalog
of an entry, and saved suggestions on the New channel page.

The in-app counterparts of the app's catalog tests
(``test_pytest_capability_catalog_add.py``: a catalog cell lands on
Capabilities without its runners, records its source, is held once added)
and of creating a channel from a suggestion
(``test_pytest_channel_create.py``). Synthetic cells and trees only.
"""
from __future__ import annotations

import pytest

from capability_cells import capability_cells, edit, generator_cell, library_cell, place, staged
from suggested_trees import build_tree, provenance


@pytest.fixture
def source(client):
    """``(client, channel id, generator cell, library cell)``: a channel whose
    Capabilities hold a library and a generator cell naming it."""
    generator, lib = generator_cell(), library_cell()
    place(client.channel_id, lib, generator)
    return client, client.channel_id, generator, lib


def _publish(client, channel, cell):
    review = client.post(f"/api/channels/{channel}/capabilities/cells/{cell.id}/publish/review")
    assert review.status_code == 200, review.text
    published = client.post(f"/api/channels/{channel}/capabilities/cells/{cell.id}/publish",
                            json={"token": review.json()["token"]})
    return review.json(), published


def _listing(client, channel):
    response = client.get(f"/api/channels/{channel}/capabilities/catalog/entries")
    assert response.status_code == 200, response.text
    return {row["name"]: row for row in response.json()["entries"]}


def _new_channel(client, name="Other"):
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_a_published_generator_lists_its_libraries_and_leaves_its_runners_out(source):
    client, channel, generator, _ = source
    review, published = _publish(client, channel, generator)
    assert published.status_code == 200, published.text
    assert review["entry"] is None and review["unchanged"] is False
    assert [(p["kind"], p["id"]) for p in review["packages"]] == [("generator", "greeter")]
    # The library is not in the catalog yet: the review names the cell to publish too.
    assert review["libraries"] == [{"id": "_greetings", "resolves": None, "entry": None, "cell": "Greetings"}]
    entry = published.json()
    assert (entry["name"], entry["section"], entry["libraries"], entry["revision"]) == (
        "Greeter", "generator", ["_greetings"], 1)
    assert [(p["kind"], p["id"]) for p in entry["packages"]] == [("generator", "greeter")]
    from collimate_server.user_catalog import entry as stored
    assert not any(path.startswith("hello/") for path in stored(entry["id"]).bodies)


def test_an_entry_published_listed_and_added_follows_the_catalog(source):
    client, channel, generator, lib = source
    _publish(client, channel, lib)
    review, published = _publish(client, channel, generator)
    assert review["libraries"] == [{"id": "_greetings", "resolves": "catalog", "entry": "Greetings", "cell": None}]
    other = _new_channel(client)
    listing = _listing(client, other)
    assert {name: (row["section"], row["held"]) for name, row in listing.items()} == {
        "Greeter": ("generator", False), "Greetings": ("library", False)}
    added = client.post(f"/api/channels/{other}/capabilities/catalog/entries/{published.json()['id']}/add")
    assert added.status_code == 200, added.text
    # The library entry came with it; both follow their entries.
    cells = capability_cells(other)
    assert set(cells) == {"Greeter", "Greetings"}
    assert [str(cells["Greeter"].id), str(cells["Greetings"].id)] == added.json()["cell_ids"]
    for name, cell in cells.items():
        entry_id = listing[name]["id"]
        assert {(r["upstream_kind"], r["upstream_id"]) for r in provenance(other, cell.id)} == {("catalog", entry_id)}
    assert staged(other, "greeter")["_greetings/__init__.py"] == "NAME = 'source library'\n"
    # Held from then on, and a second add is the plain 409.
    assert _listing(client, other)["Greeter"]["held"] is True
    again = client.post(f"/api/channels/{other}/capabilities/catalog/entries/{published.json()['id']}/add")
    assert (again.status_code, again.json()["detail"]) == (409, "Already in this channel")


def test_an_entry_whose_library_no_entry_holds_is_refused(source):
    client, channel, generator, _ = source
    entry = _publish(client, channel, generator)[1].json()
    other = _new_channel(client)
    refused = client.post(f"/api/channels/{other}/capabilities/catalog/entries/{entry['id']}/add")
    assert refused.status_code == 400 and "‘_greetings’" in refused.json()["detail"]
    assert capability_cells(other) == {}


def test_republishing_the_cell_updates_its_entry(source):
    client, channel, generator, _ = source
    first = _publish(client, channel, generator)[1].json()
    review, again = _publish(client, channel, generator)
    assert review["unchanged"] is True and again.status_code == 409
    edit(channel, generator, "greeter/main.py", "print('two')\n")
    review, second = _publish(client, channel, generator)
    assert review["entry"]["id"] == first["id"]
    assert second.status_code == 200 and (second.json()["id"], second.json()["revision"]) == (first["id"], 2)


def test_a_stale_publish_is_refused(source):
    client, channel, generator, _ = source
    review = client.post(f"/api/channels/{channel}/capabilities/cells/{generator.id}/publish/review").json()
    edit(channel, generator, "greeter/main.py", "print('changed')\n")
    stale = client.post(f"/api/channels/{channel}/capabilities/cells/{generator.id}/publish",
                        json={"token": review["token"]})
    assert stale.status_code == 409


def test_an_entry_a_saved_suggestion_lists_is_not_removed(source):
    client, channel, _, lib = source
    entry = _publish(client, channel, lib)[1].json()
    saved = client.post("/api/catalog/suggestions", json={
        "name": "Mine", "items": [{"source": "catalog", "id": entry["id"]}]})
    assert saved.status_code == 200, saved.text
    refused = client.delete(f"/api/catalog/entries/{entry['id']}")
    assert refused.status_code == 409 and "‘Mine’" in refused.json()["detail"]
    assert client.delete(f"/api/catalog/suggestions/{saved.json()['id']}").status_code == 200
    assert client.delete(f"/api/catalog/entries/{entry['id']}").status_code == 200
    assert _listing(client, channel) == {}


def test_a_saved_suggestion_creates_a_channel(source, tmp_path, monkeypatch):
    """App cells and catalog entries together, with a runner draft: the
    channel holds them, each following its upstream, and the runner."""
    import collimate_server.genesis as genesis
    root = build_tree(tmp_path / "content")
    real = genesis.bundled_dir
    monkeypatch.setattr(genesis, "bundled_dir", lambda tree: str(root) if tree == "content" else real(tree))
    client, channel, generator, lib = source
    _publish(client, channel, lib)
    entry = _publish(client, channel, generator)[1].json()
    saved = client.post("/api/catalog/suggestions", json={
        "name": "Mine", "description": "My setup",
        "items": [{"source": "app", "section": "generator", "folder": "generators/second", "name": "Second"},
                  {"source": "catalog", "id": entry["id"]}],
        "channel_id": channel, "runner_ids": ["hello"]})
    assert saved.status_code == 200, saved.text
    # The library entry the generator needs is listed too.
    assert saved.json()["added"] == ["Greetings"]
    (offered,) = client.get("/api/catalog/suggestions").json()["suggestions"]
    assert (offered["name"], offered["saved"], offered["problem"]) == ("Mine", True, None)
    assert [g["name"] for g in offered["generators"]] == ["Second", "Greeter"]
    assert [g["name"] for g in offered["libraries"]] == ["Greetings"]
    assert {g["id"] for g in offered["runner_generators"]} == {"beta", "greeter"}
    (draft,) = offered["runners"]
    assert draft["draft"]["runner"]["id"] == "hello"
    created = client.post("/api/channels", params={"name": "From saved"}, json={
        "saved": {"id": offered["id"], "runners": [draft["draft"]]}})
    assert created.status_code == 200, created.text
    new = created.json()["id"]
    cells = capability_cells(new)
    assert set(cells) == {"Second", "Greeter", "Greetings"}
    assert {(r["upstream_kind"], r["upstream_id"]) for r in provenance(new, cells["Second"].id)} == {("app", "")}
    assert {r["upstream_kind"] for r in provenance(new, cells["Greeter"].id)} == {"catalog"}
    assert any(f.name.endswith("/hello.runner") for f in cells["Greeter"].files)
    assert {row["id"] for row in client.get("/api/runners").json()} == {"hello"}


def test_a_saved_suggestion_refuses_a_runner_no_listed_cell_runs(source):
    client, channel, _, lib = source
    entry = _publish(client, channel, lib)[1].json()
    refused = client.post("/api/catalog/suggestions", json={
        "name": "Mine", "items": [{"source": "catalog", "id": entry["id"]}], "channel_id": channel,
        "runner_ids": ["hello"]})
    assert refused.status_code == 400 and "greeter" in refused.json()["detail"]


def test_one_body_names_one_source(client):
    refused = client.post("/api/channels", params={"name": "Both"}, json={
        "saved": {"id": "x"}, "suggested": {"id": "standard"}})
    assert refused.status_code == 422
