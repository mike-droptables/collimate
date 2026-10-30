"""Persistence torture contracts: real SQLite/CAS, synthetic documents only."""
from __future__ import annotations

import importlib
import json
import threading
import uuid

import pytest


@pytest.fixture
def document(fresh_cwd, monkeypatch):
    from conftest import reset_api_modules
    reset_api_modules()
    for name in ('persistence', 'routes.file_mutations'):
        import sys
        sys.modules.pop(name, None)
    persistence = importlib.import_module('persistence')
    from models import Cell, Channel, Stack, VirtualFile
    from state import state
    from database import get_db_connection
    import user_validation
    monkeypatch.setattr(user_validation, 'notify_user_validation', lambda *a, **kw: None)
    cell = Cell(files=[VirtualFile(name='a.md', content='base-a'), VirtualFile(name='b.md', content='base-b')])
    channel = Channel(name='Synthetic', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    state.channels = [channel]
    state.active_channel_id = channel.id
    conn = get_db_connection(str(channel.id))
    persistence.initialize_files(conn, str(cell.id), cell.files)
    conn.commit()
    conn.close()
    return persistence, str(channel.id), str(cell.id)


def operation(path='a.md', text='new', *, base=1, writer='writer-a', seq=1, op=None, kind='put'):
    mutation = dict(path=path, writer_id=writer, edit_seq=seq, base_revision=base, kind=kind)
    if kind == 'put':
        mutation.update(content_text=text, language='markdown')
    return dict(operation_id=op or str(uuid.uuid4()), mutations=[mutation], resolves=[])


def read(document, path='a.md', revision=None):
    persistence, channel, cell = document
    return persistence.read_file(channel, cell, path, revision=revision)


def mutate(document, payload):
    persistence, channel, cell = document
    return persistence.mutate_files(channel, cell, payload)


@pytest.mark.parametrize('reason', ['missing', 'corrupt'])
def test_referenced_body_unavailable_is_structured_refusal_without_mutation(document, reason):
    from pathlib import Path
    from fastapi import HTTPException
    from cas import store_bytes, blob_path
    from channel_db import open_channel_db
    digest = store_bytes(b'admitted reference')
    if reason == 'missing':
        Path(blob_path(digest)).unlink()
    else:
        Path(blob_path(digest)).write_bytes(b'corrupt content')
    request = operation()
    mutation = request['mutations'][0]
    del mutation['content_text']
    mutation.update(blob_hash=digest, encoding='binary')
    with pytest.raises(HTTPException) as error:
        mutate(document, request)
    assert error.value.status_code == 409
    assert error.value.detail == dict(code='body_unavailable',path='a.md',body_hash=digest,reason=reason)
    assert read(document)[1] == b'base-a'
    conn = open_channel_db(document[1])
    try:
        assert conn.execute('SELECT 1 FROM file_operations WHERE operation_id=?',
                            (request['operation_id'],)).fetchone() is None
    finally:
        conn.close()


def test_reference_storage_io_failure_is_not_mislabeled_missing(document, monkeypatch):
    import cas
    digest = cas.store_bytes(b'admitted reference')
    def fail(_digest):
        raise PermissionError('storage cannot be accessed')
    monkeypatch.setattr(cas, 'retain_blob', fail)
    request = operation()
    del request['mutations'][0]['content_text']
    request['mutations'][0].update(blob_hash=digest, encoding='binary')
    with pytest.raises(PermissionError, match='storage cannot be accessed'):
        mutate(document, request)
    assert read(document)[1] == b'base-a'


@pytest.mark.parametrize('entry', [
    {'name':'empty.md','content':''}, {'filepath':'empty.md','content_text':''},
    {'path':'empty.md'}, {'path':'empty.md','body_hash':None},
    {'path':'empty.md','blob_hash':'a'*64},
])
def test_initialization_rejects_retired_aliases_and_missing_bodies(document, entry):
    persistence, channel, _ = document
    from channel_db import open_channel_db
    conn = open_channel_db(channel)
    try:
        with pytest.raises(ValueError):
            persistence.initialize_files(conn, 'unadmitted', [entry])
        assert persistence.snapshot_files(conn, 'unadmitted') == []
    finally:
        conn.close()


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a/../b', 'a\\b', '.collrun/private'])
def test_initialization_validates_canonical_paths_before_admission(document, path):
    persistence, channel, _ = document
    from models import VirtualFile
    from channel_db import open_channel_db
    conn = open_channel_db(channel)
    try:
        with pytest.raises(ValueError, match='snapshot path'):
            persistence.initialize_files(conn, 'unadmitted', [VirtualFile(name=path, content='x')])
        assert persistence.snapshot_files(conn, 'unadmitted') == []
    finally:
        conn.close()


def test_explicit_empty_virtual_file_has_real_admitted_bytes(document):
    persistence, channel, _ = document
    from models import VirtualFile
    from channel_db import open_channel_db
    from cas import require_blob
    conn = open_channel_db(channel)
    try:
        persistence.initialize_files(conn, 'empty', [VirtualFile(name='empty.md', content='')])
        descriptor = persistence.snapshot_files(conn, 'empty')[0]
        assert descriptor['encoding'] == 'utf-8'
        assert require_blob(descriptor['body_hash']) == b''
    finally:
        conn.close()


def test_independent_sibling_writes_preserve_both(document):
    assert mutate(document, operation())['outcome'] == 'accepted'
    assert mutate(document, operation('b.md', 'other', writer='writer-b'))['outcome'] == 'accepted'
    assert read(document)[1] == b'new'
    assert read(document, 'b.md')[1] == b'other'


def test_competing_writers_retain_candidate_and_current(document):
    first = mutate(document, operation(text='first'))
    second = mutate(document, operation(text='second', writer='writer-b'))
    assert first['outcome'] == 'accepted'
    assert second['outcome'] == 'conflict'
    assert read(document)[1] == b'first'
    conflict = second['conflicts'][0]
    assert read(document, revision=conflict['candidate_revision'])[1] == b'second'
    assert conflict['current_revision'] == first['files'][0]['file_revision']


@pytest.mark.parametrize('kind', ['put', 'delete'])
def test_conflict_receipt_replay_and_http_list_preserve_exact_candidate_provenance(document, kind):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import blobs, file_mutations
    from channel_db import open_channel_db

    persistence, channel, cell = document
    mutate(document, operation(text='accepted winner', writer='winning-view'))
    request = operation(text='candidate', writer='candidate-view-a', seq=37, kind=kind)
    request['mutations'] += operation('b.md', 'sibling candidate', writer='candidate-view-b', seq=83)['mutations']
    app = FastAPI()
    app.include_router(file_mutations.router)
    app.include_router(blobs.router)
    with TestClient(app) as client:
        url = f'/api/channels/{channel}/cells/{cell}/files'
        response = client.post(f'{url}/mutate', json=request)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt['outcome'] == 'conflict'
        connection = open_channel_db(channel)
        try:
            for candidate in receipt['conflicts']:
                revision = connection.execute(
                    'SELECT writer_id,edit_seq,body_hash FROM file_revisions WHERE cell_id=? AND path=? AND revision=?',
                    (cell, candidate['path'], candidate['candidate_revision']),
                ).fetchone()
                assert (candidate['writer_id'], candidate['edit_seq'], candidate['candidate_hash']) == tuple(revision)
                expected = ('candidate-view-a', 37) if candidate['path'] == 'a.md' else ('candidate-view-b', 83)
                assert (candidate['writer_id'], candidate['edit_seq']) == expected
        finally:
            connection.close()
        assert client.post(f'{url}/mutate', json=request).json() == receipt
        persistence = importlib.reload(persistence)
        assert persistence.mutate_files(channel, cell, request) == receipt
        listed = client.get(f'{url}/conflicts')
        assert listed.status_code == 200, listed.text
        candidates = {entry['conflict_id']: entry for entry in listed.json()['conflicts']}
        assert len(candidates) == 2
        for candidate in receipt['conflicts']:
            assert all(candidates[candidate['conflict_id']][key] == value for key, value in candidate.items())
        assert read(document)[1] == b'accepted winner'
        assert read(document, 'b.md')[1] == b'base-b'


def test_receipt_replay_and_mismatched_reuse(document):
    from fastapi import HTTPException
    request = operation()
    result = mutate(document, request)
    assert mutate(document, json.loads(json.dumps(request))) == result
    request['mutations'][0]['content_text'] = 'different'
    with pytest.raises(HTTPException) as error:
        mutate(document, request)
    assert error.value.status_code == 409


def test_receipt_replay_after_module_restart(document):
    persistence, channel, cell = document
    request = operation()
    result = mutate(document, request)
    persistence = importlib.reload(persistence)
    assert persistence.mutate_files(channel, cell, request) == result


def test_older_same_writer_sequence_is_superseded(document):
    mutate(document, operation(text='later', seq=2))
    result = mutate(document, operation(text='earlier', seq=1))
    assert result['outcome'] == 'superseded'
    assert read(document)[1] == b'later'


def test_conflicted_sequence_cannot_later_overwrite_current(document):
    mutate(document, operation(writer='other', text='current'))
    mutate(document, operation(seq=3, text='candidate'))
    result = mutate(document, operation(base=2, seq=2, text='older'))
    assert result['outcome'] == 'superseded'
    assert read(document)[1] == b'current'


def test_group_conflict_accepts_no_sibling(document):
    mutate(document, operation(text='current'))
    request = operation(text='conflict', writer='other')
    request['mutations'] += operation('b.md', 'attempt', writer='other')['mutations']
    result = mutate(document, request)
    assert result['outcome'] == 'conflict'
    assert read(document, 'b.md')[1] == b'base-b'
    assert len(result['conflicts']) == 2


def test_delete_recreate_retains_monotonic_tombstone(document):
    deletion = mutate(document, operation(kind='delete'))
    assert deletion['files'][0]['deleted']
    recreation = mutate(document, operation(base=2, seq=2, text='recreated'))
    assert recreation['outcome'] == 'accepted'
    assert recreation['files'][0]['file_revision'] > 2
    assert mutate(document, operation(base=1, writer='delayed', text='stale'))['outcome'] == 'conflict'
    assert read(document)[1] == b'recreated'


def test_explicit_conflict_resolution_is_conditional(document):
    mutate(document, operation(text='current'))
    conflict = mutate(document, operation(text='candidate', writer='other'))['conflicts'][0]
    request = operation(text='merged', base=2, seq=2)
    request['resolves'] = [conflict['conflict_id']]
    assert mutate(document, request)['outcome'] == 'accepted'
    assert read(document)[1] == b'merged'
    persistence, channel, cell = document
    assert persistence.list_conflicts(channel, cell) == []


def test_failed_storage_never_publishes_or_succeeds(document, monkeypatch):
    import cas
    monkeypatch.setattr(cas, 'store_bytes', lambda *a, **kw: (_ for _ in ()).throw(OSError('full disk')))
    with pytest.raises(OSError, match='full disk'):
        mutate(document, operation())
    assert read(document)[1] == b'base-a'


def test_sql_failure_leaves_memory_and_current_intact(document, monkeypatch):
    persistence, channel, cell = document
    monkeypatch.setattr(persistence, '_commit', lambda conn: (_ for _ in ()).throw(OSError('commit failed')))
    with pytest.raises(OSError, match='commit failed'):
        mutate(document, operation())
    assert read(document)[1] == b'base-a'
    from cell_placement import find_cell
    from state import require_channel
    assert find_cell(require_channel(channel), cell)[1].files[0].content == 'base-a'


def test_slow_staging_leaves_global_state_lock_available(document, monkeypatch):
    import cas
    from state import state_lock
    original = cas.store_bytes
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def delay(raw, *args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(raw, *args, **kwargs)
    monkeypatch.setattr(cas, 'store_bytes', delay)
    def write():
        try:
            mutate(document, operation())
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=write)
    worker.start()
    assert entered.wait(3)
    def acquire():
        with state_lock:
            acquired.set()
    reader = threading.Thread(target=acquire)
    reader.start()
    try:
        assert acquired.wait(.2)
    finally:
        release.set()
        worker.join(3)
        reader.join(3)
    assert not errors


def test_empty_unicode_and_binary_use_one_metadata_model(document):
    import cas
    for seq, text in enumerate(('', 'é' * 51000), 1):
        result = mutate(document, operation(text=text, base=seq, seq=seq))
        metadata, body = read(document)
        assert body == text.encode('utf-8')
        assert metadata['body_hash'] == result['files'][0]['body_hash']
        assert 'content' not in metadata and 'is_loaded' not in metadata
    payload = operation(base=3, seq=3)
    payload['mutations'][0].pop('content_text')
    payload['mutations'][0]['blob_hash'] = cas.store_bytes(b'\xff\x00')
    assert mutate(document, payload)['outcome'] == 'accepted'
    assert read(document)[0]['encoding'] == 'binary'
    assert read(document)[1] == b'\xff\x00'


def test_replay_uses_durable_receipt_without_storage(document, monkeypatch):
    result = mutate(document, request := operation())
    import cas
    monkeypatch.setattr(cas, 'store_bytes', lambda _body: (_ for _ in ()).throw(OSError('offline storage')))
    assert mutate(document, request) == result


def test_same_channel_move_is_atomic_and_retains_stale_candidates(document):
    persistence, channel, source = document
    from models import Cell
    from state import require_channel
    from database import get_db_connection
    target = Cell()
    require_channel(channel).main_stacks[0].cells.append(target)
    request = dict(operation_id=str(uuid.uuid4()),source_cell_id=source,target_cell_id=str(target.id),
                   writer_id='move',edit_seq=1,files=[dict(source_path='a.md',target_path='copy.md',source_revision=1,target_revision=0)])
    mutate(document,operation(text='new current'))
    refused = persistence.move_files(channel,request)
    assert refused['outcome'] == 'conflict'
    assert read(document)[1] == b'new current'
    with get_db_connection(channel) as conn:
        assert persistence.snapshot_files(conn,str(target.id)) == []
    candidate = next(c for c in refused['conflicts'] if c['cell_id']==str(target.id))
    assert persistence.read_file(channel,str(target.id),'copy.md',revision=candidate['candidate_revision'])[1] == b'base-a'
    assert persistence.move_files(channel,request) == refused
    request = dict(request,operation_id=str(uuid.uuid4()),edit_seq=2,
                   files=[dict(source_path='a.md',target_path='copy.md',source_revision=2,target_revision=0)])
    accepted = persistence.move_files(channel,request)
    assert accepted['outcome'] == 'accepted'
    assert persistence.read_file(channel,str(target.id),'copy.md')[1] == b'new current'
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        read(document)


def test_http_inventory_mutations_and_pinned_body_share_revision(document):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from routes.file_mutations import router as mutation_router
    from routes.blobs import router as body_router
    from routes.runs.inspection import router as inventory_router
    persistence, channel, cell = document
    app = FastAPI()
    for router in (mutation_router,body_router,inventory_router):
        app.include_router(router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
            base = f'/api/channels/{channel}/cells/{cell}/files'
            inventory = (await client.get(base)).json()['files']
            assert all('content' not in f and 'is_loaded' not in f for f in inventory)
            accepted = (await client.post(base+'/mutate',json=operation())).json()
            response = await client.get(base+'/a.md',params={'revision':1})
            assert response.status_code == 200 and response.content == b'base-a'
            assert response.headers['X-File-Revision'] == '1'
            current = await client.get(base+'/a.md')
            assert current.content == b'new'
            assert current.headers['X-Body-Hash'] == accepted['files'][0]['body_hash']
            deletion = (await client.post(base+'/mutate',json=operation(
                kind='delete',base=accepted['files'][0]['file_revision'],seq=2))).json()
            tombstone = deletion['files'][0]
            inventory = (await client.get(base)).json()['files']
            assert next(file for file in inventory if file['path']=='a.md') == tombstone
            assert tombstone['deleted'] and tombstone['body_hash'] is None
            stale = (await client.post(base+'/mutate',json=operation(
                text='late creation',base=0,writer='unaware-writer'))).json()
            assert stale['outcome'] == 'conflict'
            assert (await client.get(base+'/a.md')).status_code == 404
            recreation = (await client.post(base+'/mutate',json=operation(
                text='explicit recreation',base=tombstone['file_revision'],seq=3))).json()
            assert recreation['outcome'] == 'accepted'
            assert recreation['files'][0]['file_revision'] > tombstone['file_revision']
            assert (await client.get(base+'/a.md')).content == b'explicit recreation'
            assert (await client.put(f'/api/channels/{channel}/cells/update_top',json={})).status_code == 404
    asyncio.run(exercise())


def test_ipc_exact_retry_does_not_append_history_and_conflict_preserves_user(document, fresh_cwd):
    import asyncio
    from database import get_db_connection
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    persistence,channel,cell = document
    rid=str(uuid.uuid4())
    with get_db_connection(channel) as conn:
        conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')",(rid,))
    manager=ProcessManager(channel=channel,run_id=rid,cell_id=cell,app_dir=str(fresh_cwd),workspace_dir=str(fresh_cwd),
                           entrypoint='unused.py',generator_name='synthetic',runner_id='synthetic',runner_name='Synthetic',permissions=UNRESTRICTED)
    replies=[]
    async def ack(reply,_request):
        replies.append(reply)
    manager._send_ack=ack
    request=dict(operation_id=str(uuid.uuid4()),writer_id='run:'+rid,edit_seq=1,base_revisions={'a.md':1},
                 artifacts=[dict(type='memory',cell_path='a.md',content='run writes',encoding='utf-8')])
    asyncio.run(manager._op_update_cell(request))
    assert replies[-1]['ok'],replies[-1]
    assert replies[-1]['outcome']=='accepted'
    first=replies[-1]
    with get_db_connection(channel) as conn:
        count=conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0]
    manager.sealed = True
    asyncio.run(manager._op_update_cell(request))
    assert replies[-1]==first
    manager.sealed = False
    with get_db_connection(channel) as conn:
        assert conn.execute('SELECT COUNT(*) FROM cells').fetchone()[0]==count
    mutate(document,operation(text='user writes',base=2,writer='user',seq=1))
    request=dict(request,operation_id=str(uuid.uuid4()),edit_seq=2,base_revisions={'a.md':2},
                 artifacts=[dict(type='memory',cell_path='a.md',content='stale run',encoding='utf-8')])
    asyncio.run(manager._op_update_cell(request))
    assert replies[-1]['ok'] and replies[-1]['outcome']=='conflict',replies[-1]
    assert read(document)[1]==b'user writes'
    assert read(document,revision=replies[-1]['conflicts'][0]['candidate_revision'])[1]==b'stale run'


def test_product_cell_ingress_initializes_sql_before_layout(document):
    from cas import store_bytes
    from transfer_import import ImportRequest, commit_import
    from state import require_channel
    persistence,channel,_cell=document
    digest=store_bytes(b'exact bytes')
    created=commit_import(channel,ImportRequest.model_validate({'stacks':[{'name':'','cells':[
        {'name':'Ingress','files':[{'path':'ingress.txt','blob_hash':digest}]}]}]}))
    cell_id=created['cell_ids'][0]
    assert persistence.read_file(channel,cell_id,'ingress.txt')[1]==b'exact bytes'
    assert any(str(c.id)==cell_id for s in require_channel(channel).main_stacks for c in s.cells)


def test_post_commit_cache_failure_preserves_receipt_and_replay_repairs(document,monkeypatch):
    persistence,channel,cell=document
    real=persistence.publish_files
    monkeypatch.setattr(persistence,'publish_files',lambda *_a,**_k: (_ for _ in ()).throw(OSError('cache unavailable')))
    request=operation(text='durable')
    accepted=mutate(document,request)
    assert accepted['outcome']=='accepted'
    assert read(document)[1]==b'durable'
    monkeypatch.setattr(persistence,'publish_files',real)
    assert mutate(document,request)==accepted
    from cell_placement import find_cell
    from state import require_channel
    assert next(f for f in find_cell(require_channel(channel),cell)[1].files if f.name=='a.md').content=='durable'


def test_file_changes_uses_logical_cell_identity_not_history_id(document,monkeypatch):
    persistence,channel,cell=document
    import events
    sent=[]
    monkeypatch.setattr(events,'broadcast',lambda identity,event: sent.append((identity,event)))
    persistence.publish_files(channel,cell,dict(operation_id='synthetic-event',outcome='accepted',files=[],conflicts=[],cell_id='history-version'))
    event=next(event for _identity,event in sent if event['type']=='file_changes')
    assert event['cell_id']==cell
    assert event['operation_id']=='synthetic-event'


def test_user_cannot_put_or_delete_run_ui(document,monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    import run_registry
    persistence,channel,cell=document
    path='_runs/probe-12345678/view.html'
    persistence.mutate_files(channel,cell,operation(path,'host UI',base=0),actor='host')
    for owner in (None,SimpleNamespace(label='probe-12345678')):
        monkeypatch.setattr(run_registry,'lock_owner',lambda *_args: owner)
        with pytest.raises(HTTPException,match='read-only'):
            mutate(document,operation(path,'tamper',base=1))
        with pytest.raises(HTTPException,match='read-only'):
            mutate(document,operation(path,base=1,kind='delete'))
        with pytest.raises(HTTPException,match='read-only'):
            mutate(document,operation('_runs/probe-12345678/view.notes','new',base=0))
    assert read(document,path)[1]==b'host UI'
