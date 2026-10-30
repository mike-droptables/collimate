"""Handler discovery and cache identity follow accepted SQL, including recovery."""
from types import SimpleNamespace

import pytest
import yaml
import test_pytest_persistence_durability

from test_pytest_handlers import install_handler, write_handler_files

workspace = test_pytest_persistence_durability.workspace


def test_catalog_observes_new_revision_at_the_same_clock_time(workspace, monkeypatch):
    import handlers
    import persistence

    cell, manifest, _ = install_handler(workspace.channel)
    first = handlers.catalog(workspace.channel).for_path('x.doc')
    stamp = cell.updated_at
    monkeypatch.setattr(persistence, 'datetime', SimpleNamespace(now=lambda: stamp))
    write_handler_files(workspace.channel, cell, {
        'document/choice.handler': yaml.safe_dump({**manifest, 'guidance': 'Accepted at the same time'})})
    assert cell.updated_at == stamp
    current = handlers.catalog(workspace.channel).for_path('x.doc')
    assert first.manifest['guidance'] == 'A title'
    assert current.manifest['guidance'] == 'Accepted at the same time'


def test_catalog_discovers_committed_paths_after_cache_publication_failure(workspace, monkeypatch):
    import handlers
    import persistence

    cell, _, _ = install_handler(workspace.channel)
    assert handlers.catalog(workspace.channel).for_path('x.fresh') is None
    cached_paths = [file.name for file in cell.files]

    def fail_publication(*args, **kwargs):
        raise OSError('injected derived cache publication failure')

    monkeypatch.setattr(persistence, 'publish_files', fail_publication)
    write_handler_files(workspace.channel, cell, {'fresh/choice.handler': yaml.safe_dump({
        'id': 'fresh', 'name': 'Fresh', 'description': 'Synthetic second handler',
        'guidance': 'Read from SQL', 'types': [{'ext': '.fresh'}], 'examples': []})})
    assert [file.name for file in cell.files] == cached_paths
    current = handlers.catalog(workspace.channel).for_path('x.fresh')
    assert current is not None
    assert current.manifest['guidance'] == 'Read from SQL'


def test_global_confirmation_repair_keeps_current_handler_catalog(workspace, monkeypatch):
    import database
    import state
    import handlers
    import cell_metadata
    from cell_lineage import user_actor
    from persistence import read_file

    cell, manifest, _ = install_handler(workspace.channel)
    state._persist_state_sync()
    old_stamp = cell.updated_at
    old = handlers.catalog(workspace.channel).for_path('x.doc')
    assert old.manifest['guidance'] == 'A title'
    updated = {**manifest, 'guidance': 'Changed accepted guidance'}
    original = database.confirm_sql_durability
    fired = False
    assert cell_metadata.get_global_db_connection.__globals__ is database.__dict__
    assert cell_metadata.DurabilityConfirmationError is database.DurabilityConfirmationError

    def confirm(conn):
        nonlocal fired
        original(conn)
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if path.endswith('/app.db') and not fired:
            fired = True
            # The file commit finishes after the global snapshot was captured;
            # its layout write waits behind the current metadata publication.
            with monkeypatch.context() as delayed_layout:
                delayed_layout.setattr(state, 'persist_state', lambda: None)
                write_handler_files(workspace.channel, cell, {
                    'document/choice.handler': yaml.safe_dump(updated)})
            assert cell.updated_at != old_stamp
            raise database.DurabilityConfirmationError('injected postcommit global directory error')

    monkeypatch.setattr(database, 'confirm_sql_durability', confirm)
    with pytest.raises(database.DurabilityConfirmationError):
        cell_metadata.publish_cell_metadata(workspace.ch, workspace.cid, {'name': 'Committed rename'},
            source_type='manual_edit', actor=user_actor())
    accepted = yaml.safe_load(read_file(workspace.ch, str(cell.id), 'document/choice.handler')[1])
    current = handlers.catalog(workspace.channel).for_path('x.doc')
    assert fired
    assert cell.updated_at == old_stamp
    assert accepted['guidance'] == 'Changed accepted guidance'
    assert current.manifest['guidance'] == accepted['guidance']
