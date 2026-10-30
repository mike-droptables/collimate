"""Isolated real HTTP mutation child with deterministic external-parent markers."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))


def prepare(directory: Path, kind: str) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    os.chdir(directory)
    import state
    from models import AppState, Channel, Stack, Cell, VirtualFile
    import cas
    import persistence
    from database import get_db_connection
    cell = Cell(name='Durability probe', files=[VirtualFile(name='source.txt', content='acknowledged base')])
    unrelated = Cell(name='Unrelated acknowledged', files=[VirtualFile(name='unrelated.txt', content='unrelated acknowledged bytes'),
                     VirtualFile(name='empty.txt', content=''),
                     VirtualFile(name='utf8.bin', blob_hash=cas.store_bytes(b'UTF8 binary fidelity')),
                     VirtualFile(name='empty.bin', blob_hash=cas.store_bytes(b''))])
    channel = Channel(name='Crash probe', main_stacks=[Stack(cells=[cell, unrelated])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    conn = get_db_connection(channel.id)
    try:
        for document in (cell, unrelated):
            persistence.initialize_files(conn, str(document.id), document.files)
        conn.commit()
    finally:
        conn.close()
    state.state = AppState(channels=[channel], active_channel_id=channel.id)
    state.persist_state()
    operation_id = str(uuid4())
    body = 'new shard content ' + operation_id
    existing = {path.name for path in Path('.collimate/objects').iterdir()}
    while hashlib.sha256(body.encode()).hexdigest()[:2] in existing:
        body += '!'
    mutation = {'path': 'new.txt', 'base_revision': 0, 'writer_id': 'crash-writer',
                'edit_seq': 7, 'kind': 'put', 'content_text': body}
    request = {'operation_id': operation_id, 'mutations': [mutation], 'resolves': []}
    expected_outcome = 'accepted'
    if kind == 'dedupe':
        mutation['content_text'] = 'acknowledged base'
    elif kind == 'rename':
        request['mutations'].append({'path': 'source.txt', 'base_revision': 1,
                    'writer_id': 'crash-writer', 'edit_seq': 7, 'kind': 'delete'})
        request['mutations'].append({'path': 'empty.bin', 'base_revision': 0,
                    'writer_id': 'crash-writer', 'edit_seq': 7, 'kind': 'put',
                    'blob_hash': cas.store_bytes(b''), 'encoding': 'binary'})
        request['mutations'].append({'path': 'utf8.bin', 'base_revision': 0,
                    'writer_id': 'crash-writer', 'edit_seq': 7, 'kind': 'put',
                    'blob_hash': cas.store_bytes(b'UTF8 binary fidelity'), 'encoding': 'binary'})
    elif kind == 'conflict':
        host = {'operation_id': str(uuid4()), 'mutations': [{'path': 'source.txt', 'base_revision': 1,
                 'writer_id': 'host:winner', 'edit_seq': 1, 'kind': 'put', 'content_text': 'accepted competing winner'}], 'resolves': []}
        persistence.mutate_files(str(channel.id), str(cell.id), host, actor='host')
        mutation.update(path='source.txt', base_revision=1)
        expected_outcome = 'conflict'
    result = {'channel_id': str(channel.id), 'cell_id': str(cell.id), 'unrelated_id': str(unrelated.id),
              'kind': kind, 'request': request, 'expected_outcome': expected_outcome}
    if kind == 'global':
        from cell_metadata import prepare_cell_metadata
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        result['metadata'] = {'name': 'Committed global title', 'digest': digest,
                              'source_type': 'generator', 'actor': {'kind': 'runner', 'runner_id': 'probe', 'runner_name': 'Probe'}}
        prepare_cell_metadata(str(channel.id), str(cell.id), operation_id, digest, result['metadata']['name'],
                              source_type='generator', actor=result['metadata']['actor'])
        persistence.mutate_files(str(channel.id), str(cell.id), request, actor='host')
    Path('durability-case.json').write_text(json.dumps(result))
    return result


def serve(directory: Path, port: int, boundary: str, case_id: str, native_ordinal: int) -> None:
    os.chdir(directory)
    import state
    import cas
    import persistence
    import database
    import uvicorn
    from fastapi import FastAPI
    from models import AppState
    from genesis import reconcile_restored_cell_files
    from routes.file_mutations import router
    conn = database.get_global_db_connection()
    try:
        snapshot = json.loads(conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0])
    finally:
        conn.close()
    state.state = AppState.model_validate(snapshot)
    reconcile_restored_cell_files(state.state)
    state.broadcast_state_update = lambda: None
    import user_validation
    user_validation.notify_user_validation = lambda *args: None
    fixture = json.loads(Path('durability-case.json').read_text())
    context = threading.local()
    ordinal = 0
    marker_fd, release_fd = int(os.environ['P5_NATIVE_MARKER_FD']), int(os.environ['P5_NATIVE_RELEASE_FD'])
    native = ctypes.CDLL(None)
    native.p5_arm.argtypes = [ctypes.c_char_p] * 6 + [ctypes.c_int, ctypes.c_int]
    native.p5_arm.restype = None
    native.p5_disarm.argtypes = []
    native.p5_disarm.restype = None

    def emit(name, path=None, *, native_commit=False):
        nonlocal ordinal
        ordinal += 1
        identity = os.stat(path) if path and os.path.exists(path) else None
        row = {'boundary': name, 'operation_id': getattr(context, 'operation_id', None), 'case_id': case_id,
               'pid': os.getpid(), 'tid': threading.get_native_id(),
               'db_role': 'global' if fixture['kind'] == 'global' else 'channel', 'channel': fixture['channel_id'],
               'path': str(Path(path).absolute()) if path else None, 'fd': None,
               'device': identity.st_dev if identity else None, 'inode': identity.st_ino if identity else None,
               'monotonic_ns': time.monotonic_ns(), 'call_ordinal': ordinal,
               'transaction_phase': 'native_commit_active' if native_commit else name}
        os.write(marker_fd, (json.dumps(row) + '\n').encode())
        if name == boundary:
            os.read(release_fd, 1)

    actual_stage = persistence._stage
    def stage(operation):
        context.operation_id = operation.operation_id
        return actual_stage(operation)
    persistence._stage = stage

    actual_write = cas._write_bytes
    def write(path, body):
        actual_write(path, body)
        if getattr(context, 'operation_id', None):
            emit('cas_temp_written', path)
    cas._write_bytes = write

    actual_file_sync = cas.sync_cas_file
    def file_sync(path):
        actual_file_sync(path)
        if getattr(context, 'operation_id', None):
            emit('cas_file_sync_returned', path)
    cas.sync_cas_file = file_sync

    actual_replace = cas.os.replace
    class CasOS:
        def __getattr__(self, name):
            return getattr(os, name)
        def replace(self, source, target):
            result = actual_replace(source, target)
            if getattr(context, 'operation_id', None):
                emit('cas_rename_returned', target)
            return result
    cas.os = CasOS()

    actual_directory_sync = cas.sync_cas_directory
    def directory_sync(path):
        actual_directory_sync(path)
        if getattr(context, 'operation_id', None) and Path(path) == Path.cwd():
            emit('cas_directory_sync_returned', path)
    cas.sync_cas_directory = directory_sync

    actual_confirm = database.confirm_sql_durability
    def confirm(conn):
        # SQLite has returned: Python checked directory calls are NOT VFS sync.
        native.p5_disarm()
        try:
            return actual_confirm(conn)
        except database.DurabilityConfirmationError:
            if getattr(context, 'operation_id', None):
                emit('sql_directory_sync_failed', conn.execute('PRAGMA database_list').fetchone()[2])
            raise
    database.confirm_sql_durability = confirm

    actual_commit = database.DurableConnection.commit
    def commit(conn):
        if not getattr(context, 'operation_id', None):
            return actual_commit(conn)
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        selected = Path(path).name == 'app.db' if fixture['kind'] == 'global' else Path(path).parent.name == 'channels'
        if not selected:
            return actual_commit(conn)
        emit('sql_transaction_staged', path)
        native.p5_arm(context.operation_id.encode(), case_id.encode(), path.encode(),
                      fixture['channel_id'].encode(),
                      b'global' if fixture['kind'] == 'global' else b'channel', boundary.encode(), native_ordinal, 0)
        try:
            actual_commit(conn)
        finally:
            native.p5_disarm()
        emit('sql_commit_returned', path)
    database.DurableConnection.commit = commit
    app = FastAPI()
    app.include_router(router)
    @app.post('/metadata')
    def metadata():
        from cell_metadata import finish_cell_metadata
        context.operation_id = fixture['request']['operation_id']
        meta = fixture['metadata']
        return finish_cell_metadata(fixture['channel_id'], fixture['cell_id'], context.operation_id,
                                    meta['digest'], meta['name'], source_type=meta['source_type'], actor=meta['actor'])
    @app.get('/ready')
    def ready():
        return {'ok': True}
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'serve', 'gc'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--kind', default='new', choices=('new', 'dedupe', 'rename', 'conflict', 'global'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--boundary', default='')
    parser.add_argument('--case-id', default='')
    parser.add_argument('--native-ordinal', type=int, default=1)
    arguments = parser.parse_args()
    if arguments.mode == 'prepare':
        print(json.dumps(prepare(arguments.directory, arguments.kind)))
    elif arguments.mode == 'gc':
        os.chdir(arguments.directory)
        from cas_gc import run_gc
        print(json.dumps(run_gc(grace_seconds=0)))
    else:
        serve(arguments.directory, arguments.port, arguments.boundary, arguments.case_id, arguments.native_ordinal)
