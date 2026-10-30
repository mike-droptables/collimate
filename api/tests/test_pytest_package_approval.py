"""Add to Capabilities: the package preview, the approval and a handler's
build review, through the real user routes.

Any kind of package goes into any capability cell (or a new one at the end
of the Capabilities stack). A handler carries its ``derived/`` folder but
never the source's ``derived:`` line: a new cell gets none, a replacement
keeps the target's line and its added skills, so the build in use stays in
use until Review and accept. Synthetic packages only: handler ``custom`` on
``.custom`` and generator ``synth-gen``.
"""

import uuid

import yaml

import test_pytest_run_folders as support

HANDLER = {
    "id": "custom",
    "name": "Custom",
    "description": "Test",
    "types": [{"ext": ".custom"}],
    "guidance": "Check",
    "examples": [],
}
VERIFY = "export default ()=>({valid:true})"


# The run-folder suite's bare channel and a fresh board cell on it; the
# approval publication suite shares these.
client, cell = support.client, support.cell


def _accept(channel, cell):
    from cell_commits import commit_source_cell
    commit_source_cell(channel, cell, cell.files, persist=False)


def _accepted(channel_id, cell_id):
    from cas import read_blob
    from database import get_db_connection
    from persistence import snapshot_files
    with get_db_connection(channel_id) as conn:
        return {row["path"]: read_blob(row["body_hash"]).decode("utf-8", errors="replace")
                for row in snapshot_files(conn, cell_id)}


def _handler(**changes):
    return yaml.safe_dump({**HANDLER, **changes})


def _generator_files(identity="synth-gen"):
    from models import VirtualFile
    return [
        VirtualFile(name=f"{identity}/choice.generator", content=yaml.safe_dump({
            "id": identity, "name": "Synth", "summary": "Synthetic generator",
            "entrypoint": "main.py", "main_config": "settings.set_yaml"})),
        VirtualFile(name=f"{identity}/main.py", content='print("synth")\n'),
        VirtualFile(name=f"{identity}/settings.set_yaml", content="fields: []\n"),
    ]


def setup_package(cell):
    """The board cell ``cell`` holds the ``custom`` handler package; an empty
    capability cell ``Target`` sits on Capabilities. Answers ``(channel,
    source, capabilities stack)``."""
    from cell_placement import capabilities_stack, find_cell
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from state import find_active_channel

    ch = find_active_channel()
    source = find_cell(ch, cell)[1]
    source.files = [
        VirtualFile(name="custom/custom.handler", content=_handler()),
        VirtualFile(name="custom/notes.txt", content="exact\r\n"),
    ]
    _accept(ch, source)
    add_capabilities_cell(ch, Cell(name="Target"))
    return ch, source, capabilities_stack(ch)


def _target(stack, name="Target"):
    return next(c for c in stack.cells if c.name == name)


def _request(cell, target_cell_id, path="custom/custom.handler"):
    return {"source_cell_id": cell, "definition_path": path, "target_cell_id": target_cell_id}


def _preview(client, request):
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def _add(client, request, preview, **extra):
    return client.post(f"/api/channels/{client.channel_id}/packages/add", json=request | {
        "expected_content_hash": preview["content_hash"],
        "expected_target_revision": preview["target_revision"], **extra})


def test_requests_name_no_target_stack(client, cell):
    _, _, stack = setup_package(cell)
    request = _request(cell, None) | {"target_stack_id": str(stack.id)}
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert response.status_code == 422


def test_add_to_rechecks_displayed_hash_before_any_mutation__D16(client, cell):
    ch, source, stack = setup_package(cell)
    request = _request(cell, str(_target(stack).id))
    preview = _preview(client, request)
    source.files[1].content = "changed"
    _accept(ch, source)
    assert _add(client, request, preview).status_code == 409
    assert _target(stack).files == []


