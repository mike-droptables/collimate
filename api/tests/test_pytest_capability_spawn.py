"""Work stacks for Build, Regenerate skills and Create (``capability_spawn``,
``POST /api/channels/{channel}/capabilities/{build,create}``).

The built-in handlers' skills are replaced by synthetic ones, so every brief
and guide body below is this file's: the ``handler`` built-in's ``build``,
``regenerate`` and ``create`` briefs (``build`` with one reference) and
each kind's authoring guide. The user handler is a synthetic ``memo`` on
``.memo``.
"""
from __future__ import annotations

import uuid

import pytest
import yaml

KINDS = ("generator", "runner", "renderer", "handler")
VERIFY = b"export default () => ({valid:true});"
SKILL = b"---\nname: memo\ndescription: Write a memo.\n---\n# Memo\n"
ADDED = b"---\nname: extra\ndescription: Extra guidance.\n---\n# Extra\n"


def _brief_skill(name: str) -> bytes:
    return f"---\nname: {name}\ndescription: Synthetic {name}.\n---\n\n# Synthetic {name} brief\n".encode()


@pytest.fixture
def synthetic_briefs(client, monkeypatch):
    """Replace every built-in handler's skills with synthetic ones."""
    from dataclasses import replace
    from types import MappingProxyType
    import builtin_packages
    real = builtin_packages.build()
    handlers = {}
    for kind, handler in real.handlers.items():
        files = {path: body for path, body in handler.files.items() if not path.startswith("derived/skills/")}
        if kind in KINDS:
            files[f"derived/skills/{kind}-authoring/SKILL.md"] = (
                f"---\nname: {kind}-authoring\ndescription: Write a {kind}.\n---\n# {kind}\n".encode())
            files[f"derived/skills/{kind}-authoring/references/notes.md"] = f"# {kind} notes\n".encode()
        if kind == "handler":
            for name in ("build", "regenerate", "create"):
                files[f"derived/skills/{name}/SKILL.md"] = _brief_skill(name)
            files["derived/skills/build/references/checks.md"] = b"# Checks\n"
        handlers[kind] = replace(handler, files=MappingProxyType(files), asset_skills=frozenset())
    patched = replace(real, handlers=MappingProxyType(handlers))
    monkeypatch.setattr(builtin_packages, "build", lambda: patched)
    return client


def _channel(client):
    from state import require_channel
    return require_channel(client.channel_id)


def _memo_cell(client, *, extra: dict[str, bytes] | None = None, identity="memo"):
    """A Capabilities cell holding the accepted handler ``memo/`` (with an
    added skill and an example) and a loose note; answers it."""
    from collimate.handlers import derived_hash
    from conftest import add_capabilities_cell
    from handlers import save_derived_skill
    from models import Cell, VirtualFile
    from state import flush_persist_state, persist_state, state_lock
    derived = {"verify.ts": VERIFY, "skills/memo/SKILL.md": SKILL}
    manifest = {"id": identity, "name": "Memo", "description": "Memos", "guidance": "A memo has a title.",
                "types": [{"ext": ".memo"}], "examples": [{"folder": "basic", "description": "A memo", "expect": "valid",
                                               "feedback": "It has a title."}],
                "derived": derived_hash(derived)}
    save_derived_skill(manifest["derived"], derived)
    files = {"memo/memo.handler": yaml.safe_dump(manifest, sort_keys=False).encode(),
             "memo/derived/verify.ts": VERIFY, "memo/derived/skills/memo/SKILL.md": SKILL,
             "memo/skills/extra/SKILL.md": ADDED, "memo/examples/basic/a.memo": b"# A memo\n",
             "notes.txt": b"Loose notes\n", **(extra or {})}
    cell = Cell(name="Memos", files=[VirtualFile(name=path, content=body.decode()) for path, body in files.items()])
    with state_lock:
        add_capabilities_cell(_channel(client), cell)
    persist_state()
    flush_persist_state()
    return cell


def _show_capabilities(client):
    """Move Capabilities from the top bar onto the board, first."""
    from cell_placement import capabilities_stack
    from state import flush_persist_state, persist_state, state_lock
    ch = _channel(client)
    with state_lock:
        stack = capabilities_stack(ch)
        ch.nav_stacks.remove(stack)
        ch.main_stacks.insert(0, stack)
    persist_state()
    flush_persist_state()


def _build(client, cell_id, folder):
    return client.post(f"/api/channels/{client.channel_id}/capabilities/build",
                       json={"cell_id": str(cell_id), "folder": folder})


