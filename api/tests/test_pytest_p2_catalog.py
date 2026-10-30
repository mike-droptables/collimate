"""Catalog queries and CAS materialization leave the global layout lock free."""
from test_pytest_options_identity import CHANNEL, client as client, make_stack


def test_shelf_reads_leave_state_lock_free__F0073(client, monkeypatch):
    import cell_catalog
    from state import state_lock
    channel, stack, _ = make_stack([('Searchable', {'note.txt': 'body'})])
    cell_catalog.remember_removed_cells(CHANNEL, stack, list(stack.cells))
    with state_lock:
        channel.main_stacks.remove(stack)
    original = cell_catalog._preview

    def checked(*args):
        assert not state_lock._is_owned(), 'shelf SQL and CAS reads must not hold the global state lock'
        return original(*args)

    monkeypatch.setattr(cell_catalog, '_preview', checked)
    assert [e['name'] for e in cell_catalog.list_removed_cells(CHANNEL, query='Searchable')] == ['Searchable']


def test_restore_reads_cas_outside_state_lock__F0073(client, monkeypatch):
    import cell_catalog
    from state import state_lock
    channel, stack, ids = make_stack([('Restore', {'note.txt': 'body'})])
    cell_catalog.remember_removed_cells(CHANNEL, stack, list(stack.cells))
    with state_lock:
        channel.main_stacks.remove(stack)
    original = cell_catalog._restore_files

    def checked(*args):
        assert not state_lock._is_owned(), 'restore CAS reads must not hold the global state lock'
        return original(*args)

    monkeypatch.setattr(cell_catalog, '_restore_files', checked)
    restored = cell_catalog.restore_cell(CHANNEL, ids[0])
    assert restored['cell_id'] == ids[0]


def test_checkpoint_after_removal_keeps_restorable_files(client):
    import cell_catalog
    from cell_files import checkpoint_cells
    from state import state_lock
    channel, stack, ids = make_stack([('Late', {'note.md': 'body'})])
    cell_catalog.remember_removed_cells(CHANNEL, stack, list(stack.cells))
    with state_lock:
        channel.main_stacks.remove(stack)
    # The closing editor's unmount checkpoint arrives after the removal.
    assert checkpoint_cells(CHANNEL, [ids[0]], 'checkpoint') == {ids[0]: None}
    cell_catalog.restore_cell(CHANNEL, ids[0])
    cell = next(c for s in channel.main_stacks for c in s.cells if str(c.id) == ids[0])
    assert [(f.name, f.content) for f in cell.files] == [('note.md', 'body')]


def test_remove_then_editor_close_checkpoint_then_restore_over_http(client):
    """The editor's unmount checkpoint lands after its cell's removal; restore
    still brings back the last edit, every time."""
    from test_pytest_options_identity import accept_file
    channel, _stack, ids = make_stack([('Notes', {'note.md': ''})])
    cell_id = ids[0]
    for text in ('first draft', 'second draft'):
        accept_file(cell_id, 'note.md', text)
        assert client.post(f'/api/channels/{CHANNEL}/cells/{cell_id}/remove').status_code == 200
        assert client.post(f'/api/channels/{CHANNEL}/cells/{cell_id}/checkpoint').json()['created'] is False
        assert client.post(f'/api/channels/{CHANNEL}/cells/{cell_id}/restore').status_code == 200
        cell = next(c for s in channel.main_stacks for c in s.cells if str(c.id) == cell_id)
        assert [(f.name, f.content) for f in cell.files] == [('note.md', text)]