def test_a_handler_into_a_new_target_carries_derived_but_never_its_line__D16(client, cell):
    from collimate.handlers import derived_hash
    from database import get_db_connection
    from handlers import catalog
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    source.files[0].content = _handler(derived=derived_hash({"verify.ts": VERIFY.encode()}))
    source.files.append(VirtualFile(name="custom/derived/verify.ts", content=VERIFY))
    _accept(ch, source)
    request = _request(cell, str(_target(stack).id))
    preview = _preview(client, request)
    assert preview["replacement"] is None
    assert preview["summary"]["derived_hash"] == derived_hash({"verify.ts": VERIFY.encode()})
    assert preview["summary"]["kept_from_target"] == []
    response = _add(client, request, preview)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["message"] == "Added. Review and accept its build from its card in Capabilities."
    assert result["source_version_id"]
    target = _target(stack)
    files = {f.name: f.content for f in target.files}
    assert set(files) == {"custom/custom.handler", "custom/notes.txt", "custom/derived/verify.ts"}
    # The source's own derived: line is never trusted: the build waits for review.
    assert "derived" not in yaml.safe_load(files["custom/custom.handler"])
    record = next(r for r in catalog(ch).records if r.cell_id == str(target.id))
    assert record.public()["accepted"] is False
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT source_type FROM cells WHERE id=?",
                            (result["version_id"],)).fetchone()[0] == "add_to"
        row = conn.execute("SELECT * FROM package_approvals WHERE version_id=?",
                           (result["version_id"],)).fetchone()
        assert row["source_cell_id"] == cell and row["source_version_id"] == result["source_version_id"]
        assert row["content_hash"] == preview["content_hash"]
    finally:
        conn.close()
    # Its provenance: Add to from the source cell, hashed over the package as
    # the target holds it (the derived: line gone).
    from app_version import sdk_version
    from collimate._host.package_hash import content_hash
    from suggested_trees import provenance
    (record,) = provenance(client.channel_id, target.id)
    assert {k: v for k, v in record.items() if k != "admitted_at"} == {
        "folder": "custom", "source_kind": "cell", "source_id": cell, "package_kind": "handler",
        "package_id": "custom", "package_version": None, "sdk_version": sdk_version(),
        "content_hash": content_hash({name[len("custom/"):]: body.encode() for name, body in files.items()})}
    assert record["content_hash"] != preview["content_hash"]


def test_a_handler_without_derived_asks_for_a_build(client, cell):
    _, _, stack = setup_package(cell)
    request = _request(cell, None)
    preview = _preview(client, request)
    assert preview["summary"]["derived_hash"] is None
    response = _add(client, request, preview)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Added. Build it from its card in Capabilities."
    assert stack.cells[-1].name == "Custom"


def test_replace_keeps_the_targets_derived_line_and_added_skills(client, cell):
    from collimate.handlers import derived_hash
    from handlers import catalog, save_derived_skill
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    accepted = derived_hash({"verify.ts": VERIFY.encode()})
    target = _target(stack)
    target.files = [
        VirtualFile(name="custom/custom.handler", content=_handler(derived=accepted)),
        VirtualFile(name="custom/derived/verify.ts", content=VERIFY),
        VirtualFile(name="custom/skills/mine/SKILL.md", content="---\nname: mine\ndescription: Mine.\n---\n"),
        VirtualFile(name="other/keep.txt", content="keep"),
    ]
    _accept(ch, target)
    save_derived_skill(accepted, {"verify.ts": VERIFY.encode()})
    rebuilt = 'export default ()=>({valid:false,error:"rebuilt"})'
    source.files = [
        VirtualFile(name="custom/custom.handler", content=_handler(derived="0" * 64)),
        VirtualFile(name="custom/derived/verify.ts", content=rebuilt),
        VirtualFile(name="custom/skills/theirs/SKILL.md", content="---\nname: theirs\ndescription: No.\n---\n"),
    ]
    _accept(ch, source)
    probe = _preview(client, _request(cell, None))
    assert [(t["name"], t["replaces"]) for t in probe["targets"]] == [("New cell", False), ("Target", True)]
    request = _request(cell, str(target.id))
    preview = _preview(client, request)
    assert preview["replacement"]["folder"] == "custom"
    assert preview["summary"]["derived_hash"] == derived_hash({"verify.ts": rebuilt.encode()})
    assert preview["summary"]["kept_from_target"] == ["skills/mine/SKILL.md"]
    assert _add(client, request, preview).status_code == 409  # replacement unconfirmed
    response = _add(client, request, preview, replace=True)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Added. Review and accept its build from its card in Capabilities."
    files = _accepted(client.channel_id, str(target.id))
    assert set(files) == {"custom/custom.handler", "custom/derived/verify.ts",
                          "custom/skills/mine/SKILL.md", "other/keep.txt"}
    assert yaml.safe_load(files["custom/custom.handler"])["derived"] == accepted
    assert files["custom/derived/verify.ts"] == rebuilt
    # The accepted build stays in use; the new one waits for review.
    record = next(r for r in catalog(ch).records if r.cell_id == str(target.id))
    assert record.accepted_files["verify.ts"] == VERIFY.encode()
    assert record.public()["accepted"] is False


