"""The app's built-in packages (``builtin_packages``): the vendored build's
handlers and renderers, served for every channel as three cards at the head
of the Capabilities stack.

Covers the loader (the app's list of its built-ins, a tree that differs from
it and a broken build refusing to load), the cards on every
creation path and at restore, the read-side projection of their files, the
write refusals and the overlay policy, the renderer listing and its surface
lock, admission holds for packages that take a built-in id, type or surface,
and export and channel copy leaving the cards behind.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest
import yaml

from suggested_trees import build_tree, use_source, write

REFUSAL = "Built-in packages are part of the app"
CARDS = [("Definitions", "braces"), ("Live", "radio"), ("Text", "file-text")]
HANDLERS = {"generator", "renderer", "runner", "handler", "options", "git-checkout"}
RENDERERS = {"renderer_example", "text", "run", "assistant", "terminal", "website", "webview"}


def _channel(channel_id):
    from state import state
    return next(c for c in state.channels if str(c.id) == str(channel_id))


def _capabilities(channel):
    from cell_placement import capabilities_stack
    return capabilities_stack(channel)


def _wire_channel(client, channel_id):
    return next(c for c in client.get("/api/state").json()["channels"] if c["id"] == channel_id)


def _wire_cards(client, channel_id):
    wire = _wire_channel(client, channel_id)
    stack = next(s for s in wire["main_stacks"] + wire["nav_stacks"] if s["stack_type"] == "capabilities")
    return [c for c in stack["cells"] if c.get("builtin")]


def _definitions_card(channel_id):
    return _capabilities(_channel(channel_id)).cells[0]


def _standard_cell(client):
    response = client.post(f"/api/stacks/{client.channel_id}/create")
    assert response.status_code == 200, response.text
    cell_id = response.json()["main_stacks"][-1]["cells"][0]["id"]
    return next(c for s in _channel(client.channel_id).main_stacks for c in s.cells if str(c.id) == cell_id)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------

def test_the_build_holds_three_cards_six_handlers_and_seven_capabilities():
    from builtin_packages import build, builtin_types
    from collimate.handlers import RESERVED_TYPES
    loaded = build()
    assert [(cell.name, cell.icon) for cell in loaded.cells] == CARDS
    assert set(loaded.handlers) == HANDLERS
    assert set(loaded.renderers) == RENDERERS
    assert set(builtin_types()) == set(RESERVED_TYPES)


def test_git_checkout_record_is_typed_and_its_skill_served_without_acceptance(client):
    """The Git checkout record is typed by the built-in git-checkout
    handler, whose verifier and skill are in use from the start: no Review
    and accept."""
    from builtin_packages import builtin_handlers
    from handlers import catalog
    skill = builtin_handlers()["git-checkout"].files["derived/skills/git-operations/SKILL.md"].decode("utf-8")
    current = catalog(_channel(client.channel_id))
    record = current.for_path("api.git-checkout.json")
    assert record is not None and record.manifest["id"] == "git-checkout"
    for key in ("git-checkout", ".git-checkout.json", "api.git-checkout.json"):
        assert current.skills(key) == {"git-checkout--git-operations/SKILL.md": skill}, key
    assert "verify.ts" in record.accepted_files
    row = next(h for h in client.get("/api/handlers").json() if h["id"] == "git-checkout")
    assert row["builtin"] is True


def test_every_builtin_renderer_is_served_from_startup_as_the_node_build_compiles_it(client, tmp_path):
    """Startup stores each built-in renderer's bundle as its server bundle:
    served from the first request with no upload, byte for byte the Node
    build's compilation of the built-ins tree."""
    import subprocess
    import sys
    import database
    import genesis
    from builtin_packages import builtin_renderers
    renderers, out = tmp_path / "no-renderers", tmp_path / "node-build"
    renderers.mkdir()
    subprocess.run([sys.executable, "-m", "collimate._host.renderer_bundle", "--build", str(renderers),
                    "--builtin", genesis.BUILTIN_DIR, "--out", str(out)], check=True, capture_output=True, timeout=120)
    index = {entry["source_hash"]: entry for entry in json.loads((out / "index.json").read_text())["entries"]}
    assert set(builtin_renderers()) == RENDERERS
    conn = database.get_global_db_connection()
    try:
        origins = {row[0]: row[1] for row in conn.execute("SELECT source_hash, origin FROM renderer_bundles")}
    finally:
        conn.close()
    for renderer in builtin_renderers().values():
        served = client.get(f"/api/renderers/{renderer.id}/source", params={"v": renderer.source_hash})
        assert served.status_code == 200, renderer.id
        assert served.content == renderer.bundle == (out / index[renderer.source_hash]["file"]).read_bytes()
        assert origins[renderer.source_hash] == "server"


