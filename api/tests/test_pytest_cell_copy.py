"""Copy to channel: a Capabilities cell admitted onto another channel's
Capabilities (``…/capabilities/cells/{cell_id}/copy/review`` and ``…/copy``).

The in-app counterpart of a channel copied from another one
(``test_pytest_provenance.test_a_channel_copy_records_the_source_channel``):
one cell, its packages admitted as every door admits them, each recording
the source channel as its upstream, and the libraries its ``cell.yaml``
names resolved on the target, the source's library cell travelling with
the copy when the target resolves the name nowhere. Synthetic cells only.
"""
from __future__ import annotations

import pytest

from capability_cells import capability_cells as _cells, edit as _edit, generator_cell as _generator_cell
from capability_cells import library_cell as _library_cell, place as _place
from collimate_server.models import Cell
from suggested_trees import install_libraries, provenance


@pytest.fixture
def pair(client):
    """``(client, source id, target id, generator cell, library cell)``: the
    source channel holds a library cell and a generator cell naming it, the
    target channel holds nothing yet."""
    source = client.channel_id
    generator, lib = _generator_cell(), _library_cell()
    _place(source, lib, generator)
    created = client.post("/api/channels", params={"name": "Target"})
    assert created.status_code == 200, created.text
    return client, source, created.json()["id"], generator, lib


def _url(source, cell_id, tail=""):
    return f"/api/channels/{source}/capabilities/cells/{cell_id}/copy{tail}"


def _review(client, source, cell, target):
    return client.post(_url(source, cell.id, "/review"), json={"target_channel_id": target})


def _copy(client, source, cell, target):
    review = _review(client, source, cell, target)
    assert review.status_code == 200, review.text
    return review.json(), client.post(_url(source, cell.id), json={
        "target_channel_id": target, "token": review.json()["token"]})


def test_a_copied_cell_is_admitted_on_the_target_with_its_library(pair):
    client, source, target, generator, lib = pair
    review, copied = _copy(client, source, generator, target)
    assert copied.status_code == 200, copied.text
    assert review["libraries"] == [{"id": "_greetings", "resolves": "copy", "cell": "Greetings"}]
    assert [(cell["name"], cell["brought"]) for cell in review["cells"]] == [("Greeter", False), ("Greetings", True)]
    assert [(p["kind"], p["id"]) for p in review["cells"][0]["packages"]] == [("generator", "greeter"),
                                                                               ("runner", "hello")]
    cells = _cells(target)
    assert set(cells) == {"Greeter", "Greetings"}
    copy = cells["Greeter"]
    assert str(copy.id) == copied.json()["cell_id"] != str(generator.id)
    assert {f.name for f in copy.files} == {f.name for f in generator.files}
    # Every package records the source channel as its upstream.
    for cell in cells.values():
        assert {(r["upstream_kind"], r["upstream_id"]) for r in provenance(target, cell.id)} == {("channel", source)}
    # The runner travelled with its generator and runs there.
    from collimate_server.run_staging import snapshot_generator_source
    _, writes = snapshot_generator_source("greeter", target)
    staged = {rel: file.content for rel, file in writes}
    assert staged["_greetings/__init__.py"] == "NAME = 'source library'\n"
    from collimate_server.runners import find_runner
    from collimate_server.state import require_channel
    assert find_runner(require_channel(target), "hello") is not None


def test_a_library_the_target_holds_is_not_brought(pair):
    client, source, target, generator, _ = pair
    _place(target, _library_cell(body="NAME = 'target library'\n"))
    review, copied = _copy(client, source, generator, target)
    assert copied.status_code == 200, copied.text
    assert review["libraries"] == [{"id": "_greetings", "resolves": "capabilities", "cell": None}]
    assert sorted(_cells(target)) == ["Greeter", "Greetings"]
    from collimate_server.run_staging import snapshot_generator_source
    staged = {rel: file.content for rel, file in snapshot_generator_source("greeter", target)[1]}
    assert staged["_greetings/__init__.py"] == "NAME = 'target library'\n"


def test_a_library_the_app_ships_resolves_there(client, tmp_path, monkeypatch):
    install_libraries(monkeypatch, {"_shipped": {"__init__.py": "NAME = 'app'\n"}}, tmp_path / "bundled")
    generator = _generator_cell(shared=("_shipped",))
    _place(client.channel_id, generator)
    target = client.post("/api/channels", params={"name": "Target"}).json()["id"]
    review, copied = _copy(client, client.channel_id, generator, target)
    assert copied.status_code == 200, copied.text
    assert review["libraries"] == [{"id": "_shipped", "resolves": "app", "cell": None}]
    assert list(_cells(target)) == ["Greeter"]


