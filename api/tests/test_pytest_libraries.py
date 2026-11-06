"""Libraries: ``.library`` packages and the ``shared:`` names that use them.

A generator's cell names the libraries its generators import under
``shared:`` in its ``cell.yaml``. Each name resolves to the library of that
id on the channel's Capabilities stack, else to one the app ships (the
bundled content's ``generators/``). Admission (a suggestion, a packages zip,
Add to, a move onto Capabilities) refuses a name that resolves nowhere and
copies nothing into the generator's cell; a run's staging places each
resolved library beside the generator. Synthetic trees and libraries only
(``suggested_trees``), but for the bundled Standard case at the end.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from collimate_server.models import Cell, Channel, Stack, VirtualFile
from suggested_trees import MAIN, build_tree, create_suggested, install_libraries, library, provenance, use_content, write


@pytest.fixture
def app(api_app):
    from fastapi.testclient import TestClient
    with TestClient(api_app) as client:
        yield client


@pytest.fixture
def client(channel_client):
    """A client on a fresh empty channel (``client.channel_id``)."""
    return channel_client[0]


def _cells(channel_id: str) -> dict:
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.state import require_channel
    return {cell.name: cell for cell in capabilities_stack_cells(require_channel(channel_id))}


def _names(cell) -> set[str]:
    return {f.name for f in cell.files}


def _tree(root, *, shared, libraries=(), suggest=()):
    """The synthetic tree with ``First`` naming ``shared``, the libraries
    ``libraries`` in its generators tree and ``suggest`` of them listed in its
    suggestion's ``libraries`` section."""
    build_tree(root)
    for name in libraries:
        library(root / "generators", name)
    write(root / "generators" / "first" / "cell.yaml",
          yaml.safe_dump({"name": "First", "icon": "scale", "order": 1, "shared": list(shared)}))
    path = root / "suggested" / "basic.json"
    data = json.loads(path.read_text())
    data["libraries"] = [[name.strip("_").title(), f"generators/{name}"] for name in suggest]
    path.write_text(json.dumps(data))
    return root


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

def test_a_suggested_library_is_a_cell_of_its_own_and_the_generator_cell_holds_no_copy(app, tmp_path, monkeypatch):
    root = _tree(tmp_path / "sdk", shared=["_tools"], libraries=["_tools"], suggest=["_tools"])
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Seeded", tutorial=False)
    assert response.status_code == 200, response.text
    cells = _cells(response.json()["id"])
    assert _names(cells["Tools"]) == {"_tools/_tools.library", "_tools/__init__.py"}
    assert cells["Tools"].icon == "library"
    assert not any(name.startswith("_") for name in _names(cells["First"]))
    records = provenance(response.json()["id"], cells["Tools"].id)
    assert [(r["package_kind"], r["package_id"], r["folder"]) for r in records] == [("library", "_tools", "_tools")]


def test_a_name_neither_capabilities_nor_the_app_holds_refuses_the_cell(app, tmp_path, monkeypatch):
    root = _tree(tmp_path / "sdk", shared=["_missing"])
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Refused", tutorial=False)
    assert response.status_code == 400, response.text
    assert "‘_missing’" in response.json()["detail"] and "add it to Capabilities first" in response.json()["detail"]
    assert app.get("/api/channels").json()["channels"] == []


def test_a_name_the_app_ships_resolves_without_a_library_cell(app, tmp_path, monkeypatch):
    """A Basic-like channel: no library cell, the name resolves to the app's
    library, and nothing is copied into the generator's cell."""
    root = _tree(tmp_path / "sdk", shared=["_shipped"])
    install_libraries(monkeypatch, {"_shipped": {"__init__.py": "SHIPPED = 1\n"}}, root)
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Shipped", tutorial=False)
    assert response.status_code == 200, response.text
    cells = _cells(response.json()["id"])
    assert set(cells) == {"First", "Second", "Views"}
    assert not any(name.startswith("_") for name in _names(cells["First"]))


@pytest.mark.parametrize("shared, detail", [
    ("shared: _tools\n", "must be a list of library names"),
    ("shared: [tools]\n", "which is no library id"),
])
def test_a_malformed_shared_list_refuses_the_cell(app, tmp_path, shared, detail, monkeypatch):
    root = _tree(tmp_path / "sdk", shared=[])
    write(root / "generators" / "first" / "cell.yaml", shared)
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Malformed", tutorial=False)
    assert response.status_code == 400 and detail in response.json()["detail"], response.text


