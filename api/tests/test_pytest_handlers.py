"""Real handler catalog and accepted snapshot boundaries: the catalog's
members are the Capabilities cells, whichever other packages they hold."""
import uuid

import pytest
import yaml

from conftest import install_handler


@pytest.fixture
def client(api_app):
    """Real lifecycle and registered channel; definitions are synthetic."""
    from fastapi.testclient import TestClient
    from database import get_db_connection
    from genesis import ensure_capabilities_stack
    from models import Channel
    from state import state, persist_state, flush_persist_state
    with TestClient(api_app, raise_server_exceptions=False) as http:
        channel = Channel(name="Starting")
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        persist_state()
        flush_persist_state()
        yield http


def write_handler_files(channel, cell, files):
    """Publish exact accepted revisions rather than changing derived UI objects."""
    from database import get_db_connection
    from persistence import mutate_files, snapshot_files
    with get_db_connection(str(channel.id)) as conn:
        current = {f['path']: f for f in snapshot_files(conn, str(cell.id), include_deleted=True)}
    receipt = mutate_files(str(channel.id), str(cell.id), {
        'operation_id': str(uuid.uuid4()), 'mutations': [
            {'path': path, 'base_revision': current.get(path, {}).get('file_revision', 0),
             'writer_id': 'host:handler-test:' + str(uuid.uuid4()), 'edit_seq': 1,
             'kind': 'put', 'content_text': body, 'language': 'plaintext'}
            for path, body in files.items()], 'resolves': []}, actor='host')
    assert receipt['outcome'] == 'accepted'
    return receipt


def install_board_handler(channel, *, identity, ext):
    """The same accepted handler package as ``install_handler``, in a
    standard stack on the board instead of on Capabilities."""
    from cell_placement import capabilities_stack
    from models import Stack
    cell, manifest, derived = install_handler(channel, identity=identity, ext=ext)
    capabilities_stack(channel).cells.remove(cell)
    channel.main_stacks.append(Stack(name="Board", cells=[cell]))
    return cell, manifest, derived


def test_handler_catalog_is_stack_scoped_and_serves_only_accepted_skills__D09(client):
    from state import find_active_channel
    from handlers import catalog
    channel = find_active_channel()
    install_handler(channel)
    install_board_handler(channel, identity="draft", ext=".draft")
    install_handler(channel, identity="unaccepted", ext=".unaccepted", accepted=False)
    response = client.get('/api/handlers')
    assert response.status_code == 200
    entries = response.json()
    assert "document" in {row["id"] for row in entries}
    assert "draft" not in {row["id"] for row in entries}
    current = catalog(channel)
    assert current.skills('.DOC') == {"document--doc/SKILL.md": "# Write a document"}
    assert current.skills('unaccepted') == {}


def test_additive_skills_are_distinct_and_stack_approved_without_hash_changes_P4F01(client):
    from state import find_active_channel
    from handlers import catalog
    channel = find_active_channel()
    cell, manifest, derived = install_handler(channel)
    write_handler_files(channel, cell, {'document/skills/local/SKILL.md': '# Local guidance'})
    record = catalog(channel).for_path('x.doc')
    assert record.accepted_files == derived
    assert record.manifest['derived'] == manifest['derived']
    assert record.public()['accepted_skills'] == ['doc']
    assert record.public()['added_skills'] == ['local']
    # Only a built-in uses the app's skills.
    assert record.public()['uses_app_skills'] is False
    assert catalog(channel).skills('.doc') == {
        'document--doc/SKILL.md': '# Write a document',
        'document--local/SKILL.md': '# Local guidance'}
    other, _, _ = install_board_handler(channel, identity='outside', ext='.outside')
    write_handler_files(channel, other, {'outside/skills/local/SKILL.md': '# Unapproved guidance'})
    assert catalog(channel).skills('.outside') == {}


def test_catalog_refuses_reserved_claim_and_type_clash__D09(client):
    from state import find_active_channel
    from handlers import catalog
    channel = find_active_channel()
    install_handler(channel, identity="spoof", ext=".renderer")
    install_handler(channel, identity="one", ext=".doc")
    install_handler(channel, identity="two", ext=".DOC")
    current = catalog(channel)
    assert current.for_path('report.doc') is None
    assert not any(row['id'] == 'spoof' for row in current.public())
    assert any('reserved' in error['error'] for error in current.errors)
    assert all(row['clashes'] == ['.doc'] for row in current.public() if row['id'] in ('one', 'two'))


def test_unaccepted_build_preserves_previous_accepted_snapshot_after_cache_reset__D09_D17__P6F01(client):
    from state import find_active_channel
    import handlers
    channel = find_active_channel()
    cell, manifest, derived = install_handler(channel)
    first = handlers.catalog(channel).for_path('x.doc')
    assert first.accepted_files == derived
    write_handler_files(channel, cell, {'document/derived/verify.ts': 'export default () => ({valid:false,error:"new"});'})
    handlers.clear_catalog_cache()
    second = handlers.catalog(channel).for_path('x.doc')
    assert second.accepted_files == derived
    assert second.public()['accepted'] is False
    assert second.public()['accepted_derived'] == manifest['derived']
    from cas_gc import collect_live_blobs
    import hashlib
    assert {hashlib.sha256(body).hexdigest() for body in derived.values()} <= collect_live_blobs()


def test_catalog_shallow_discovery_and_duplicate_ids__D09(client):
    from state import find_active_channel
    from handlers import catalog
    channel = find_active_channel()
    cell, manifest, _ = install_handler(channel)
    write_handler_files(channel, cell, {'document/examples/x/nested.handler': yaml.safe_dump({**manifest, 'id': 'nested'})})
    assert not any(row['id'] == 'nested' for row in catalog(channel).public())
    install_handler(channel)
    current = catalog(channel)
    assert current.for_path('x.doc') is None
    assert any('Duplicate handler id' in error['error'] for error in current.errors)


def test_a_handler_beside_a_generator_in_one_capabilities_cell_is_listed(client):
    """Which cell holds a package is only organisation: a handler in the cell
    that also holds a generator is in the catalog."""
    from conftest import add_capabilities_cell
    from handlers import catalog
    from models import Cell, VirtualFile
    from state import find_active_channel
    channel = find_active_channel()
    cell, _, _ = install_handler(channel, identity="memo", ext=".memo")
    from cell_placement import capabilities_stack
    capabilities_stack(channel).cells.remove(cell)
    mixed = Cell(name="Mixed", files=[
        *cell.files,
        VirtualFile(name="synth-gen/synth.generator",
                    content="id: synth-gen\nname: Synth\nentrypoint: main.py\nmain_config: settings.set_yaml\n"),
        VirtualFile(name="synth-gen/settings.set_yaml", content="fields: []\n"),
        VirtualFile(name="synth-gen/main.py", content="print('ok')\n"),
    ])
    add_capabilities_cell(channel, mixed)
    record = catalog(channel).for_path("notes.memo")
    assert record is not None and record.cell_id == str(mixed.id)
    assert record.public()["accepted"] is True
