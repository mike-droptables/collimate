"""Add from catalog: a suggested cell onto Capabilities
(``POST /api/channels/{channel}/capabilities/add``).

The cell is read from either section of a suggestion of the synthetic tree
(``suggested_trees``), takes a fresh id, leaves its build's runner packages
out when it holds generators, and passes package admission as an import's
packages do, each package recording the catalog's source as its provenance.
It lands at the end of Capabilities. The catalog's ``held`` and the add's
409 'Already in this channel' are one rule: every package of the cell
recorded with the same contents in a Capabilities cell's provenance, or
every package the cell declares already on Capabilities.
"""
from __future__ import annotations

import json

import pytest
import yaml

from suggested_trees import build_tree, folder_hash, provenance, use_source, write


@pytest.fixture
def catalog(client, tmp_path):
    client.root = build_tree(tmp_path / "sdk")
    use_source(client, client.root)
    return client


def _channel(client):
    from state import require_channel
    return require_channel(client.channel_id)


def _add(client, folder, suggestion="basic"):
    return client.post(f"/api/channels/{client.channel_id}/capabilities/add",
                       json={"suggestion_id": suggestion, "folder": folder})


def _capabilities_cells(client):
    from cell_placement import capabilities_stack_cells
    return capabilities_stack_cells(_channel(client))


def test_a_generator_cell_lands_on_capabilities_without_its_runners(catalog):
    response = _add(catalog, "generators/first")
    assert response.status_code == 200, response.text
    answer = response.json()
    cells = _capabilities_cells(catalog)
    assert [str(c.id) for c in cells] == [answer["cell_id"]]
    (cell,) = cells
    assert cell.name == "First"
    names = {f.name for f in cell.files}
    assert "alpha/choice.generator" in names
    assert not any(name.endswith(".runner") for name in names)
    assert answer["package_skips"] == []
    assert "main_stacks" in answer and "nav_stacks" in answer
    assert "alpha" in {g["name"] for g in catalog.get("/api/generators").json()}


def test_a_renderer_cell_comes_from_the_other_section_and_goes_last(catalog):
    assert _add(catalog, "generators/second").status_code == 200
    response = _add(catalog, "renderers/views")
    assert response.status_code == 200, response.text
    assert [c.name for c in _capabilities_cells(catalog)] == ["Second", "Views"]
    assert "widget" in {r["id"] for r in catalog.get("/api/renderers").json()}


def test_the_cell_takes_a_fresh_id_and_records_the_catalog_source(catalog):
    import uuid
    answer = _add(catalog, "generators/second").json()
    assert uuid.UUID(answer["cell_id"]).version == 4
    (record,) = provenance(catalog.channel_id, answer["cell_id"])
    assert (record["source_kind"], record["source_id"]) == ("folder", str(catalog.root))
    assert (record["folder"], record["package_kind"], record["package_id"]) == ("beta", "generator", "beta")
    assert record["content_hash"] == folder_hash(catalog.root / "generators" / "second" / "beta")


def test_a_cell_the_channel_holds_is_not_added_twice(catalog):
    assert _add(catalog, "generators/second").status_code == 200
    again = _add(catalog, "generators/second")
    assert (again.status_code, again.json()["detail"]) == (409, "Already in this channel")
    assert len(_capabilities_cells(catalog)) == 1


def _shelve(client, cell_id):
    """Take a cell off Capabilities, its channel-DB history kept."""
    from cell_placement import capabilities_stack
    from state import flush_persist_state, persist_state, state_lock
    with state_lock:
        stack = capabilities_stack(_channel(client))
        stack.cells = [c for c in stack.cells if str(c.id) != cell_id]
    persist_state()
    flush_persist_state()


def test_a_cell_the_channel_once_held_comes_back_under_a_fresh_id(catalog):
    seeded = _add(catalog, "generators/second").json()["cell_id"]
    _shelve(catalog, seeded)
    response = _add(catalog, "generators/second")
    assert response.status_code == 200, response.text
    assert response.json()["cell_id"] != seeded
    assert [c.name for c in _capabilities_cells(catalog)] == ["Second"]