def test_a_definition_naming_another_folder_is_no_library(app, tmp_path, monkeypatch):
    root = _tree(tmp_path / "sdk", shared=[], libraries=["_tools"], suggest=["_tools"])
    second = root / "generators" / "_tools_copy"
    library(root / "generators", "_tools_copy")
    (second / "_tools_copy.library").write_text("id: _tools\nname: Copy\ndescription: Copy\n")
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Twice", tutorial=False)
    # The copy's definition names another folder, so it is no library at all.
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Run staging
# ---------------------------------------------------------------------------

def _channel_with(app, tmp_path, monkeypatch, *, shared, on_capabilities=(), shipped=()):
    """A channel seeded with ``on_capabilities`` as library cells, whose
    generator names ``shared``; afterwards the app's content tree holds only
    the ``shipped`` libraries, so a library cell's name resolves from the
    channel alone."""
    app_libraries = {name: {"__init__.py": f"FROM = 'app {name}'\n"} for name in shipped}
    root = _tree(tmp_path / "sdk", shared=shared, libraries=on_capabilities, suggest=on_capabilities)
    install_libraries(monkeypatch, app_libraries, root)
    for name in on_capabilities:
        write(root / "generators" / name / "__init__.py", f"FROM = 'capabilities {name}'\n")
    use_content(monkeypatch, app, root)
    response = create_suggested(app, "Staged", tutorial=False)
    assert response.status_code == 200, response.text
    install_libraries(monkeypatch, app_libraries, tmp_path / "bundled")
    return response.json()["id"]


def test_a_run_stages_each_named_library_beside_the_generator(app, tmp_path, monkeypatch):
    from collimate_server.run_staging import snapshot_generator_source
    channel = _channel_with(app, tmp_path, monkeypatch, shared=["_tools", "_shipped"],
                            on_capabilities=["_tools"], shipped=["_shipped", "_tools"])
    subdir, writes = snapshot_generator_source("alpha", channel)
    staged = {rel: file.content for rel, file in writes}
    assert subdir == "alpha" and "alpha/alpha.py" in staged
    # Capabilities first, the app's copy for a name it does not hold.
    assert staged["_tools/__init__.py"] == "FROM = 'capabilities _tools'\n"
    assert staged["_shipped/__init__.py"] == "FROM = 'app _shipped'\n"
    assert {rel.split("/")[0] for rel in staged} == {"alpha", "_tools", "_shipped"}


def test_a_library_the_cell_does_not_name_is_not_staged(app, tmp_path, monkeypatch):
    from collimate_server.run_staging import snapshot_generator_source
    channel = _channel_with(app, tmp_path, monkeypatch, shared=[], on_capabilities=["_tools"])
    _, writes = snapshot_generator_source("alpha", channel)
    assert {rel.split("/")[0] for rel, _ in writes} == {"alpha"}


def test_a_name_that_stops_resolving_refuses_the_run(app, tmp_path, monkeypatch):
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.run_staging import snapshot_generator_source
    from collimate_server.runners import RunnerError
    from collimate_server.state import require_channel
    channel = _channel_with(app, tmp_path, monkeypatch, shared=["_tools"], on_capabilities=["_tools"])
    tools = next(c for c in capabilities_stack_cells(require_channel(channel)) if c.name == "Tools")
    tools.files = [f for f in tools.files if not f.name.endswith(".library")]
    with pytest.raises(RunnerError) as refused:
        snapshot_generator_source("alpha", channel)
    assert refused.value.status == 409 and "‘_tools’" in refused.value.reason


# ---------------------------------------------------------------------------
# The libraries listing and problems
# ---------------------------------------------------------------------------

def test_the_listing_names_each_library_and_who_names_it(app, tmp_path, monkeypatch):
    channel = _channel_with(app, tmp_path, monkeypatch, shared=["_tools", "_shipped"],
                            on_capabilities=["_tools"], shipped=["_shipped"])
    cells = _cells(channel)
    listing = app.get(f"/api/channels/{channel}/capabilities/libraries").json()
    assert [(row["id"], row["cell_id"], row["named_by"]) for row in listing["libraries"]] == [
        ("_tools", str(cells["Tools"].id), [str(cells["First"].id)])]
    assert listing["libraries"][0]["description"] == "Synthetic library"
    assert [(row["id"], row["named_by"]) for row in listing["app"]] == [("_shipped", [str(cells["First"].id)])]