def _spawned(client, response) -> dict[str, bytes]:
    """The spawned cell's files (cell-relative path → bytes)."""
    from package_approval import snapshot_cell
    assert response.status_code == 200, response.text
    return snapshot_cell(_channel(client), response.json()["cell_id"]).bytes()


def _front(brief: bytes) -> tuple[dict, bytes]:
    """``brief.md``'s fenced YAML block (first) and the body after the blank
    line that follows it."""
    opening = b"```yaml\n"
    assert brief.startswith(opening)
    head, closing, body = brief[len(opening):].partition(b"```\n\n")
    assert closing and head.endswith(b"\n") and b"```" not in head
    return yaml.safe_load(head), body


def _guides(kind: str) -> dict[str, bytes]:
    return {f"guides/{kind}-authoring/SKILL.md":
            f"---\nname: {kind}-authoring\ndescription: Write a {kind}.\n---\n# {kind}\n".encode(),
            f"guides/{kind}-authoring/references/notes.md": f"# {kind} notes\n".encode()}


def _card(client):
    from builtin_packages import handler_card
    return handler_card(_channel(client))


# ---------------------------------------------------------------------------
# The briefs
# ---------------------------------------------------------------------------

def test_a_brief_body_is_its_skill_without_front_matter(synthetic_briefs):
    from handler_build import brief_body, brief_references
    assert brief_body("create") == b"# Synthetic create brief\n"
    assert brief_references("build") == {"checks.md": b"# Checks\n"}
    assert brief_references("create") == {}


def test_create_is_app_machinery_pinned_like_build_and_regenerate(client):
    from builtin_packages import pinned
    for name in ("build", "regenerate", "create"):
        assert pinned("handler", f"derived/skills/{name}/SKILL.md"), name
    assert not pinned("handler", "derived/skills/handler-authoring/SKILL.md")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def test_a_build_copies_the_package_without_its_added_skills(synthetic_briefs):
    client = synthetic_briefs
    cell = _memo_cell(client)
    files = _spawned(client, _build(client, cell.id, "memo"))
    source = {f.name: f.content.encode() for f in cell.files}
    assert set(files) == {"memo/memo.handler", "memo/derived/verify.ts", "memo/derived/skills/memo/SKILL.md",
                          "memo/examples/basic/a.memo", "brief.md", "guides/build/references/checks.md",
                          *_guides("handler")}
    for path in ("memo/memo.handler", "memo/derived/verify.ts", "memo/examples/basic/a.memo"):
        assert files[path] == source[path], path
    assert yaml.safe_load(files["memo/memo.handler"])["derived"]  # the build in use stays declared
    assert {path: files[path] for path in _guides("handler")} == _guides("handler")


def test_the_build_brief_names_the_handler_and_its_folder(synthetic_briefs):
    client = synthetic_briefs
    cell = _memo_cell(client)
    front, body = _front(_spawned(client, _build(client, cell.id, "memo/"))["brief.md"])
    assert front == {"brief": "build", "handler": "memo", "package": "memo/"}
    assert body == b"# Synthetic build brief\n"


def test_the_work_cell_pins_its_brief_and_is_named_for_the_handler(synthetic_briefs):
    from cell_placement import find_cell
    client = synthetic_briefs
    cell = _memo_cell(client)
    response = _build(client, cell.id, "memo")
    answer = response.json()
    assert answer["brief_path"] == "brief.md"
    stack, spawned = find_cell(_channel(client), answer["cell_id"])
    assert spawned is not None and stack is not None
    assert (spawned.pinned, spawned.name, stack.name, stack.stack_type) == (
        "brief.md", "Build ‘Memo’", "Build ‘Memo’", "standard")
    assert str(stack.id) == answer["stack_id"]
    assert [c.id for c in stack.cells] == [spawned.id]


def test_a_work_stack_opens_at_the_end_of_the_board_while_capabilities_is_hidden(synthetic_briefs):
    client = synthetic_briefs
    cell = _memo_cell(client)
    answer = _build(client, cell.id, "memo").json()
    main = answer["main_stacks"]
    assert main[-1]["id"] == answer["stack_id"]
    assert answer["main_selection_id"] == answer["stack_id"]


def test_a_work_stack_opens_right_of_capabilities_on_the_board(synthetic_briefs):
    client = synthetic_briefs
    cell = _memo_cell(client)
    _show_capabilities(client)
    blank = client.post(f"/api/stacks/{client.channel_id}/create")
    assert blank.status_code == 200, blank.text
    answer = _build(client, cell.id, "memo").json()
    ids = [stack["id"] for stack in answer["main_stacks"]]
    capabilities = next(i for i, stack in enumerate(answer["main_stacks"]) if stack["stack_type"] == "capabilities")
    assert ids[capabilities + 1] == answer["stack_id"]
    assert len(ids) > capabilities + 2  # the blank stack stays after it
    assert answer["main_selection_id"] == answer["stack_id"]