def test_a_shelved_cell_added_back_reads_held_and_is_not_added_again(catalog):
    """The re-added copy has a fresh id, but its packages are the catalog
    cell's: the catalog says held, and a further add is the plain 409."""
    seeded = _add(catalog, "generators/second").json()["cell_id"]
    _shelve(catalog, seeded)
    assert _catalog_rows(catalog)[1]["generators/second"]["held"] is False
    copy = _add(catalog, "generators/second").json()["cell_id"]
    assert copy != seeded
    assert _catalog_rows(catalog)[1]["generators/second"]["held"] is True
    again = _add(catalog, "generators/second")
    assert (again.status_code, again.json()["detail"]) == (409, "Already in this channel")
    assert [str(c.id) for c in _capabilities_cells(catalog)] == [copy]


def _extra_cell(root, section, folder, name, packages):
    """Add the cell ``folder`` of ``section`` with ``packages`` (folder →
    definition file → body) to the tree's suggestion."""
    for package, (definition, body) in packages.items():
        write(root / section / folder / package / definition, body)
    write(root / section / folder / "cell.yaml", yaml.safe_dump({"name": name}))
    suggestion = json.loads((root / "suggested" / "basic.json").read_text())
    suggestion[section].append([name, f"{section}/{folder}"])
    write(root / "suggested" / "basic.json", json.dumps(suggestion))


def _generator_package(root, folder, gid):
    write(root / "generators" / folder / gid / f"{gid}.py", f"print('{gid}')\n")
    write(root / "generators" / folder / gid / "settings.set_yaml", "fields: []\n")
    return ("choice.generator", yaml.safe_dump({
        "id": gid, "name": gid.title(), "summary": "Again", "entrypoint": f"{gid}.py",
        "main_config": "settings.set_yaml"}))


def test_a_cell_whose_packages_are_all_on_capabilities_is_held(client, tmp_path):
    """A catalog cell under another folder whose every package id another
    capability cell already declares is held: the catalog says so and the
    add is the plain 409."""
    root = build_tree(tmp_path / "sdk")
    _extra_cell(root, "generators", "copy", "Copy", {"beta": _generator_package(root, "copy", "beta")})
    use_source(client, root)
    assert _catalog_rows(client)[1]["generators/copy"]["held"] is False
    assert _add(client, "generators/second").status_code == 200
    assert _catalog_rows(client)[1]["generators/copy"]["held"] is True
    held = _add(client, "generators/copy")
    assert (held.status_code, held.json()["detail"]) == (409, "Already in this channel")
    assert [c.name for c in _capabilities_cells(client)] == ["Second"]


def test_a_cell_sharing_only_some_packages_is_not_held(client, tmp_path):
    """A partial overlap is not held: the add brings the packages
    Capabilities lacks and reports the held one."""
    root = build_tree(tmp_path / "sdk")
    _extra_cell(root, "generators", "mixed", "Mixed", {
        "beta": _generator_package(root, "mixed", "beta"), "gamma": _generator_package(root, "mixed", "gamma")})
    use_source(client, root)
    assert _add(client, "generators/second").status_code == 200
    assert _catalog_rows(client)[1]["generators/mixed"]["held"] is False
    response = _add(client, "generators/mixed")
    assert response.status_code == 200, response.text
    assert [skip["id"] for skip in response.json()["package_skips"]] == ["beta"]
    mixed = next(c for c in _capabilities_cells(client) if c.name == "Mixed")
    assert {f.name.split("/", 1)[0] for f in mixed.files} == {"cell.yaml", "gamma"}


def test_nothing_to_add_names_each_reason_once(client, tmp_path):
    """Two packages refused for the same reason (a built-in surface) are
    not held, and the 409 says the reason once."""
    root = build_tree(tmp_path / "sdk")
    tsx = "import { defineRenderer } from '/api/sdk/react.js';\nexport default defineRenderer({ component: () => null });\n"
    packages = {}
    for rid in ("one", "two"):
        write(root / "renderers" / "claims" / rid / "main.tsx", tsx)
        packages[rid] = ("view.renderer", f"id: {rid}\nname: {rid.title()}\nsummary: Claims\nentrypoint: main.tsx\n"
                                          "embed: native\nextensions: [.chat]\n")
    _extra_cell(root, "renderers", "claims", "Claims", packages)
    use_source(client, root)
    assert _catalog_rows(client)[1]["renderers/claims"]["held"] is False
    response = _add(client, "renderers/claims")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Nothing to add: Renderer extension .chat is a built-in renderer's surface"
    assert _capabilities_cells(client) == []