def _copy_build(tmp_path):
    import genesis
    root = tmp_path / "builtin"
    shutil.copytree(genesis.BUILTIN_DIR, root)
    return root


def _serve(monkeypatch, root):
    """Point the app's bundled built-ins at ``root``, as an installation
    holding that tree would read it."""
    import genesis
    monkeypatch.setattr(genesis, "BUILTIN_DIR", str(root))


def _sync_tool():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "build" / "sync_builtins.py"
    spec = importlib.util.spec_from_file_location("sync_builtins", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_list_names_the_twelve_builtins_and_pins_the_checkout():
    """``api/builtins.json`` names exactly the build's packages, and the
    updater's ``--check`` passes on the checkout. A built-in renderer's pin is
    the provenance hash the same package would get if it were admitted."""
    import os
    import subprocess
    import sys
    from builtin_packages import LIST_PATH, build
    from package_import import package_identities
    root = Path(__file__).resolve().parents[2]
    check = subprocess.run([sys.executable, str(root / "build" / "sync_builtins.py"), "--check"], cwd=root,
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": str(root / "api")})
    assert check.returncode == 0, check.stdout + check.stderr
    entries = json.loads(LIST_PATH.read_text())
    assert [(e["kind"], e["id"]) for e in entries] == sorted((e["kind"], e["id"]) for e in entries)
    assert {e["id"] for e in entries if e["kind"] == "handler"} == HANDLERS == set(build().handlers)
    assert {e["id"] for e in entries if e["kind"] == "renderer"} == RENDERERS == set(build().renderers)
    admitted = {identity for cell in build().cells for identity in package_identities(dict(cell.files))}
    for entry in entries:
        if entry["kind"] == "renderer":
            assert ("renderer", entry["id"], entry["content_hash"]) in admitted, entry["id"]


def test_a_thirteenth_renderer_refuses_the_build_rather_than_gaining_privilege(monkeypatch, tmp_path):
    """A package the list does not name is no built-in: the tree holding it
    refuses to load, so it never takes a surface, a held id or a card."""
    from builtin_packages import build
    root = _copy_build(tmp_path)
    write(root / "text" / "extra" / "extra.renderer", yaml.safe_dump({
        "id": "extra", "name": "Extra", "summary": "Claims the run card", "entrypoint": "main.tsx",
        "embed": "native", "extensions": [".run"]}))
    shutil.copy(root / "text" / "text" / "main.tsx", root / "text" / "extra" / "main.tsx")
    _serve(monkeypatch, root)
    with pytest.raises(ValueError) as refused:
        build()
    assert str(refused.value).startswith("The built-in tree does not match api/builtins.json: ")
    assert "the tree holds the renderer 'extra', which the list does not name" in str(refused.value)


@pytest.mark.parametrize("change", ["byte", "removed"])
def test_a_changed_or_missing_builtin_refuses_the_build(monkeypatch, tmp_path, change):
    from builtin_packages import LIST_PATH, build
    root = _copy_build(tmp_path)
    if change == "byte":
        main = root / "live" / "terminal" / "main.tsx"
        body = bytearray(main.read_bytes())
        body[-1] ^= 0x01
        main.write_bytes(bytes(body))
        expected = next(e for e in json.loads(LIST_PATH.read_text()) if e["id"] == "terminal")["content_hash"]
        reason = "the renderer 'terminal' hashes "
    else:
        shutil.rmtree(root / "live" / "webview")
        expected = "the list names the renderer 'webview', which the tree does not hold"
        reason = expected
    _serve(monkeypatch, root)
    with pytest.raises(ValueError, match="does not match api/builtins.json") as refused:
        build()
    assert reason in str(refused.value) and expected in str(refused.value)


def test_a_card_holds_its_packages_and_nothing_else(monkeypatch, tmp_path):
    """A file of a card outside its packages, or a cell holding no package,
    is no built-in either."""
    from builtin_packages import build
    root = _copy_build(tmp_path)
    (root / "live" / "notes.md").write_text("notes\n")
    _serve(monkeypatch, root)
    with pytest.raises(ValueError, match="Built-in card 'Live' holds files outside its packages: notes.md"):
        build()
    (root / "live" / "notes.md").unlink()
    write(root / "extra" / "cell.yaml", "name: Extra\n")
    write(root / "extra" / "loose" / "readme.txt", "loose\n")
    with pytest.raises(ValueError, match="Built-in card 'Extra' holds files outside its packages"):
        build()


def test_the_updater_pins_an_intended_edit(monkeypatch, tmp_path):
    """Edit, run the updater, and the edited tree loads under its new list;
    ``--check`` reads the drift before and nothing after."""
    from builtin_packages import _load
    root = _copy_build(tmp_path)
    listing = tmp_path / "builtins.json"
    shutil.copy(Path(__file__).resolve().parents[1] / "builtins.json", listing)
    tool = _sync_tool()
    monkeypatch.setattr(tool, "TREE", root)
    monkeypatch.setattr(tool, "LIST", listing)
    test = root / "text" / "text" / "text.test.ts"
    test.write_text(test.read_text() + "\n// edited\n")
    assert [line.split(":")[0] for line in tool.differences()] == ["renderer text"]
    assert tool.main(["--check"]) == 1
    assert tool.main([]) == 0
    assert tool.differences() == [] and tool.main(["--check"]) == 0
    assert "text" in _load(str(root), str(listing)).renderers


def test_a_handler_whose_verifier_drifted_from_its_hash_refuses_to_load(monkeypatch, tmp_path):
    """Drift refuses whether or not the list is re-pinned over it: a listed
    handler's own ``derived:`` line must still accept its derived folder."""
    from builtin_packages import _load, build
    root = _copy_build(tmp_path)
    verify = root / "definitions" / "runner" / "derived" / "verify.ts"
    verify.write_text(verify.read_text() + "\n// drift\n")
    _serve(monkeypatch, root)
    with pytest.raises(ValueError, match="the handler 'runner' hashes"):
        build()
    tool = _sync_tool()
    monkeypatch.setattr(tool, "TREE", root)
    monkeypatch.setattr(tool, "LIST", tmp_path / "builtins.json")
    tool.main([])
    with pytest.raises(ValueError, match="'runner' has no matching accepted derived folder"):
        _load(str(root), str(tmp_path / "builtins.json"))


def test_a_symlink_in_the_build_refuses_to_load(monkeypatch, tmp_path):
    from builtin_packages import build
    root = _copy_build(tmp_path)
    (root / "text" / "text" / "outside.txt").symlink_to(tmp_path)
    _serve(monkeypatch, root)
    with pytest.raises(ValueError, match="symlinks"):
        build()


# ---------------------------------------------------------------------------
# The cards in the layout
# ---------------------------------------------------------------------------

def _card_ids(channel_id):
    from builtin_packages import builtin_cell_id, builtin_cells
    return [builtin_cell_id(channel_id, spec.spec_id) for spec in builtin_cells()]


def test_an_empty_channel_holds_only_the_cards_on_a_hidden_capabilities_stack(client):
    channel_id = client.channel_id
    channel = _channel(channel_id)
    stack = _capabilities(channel)
    assert [(str(c.id), c.name, c.icon, c.builtin) for c in stack.cells] == [
        (cid, name, icon, True) for cid, (name, icon) in zip(_card_ids(channel_id), CARDS)]
    assert stack in channel.nav_stacks and [s.stack_type for s in channel.nav_stacks] == ["capabilities"]
    cards = _wire_cards(client, channel_id)
    assert [(c["name"], c["icon"], c["builtin"]) for c in cards] == [(n, i, True) for n, i in CARDS]


def test_a_suggested_channel_puts_its_cells_after_the_cards(client, tmp_path):
    use_source(client, build_tree(tmp_path / "sdk"))
    response = client.post("/api/channels", params={"name": "From basic"},
                           json={"suggested": {"id": "basic", "tutorial": False}})
    assert response.status_code == 200, response.text
    channel_id = response.json()["id"]
    assert [c.name for c in _capabilities(_channel(channel_id)).cells] == [
        "Definitions", "Live", "Text", "First", "Second", "Views"]
    assert [str(c.id) for c in _capabilities(_channel(channel_id)).cells[:3]] == _card_ids(channel_id)


def test_restore_inserts_missing_cards_and_refreshes_their_name(client):
    import main
    from state import flush_persist_state, persist_state, state, state_lock
    channel_id = client.channel_id
    with state_lock:
        stack = _capabilities(_channel(channel_id))
        stack.cells[0].name = "Renamed"
        del stack.cells[1:]
    persist_state()
    flush_persist_state()
    main.restore_app_state()
    restored = next(c for c in state.channels if str(c.id) == channel_id)
    assert [(c.name, c.builtin) for c in _capabilities(restored).cells] == [(n, True) for n, _ in CARDS]


# ---------------------------------------------------------------------------
# Files: the read-side projection
# ---------------------------------------------------------------------------

def test_a_cards_files_are_the_vendored_tree_on_the_wire_and_by_path(client):
    from builtin_packages import builtin_cells
    channel_id = client.channel_id
    definitions = builtin_cells()[0]
    card = _wire_cards(client, channel_id)[0]
    by_name = {f["name"]: f for f in card["files"]}
    assert set(by_name) == set(definitions.files)
    entry = by_name["renderer/renderer.handler"]
    body = definitions.files["renderer/renderer.handler"]
    assert (entry["file_revision"], entry["body_hash"], entry["writer_id"], entry["builtin"]) == (
        0, hashlib.sha256(body).hexdigest(), "host:builtin", True)
    cell_id = card["id"]
    for params in ({}, {"revision": 0}):
        response = client.get(f"/api/channels/{channel_id}/cells/{cell_id}/files/renderer/renderer.handler",
                              params=params)
        assert response.status_code == 200, response.text
        assert response.content == body and response.headers["X-File-Revision"] == "0"
    paths = client.get(f"/api/channels/{channel_id}/cells/{cell_id}/file-paths").json()["paths"]
    assert set(paths) == set(definitions.files)
    missing = client.get(f"/api/channels/{channel_id}/cells/{cell_id}/files/renderer/nope.txt")
    assert missing.status_code == 404


def test_regenerated_skills_hide_the_builds_unpinned_skills_but_never_pinned_ones():
    from builtin_packages import builtin_cells, projection
    definitions = builtin_cells()[0]
    overlay = [{"path": "handler/derived/skills/fresh/SKILL.md", "file_revision": 1}]
    paths = {entry["path"] for entry in projection(definitions, overlay)}
    prefix = "handler/derived/skills/"
    assert prefix + "fresh/SKILL.md" in paths
    assert prefix + "handler-authoring/SKILL.md" not in paths  # unpinned: the overlay replaces it
    assert prefix + "build/SKILL.md" in paths  # app machinery, pinned whole
    assert prefix + "handler-authoring/references/interface.md" in paths
    assert "handler/derived/verify.ts" in paths
    assert "renderer/derived/skills/renderer-authoring/SKILL.md" in paths  # another handler
    unpinned = {p for p in definitions.files if p.startswith("generator/derived/skills/")
                and "/references/" not in p}
    overlay = [{"path": "generator/derived/skills/generator-authoring/notes.md", "file_revision": 1}]
    paths = {entry["path"] for entry in projection(definitions, overlay)}
    assert not unpinned & paths
    assert "generator/derived/skills/generator-authoring/references/interface.md" in paths


def test_the_overlay_is_examples_skills_and_additions():
    from builtin_packages import builtin_cells, overlay_path
    definitions, live, _text = builtin_cells()
    allowed = ["renderer/examples/example-1a2b3c4d/x.renderer", "renderer/additions.yaml",
               "generator/derived/skills/generator-authoring/SKILL.md"]
    refused = ["renderer/renderer.handler", "renderer/derived/verify.ts", "renderer/examples/basic/main.tsx",
               "handler/derived/skills/build/SKILL.md", "handler/derived/skills/regenerate/SKILL.md",
               "handler/derived/skills/create/SKILL.md", "_runs/r1/r1.collrun",
               "generator/derived/skills/generator-authoring/references/interface.md",
               "renderer-example/main.tsx", "notes.txt", "renderer/examples/loose.txt"]
    assert [overlay_path(definitions, p) for p in allowed] == [True] * len(allowed)
    assert [overlay_path(definitions, p) for p in refused] == [False] * len(refused)
    assert not overlay_path(live, "renderer/additions.yaml")
    assert not overlay_path(live, "_runs/r1/r1.collrun")


def _mutation(path, *, kind="put", text="x"):
    return {"operation_id": str(uuid.uuid4()), "mutations": [dict(
        path=path, writer_id="tab:1", edit_seq=1, base_revision=0, kind=kind,
        **({"content_text": text} if kind == "put" else {}))]}


def test_user_writes_to_a_card_are_refused_even_on_overlay_paths(client):
    channel_id = client.channel_id
    card = _definitions_card(channel_id)
    for path in ("renderer/renderer.handler", "renderer/additions.yaml", "notes.txt"):
        response = client.post(f"/api/channels/{channel_id}/cells/{card.id}/files/mutate", json=_mutation(path))
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == REFUSAL


def test_a_user_write_under_a_run_folder_follows_the_run_folder_rules(client):
    channel_id = client.channel_id
    card = _definitions_card(channel_id)
    response = client.post(f"/api/channels/{channel_id}/cells/{card.id}/files/mutate",
                           json=_mutation("_runs/old/notes.txt"))
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Run UI files are read-only"


def test_host_writes_to_a_card_stay_inside_its_overlay(client):
    from fastapi import HTTPException
    from persistence import mutate_files
    channel_id = client.channel_id
    card = _definitions_card(channel_id)
    with pytest.raises(HTTPException) as refused:
        mutate_files(channel_id, str(card.id), _mutation("renderer/derived/verify.ts"), actor="host")
    assert refused.value.detail == REFUSAL
    result = mutate_files(channel_id, str(card.id), _mutation("renderer/additions.yaml", text="examples: []\n"),
                          actor="host")
    assert result["outcome"] == "accepted"


def test_files_never_move_into_or_out_of_a_card(client):
    channel_id = client.channel_id
    card = _definitions_card(channel_id)
    standard = _standard_cell(client)
    put = client.post(f"/api/channels/{channel_id}/cells/{standard.id}/files/mutate", json=_mutation("a.txt"))
    assert put.status_code == 200, put.text
    move = {"operation_id": str(uuid.uuid4()), "source_cell_id": str(standard.id), "target_cell_id": str(card.id),
            "files": [{"source_path": "a.txt", "target_path": "renderer/a.txt", "source_revision": 1,
                       "target_revision": 0}], "writer_id": "tab:1", "edit_seq": 2}
    response = client.post(f"/api/channels/{channel_id}/files/move", json=move)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == REFUSAL


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def test_the_listing_ends_with_the_builtins_and_serves_them_from_the_build(client):
    from builtin_packages import builtin_renderers, renderer_card
    channel_id = client.channel_id
    rows = client.get("/api/renderers").json()
    assert {row["id"] for row in rows} == RENDERERS
    channel = _channel(channel_id)
    for row in rows:
        renderer = builtin_renderers()[row["id"]]
        assert (row["builtin"], row["available"], row["content_hash"], row["cell_id"]) == (
            True, True, renderer.source_hash, renderer_card(channel, renderer))
    package = client.get("/api/renderers/text/package", params={"v": builtin_renderers()["text"].source_hash})
    assert package.status_code == 200, package.text
    files = {p: base64.b64decode(b) for p, b in package.json()["files"].items()}
    assert files == dict(builtin_renderers()["text"].files)
    source = client.get("/api/renderers/text/source", params={"v": builtin_renderers()["text"].source_hash})
    assert source.status_code == 200, source.text


def test_a_builtin_renderer_is_always_available(client):
    response = client.put("/api/renderer_availability/text", json={"enabled": False})
    assert response.status_code == 409, response.text
    assert all(row["available"] for row in client.get("/api/renderers").json())


def _user_renderer_tree(root: Path, *, renderer_id="claimer", extensions=(".claim", ".terminal", ".x.run")) -> Path:
    build_tree(root)
    write(root / "renderers" / "views" / "widget" / "view.renderer", yaml.safe_dump({
        "id": renderer_id, "name": "Claimer", "summary": "Claims", "entrypoint": "main.tsx",
        "embed": "native", "extensions": list(extensions)}))
    return root


def test_a_user_renderer_never_takes_a_surface_or_a_builtin_id(client, tmp_path):
    use_source(client, _user_renderer_tree(tmp_path / "sdk"))
    response = client.post("/api/channels", params={"name": "Claims"},
                           json={"suggested": {"id": "basic", "tutorial": False}})
    assert response.status_code == 200, response.text
    skips = response.json()["package_skips"]
    assert [skip["id"] for skip in skips] == ["claimer"]
    assert "surface" in skips[0]["reason"]
    use_source(client, _user_renderer_tree(tmp_path / "sdk2", renderer_id="text", extensions=(".claim",)))
    response = client.post("/api/channels", params={"name": "Text again"},
                           json={"suggested": {"id": "basic", "tutorial": False}})
    assert response.status_code == 200, response.text
    assert [skip["id"] for skip in response.json()["package_skips"]] == ["text"]


def test_a_user_renderer_claiming_a_surface_is_left_out_of_the_listing(client, tmp_path):
    from collimate._host.packaging.suggested import read_suggested_cell, read_suggestions
    from channel_sources import specs_to_cells
    from import_plan import ImportPlan
    from state import persist_state
    root = _user_renderer_tree(tmp_path / "sdk")
    (suggestion,) = read_suggestions(root)
    channel = _channel(client.channel_id)
    (cell,) = specs_to_cells([read_suggested_cell(root, suggestion.renderers[0], "renderer")])
    plan = ImportPlan(channel)
    stack = _capabilities(plan.candidate)
    stack.cells.append(cell)  # written straight onto Capabilities: never admitted
    plan.publish()
    persist_state()
    rows = client.get("/api/renderers", params={"include_disabled": 1}).json()
    assert "claimer" not in {r["id"] for r in rows}
    problems = client.get(f"/api/channels/{client.channel_id}/capabilities/problems").json()["problems"]
    assert any(p["cell_id"] == str(cell.id) and p["kind"] == "renderer" and "surface" in p["error"]
               for p in problems)


def test_a_surface_preference_is_refused_and_a_stored_one_ignored(client):
    """Keys lock as the browser matches them: a case-folded plain suffix of
    the file name, so a key without its dot or shorter than the surface
    (``terminal``, ``nal``) is locked as surely as ``.terminal``."""
    from channel_config import RENDERER_PREFERENCES_KEY, write_ui_state_json
    for prefs in ({".terminal": "monaco"}, {".x.run": "text"}, {".terminal": "text"}, {"terminal": "monaco"},
                  {"nal": "monaco"}, {".TERMINAL": "monaco"}, {"Chat": "text"}, {"example": "html"}):
        response = client.put("/api/renderer_preferences", json=prefs)
        assert response.status_code == 400, (prefs, response.text)
    ok = client.put("/api/renderer_preferences", json={".terminal": "terminal", ".md": "text",
                                                       "md": "text", "*": "text"})
    assert ok.status_code == 200, ok.text
    write_ui_state_json(client.channel_id, RENDERER_PREFERENCES_KEY,
                        {".terminal": "monaco", "terminal": "monaco", "nal": "monaco", ".md": "text"})
    assert client.get("/api/renderer_preferences").json() == {".md": "text"}
    settings = client.put(f"/api/channels/{client.channel_id}/settings",
                          json={"renderer_preferences": {".chat": "monaco"}})
    assert settings.status_code == 400, settings.text


# ---------------------------------------------------------------------------
# Admission holds
# ---------------------------------------------------------------------------

def _handler_tree(root: Path, *, handler_id="notes", ext=".note") -> Path:
    build_tree(root)
    package = root / "renderers" / "views" / handler_id
    write(package / f"{handler_id}.handler", yaml.safe_dump({
        "id": handler_id, "name": "Notes", "description": "Notes", "guidance": "Write notes.",
        "types": [{"ext": ext}], "examples": []}))
    return root


@pytest.mark.parametrize(("handler_id", "ext"), [("notes", ".terminal"), ("notes", ".renderer"), ("runner", ".note"),
                                                  ("notes", ".chat"), ("notes", ".x.run"), ("notes", ".webview")])
def test_a_handler_taking_a_builtin_type_or_id_is_held(client, tmp_path, handler_id, ext):
    use_source(client, _handler_tree(tmp_path / "sdk", handler_id=handler_id, ext=ext))
    response = client.post("/api/channels", params={"name": "Held"},
                           json={"suggested": {"id": "basic", "tutorial": False}})
    assert response.status_code == 200, response.text
    (skip,) = response.json()["package_skips"]
    assert skip["id"] == handler_id and "built-in" in skip["reason"]
    channel = _channel(response.json()["id"])
    assert [c.name for c in _capabilities(channel).cells] == ["Definitions", "Live", "Text", "First", "Second", "Views"]


def test_the_catalog_excludes_a_user_handler_typed_on_a_surface(client, tmp_path):
    """A handler written straight onto Capabilities (never admitted) whose type
    is a surface extension is left out of the catalog with a problem: its
    ``render_as`` would otherwise move the surface off its built-in renderer."""
    from handlers import catalog
    channel = _channel(client.channel_id)
    manifest = yaml.safe_dump({"id": "chats", "name": "Chats", "description": "Chats", "guidance": "Chat.",
                               "types": [{"ext": ".chat", "render_as": ".md"}], "examples": []})
    _append_capabilities_cell(channel, tmp_path / "cells", "Chats", {"chats/chats.handler": manifest.encode()})
    current = catalog(channel)
    assert not any(t["ext"] == ".chat" for r in current.records for t in r.manifest["types"])
    assert any(e["path"] == "chats/chats.handler" and "surface" in e["error"] for e in current.errors)
    assert current.for_path("transcript.chat") is None


def test_add_to_never_targets_a_card_and_refuses_a_builtin_claim(client):
    channel_id = client.channel_id
    source = _standard_cell(client)
    manifest = "id: text\nname: Text\nsummary: Mine\nentrypoint: main.tsx\nembed: native\nextensions: [.mine]\n"
    for path, text in (("mine/mine.renderer", manifest),
                       ("mine/main.tsx", "import { defineRenderer } from '/api/sdk/react.js';\n"
                                         "export default defineRenderer({ component: () => null });\n")):
        put = client.post(f"/api/channels/{channel_id}/cells/{source.id}/files/mutate",
                          json=_mutation(path, text=text))
        assert put.status_code == 200, put.text
    stack = _capabilities(_channel(channel_id))
    request = {"source_cell_id": str(source.id), "definition_path": "mine/mine.renderer"}
    held = client.post(f"/api/channels/{channel_id}/packages/preview", json=request)
    assert held.status_code == 400 and "built-in" in held.json()["detail"], held.text
    card = client.post(f"/api/channels/{channel_id}/packages/preview",
                       json=request | {"target_cell_id": str(stack.cells[0].id)})
    assert card.status_code == 400 and card.json()["detail"] == REFUSAL, card.text


# ---------------------------------------------------------------------------
# Export, copy and outline leave the cards behind
# ---------------------------------------------------------------------------

def test_export_skips_the_cards_in_every_scope(client):
    channel_id = client.channel_id
    stack = _capabilities(_channel(channel_id))
    for scope, params in (("packages", {}), ("all", {}), ("stack", {"stack_id": str(stack.id)})):
        manifest = client.get(f"/api/channels/{channel_id}/export/manifest", params={"scope": scope, **params})
        assert manifest.status_code == 200, manifest.text
        assert not any("/Definitions" in p["path"] or "/Live" in p["path"] for p in manifest.json()["entries"])
    card = client.get(f"/api/channels/{channel_id}/export/manifest",
                      params={"scope": "cell", "cell_id": str(stack.cells[0].id)})
    assert card.status_code == 400 and card.json()["detail"] == REFUSAL


def test_copying_a_channels_packages_brings_none_of_its_cards(client):
    source = client.channel_id
    response = client.post("/api/channels", params={"name": "Copy"},
                           json={"config": {"packages": {"channel_id": source}}})
    assert response.status_code == 200, response.text
    channel_id = response.json()["id"]
    assert [str(c.id) for c in _capabilities(_channel(channel_id)).cells] == _card_ids(channel_id)
    outline = client.get(f"/api/channels/{source}/outline").json()
    assert outline["capabilities_cells"] == []


def _asset_cell(root: Path, name: str, files: dict[str, bytes]) -> Path:
    """Write an asset-tree cell ``name`` holding ``files`` under ``root``."""
    cell_root = root / name.casefold()
    write(cell_root / "cell.yaml", yaml.safe_dump({"name": name}))
    for path, body in files.items():
        (cell_root / path).parent.mkdir(parents=True, exist_ok=True)
        (cell_root / path).write_bytes(body)
    return root


def _append_capabilities_cell(channel, root: Path, name: str, files: dict[str, bytes]):
    """Write a cell ``name`` holding ``files`` straight onto Capabilities
    (never admitted); returns it."""
    from collimate._host.packaging.asset_cells import read_asset_cells
    from channel_sources import specs_to_cells
    from import_plan import ImportPlan
    from state import persist_state
    _asset_cell(root, name, files)
    (cell,) = specs_to_cells(read_asset_cells(root, "renderer"))
    plan = ImportPlan(channel)
    _capabilities(plan.candidate).cells.append(cell)
    plan.publish()
    persist_state()
    return cell


def test_a_packages_zip_holds_a_renderer_taking_a_builtin_id(client):
    def blob(text):
        response = client.post("/api/blob", content=text.encode())
        assert response.status_code == 200, response.text
        return response.json()["blob_hash"]

    manifest = "id: text\nname: Text\nsummary: Mine\nentrypoint: main.tsx\nembed: native\nextensions: [.mine]\n"
    main = "import { defineRenderer } from '/api/sdk/react.js';\nexport default defineRenderer({ component: () => null });\n"
    files = [{"path": "mine/mine.renderer", "blob_hash": blob(manifest)},
             {"path": "mine/main.tsx", "blob_hash": blob(main)}]
    response = client.post("/api/channels", params={"name": "Zip"}, json={
        "config": {"packages": {"cells": [{"name": "Mine", "files": files}]}}})
    assert response.status_code == 200, response.text
    assert [(s["id"], "built-in" in s["reason"]) for s in response.json()["package_skips"]] == [("text", True)]
    assert [c.name for c in _capabilities(_channel(response.json()["id"])).cells] == [n for n, _ in CARDS]