def test_any_kind_goes_into_any_capability_cell(client, cell):
    from cell_placement import find_cell
    from state import find_active_channel

    ch, _, stack = setup_package(cell)
    request = _request(cell, None)
    added = _add(client, request, _preview(client, request))
    assert added.status_code == 200, added.text
    handler_cell = added.json()["cell_id"]
    source = find_cell(find_active_channel(), cell)[1]
    source.files = _generator_files()
    _accept(ch, source)
    request = _request(cell, handler_cell, "synth-gen/choice.generator")
    preview = _preview(client, request)
    assert preview["kind"] == "generator" and preview["summary"]["derived_hash"] is None
    response = _add(client, request, preview)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Added"
    files = _accepted(client.channel_id, handler_cell)
    assert {"custom/custom.handler", "synth-gen/choice.generator"} <= set(files)
    # The id now lives in the handler's cell: a copy into another cell clashes.
    again = _request(cell, str(_target(stack).id), "synth-gen/choice.generator")
    refused = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=again)
    assert refused.status_code == 400 and "rename first" in refused.text


def test_handler_accept_is_exact_hash_and_old_acceptance_survives_build__D17(client, cell):
    from collimate.handlers import derived_hash
    from handlers import catalog
    from models import VirtualFile

    ch, _, stack = setup_package(cell)
    target = _target(stack)
    target.files = [
        VirtualFile(name="custom/custom.handler", content=_handler(derived=derived_hash({"verify.ts": VERIFY.encode()}))),
        VirtualFile(name="custom/derived/verify.ts", content=VERIFY),
    ]
    _accept(ch, target)
    url = f"/api/channels/{client.channel_id}/cells/{target.id}/handler-build/"
    first = client.post(url + "inspect", json={"folder": "custom"})
    assert first.status_code == 200, first.text
    assert client.post(url + "accept", json={"folder": "custom", "expected_content_hash":
                                             first.json()["content_hash"]}).status_code == 200
    record = next(r for r in catalog(ch).records if r.cell_id == str(target.id))
    assert record.public()["accepted"] and record.accepted_files["verify.ts"] == VERIFY.encode()
    next(f for f in target.files if f.name == "custom/derived/verify.ts").content = \
        'export default ()=>({valid:false,error:"new"})'
    _accept(ch, target)
    record = next(r for r in catalog(ch).records if r.cell_id == str(target.id))
    assert record.accepted_files["verify.ts"] == VERIFY.encode() and not record.public()["accepted"]
    result = client.post(url + "inspect", json={"folder": "custom"})
    assert result.status_code == 200, result.text
    assert result.json()["compile"]["valid"], result.text
    assert client.post(url + "accept", json={"folder": "custom", "expected_content_hash": "0" * 64}).status_code == 409
    response = client.post(url + "accept", json={"folder": "custom", "expected_content_hash":
                                                 result.json()["content_hash"]})
    assert response.status_code == 200, response.text
    record = next(r for r in catalog(ch).records if r.cell_id == str(target.id))
    assert record.public()["accepted"] and b'error:"new"' in record.accepted_files["verify.ts"]


