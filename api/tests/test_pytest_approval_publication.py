"""Exact package publication from accepted SQL snapshots."""


def _accept(channel,cell,files):
    from cell_commits import commit_source_cell
    commit_source_cell(channel,cell,files,persist=False)


def _descriptors(channel,cell):
    from database import get_db_connection
    from persistence import snapshot_files
    conn=get_db_connection(str(channel.id))
    try:
        return {f['path']:f for f in snapshot_files(conn,str(cell.id),include_deleted=True)}
    finally:
        conn.close()

from test_pytest_package_approval import setup_package


def _target(stack):
    """The 'Target' cell setup_package adds to Capabilities."""
    return next(c for c in stack.cells if c.name == "Target")

import test_pytest_package_approval as approval_support

client, cell = approval_support.client, approval_support.cell
from test_pytest_run_folders import _accepted_files


def test_add_to_replacement_publishes_exact_package_and_keeps_unrelated_revision__D16(
    client, cell
):
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    target = _target(stack)
    initial = [f.model_copy() for f in source.files]
    initial[1].content = "old committed"
    initial += [
        VirtualFile(name="custom/obsolete.txt", content="old member"),
        VirtualFile(name="other/unrelated.txt", content="committed sibling"),
    ]
    _accept(ch,target,initial)
    unrelated_before=_descriptors(ch,target)['other/unrelated.txt']
    request = {
        "source_cell_id": cell,
        "definition_path": "custom/custom.handler",
        "target_cell_id": str(target.id),
    }
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert response.status_code == 200, response.text
    preview = response.json()
    response = client.post(
        f"/api/channels/{client.channel_id}/packages/add",
        json=request
        | {
            "expected_content_hash": preview["content_hash"],
            "expected_target_revision": preview["target_revision"],
            "replace": True,
        },
    )
    assert response.status_code == 200, response.text
    committed = _accepted_files(str(target.id))
    assert committed["custom/notes.txt"] == source.files[1].content
    assert "custom/obsolete.txt" not in committed
    assert committed["other/unrelated.txt"] == "committed sibling"
    assert _descriptors(ch,target)['other/unrelated.txt']==unrelated_before


def test_add_to_refuses_a_handler_id_another_capabilities_cell_holds__D16(client, cell):
    import yaml
    from cell_placement import capabilities_stack
    from models import Cell, VirtualFile

    ch, source, stack = setup_package(cell)
    other = yaml.safe_load(source.files[0].content)
    other["types"] = [{"ext": ".different"}]
    holder = Cell(files=[VirtualFile(name="other/other.handler", content=yaml.safe_dump(other))])
    capabilities_stack(ch).cells.append(holder)
    _accept(ch,holder,holder.files)
    request = {
        "source_cell_id": cell,
        "definition_path": "custom/custom.handler",
        "target_cell_id": str(_target(stack).id),
    }
    response = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert response.status_code == 400, response.text
    assert "rename first" in response.text


def test_add_to_preserves_explicit_binary_utf8_storage__D16(client, cell):
    from cas import store_bytes, read_blob
    from models import VirtualFile

    ch, source, stack = setup_package(cell)
    body = b"valid UTF8 but deliberately binary\r\n"
    digest = store_bytes(body)
    source.files.append(
        VirtualFile(name="custom/data.bin", blob_hash=digest, language="binary")
    )
    _accept(ch,source,source.files)
    request = {
        "source_cell_id": cell,
        "definition_path": "custom/custom.handler",
        "target_cell_id": str(_target(stack).id),
    }
    preview = client.post(
        f"/api/channels/{client.channel_id}/packages/preview", json=request
    ).json()
    response = client.post(
        f"/api/channels/{client.channel_id}/packages/add",
        json=request
        | {
            "expected_content_hash": preview["content_hash"],
            "expected_target_revision": preview["target_revision"],
        },
    )
    assert response.status_code == 200, response.text
    copied = next(f for f in _target(stack).files if f.name == "custom/data.bin")
    assert (
        copied.blob_hash == digest
        and copied.content == ""
        and copied.language == "binary"
    )
    assert read_blob(copied.blob_hash) == body


def test_add_to_first_genesis_preserves_unrelated_accepted_refs__D16(client, cell):
    from database import get_db_connection
    from models import VirtualFile
    from persistence import initialize_files,publish_files

    ch, source, stack = setup_package(cell)
    target = _target(stack)
    for owner in (source, target):
        owner.files.append(
            VirtualFile(name="unrelated/accepted.txt", content="accepted sibling")
        )
    conn = get_db_connection(str(ch.id))
    try:
        for owner in (source, target):
            initialize_files(conn,str(owner.id),[owner.files[-1]])
        conn.commit()
    finally:
        conn.close()
    for owner in (source,target):
        publish_files(ch,str(owner.id),{},persist=False)
    unchanged={str(owner.id):_descriptors(ch,owner)['unrelated/accepted.txt'] for owner in (source,target)}
    request = {
        "source_cell_id": cell,
        "definition_path": "custom/custom.handler",
        "target_cell_id": str(target.id),
    }
    preview = client.post(f"/api/channels/{client.channel_id}/packages/preview", json=request)
    assert preview.status_code == 200, preview.text
    response = client.post(
        f"/api/channels/{client.channel_id}/packages/add",
        json=request
        | {
            "expected_content_hash": preview.json()["content_hash"],
            "expected_target_revision": preview.json()["target_revision"],
        },
    )
    assert response.status_code == 200, response.text
    for owner in (source, target):
        assert _descriptors(ch,owner)['unrelated/accepted.txt']==unchanged[str(owner.id)]
        assert next(f for f in owner.files if f.name=='unrelated/accepted.txt').content=='accepted sibling'
    assert _accepted_files(str(target.id))['unrelated/accepted.txt']=='accepted sibling'


import test_pytest_runners as runner_support

runner_app = runner_support.runner_app


def test_runner_approval_reports_actual_mcp_settings__D16(runner_app):
    import yaml
    from models import Cell, Stack, VirtualFile
    from test_pytest_runners import draft
    import routes.handlers as routes

    client, ch, generator, _ = runner_app
    client.app.include_router(routes.router)
    schema = next(f for f in generator.files if f.name == "echo/settings.set_yaml")
    schema.content += "  - key: mcp\n    type: select\n    options: [off, collimate]\n    default: off\n"
    _accept(ch,generator,generator.files)
    body = draft()["runner"]
    body["settings"]["settings.set_yaml"]["mcp"] = "collimate"
    source = Cell(
        files=[
            VirtualFile(name="example/example.runner", content=yaml.safe_dump(body)),
            VirtualFile(name="example/files/scripts/start.sh", content="echo original"),
        ]
    )
    ch.main_stacks.append(Stack(cells=[source]))
    _accept(ch,source,source.files)
    response = client.post(
        f"/api/channels/{ch.id}/packages/preview",
        json={
            "source_cell_id": str(source.id),
            "definition_path": "example/example.runner",
            "target_cell_id": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["mcp"] == {"settings.set_yaml": "collimate"}