def test_problems_name_a_shared_list_that_no_longer_resolves(app, tmp_path, monkeypatch):
    channel = _channel_with(app, tmp_path, monkeypatch, shared=["_tools"], on_capabilities=["_tools"])
    cells = _cells(channel)
    cells["Tools"].files = [f for f in cells["Tools"].files if not f.name.endswith(".library")]
    problems = app.get(f"/api/channels/{channel}/capabilities/problems").json()["problems"]
    assert [(p["cell_id"], p["path"], p["kind"]) for p in problems if p["kind"] == "library"] == [
        (str(cells["First"].id), "cell.yaml", "library")]


# ---------------------------------------------------------------------------
# Add to carries the source cell's shared: list
# ---------------------------------------------------------------------------

def _generator_files(gid: str, *, shared=None) -> list:
    files = [
        VirtualFile(name=f"{gid}/choice.generator", content=yaml.safe_dump({
            "id": gid, "name": gid.title(), "summary": "Synthetic generator", "entrypoint": "main.py",
            "main_config": MAIN})),
        VirtualFile(name=f"{gid}/main.py", content="from _tools import FROM\n"),
        VirtualFile(name=f"{gid}/{MAIN}", content="fields: []\n"),
    ]
    if shared is not None:
        files.append(VirtualFile(name="cell.yaml", content=yaml.safe_dump({"shared": shared})))
    return files


def _library_files(name: str) -> list:
    return [VirtualFile(name=f"{name}/{name}.library",
                        content=yaml.safe_dump({"id": name, "name": "Tools", "description": "Shared tools"})),
            VirtualFile(name=f"{name}/__init__.py", content="FROM = 1\n")]


def _work_cell(files):
    from collimate_server.cell_commits import commit_source_cell
    from collimate_server.state import find_active_channel, state_lock
    channel = find_active_channel()
    cell = Cell(name="Work", files=files)
    with state_lock:
        channel.main_stacks.append(Stack(name="Work", cells=[cell]))
    commit_source_cell(channel, cell, cell.files, persist=False)
    return channel, cell


def _add(client, cell, path, target=None):
    request = {"source_cell_id": str(cell.id), "definition_path": path, "target_cell_id": target}
    preview = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    if preview.status_code != 200:
        return preview, None
    added = client.post(f"/api/channels/{client.channel_id}/packages/add", json=request | {
        "expected_content_hash": preview.json()["content_hash"],
        "expected_target_revision": preview.json()["target_revision"]})
    return preview, added


def _accepted(channel_id, cell_id):
    from collimate_server.cas import channel_store
    from collimate_server.database import get_db_connection
    from collimate_server.persistence import snapshot_files
    with get_db_connection(channel_id) as conn:
        return {row["path"]: channel_store(channel_id).read_blob(row["body_hash"]).decode() for row in snapshot_files(conn, cell_id)}


def test_add_to_refuses_a_generator_whose_library_is_not_on_capabilities(client):
    _, work = _work_cell(_generator_files("tooled", shared=["_tools"]) + _library_files("_tools"))
    preview, _ = _add(client, work, "tooled/choice.generator")
    assert preview.status_code == 400 and "add it to Capabilities first" in preview.json()["detail"]


def test_add_to_carries_the_shared_list_into_the_target_cell(client):
    _, work = _work_cell(_generator_files("tooled", shared=["_tools"]) + _library_files("_tools"))
    preview, added = _add(client, work, "_tools/_tools.library")
    assert added.status_code == 200, added.text
    assert preview.json()["kind"] == "library"
    preview, added = _add(client, work, "tooled/choice.generator")
    assert added.status_code == 200, added.text
    assert preview.json()["summary"]["shared"] == ["_tools (Capabilities)"]
    files = _accepted(client.channel_id, added.json()["cell_id"])
    assert yaml.safe_load(files["cell.yaml"]) == {"shared": ["_tools"]}
    assert not any(path.startswith("_tools/") for path in files)


def test_add_to_an_existing_cell_keeps_its_cell_yaml_and_adds_the_names(client):
    _, work = _work_cell(_library_files("_tools") + _generator_files("first", shared=["_tools"]))
    _, lib = _add(client, work, "_tools/_tools.library")
    assert lib.status_code == 200, lib.text
    target = lib.json()["cell_id"]
    from collimate_server.state import find_active_channel
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.cell_commits import commit_source_cell
    cell = next(c for c in capabilities_stack_cells(find_active_channel()) if str(c.id) == target)
    commit_source_cell(find_active_channel(), cell,
                       [*cell.files, VirtualFile(name="cell.yaml", content="name: Mine\nshared: [_tools]\n")],
                       persist=False)
    _, added = _add(client, work, "first/choice.generator", target)
    assert added.status_code == 200, added.text
    assert yaml.safe_load(_accepted(client.channel_id, target)["cell.yaml"]) == {
        "name": "Mine", "shared": ["_tools"]}


