"""The own-cell host boundary and the remaining grant surface."""
import asyncio
from collections import deque
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from models import Cell, Channel, Stack
from process_manager.ipc import IPCMixin
from runner_permissions import RunnerPermissions, RunnerPermissionsError


def test_cell_has_no_obsolete_state_surface__F0057():
    import models

    assert not hasattr(models, 'CellState')
    assert 'state' not in models.Cell.model_fields


@pytest.mark.parametrize('axis,value', [
    ('side_cells', True), ('authoring', 'read'), ('renderers', 'see'),
])
def test_removed_permission_axis_is_refused_with_its_name__D03(axis, value):
    with pytest.raises(RunnerPermissionsError, match=axis):
        RunnerPermissions.from_definition({axis: value})


@pytest.mark.parametrize('op', [
    'create_side_cell', 'start_runner', 'sub_run_status', 'surface_sub_run',
    'serve_environment_register', 'start_modify_run',
    'list_source_cells', 'read_cell', 'write_cell', 'create_source_cell',
    'create_runner', 'list_runners', 'validate_runner', 'list_generators',
    'list_renderers', 'check_renderer', 'request_settings_update',
])
def test_removed_op_reaches_unknown_op_refusal__D01_D02_F1226_F2029(op):
    class Manager(IPCMixin):
        async def _unknown_op(self, data):
            self.refused = data['op']
    manager = Manager()
    manager.refused = None
    manager.log_buffer = deque()
    manager.permissions = RunnerPermissions.from_definition({})
    manager._ipc_writer = SimpleNamespace(write=lambda _: None)
    assert op not in manager._ipc_handlers
    asyncio.run(manager._handle_ipc(json.dumps({'op': op})))
    assert manager.refused == op


def _inline_moves(monkeypatch, *stacks_):
    """A channel of ``stacks_`` on the board with moves run inline and
    nothing persisted; answers ``(routes.stacks, channel)``."""
    from routes import stacks
    import state

    async def in_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(stacks.asyncio, 'to_thread', in_thread)
    channel = Channel(name='test', main_stacks=list(stacks_))
    monkeypatch.setattr(state.state, 'channels', [channel])
    monkeypatch.setattr(stacks, 'persist_state', lambda: None)
    return stacks, channel


def _capabilities(*cells):
    from cell_placement import CAPABILITIES_STACK_TYPE
    return Stack(name='Capabilities', stack_type=CAPABILITIES_STACK_TYPE, cells=list(cells))


def test_a_cell_moving_off_capabilities_gets_a_fresh_standard_column(monkeypatch):
    card, capability = Cell(builtin=True), Cell()
    capabilities, board = _capabilities(card, capability), Stack(cells=[Cell()])
    stacks, channel = _inline_moves(monkeypatch, capabilities, board)
    asyncio.run(stacks.move_cell_to(str(channel.id), capabilities.id, 2, 'insert', capability.id))
    assert [s.id for s in channel.main_stacks[:2]] == [capabilities.id, board.id]
    column = channel.main_stacks[2]
    assert (column.stack_type, column.cells) == ('standard', [capability])
    assert capabilities.cells == [card] and channel.main_selection_id == column.id


def test_a_cell_moving_onto_capabilities_lands_after_the_cards(monkeypatch):
    cards = [Cell(builtin=True), Cell(builtin=True)]
    capabilities = _capabilities(*cards, Cell())
    moving = Cell()
    board = Stack(cells=[moving, Cell()])
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    asyncio.run(stacks.move_cell_to(str(channel.id), board.id, 1, 'stack', moving.id, 0))
    assert capabilities.cells[:3] == [*cards, moving] and len(capabilities.cells) == 4
    assert channel.main_selection_id == capabilities.id


@pytest.mark.parametrize('mode,target', [('insert', 2), ('stack', 0), ('stack', 5)])
def test_capabilities_never_empties_away(monkeypatch, mode, target):
    capability = Cell()
    capabilities, board = _capabilities(capability), Stack(cells=[Cell()])
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    asyncio.run(stacks.move_cell_to(str(channel.id), capabilities.id, target, mode, capability.id))
    assert capabilities in channel.main_stacks and capabilities.cells == []
    landed = next(s for s in channel.main_stacks if capability in s.cells)
    assert landed.stack_type == 'standard' and landed is not capabilities