def test_a_build_is_a_duplicate_of_the_handler_cell_in_the_lineage(synthetic_briefs):
    from channel_db import open_channel_db
    client = synthetic_briefs
    cell = _memo_cell(client)
    answer = _build(client, cell.id, "memo").json()
    conn = open_channel_db(client.channel_id)
    try:
        rows = conn.execute("SELECT op, from_cell, to_cell FROM cell_moves").fetchall()
    finally:
        conn.close()
    assert [tuple(row) for row in rows] == [("duplicate", str(cell.id), answer["cell_id"])]


def test_a_build_leaves_the_capabilities_cell_unchanged(synthetic_briefs):
    from package_approval import snapshot_cell
    client = synthetic_briefs
    cell = _memo_cell(client)
    before = snapshot_cell(_channel(client), str(cell.id)).bytes()
    _build(client, cell.id, "memo")
    assert snapshot_cell(_channel(client), str(cell.id)).bytes() == before


@pytest.mark.parametrize(("cell", "folder", "status", "detail"), [
    ("unknown", "memo", 404, "Cell not found"),
    ("memo", "nope", 404, "No folder 'nope' in this cell"),
    ("memo", "memo/examples", 400, "memo/examples/ needs exactly one .handler"),
    ("card", "nope", 400, "Regenerate skills names a built-in handler on its card"),
    ("standard", "memo", 400, "Handlers are built from their Capabilities cell"),
])
def test_a_build_refuses_what_is_not_one_handler_on_capabilities(synthetic_briefs, cell, folder, status, detail):
    client = synthetic_briefs
    memo = _memo_cell(client)
    cell_id = {"unknown": uuid.uuid4(), "memo": memo.id, "card": _card(client)}.get(cell)
    if cell == "standard":
        created = client.post(f"/api/stacks/{client.channel_id}/create")
        cell_id = created.json()["main_stacks"][-1]["cells"][0]["id"]
    response = _build(client, cell_id, folder)
    assert (response.status_code, response.json()["detail"]) == (status, detail)


def test_a_build_of_a_disabled_handler_is_refused(synthetic_briefs):
    client = synthetic_briefs
    memo = _memo_cell(client)
    _memo_cell(client)  # a second cell declaring the same id disables both
    response = _build(client, memo.id, "memo")
    assert (response.status_code, response.json()["detail"]) == (400, "Handler declaration is unavailable or invalid")


def test_a_handler_off_capabilities_is_never_inspected(synthetic_briefs):
    client = synthetic_briefs
    memo = _memo_cell(client)
    answer = _build(client, memo.id, "memo").json()
    response = client.post(f"/api/channels/{client.channel_id}/cells/{answer['cell_id']}/handler-build/inspect",
                           json={"folder": "memo"})
    assert (response.status_code, response.json()["detail"]) == (
        400, "Handlers are reviewed from their Capabilities cell")


def test_build_requests_are_bounded(client):
    url = f"/api/channels/{client.channel_id}/capabilities/build"
    for body in ({"cell_id": str(uuid.uuid4()), "folder": ""}, {"cell_id": "x", "folder": "memo"},
                 {"cell_id": str(uuid.uuid4()), "folder": "memo", "extra": 1}):
        assert client.post(url, json=body).status_code == 422, body


# ---------------------------------------------------------------------------
# Regenerate skills
# ---------------------------------------------------------------------------

def _add_runner_example(client) -> str:
    response = client.post(f"/api/stacks/{client.channel_id}/create")
    cell_id = response.json()["main_stacks"][-1]["cells"][0]["id"]
    put = client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate", json={
        "operation_id": str(uuid.uuid4()), "mutations": [dict(
            path="saved.runner", writer_id="tab:1", edit_seq=1, base_revision=0, kind="put",
            content_text="id: saved\n")]})
    assert put.status_code == 200, put.text
    added = client.post(f"/api/channels/{client.channel_id}/builtin/handlers/runner/examples", json={
        "source_cell_id": cell_id, "paths": ["saved.runner"], "description": "Saved", "expect": "valid",
        "feedback": "Complete."})
    assert added.status_code == 200, added.text
    return added.json()["folder"]