# ---------------------------------------------------------------------------
# A move onto Capabilities passes admission
# ---------------------------------------------------------------------------

def _inline_moves(monkeypatch, *stacks_):
    """A channel of ``stacks_`` whose moves run inline. Admission reads the
    handler acceptance table in the app database under the cwd, so a test
    takes ``fresh_cwd`` and never opens the developer's own."""
    from collimate_server.routes import stacks
    import collimate_server.state as state

    async def in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(stacks.asyncio, "to_thread", in_thread)
    channel = Channel(name="test", main_stacks=list(stacks_))
    monkeypatch.setattr(state.state, "channels", [channel])
    monkeypatch.setattr(stacks, "persist_state", lambda: None)
    return stacks, channel


def _capabilities(*cells):
    from collimate_server.cell_placement import CAPABILITIES_STACK_TYPE
    return Stack(name="Capabilities", stack_type=CAPABILITIES_STACK_TYPE, cells=list(cells))


@pytest.mark.parametrize("files, detail", [
    (_generator_files("tooled", shared=["_absent"]), "‘_absent’"),
    ([VirtualFile(name="bad/bad.generator", content="id: bad\nname: Bad\n")], "cannot move onto Capabilities"),
    ([VirtualFile(name="run/run.runner", content=yaml.safe_dump({
        "id": "run", "name": "Run", "generator": "elsewhere", "settings": {MAIN: {}}}))], "Add elsewhere first"),
    ([VirtualFile(name="text/text.renderer", content=yaml.safe_dump({
        "id": "text", "name": "Text", "summary": "Mine", "entrypoint": "main.tsx"})),
      VirtualFile(name="text/main.tsx", content="export default {};\n")], "built-in renderer"),
])
def test_a_move_onto_capabilities_refuses_what_admission_refuses(fresh_cwd, monkeypatch, files, detail):
    from fastapi import HTTPException
    moving = Cell(name="Moving", files=files)
    board, capabilities = Stack(cells=[moving, Cell()]), _capabilities(Cell(builtin=True))
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(stacks.move_cell_to(str(channel.id), board.id, 1, "stack", moving.id))
    assert refused.value.status_code == 400 and detail in refused.value.detail
    assert moving in board.cells and moving not in capabilities.cells


def test_a_move_admits_a_generator_whose_library_moves_with_it(fresh_cwd, monkeypatch):
    moving = Cell(name="Moving", files=_generator_files("tooled", shared=["_tools"]) + _library_files("_tools"))
    board, capabilities = Stack(cells=[moving, Cell()]), _capabilities(Cell(builtin=True))
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    asyncio.run(stacks.move_cell_to(str(channel.id), board.id, 1, "stack", moving.id))
    assert capabilities.cells[-1] is moving


def test_a_move_refuses_a_library_id_another_capabilities_cell_holds(fresh_cwd, monkeypatch):
    from fastapi import HTTPException
    held = Cell(name="Held", files=_library_files("_tools"))
    moving = Cell(name="Moving", files=_library_files("_tools"))
    board, capabilities = Stack(cells=[moving, Cell()]), _capabilities(Cell(builtin=True), held)
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(stacks.move_cell_to(str(channel.id), board.id, 1, "stack", moving.id))
    assert refused.value.status_code == 400 and "rename first" in refused.value.detail


# ---------------------------------------------------------------------------
# The bundled Standard suggestion
# ---------------------------------------------------------------------------

def test_standard_seeds_the_apps_libraries_as_cells_and_its_generators_resolve_there(app):
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.libraries import CAPABILITIES, cell_shared, resolve
    from collimate_server.state import require_channel
    catalog = app.get("/api/suggested").json()
    standard = next(s for s in catalog["suggestions"] if s["id"] == "standard")
    response = create_suggested(app, "Standard", "standard", runners=[])
    assert response.status_code == 200, response.text
    cells = _cells(response.json()["id"])
    libraries = {row["name"]: row["folder"].rsplit("/", 1)[-1] for row in standard["libraries"]}
    assert set(libraries.values()) == {"_env_shared", "_agent_shared", "_web_shared"}
    for name, identifier in libraries.items():
        assert f"{identifier}/{identifier}.library" in _names(cells[name])
    channel = require_channel(response.json()["id"])
    for name in ("Agent", "Git", "Launch", "Web"):
        assert not any(path.startswith("_") for path in _names(cells[name])), name
        resolved = resolve(capabilities_stack_cells(channel), cell_shared(cells[name].files))
        assert resolved and all(lib.origin == CAPABILITIES for lib in resolved), name