def test_handler_accept_refuses_non_callable_default__D17(client, cell):
    from models import VirtualFile

    ch, _, stack = setup_package(cell)
    target = _target(stack)
    target.files = [
        VirtualFile(name="custom/custom.handler", content=_handler(guidance="")),
        VirtualFile(name="custom/derived/verify.ts", content="export default 123;"),
    ]
    _accept(ch, target)
    url = f"/api/channels/{client.channel_id}/cells/{target.id}/handler-build/"
    inspect = client.post(url + "inspect", json={"folder": "custom"})
    assert inspect.status_code == 200, inspect.text
    assert not inspect.json()["compile"]["valid"]
    response = client.post(url + "accept", json={"folder": "custom", "expected_content_hash":
                                                 inspect.json()["content_hash"]})
    assert response.status_code == 400 and "callable" in response.text
    assert "derived" not in yaml.safe_load(target.files[0].content)


def test_add_to_replacement_preserves_other_package_and_requires_confirmation__D16(client, cell):
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    target = _target(stack)
    target.files = [f.model_copy() for f in source.files] + [VirtualFile(name="other/keep.txt", content="keep")]
    source.files[1].content = "replacement"
    _accept(ch, target)
    _accept(ch, source)
    request = _request(cell, str(target.id))
    preview = _preview(client, request)
    assert preview["replacement"]["folder"] == "custom"
    assert _add(client, request, preview).status_code == 409
    response = _add(client, request, preview, replace=True)
    assert response.status_code == 200, response.text
    assert {f.name: f.content for f in target.files}["other/keep.txt"] == "keep"
    assert _accepted(client.channel_id, str(target.id))["custom/notes.txt"] == "replacement"


def test_add_to_rejects_nested_definition_and_id_elsewhere__D16(client, cell):
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile

    ch, source, stack = setup_package(cell)
    request = _request(cell, None)
    source.files.append(VirtualFile(name="custom/nested/bad.renderer", content="id: bad"))
    _accept(ch, source)
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert response.status_code == 400 and "second definition" in response.text
    source.files = [f for f in source.files if f.name != "custom/nested/bad.renderer"]
    _accept(ch, source)
    holder = Cell(name="Holder", files=[f.model_copy() for f in source.files])
    add_capabilities_cell(ch, holder)
    # The probe answers, naming the holder; a New cell or another cell is refused.
    probe = _preview(client, request)
    assert [t["cell_id"] for t in probe["targets"] if t["replaces"]] == [str(holder.id)]
    refused = _add(client, request, probe)
    assert refused.status_code == 400
    assert refused.json()["detail"] == "Definition id already exists elsewhere in Capabilities; rename first"
    other = client.post(f"/api/channels/{client.channel_id}/packages/preview",
                        json=_request(cell, str(_target(stack).id)))
    assert other.status_code == 400 and "rename first" in other.text


def test_a_builtin_card_is_never_a_target(client, cell):
    _, _, stack = setup_package(cell)
    card = next(c for c in stack.cells if c.builtin)
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview",
                           json=_request(cell, str(card.id)))
    assert response.status_code == 400 and "part of the app" in response.text
    stray = client.post(f"/api/channels/{client.channel_id}/packages/preview",
                        json=_request(cell, str(uuid.uuid4())))
    assert stray.status_code == 400 and "not on Capabilities" in stray.text


def test_accept_rechecks_concurrent_derived_change_before_manifest_write__D17(client, cell, monkeypatch):
    import handler_build
    from models import VirtualFile
    from state import find_active_channel

    ch, source, stack = setup_package(cell)
    target = _target(stack)
    target.files = [f.model_copy() for f in source.files] + [
        VirtualFile(name="custom/derived/verify.ts", content=VERIFY)]
    _accept(ch, target)
    actual = handler_build.inspect_build
    first, _ = actual(find_active_channel(), str(target.id), "custom")

    def race(*args):
        result = actual(*args)
        next(f for f in target.files if f.name == "custom/derived/verify.ts").content = \
            'export default ()=>({valid:false,error:"late"})'
        _accept(ch, target)
        return result

    monkeypatch.setattr(handler_build, "inspect_build", race)
    response = client.post(f"/api/channels/{client.channel_id}/cells/{target.id}/handler-build/accept",
                           json={"folder": "custom", "expected_content_hash": first["content_hash"]})
    assert response.status_code == 409
    assert "derived" not in yaml.safe_load(target.files[0].content)


