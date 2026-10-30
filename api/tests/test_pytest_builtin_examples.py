"""Examples a channel adds to a built-in handler (``routes/builtins.py``).

The files are copied from another cell into the Definitions card's overlay
(``<handler>/examples/<folder>/``) and declared in ``<handler>/additions.yaml``;
the catalog record lists the build's examples and the added ones (``added``).
The build's own examples are never changed.
"""
from __future__ import annotations

import uuid

import pytest

REFUSAL = "Built-in packages are part of the app"
NOTE = ("generator: example\nid: example\nname: Example\npermissions:\n  commands: deny\n"
        "  docker: none\n  internet: full\nsettings:\n  settings.set_yaml: {}\n")


def _channel(channel_id):
    from state import state
    return next(c for c in state.channels if str(c.id) == str(channel_id))


def _source(client, files: dict[str, str]):
    """A new standard cell holding ``files``; answers its id."""
    response = client.post(f"/api/stacks/{client.channel_id}/create")
    assert response.status_code == 200, response.text
    cell_id = response.json()["main_stacks"][-1]["cells"][0]["id"]
    mutations = [dict(path=path, writer_id="tab:1", edit_seq=1, base_revision=0, kind="put", content_text=text)
                 for path, text in files.items()]
    put = client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate",
                      json={"operation_id": str(uuid.uuid4()), "mutations": mutations})
    assert put.status_code == 200, put.text
    return cell_id


def _examples_url(client, handler="runner"):
    return f"/api/channels/{client.channel_id}/builtin/handlers/{handler}/examples"


def _record(client, handler="runner"):
    return next(h for h in client.get("/api/handlers").json() if h["builtin"] and h["id"] == handler)


def _card_files(client):
    from builtin_packages import handler_card
    from persistence import snapshot_files
    from channel_db import open_channel_db
    conn = open_channel_db(client.channel_id)
    try:
        return {f["path"] for f in snapshot_files(conn, handler_card(_channel(client.channel_id)))}
    finally:
        conn.close()


@pytest.fixture
def added(client):
    """An example added to the runner handler: ``(client, folder)``."""
    source = _source(client, {"reports/saved.runner": NOTE})
    response = client.post(_examples_url(client), json={
        "source_cell_id": source, "paths": ["reports/saved.runner"],
        "description": "A short runner", "expect": "valid", "feedback": "It is complete."})
    assert response.status_code == 200, response.text
    return client, response.json()["folder"], response.json()


def test_an_added_example_is_copied_declared_and_listed(added):
    client, folder, answer = added
    assert folder.startswith("example-") and len(folder) == len("example-") + 8
    assert _card_files(client) == {f"runner/examples/{folder}/saved.runner",
                                   "runner/additions.yaml"}
    record = _record(client)
    assert answer["handler"] == record
    assert [(e["folder"], e["added"]) for e in record["examples"]] == [
        ("basic", False), (folder, True)]
    mine = record["examples"][-1]
    assert (mine["description"], mine["expect"], mine["feedback"]) == ("A short runner", "valid", "It is complete.")
    body = client.get(f"/api/channels/{client.channel_id}/cells/{record['cell_id']}/files/"
                      f"runner/examples/{folder}/saved.runner")
    assert body.status_code == 200 and body.text == NOTE


def test_an_added_example_runs_through_the_builds_verifier(added):
    client, folder, _answer = added
    record = _record(client)
    response = client.post(f"/api/channels/{client.channel_id}/cells/{record['cell_id']}/handler-build/inspect",
                           json={"folder": "runner"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["builtin"] is True and result["compile"]["valid"] is True
    assert [e["folder"] for e in result["examples"]] == ["basic", folder]
    assert all(e["verdicts"] for e in result["examples"])


def test_patch_changes_only_the_added_declaration(added):
    client, folder, _answer = added
    response = client.patch(f"{_examples_url(client)}/{folder}", json={"expect": "invalid", "feedback": "No."})
    assert response.status_code == 200, response.text
    mine = response.json()["handler"]["examples"][-1]
    assert (mine["description"], mine["expect"], mine["feedback"]) == ("A short runner", "invalid", "No.")
    shipped = client.patch(f"{_examples_url(client)}/basic", json={"expect": "invalid"})
    assert shipped.status_code == 400 and shipped.json()["detail"] == REFUSAL
    unknown = client.patch(f"{_examples_url(client)}/example-00000000", json={"expect": "invalid"})
    assert unknown.status_code == 404


def test_delete_removes_the_files_and_the_declaration(added):
    client, folder, _answer = added
    response = client.delete(f"{_examples_url(client)}/{folder}")
    assert response.status_code == 200, response.text
    assert [e["folder"] for e in response.json()["handler"]["examples"]] == ["basic"]
    assert _card_files(client) == {"runner/additions.yaml"}
    shipped = client.delete(f"{_examples_url(client)}/basic")
    assert shipped.status_code == 400


@pytest.mark.parametrize(("paths", "status", "detail"), [
    (["reports/notes.txt"], 400, "not a type"),
    (["reports/saved.runner", "other/saved.runner"], 400, "distinct names"),
    (["reports/missing.runner"], 404, "not in the source cell"),
])
def test_an_example_needs_existing_covered_files(client, paths, status, detail):
    source = _source(client, {"reports/saved.runner": NOTE, "reports/notes.txt": "x",
                              "other/saved.runner": NOTE})
    response = client.post(_examples_url(client), json={
        "source_cell_id": source, "paths": paths, "description": "d", "expect": "valid", "feedback": ""})
    assert response.status_code == status, response.text
    assert detail in response.json()["detail"]
    assert _card_files(client) == set()


def test_an_example_is_never_copied_from_a_card_or_to_an_unknown_handler(client):
    from builtin_packages import handler_card
    card = handler_card(_channel(client.channel_id))
    response = client.post(_examples_url(client), json={
        "source_cell_id": card, "paths": ["runner/examples/basic/sample.runner"],
        "description": "d", "expect": "valid", "feedback": ""})
    assert response.status_code == 400 and response.json()["detail"] == REFUSAL
    unknown = client.post(_examples_url(client, "nope"), json={
        "source_cell_id": card, "paths": ["a.runner"], "description": "d", "expect": "valid", "feedback": ""})
    assert unknown.status_code == 404
    bad = client.post(_examples_url(client), json={
        "source_cell_id": card, "paths": ["a.runner"], "description": "d", "expect": "maybe", "feedback": ""})
    assert bad.status_code == 422


def test_an_added_renderer_example_is_never_a_live_package(client):
    """An added example that is itself a definition file stays content of
    the card: no renderer listing, Add to target or catalog entry sees it."""
    manifest = "id: sample\nname: Sample\nsummary: s\nentrypoint: main.tsx\nembed: native\nextensions: [.sample]\n"
    source = _source(client, {"pkg/sample.renderer": manifest})
    response = client.post(_examples_url(client, "renderer"), json={
        "source_cell_id": source, "paths": ["pkg/sample.renderer"], "description": "d",
        "expect": "invalid", "feedback": "No entrypoint file."})
    assert response.status_code == 200, response.text
    assert "sample" not in {r["id"] for r in client.get("/api/renderers").json()}
    assert "sample" not in {h["id"] for h in client.get("/api/handlers").json()}
    assert client.get(f"/api/channels/{client.channel_id}/outline").json()["capabilities_cells"] == []