def test_regenerate_holds_the_projection_with_the_channels_added_examples(synthetic_briefs):
    from builtin_packages import builtin_handlers
    client = synthetic_briefs
    folder = _add_runner_example(client)
    files = _spawned(client, _build(client, _card(client), "runner"))
    vendored = builtin_handlers()["runner"].files
    manifest = yaml.safe_load(files["runner/runner.handler"])
    shipped = yaml.safe_load(vendored["runner.handler"])["examples"]
    assert manifest["examples"][:len(shipped)] == shipped
    assert manifest["examples"][len(shipped):] == [
        {"folder": folder, "description": "Saved", "expect": "valid", "feedback": "Complete."}]
    assert files[f"runner/examples/{folder}/saved.runner"] == b"id: saved\n"
    assert files["runner/derived/verify.ts"] == vendored["derived/verify.ts"]
    assert files["runner/derived/skills/runner-authoring/SKILL.md"] == \
        vendored["derived/skills/runner-authoring/SKILL.md"]
    assert "runner/additions.yaml" not in files
    assert {p for p in files if not p.startswith("runner/")} == {"brief.md", *_guides("handler")}
    front, body = _front(files["brief.md"])
    assert front == {"brief": "regenerate", "handler": "runner", "package": "runner/"}
    assert body == b"# Synthetic regenerate brief\n"


def test_regenerate_starts_from_skills_waiting_for_review(synthetic_briefs):
    from capability_spawn import builtin_projection
    client = synthetic_briefs
    response = client.post(f"/api/stacks/{client.channel_id}/create")
    cell_id = response.json()["main_stacks"][-1]["cells"][0]["id"]
    regenerated = "---\nname: runner-authoring\ndescription: Better.\n---\n# Better\n"
    put = client.post(f"/api/channels/{client.channel_id}/cells/{cell_id}/files/mutate", json={
        "operation_id": str(uuid.uuid4()), "mutations": [dict(
            path="runner/derived/skills/runner-authoring/SKILL.md", writer_id="tab:1", edit_seq=1,
            base_revision=0, kind="put", content_text=regenerated)]})
    assert put.status_code == 200, put.text
    offered = client.post(f"/api/channels/{client.channel_id}/builtin/handlers/runner/skills",
                          json={"source_cell_id": cell_id, "folder": "runner"})
    assert offered.status_code == 200, offered.text
    files = builtin_projection(_channel(client), "runner")
    assert files["derived/skills/runner-authoring/SKILL.md"] == regenerated.encode()
    assert "derived/skills/runner-authoring/references/notes.md" in files  # pinned: the build's


def test_regenerate_records_no_lineage(synthetic_briefs):
    from channel_db import open_channel_db
    client = synthetic_briefs
    _spawned(client, _build(client, _card(client), "handler"))
    conn = open_channel_db(client.channel_id)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cell_moves").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

ANSWERS = [{"question": "What should it do?", "answer": "Check recipes."},
           {"question": "Anything it must follow?", "answer": ""}]


def _create(client, **body):
    return client.post(f"/api/channels/{client.channel_id}/capabilities/create",
                       json={"title": "Recipe", "make": [], "answers": ANSWERS, **body})


def test_create_writes_the_answers_as_the_briefs_yaml_block(synthetic_briefs):
    client = synthetic_briefs
    related = [{"kind": "renderer", "id": "widget"}]
    response = _create(client, make=["handler", "renderer"], related=related)
    files = _spawned(client, response)
    front, body = _front(files["brief.md"])
    assert front == {"brief": "create", "title": "Recipe", "make": ["handler", "renderer"],
                     "answers": ANSWERS, "related": related}
    assert body == b"# Synthetic create brief\n"
    assert set(files) == {"brief.md", *_guides("handler"), *_guides("renderer")}


def test_create_with_nothing_chosen_carries_every_authoring_guide(synthetic_briefs):
    from cell_placement import find_cell
    client = synthetic_briefs
    response = _create(client)
    files = _spawned(client, response)
    assert set(files) == {"brief.md", *(path for kind in KINDS for path in _guides(kind))}
    stack, cell = find_cell(_channel(client), response.json()["cell_id"])
    assert cell is not None and stack is not None
    assert (cell.name, cell.pinned, stack.stack_type) == ("Create ‘Recipe’", "brief.md", "standard")


@pytest.mark.parametrize("body", [
    {"make": ["handler", "handler"]},
    {"make": ["template"]},
    {"answers": []},
    {"title": ""},
    {"title": "x" * 81},
    {"related": [{"kind": "handler", "id": ""}]},
    {"answers": [{"question": "", "answer": "x"}]},
    {"extra": True},
])
def test_create_requests_are_bounded(client, body):
    assert _create(client, **body).status_code == 422
