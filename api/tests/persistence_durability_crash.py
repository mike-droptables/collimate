"""External-parent boundary kill, real HTTP ACK and exact restart/replay proof."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import selectors
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = Path(__file__).resolve().parents[1]
CHILD = API / 'tests/persistence_durability_server.py'
BOUNDARIES = ('cas_temp_written', 'cas_file_sync_returned', 'cas_rename_returned',
              'cas_directory_sync_returned', 'sql_transaction_staged',
              'sqlite_native_sync_enter', 'sqlite_native_sync_return',
              'sql_commit_returned', 'client_ack_observed')
PRECOMMIT = set(BOUNDARIES[:5])


def compile_native(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    library = destination / 'persistence_durability_sync.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                    str(API / 'tests/persistence_durability_sync.c'), '-ldl', '-o', str(library)], check=True)
    return library


def request(url: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, json.loads(response.read())


def inspect(directory: Path, fixture: dict) -> dict:
    path = directory / '.collimate/channels' / (fixture['channel_id'] + '.db')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {name: [dict(row) for row in conn.execute('SELECT * FROM ' + name)]
                  for name in ('working_files', 'file_revisions', 'file_operations',
                               'file_path_sequences', 'file_writer_sequences', 'file_conflicts')}
    finally:
        conn.close()
    bodies = {}
    for row in tables['file_revisions']:
        digest = row['body_hash']
        if digest:
            body = (directory / '.collimate/objects' / digest[:2] / digest[2:]).read_bytes()
            assert hashlib.sha256(body).hexdigest() == digest, 'Dangling or corrupt accepted/candidate reference'
            bodies[digest] = {'size': len(body), 'hex': body.hex()}
    tables['bodies'] = bodies
    conn = sqlite3.connect((directory / '.collimate/app.db').as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables['global'] = {name: [dict(row) for row in conn.execute('SELECT * FROM ' + name)]
                            for name in ('ui_state', 'cell_metadata_heads', 'cell_metadata_operations', 'cell_rename_events')}
    finally:
        conn.close()
    return tables


class Server:
    def __init__(self, directory, fixture, library, boundary, case_id, ordinal=1):
        self.directory, self.fixture = directory, fixture
        self.trace_read, trace_write = os.pipe()
        release_read, self.release_write = os.pipe()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.trace_read, selectors.EVENT_READ)
        self.buffer = b''
        self.markers = []
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        self.url = f'http://127.0.0.1:{port}'
        env = dict(os.environ, LD_PRELOAD=str(library), P5_NATIVE_MARKER_FD=str(trace_write),
                   P5_NATIVE_RELEASE_FD=str(release_read), PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring')
        self.log = (directory / f'{case_id}-server.log').open('wb')
        self.process = subprocess.Popen([sys.executable, str(CHILD), 'serve', str(directory),
             '--port', str(port), '--boundary', boundary, '--case-id', case_id,
             '--native-ordinal', str(ordinal)], env=env, pass_fds=(trace_write, release_read),
             stdout=self.log, stderr=subprocess.STDOUT)
        os.close(trace_write)
        os.close(release_read)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError((directory / f'{case_id}-server.log').read_text())
            try:
                if request(self.url + '/ready')[0] == 200:
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(.025)
        self.close()
        raise TimeoutError('Owned crash child did not become ready')

    def next_marker(self, deadline):
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                marker = json.loads(line)
                self.markers.append(marker)
                return marker
            if not self.selector.select(max(0, deadline-time.monotonic())):
                break
            chunk = os.read(self.trace_read, 8192)
            if not chunk:
                break
            self.buffer += chunk
        raise TimeoutError('Selected deterministic boundary was not observed')

    def mutate(self):
        f = self.fixture
        if f['kind'] == 'global':
            return request(self.url + '/metadata', {})
        return request(self.url + f"/api/channels/{f['channel_id']}/cells/{f['cell_id']}/files/mutate", f['request'])

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        self.selector.close()
        os.close(self.trace_read)
        os.close(self.release_write)
        self.log.close()


def run_case(directory: Path, kind: str, boundary: str, library: Path, *, ordinal=1, gc=False) -> dict:
    case_id = kind + '-' + boundary + '-' + str(ordinal)
    prepared = subprocess.run([sys.executable, str(CHILD), 'prepare', str(directory), '--kind', kind],
                              check=True, capture_output=True, text=True, timeout=30)
    fixture = json.loads(prepared.stdout)
    before = inspect(directory, fixture)
    server = Server(directory, fixture, library, boundary if boundary != 'client_ack_observed' else '', case_id, ordinal)
    parent_ack = None
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(server.mutate)
        try:
            if boundary == 'client_ack_observed':
                status, body = future.result(timeout=20)
                assert status == 200
                parent_ack = {'boundary': boundary, 'operation_id': fixture['request']['operation_id'],
                    'case_id': case_id, 'pid': os.getpid(), 'tid': __import__('threading').get_native_id(),
                    'db_role': 'global' if kind == 'global' else 'channel',
                    'channel': fixture['channel_id'], 'path': None, 'fd': None,
                    'monotonic_ns': time.monotonic_ns(), 'call_ordinal': 1, 'status': status, 'receipt': body,
                    'observer': 'external_http_client'}
                while True:
                    try:
                        server.next_marker(time.monotonic() + .01)
                    except TimeoutError:
                        break
            else:
                deadline = time.monotonic() + 20
                while True:
                    marker = server.next_marker(deadline)
                    if marker['boundary'] == boundary and (not boundary.startswith('sqlite_native_')
                        or (marker['path_kind'] == 'wal' and marker['wal_ordinal'] == ordinal)):
                        break
            # Persist parent evidence BEFORE terminating the owned child.
            evidence = {'fixture': fixture, 'markers': server.markers, 'parent_ack': parent_ack,
                        'selected_boundary': boundary, 'case_id': case_id}
            (directory / 'parent-boundary.json').write_text(json.dumps(evidence, indent=2))
        finally:
            server.close()
        try:
            response = future.result(timeout=10)
        except (OSError, urllib.error.URLError):
            response = None
        if boundary != 'client_ack_observed':
            assert response is None, 'Response escaped the blocked selected boundary'
    after_kill = inspect(directory, fixture)
    operation_id = fixture['request']['operation_id']
    old_receipts = {row['operation_id'] for row in before['file_operations']}
    if kind == 'global':
        receipt_row = next((r for r in after_kill['global']['cell_metadata_operations']
                            if r['operation_id'] == operation_id and r['status'] != 'pending'), None)
    else:
        receipt_row = next((r for r in after_kill['file_operations'] if r['operation_id'] == operation_id), None)
    if boundary in PRECOMMIT:
        assert receipt_row is None
        for table in ('working_files', 'file_revisions', 'file_operations', 'file_path_sequences', 'file_writer_sequences', 'file_conflicts'):
            assert after_kill[table] == before[table], (boundary, table)
        if kind == 'global':
            assert after_kill['global'] == before['global']
    elif boundary in ('sql_commit_returned', 'client_ack_observed'):
        assert receipt_row is not None
    # Native-enter/return are uncertain, not a supposed rollback guarantee.
    restart = Server(directory, fixture, library, '', case_id + '-restart')
    try:
        status, receipt = restart.mutate()
        assert status == 200
        if kind == 'global':
            assert receipt['status'] == 'applied'
        else:
            assert receipt['outcome'] == fixture['expected_outcome']
        if receipt_row:
            assert receipt == json.loads(receipt_row['result_json'])
        if parent_ack:
            assert receipt == parent_ack['receipt']
        assert restart.mutate() == (200, receipt)
    finally:
        restart.close()
    final = inspect(directory, fixture)
    assert {r['operation_id'] for r in final['file_operations']} == old_receipts | {operation_id}
    for table in ('working_files', 'file_revisions', 'file_path_sequences', 'file_writer_sequences', 'file_conflicts'):
        old = [r for r in before[table] if r['cell_id'] == fixture['unrelated_id']]
        current = [r for r in final[table] if r['cell_id'] == fixture['unrelated_id']]
        assert current == old, ('unrelated acknowledged bytes', table)
    if receipt_row:
        # File replay republishes derived canvas timestamps independently of
        # accepted storage. Every channel row and immutable body must be exact.
        replay_scope = final if kind == 'global' else {k: v for k, v in final.items() if k != 'global'}
        killed_scope = after_kill if kind == 'global' else {k: v for k, v in after_kill.items() if k != 'global'}
        assert replay_scope == killed_scope, 'Exact replay created another revision/highwater/receipt'
    if kind == 'global':
        assert len(final['global']['cell_rename_events']) == 1
        layout = json.loads(next(row['value'] for row in final['global']['ui_state'] if row['key'] == 'app_state'))
        assert layout['channels'][0]['main_stacks'][0]['cells'][0]['name'] == fixture['metadata']['name']
    result = {'case_id': case_id, 'kind': kind, 'selected_boundary': boundary,
              'native_wal_ordinal': ordinal if boundary.startswith('sqlite_native_') else None,
              'parent_ack_observed': parent_ack is not None, 'committed_at_restart': receipt_row is not None,
              'receipt': receipt, 'before': before, 'after_kill': after_kill, 'after_replay': final,
              'parent_boundary_evidence': str(directory / 'parent-boundary.json')}
    if gc:
        collected = subprocess.run([sys.executable, str(CHILD), 'gc', str(directory)],
                                   check=True, capture_output=True, text=True, timeout=30)
        after_gc = inspect(directory, fixture)
        for table in ('working_files', 'file_operations', 'file_path_sequences', 'file_writer_sequences', 'file_conflicts'):
            assert after_gc[table] == final[table], ('GC changed a durable root or receipt', table)
        # Current refs and every unresolved comparison are roots, including
        # candidate, captured base and competing current bodies.
        retained = {(r['cell_id'], r['path'], r['revision']) for r in after_gc['file_revisions']}
        for row in after_gc['working_files']:
            assert (row['cell_id'], row['path'], row['revision']) in retained
        for conflict in after_gc['file_conflicts']:
            for column in ('candidate_revision', 'base_revision', 'current_revision'):
                if conflict[column]:
                    assert (conflict['cell_id'], conflict['path'], conflict[column]) in retained
        third = Server(directory, fixture, library, '', case_id + '-after-gc')
        try:
            assert third.mutate() == (200, receipt)
        finally:
            third.close()
        after_gc_replay = inspect(directory, fixture)
        assert {k: v for k, v in after_gc_replay.items() if k != 'global'} == {
            k: v for k, v in after_gc.items() if k != 'global'}
        result.update(gc_result=json.loads(collected.stdout), after_gc=after_gc, after_gc_replay=after_gc_replay)
    (directory / 'result.json').write_text(json.dumps(result, indent=2))
    return result