@pytest.mark.parametrize(("suggestion", "folder"), [("nope", "generators/first"), ("basic", "generators/nope"),
                                                    ("basic", "suggested/tutorial/tour")])
def test_an_unknown_suggestion_or_folder_is_a_404(catalog, suggestion, folder):
    response = _add(catalog, folder, suggestion)
    assert response.status_code == 404, response.text
    assert _capabilities_cells(catalog) == []


def test_an_add_request_is_bounded(catalog):
    url = f"/api/channels/{catalog.channel_id}/capabilities/add"
    for body in ({"suggestion_id": "basic"}, {"suggestion_id": "", "folder": "generators/first"},
                 {"suggestion_id": "basic", "folder": "generators/first", "kind": "generator"}):
        assert catalog.post(url, json=body).status_code == 422, body


def _catalog_rows(client):
    response = client.get(f"/api/channels/{client.channel_id}/capabilities/catalog")
    assert response.status_code == 200, response.text
    (entry,) = response.json()["suggestions"]
    return entry, {row["folder"]: row for row in entry["cells"]}


def test_the_channel_catalog_lists_each_cell_with_whether_it_is_held(catalog):
    entry, rows = _catalog_rows(catalog)
    assert (entry["id"], entry["name"], entry["description"]) == ("basic", "Basic", "Synthetic suggestion")
    assert entry["cells"] == [
        {"folder": "generators/first", "name": "First", "section": "generator", "held": False},
        {"folder": "generators/second", "name": "Second", "section": "generator", "held": False},
        {"folder": "renderers/views", "name": "Views", "section": "renderer", "held": False}]


def test_held_follows_the_provenance_on_capabilities(catalog):
    """``held`` is the add route's 409 rule. A board cell that only shares
    the catalog cell's name holds nothing; the added cell holds it by its
    provenance even once its package is renamed in the cell (no id on
    Capabilities is the catalog cell's any more); moved off Capabilities, it
    holds nothing and the add brings a new copy."""
    import uuid

    from cell_commits import commit_source_cell
    from cell_placement import capabilities_stack
    from models import Cell, Stack, VirtualFile
    from state import persist_state, state_lock

    with state_lock:
        _channel(catalog).main_stacks.append(Stack(name="Board", cells=[Cell(id=uuid.uuid4(), name="Views")]))
    persist_state()
    assert _catalog_rows(catalog)[1]["renderers/views"]["held"] is False

    added = _add(catalog, "renderers/views").json()["cell_id"]
    rows = _catalog_rows(catalog)[1]
    assert rows["renderers/views"]["held"] is True
    assert rows["generators/first"]["held"] is False

    (cell,) = [c for c in _capabilities_cells(catalog) if str(c.id) == added]
    commit_source_cell(_channel(catalog), cell, [
        VirtualFile(name=f.name, language=f.language,
                    content=f.content.replace("id: widget", "id: my-widget")) for f in cell.files])
    assert "id: my-widget" in next(f.content for f in cell.files if f.name == "widget/view.renderer")
    assert _catalog_rows(catalog)[1]["renderers/views"]["held"] is True
    again = _add(catalog, "renderers/views")
    assert (again.status_code, again.json()["detail"]) == (409, "Already in this channel")

    with state_lock:
        caps = capabilities_stack(_channel(catalog))
        caps.cells.remove(cell)
        _channel(catalog).main_stacks.append(Stack(name="Moved", cells=[cell]))
    persist_state()
    assert _catalog_rows(catalog)[1]["renderers/views"]["held"] is False
    response = _add(catalog, "renderers/views")
    assert response.status_code == 200, response.text
    assert response.json()["cell_id"] != added


def test_an_unparseable_definition_on_capabilities_does_not_stop_a_catalog_add(catalog):
    """A definition that does not parse claims no id or type: admission of
    the catalog cell's packages skips it."""
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    add_capabilities_cell(_channel(catalog), Cell(name="Broken", files=[
        VirtualFile(name="x/x.handler", content="id: [unclosed\n")]))
    response = _add(catalog, "generators/first")
    assert response.status_code == 200, response.text
    assert response.json()["package_skips"] == []
    assert [c.name for c in _capabilities_cells(catalog)] == ["Broken", "First"]