def test_add_to_new_target_binary_bytes_and_history_projection__D16(client, cell):
    from cas import read_blob, store_bytes
    from cell_files import bulk_version_rows
    from database import get_db_connection
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    raw = b"\x00\xffexact\r\n"
    source.files.append(VirtualFile(name="custom/asset.bin", blob_hash=store_bytes(raw), language="binary"))
    _accept(ch, source)
    request = _request(cell, None)
    preview = _preview(client, request)
    assert preview["targets"][0] == {"cell_id": None, "name": "New cell", "replaces": False}
    assert [t["name"] for t in preview["targets"][1:]] == ["Target"]
    response = _add(client, request, preview)
    assert response.status_code == 200, response.text
    result = response.json()
    copied = stack.cells[-1]
    assert str(copied.id) == result["cell_id"]
    blob = next(f for f in copied.files if f.name.endswith("asset.bin"))
    assert blob.language == "binary" and read_blob(blob.blob_hash) == raw
    conn = get_db_connection(client.channel_id)
    try:
        row = bulk_version_rows(conn, [result["version_id"]])[0]
        assert row["approval"] == {"source_cell_id": cell, "source_version_id": result["source_version_id"],
                                   "content_hash": preview["content_hash"]}
    finally:
        conn.close()


def test_inspect_reports_declared_example_verdicts_without_auto_accept__D17(client, cell):
    from models import VirtualFile

    ch, _, stack = setup_package(cell)
    target = _target(stack)
    manifest = dict(HANDLER, examples=[{"folder": "yes", "description": "Pass", "expect": "valid",
                                        "feedback": "Expected"}])
    target.files = [
        VirtualFile(name="custom/custom.handler", content=yaml.safe_dump(manifest)),
        VirtualFile(name="custom/derived/verify.ts",
                    content='export default input=>({valid:input.text==="yes",error:"no"})'),
        VirtualFile(name="custom/examples/yes/sample.custom", content="yes"),
    ]
    _accept(ch, target)
    response = client.post(f"/api/channels/{client.channel_id}/cells/{target.id}/handler-build/inspect",
                           json={"folder": "custom"})
    assert response.status_code == 200, response.text
    assert response.json()["examples"] == [{"folder": "yes", "expect": "valid",
                                            "verdicts": [{"path": "sample.custom", "valid": True}],
                                            "error": None}]
    assert "derived" not in yaml.safe_load(target.files[0].content)


def test_add_to_late_target_change_and_sql_failure_leave_source_and_target_intact__D16(
        client, cell, monkeypatch):
    import cell_commits
    from database import get_db_connection

    _, _, stack = setup_package(cell)
    request = _request(cell, str(_target(stack).id))
    preview = _preview(client, request)

    def fail(*args, **kwargs):
        raise ValueError("synthetic transaction failure")

    monkeypatch.setattr(cell_commits, "write_source_version", fail)
    response = _add(client, request, preview)
    assert response.status_code == 400, response.text
    assert _target(stack).files == []
    conn = get_db_connection(client.channel_id)
    try:
        assert conn.execute("SELECT count(*) FROM package_approvals").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM runs WHERE prompt='Add to custom'").fetchone()[0] == 0
    finally:
        conn.close()


def test_an_unparseable_definition_on_capabilities_does_not_stop_add_to(client, cell):
    """A capability cell whose definition does not parse (a move needs no
    admission) claims no id or type: another package's preview and Add to
    still go through."""
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile

    ch, _, stack = setup_package(cell)
    add_capabilities_cell(ch, Cell(name="Broken", files=[VirtualFile(name="x/x.handler", content="id: [unclosed\n")]))
    request = _request(cell, None)
    preview = _preview(client, request)
    response = _add(client, request, preview)
    assert response.status_code == 200, response.text
    assert [c.name for c in stack.cells if not c.builtin] == ["Target", "Broken", "Custom"]