def test_a_board_stack_whose_only_cell_moves_onto_capabilities_leaves(monkeypatch):
    moving = Cell()
    board, capabilities = Stack(cells=[moving]), _capabilities(Cell(builtin=True))
    stacks, channel = _inline_moves(monkeypatch, board, capabilities)
    asyncio.run(stacks.move_cell_to(str(channel.id), board.id, 1, 'stack', moving.id))
    assert channel.main_stacks == [capabilities] and capabilities.cells[-1] is moving


def test_a_stale_cell_id_answers_409_and_moves_nothing(monkeypatch):
    a, b = Stack(cells=[Cell(), Cell()]), Stack(cells=[Cell()])
    stacks, channel = _inline_moves(monkeypatch, a, b)
    elsewhere = b.cells[0]
    before = channel.model_dump()
    for cell_id in (elsewhere.id, Cell().id):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(stacks.move_cell_to(str(channel.id), a.id, 1, 'stack', cell_id))
        assert (refused.value.status_code, refused.value.detail) == (409, 'The cell moved — try again')
    assert channel.model_dump() == before


def test_cell_move_rejects_unknown_mode__F0262(monkeypatch):
    a = Stack(cells=[Cell(), Cell()])
    stacks, channel = _inline_moves(monkeypatch, a)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(stacks.move_cell_to(str(channel.id), a.id, 1, 'bogus', a.cells[0].id))
    assert refused.value.status_code == 400


def test_channel_migration_removes_obsolete_columns_and_index__D01_D02_F0084():
    import sqlite3
    import migrations

    conn = sqlite3.connect(':memory:')
    migrations.apply(conn, migrations.CHANNEL_MIGRATIONS[:9])
    conn.execute("INSERT INTO runs(id, prompt, parent_run_id, call_config) VALUES('r', 'keep', 'p', '{}')")
    conn.execute("INSERT INTO cells(id, run_id, code_saved_by_user) VALUES('c', 'r', 1)")
    conn.commit()
    migrations.apply(conn, migrations.CHANNEL_MIGRATIONS)
    assert {'parent_run_id', 'call_config'}.isdisjoint(
        row[1] for row in conn.execute('PRAGMA table_info(runs)'))
    assert 'code_saved_by_user' not in {
        row[1] for row in conn.execute('PRAGMA table_info(cells)')}
    assert 'idx_cells_deleted_at' not in {
        row[1] for row in conn.execute('PRAGMA index_list(cells)')}
    assert conn.execute("SELECT prompt FROM runs WHERE id='r'").fetchone() == ('keep',)
    assert conn.execute("SELECT run_id FROM cells WHERE id='c'").fetchone() == ('r',)


def test_standard_cell_move_holds_its_commit_lock__F0262(monkeypatch):
    from contextlib import contextmanager
    from routes import stacks
    import cell_commits
    import state
    a, b = Stack(cells=[Cell(), Cell()]), Stack(cells=[Cell()])
    channel = Channel(name='test', main_stacks=[a, b])
    moving = a.cells[0]
    locked = []

    @contextmanager
    def locks(ids):
        locked.append(set(ids))
        yield
        locked.pop()

    class CheckedList(list):
        def pop(self, index=-1):
            assert any(str(moving.id) in ids for ids in locked)
            return super().pop(index)

    a.cells = CheckedList(a.cells)
    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(stacks.asyncio, 'to_thread', inline)
    monkeypatch.setattr(state.state, 'channels', [channel])
    monkeypatch.setattr(stacks, 'persist_state', lambda: None)
    monkeypatch.setattr(cell_commits, 'cell_commit_locks', locks)
    asyncio.run(stacks.move_cell_to(str(channel.id), a.id, 1, 'stack', moving.id))
    assert b.cells[0] is moving
    assert not locked