def test_a_library_that_resolves_nowhere_refuses_the_copy(client):
    generator = _generator_cell(shared=("_missing",))
    _place(client.channel_id, generator)
    target = client.post("/api/channels", params={"name": "Target"}).json()["id"]
    refused = _review(client, client.channel_id, generator, target)
    assert refused.status_code == 400
    assert "‘_missing’" in refused.json()["detail"] and "‘Target’" in refused.json()["detail"]
    assert _cells(target) == {}


def test_a_cell_the_target_holds_every_package_of_is_not_copied(pair):
    client, source, target, generator, _ = pair
    assert _copy(client, source, generator, target)[1].status_code == 200
    again = _review(client, source, generator, target)
    assert again.status_code == 409 and again.json()["detail"].startswith("Nothing to copy: ")


def test_a_cell_changed_since_the_review_is_refused(pair):
    client, source, target, generator, _ = pair
    review = _review(client, source, generator, target).json()
    _edit(source, generator, "greeter/main.py", "print('changed')\n")
    stale = client.post(_url(source, generator.id), json={"target_channel_id": target, "token": review["token"]})
    assert stale.status_code == 409
    assert _cells(target) == {}


def test_a_copy_into_its_own_channel_is_refused(pair):
    client, source, _, generator, _ = pair
    assert _review(client, source, generator, source).status_code == 400


def test_a_built_in_card_or_unknown_cell_is_not_copied(pair):
    import uuid
    client, source, target, _, _ = pair
    assert _review(client, source, Cell(id=uuid.uuid4(), name="x", files=[]), target).status_code == 404


def test_a_custom_role_s_structure_and_a_renderer_s_codec_travel_with_the_copy(client):
    """A role's ``structure:`` and a renderer's ``snapshot_codecs:`` are
    ordinary manifest content to these doors: the copy holds the manifest's
    bytes as the source does, and on the target the copied renderer's codec
    is trusted there (admission to the target's Capabilities is the trust)."""
    import hashlib
    import json
    import yaml
    from collimate_server.models import VirtualFile
    manifest = yaml.safe_dump({"id": "acme", "name": "Acme", "summary": "Files Acme issues", "entrypoint": "main.py",
                               "main_config": "settings.set_yaml", "account_requirements": {"tracker": {
                                   "label": "Issue tracker", "kind": "custom",
                                   "structure": {"url": "public", "auth": {"user": "public", "token": "secret"}}}}})
    source = "self.onmessage=()=>{};"
    codec = {"id": "acme.notes", "version": 1, "implementationHash": hashlib.sha256(source.encode()).hexdigest(),
             "source": source}
    renderer = ("id: acme-notes\nname: Acme notes\nsummary: Edits Acme notes\nentrypoint: main.tsx\n"
                "embed: native\nextensions: [.acmenote]\nsnapshot_codecs: [notes.codec.json]\n")
    cell = Cell(name="Acme", files=[
        VirtualFile(name="acme/acme.generator", content=manifest),
        VirtualFile(name="acme/main.py", content="import gen\n"),
        VirtualFile(name="acme/settings.set_yaml", content="fields: []\n"),
        VirtualFile(name="notes/view.renderer", content=renderer),
        VirtualFile(name="notes/main.tsx", content=(
            "import { defineRenderer } from '/api/sdk/react.js';\nimport codec from './notes.codec.json';\n"
            "export default defineRenderer({ component: () => codec.id });\n")),
        VirtualFile(name="notes/notes.codec.json", content=json.dumps(codec))])
    _place(client.channel_id, cell)
    target = client.post("/api/channels", params={"name": "Target"}).json()["id"]

    def trusted(channel):
        return {c["implementationHash"] for c in client.get(f"/api/channels/{channel}/snapshot-codecs").json()["codecs"]}
    assert codec["implementationHash"] in trusted(client.channel_id)
    assert codec["implementationHash"] not in trusted(target)
    _, copied = _copy(client, client.channel_id, cell, target)
    assert copied.status_code == 200, copied.text
    files = {f.name: f.content for f in _cells(target)["Acme"].files}
    assert files["acme/acme.generator"] == manifest
    assert files["notes/view.renderer"] == renderer
    assert codec["implementationHash"] in trusted(target)
