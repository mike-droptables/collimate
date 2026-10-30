"""Prepared imports do no state acquisition under a channel SQL writer."""
import threading
from contextlib import contextmanager

import pytest


def test_import_prepares_without_publication_and_never_takes_state_under_sql(fresh_cwd,monkeypatch):
    import database
    import state
    from import_plan import ImportPlan
    from models import Cell,Channel,Stack,VirtualFile
    from persistence import snapshot_files
    channel=Channel(name='Synthetic prepared import')
    state.state.channels=[channel]
    plan=ImportPlan(channel)
    added=Cell(name='Added',files=[VirtualFile(name='new.txt',content='new bytes')])
    plan.candidate.main_stacks.append(Stack(cells=[added]))
    assert channel.main_stacks==[]
    phase=threading.local()
    transaction=database.channel_transaction
    @contextmanager
    def observed_transaction(*args,**kwargs):
        with transaction(*args,**kwargs) as conn:
            phase.writing=True
            try:
                yield conn
            finally:
                phase.writing=False
    lock=state.state_lock
    class GuardedStateLock:
        def __enter__(self):
            assert not getattr(phase,'writing',False),'state lock acquired under channel writer'
            lock.acquire()
        def __exit__(self,*args):
            lock.release()
    monkeypatch.setattr(database,'channel_transaction',observed_transaction)
    monkeypatch.setattr(state,'state_lock',GuardedStateLock())
    plan.publish()
    assert channel.main_stacks[0].cells[0].id==added.id
    conn=database.get_db_connection(str(channel.id))
    try:
        assert snapshot_files(conn,str(added.id))[0]['path']=='new.txt'
    finally:
        conn.close()


def test_import_preserves_unrelated_concurrent_addition_and_refuses_changed_sources(fresh_cwd):
    import database
    import state
    from import_plan import ImportPlan
    from models import Cell,Channel,Stack,VirtualFile
    from persistence import initialize_files,restore_snapshot
    source=Cell(files=[VirtualFile(name='source.txt',content='old')])
    channel=Channel(name='Synthetic import guard',nav_stacks=[Stack(stack_type='capabilities',cells=[source])])
    state.state.channels=[channel]
    conn=database.get_db_connection(str(channel.id))
    initialize_files(conn,str(source.id),source.files)
    conn.commit()
    plan=ImportPlan(channel)
    added=Cell(files=[VirtualFile(name='added.txt',content='added')])
    plan.candidate.main_stacks.append(Stack(cells=[added]))
    unrelated=Stack(cells=[Cell(name='Concurrent layout addition')])
    channel.main_stacks.append(unrelated)
    plan.publish()
    assert channel.main_stacks[0] is unrelated
    assert channel.main_stacks[1].cells[0].id==added.id
    rejected=ImportPlan(channel)
    rejected.candidate.main_stacks.append(Stack(cells=[Cell(files=[VirtualFile(name='refused',content='no')])]))
    restore_snapshot(conn,str(source.id),[VirtualFile(name='source.txt',content='changed')])
    conn.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refusal:
        rejected.publish()
    assert refusal.value.status_code==409
    assert len(channel.main_stacks)==2
    conn.close()


def test_a_new_channels_plan_places_the_builtin_cards_without_rows(fresh_cwd):
    """A new channel's candidate gets its Capabilities stack before
    admission; the cards publish into the layout but, never imported,
    get no rows until their overlay is first written."""
    import database
    from cell_placement import capabilities_stack
    from genesis import ensure_capabilities_stack
    from import_plan import ImportPlan
    from models import Cell,Channel,VirtualFile
    channel=Channel(name='Synthetic new channel')
    plan=ImportPlan(channel,new_channel=True)
    ensure_capabilities_stack(plan.candidate)
    added=Cell(name='Added',files=[VirtualFile(name='notes.txt',content='notes')])
    capabilities_stack(plan.candidate).cells.append(added)
    plan.publish()
    stack=capabilities_stack(channel)
    cards=[c for c in stack.cells if c.builtin]
    assert cards and stack.cells[-1].id==added.id
    conn=database.get_db_connection(str(channel.id))
    try:
        rows={r[0] for r in conn.execute('SELECT id FROM cells')}
    finally:
        conn.close()
    assert str(added.id) in rows and not rows & {str(c.id) for c in cards}


def test_captured_live_channel_deleted_before_preparation_cannot_be_recreated(fresh_cwd):
    import state
    from fastapi import HTTPException
    from pathlib import Path
    from import_plan import ImportPlan
    from models import Channel
    channel=Channel(name='Captured then deleted')
    state.state.channels=[channel]
    captured=channel
    state.state.channels=[]
    path=Path('.collimate/channels')/(str(channel.id)+'.db')
    with pytest.raises(HTTPException) as refusal:
        ImportPlan(captured)
    assert refusal.value.status_code==409
    assert not path.exists()
    # A replacement object with the same UUID also cannot authorize this request.
    state.state.channels=[channel.model_copy(deep=True)]
    with pytest.raises(HTTPException) as replaced:
        ImportPlan(captured)
    assert replaced.value.status_code==409
    assert not path.exists()


def test_only_explicit_new_channel_admission_accepts_a_detached_creation(fresh_cwd):
    import state
    from fastapi import HTTPException
    from import_plan import ImportPlan
    from models import Cell,Channel,Stack,VirtualFile
    channel=Channel(name='Explicit new channel')
    plan=ImportPlan(channel,new_channel=True)
    plan.candidate.main_stacks.append(Stack(cells=[Cell(files=[VirtualFile(name='new',content='new')])]))
    plan.publish()
    assert channel.main_stacks[0].cells[0].files[0].content=='new'
    state.state.channels=[channel]
    with pytest.raises(HTTPException) as collision:
        ImportPlan(channel,new_channel=True)
    assert collision.value.status_code==409


def test_channel_transaction_uses_explicit_connection_and_rolls_back(fresh_cwd):
    from uuid import uuid4
    from database import channel_transaction, get_db_connection
    channel_id = str(uuid4())
    with pytest.raises(RuntimeError, match='abort'):
        with channel_transaction(channel_id) as transaction:
            transaction.execute("INSERT INTO ui_state (key, value) VALUES ('pending', 'value')")
            independent = get_db_connection(channel_id)
            try:
                assert independent is not transaction
                assert independent.execute("SELECT value FROM ui_state WHERE key='pending'").fetchone() is None
                independent.close()
                assert transaction.execute("SELECT value FROM ui_state WHERE key='pending'").fetchone()[0] == 'value'
            finally:
                independent.close()
            raise RuntimeError('abort')
    with channel_transaction(channel_id) as transaction:
        assert transaction.execute("SELECT value FROM ui_state WHERE key='pending'").fetchone() is None
        transaction.execute("INSERT INTO ui_state (key, value) VALUES ('committed', 'value')")
    connection = get_db_connection(channel_id)
    try:
        assert connection.execute("SELECT value FROM ui_state WHERE key='committed'").fetchone()[0] == 'value'
    finally:
        connection.close()
