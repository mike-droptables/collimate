"""D23 restricted child execution borrows only the parent's explicit op table."""
import json
import socket
import threading

import pytest


class Parent:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def emit_and_wait(self, payload, wait_op, **kwargs):
        with self.lock:
            assert 'req_id' not in payload
            self.calls.append(payload)
            return {'op': wait_op, 'req_id': 'parent-generated-id', 'ok': True, 'files': {}}

    def emit(self, payload):
        self.calls.append(payload)


def test_forward_table_is_closed_and_child_ids_are_remapped_D23():
    from collimate.testing.forward import Forwarder, FORWARDED_OPS, LOCAL_OPS
    parent = Parent()
    relay = Forwarder(parent)
    assert set(FORWARDED_OPS) == {
        'update_cell', 'read_cell_files', 'reveal_file', 'client_caps',
        'list_handlers', 'handler_skills', 'validate_file',
    }
    assert LOCAL_OPS == {'finish', 'phase', 'progress', 'port_bound'}
    assert FORWARDED_OPS['update_cell'] == 'ack_update'
    for op in FORWARDED_OPS:
        reply = relay.dispatch({'op': op, 'req_id': 'child-id'})
        if reply is not None:
            assert reply['req_id'] == 'child-id'
    for op in ('surface_register', 'chat_poll', 'create_terminal_session', 'env_exec',
               'read_account', 'accounts', 'unknown', 'status'):
        reply = relay.dispatch({'op': op, 'req_id': 'bad'})
        assert reply['op'] == 'nak' and reply['reason'] == 'forward_refused'
    assert len(parent.calls) == len(FORWARDED_OPS)
    relay.dispatch({'op': 'phase', 'phase': 'working'})
    relay.dispatch({'op': 'finish', 'success': True})
    assert relay.result['success'] is True and relay.result['phase'] == 'working'
    assert len(parent.calls) == len(FORWARDED_OPS)


def test_a_forwarded_child_has_no_annotation_ops():
    """The annotation ops are not relayed, so the child's transport does not
    carry them and its ``gen.annotations.available`` is False."""
    from collimate._host import ipc
    from collimate.testing.forward import FORWARDED_OPS, LOCAL_OPS
    child_ops = set(FORWARDED_OPS) | LOCAL_OPS
    assert set(ipc.ANNOTATION_OPS).isdisjoint(child_ops)


def test_forward_socket_requires_token_before_any_operation_D23():
    from collimate.testing.forward import Forwarder
    parent = Parent()
    with Forwarder(parent) as relay:
        with socket.create_connection(('127.0.0.1', relay.port), timeout=2) as client:
            client.sendall(b'{"op":"read_cell_files","req_id":"unauthenticated"}\n')
            assert client.recv(1024) == b''
        assert not parent.calls
        with socket.create_connection(('127.0.0.1', relay.port), timeout=2) as client:
            auth = json.dumps({'op': '__auth__', 'token': relay.token}).encode()+b'\n'
            client.sendall(auth+b'{"op":"read_cell_files","req_id":"child"}\n')
            reply = json.loads(client.makefile('rb').readline())
            assert reply['op'] == 'ack_read_cell_files' and reply['req_id'] == 'child'


def test_private_run_directory_relocates_prompt_config_and_hides_bytes_D23(tmp_path, monkeypatch):
    from collimate._host.bridge import HostBridge
    workspace = tmp_path / 'workspace'
    run_dir = workspace / 'private' / 'child'
    (run_dir / 'config/files').mkdir(parents=True)
    (run_dir / 'config/files/main.set_yaml').write_text('values:\n  answer: 42\n')
    (run_dir / 'prompt.md').write_text('child prompt')
    (workspace / 'visible.txt').write_text('visible')
    monkeypatch.setenv('COLLIMATE_WORKSPACE', str(workspace))
    monkeypatch.setenv('COLLIMATE_RUN_DIR', str(run_dir))
    monkeypatch.delenv('COLLIMATE_IPC_PORT', raising=False)
    bridge = HostBridge()
    try:
        assert bridge.prompt == 'child prompt'
        assert bridge.config('main.set_yaml')['answer'] == 42
        assert bridge.config_folder_path('skills') == str(run_dir / 'config/folders/skills')
        assert bridge.fs_list() == ['visible.txt']
        assert set(bridge.snapshot_workspace()) == {'visible.txt'}
        with pytest.raises(PermissionError):
            bridge.fs_read_bytes('private/child/prompt.md')
        with pytest.raises(PermissionError):
            bridge.fs_read_bytes('private//child/prompt.md')
    finally:
        bridge.finish(success=True)


def test_prepare_child_preserves_parent_configuration_and_maps_command_paths_D23(native_bridge, tmp_path):
    from pathlib import Path
    from collimate.testing.forward import Forwarder, prepare_child
    root = Path(native_bridge.workspace)
    package = root / 'draft'
    package.mkdir()
    (package / 'draft.generator').write_text(
        'entrypoint: main.py\nmain_config: main.set_yaml\ndraft_files: [main.set_yaml]\n')
    (package / 'main.py').write_text('raise AssertionError("must not execute in parent")\n')
    (package / 'main.set_yaml').write_text('fields:\n- key: answer\n  default: 7\nvalues:\n  keep: old\n')
    parent_dir = root / '.collrun'
    parent_dir.mkdir(exist_ok=True)
    (parent_dir / 'prompt.md').write_text('parent prompt')
    relay = Forwarder(native_bridge)
    relay.port = 1234
    child = prepare_child(relay, 'draft/draft.generator', settings={'answer': 42},
                          prompt='child prompt', child_workspace='/work', ipc_host='host.docker.internal')
    try:
        assert child.argv[:3] == ['uv', 'run', '--no-project']
        assert child.argv[-1] == '/work/draft/main.py'
        assert child.cwd == '/work/draft'
        assert child.env['COLLIMATE_WORKSPACE'] == '/work'
        assert child.env['COLLIMATE_RUN_DIR'].startswith('/work/.collrun/forward-')
        assert child.env['COLLIMATE_RUN_ID'] == native_bridge.run_id
        assert set(child.env) == {
            'COLLIMATE_IPC_PORT', 'COLLIMATE_IPC_TOKEN', 'COLLIMATE_IPC_HOST',
            'COLLIMATE_WORKSPACE', 'COLLIMATE_IPC_OPS', 'COLLIMATE_RUN_DIR', 'COLLIMATE_RUN_ID',
            'COLLIMATE_CHANNEL', 'COLLIMATE_CELL_ID', 'COLLIMATE_RUNNER', 'COLLIMATE_PERMISSIONS',
            'COLLIMATE_PACKAGES', 'COLLIMATE_UI_FILES', 'COLLIMATE_LIVE_FILES_MODE',
        }
        assert (parent_dir / 'prompt.md').read_text() == 'parent prompt'
        assert Path(child.host_run_dir, 'prompt.md').read_text() == 'child prompt'
        import yaml
        config = yaml.safe_load(Path(child.host_run_dir, 'config/files/main.set_yaml').read_text())
        assert config['values'] == {'answer': 42, 'keep': 'old'}
    finally:
        child.close()
    assert not Path(child.host_run_dir).exists()


def test_restricted_transport_hashes_use_only_declared_file_read_D23(tmp_path, monkeypatch):
    import hashlib
    from collimate._host.bridge import HostBridge
    monkeypatch.setenv('COLLIMATE_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('COLLIMATE_IPC_OPS', '["read_cell_files"]')
    monkeypatch.delenv('COLLIMATE_IPC_PORT', raising=False)
    bridge = HostBridge()
    calls = []
    def reply(payload, wait_op):
        calls.append(payload)
        assert payload['op'] == 'read_cell_files'
        return {'op': wait_op, 'files': {'a.txt': {'content':'aGVsbG8=', 'body_hash':hashlib.sha256(b'hello').hexdigest(), 'file_revision':1, 'encoding':'utf-8'}}}
    monkeypatch.setattr(bridge, 'emit_and_wait', reply)
    try:
        assert bridge.read_cell_hashes(paths=['a.txt']) == {
            'a.txt': hashlib.sha256(b'hello').hexdigest()}
        assert calls == [{'op': 'read_cell_files', 'paths': ['a.txt']}]
    finally:
        bridge.finish(success=True)


def test_forward_and_parent_calls_share_real_request_lock_D23(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from collimate._host.bridge import HostBridge
    from collimate.testing.forward import Forwarder
    monkeypatch.setenv('COLLIMATE_WORKSPACE', str(tmp_path))
    monkeypatch.delenv('COLLIMATE_IPC_PORT', raising=False)
    monkeypatch.delenv('COLLIMATE_IPC_OPS', raising=False)
    bridge = HostBridge()
    client, server = socket.socketpair()
    bridge._socket = client
    bridge._sock_rfile = client.makefile('r', encoding='utf-8')
    bridge._sock_wfile = client.makefile('w', encoding='utf-8')
    requests = []
    failures = []
    def host():
        try:
            with server, server.makefile('rwb') as stream:
                for raw in stream:
                    frame = json.loads(raw)
                    if frame['op'] == 'finish':
                        break
                    requests.append(frame)
                    stream.write((json.dumps({'op': 'ack_read_cell_files',
                        'req_id': frame['req_id'], 'ok': True, 'files': {},
                        'echo': frame['index']})+'\n').encode())
                    stream.flush()
        except Exception as exc:
            failures.append(exc)
    peer = threading.Thread(target=host)
    peer.start()
    relay = Forwarder(bridge)
    def request(index):
        payload = {'op': 'read_cell_files', 'index': index}
        if index % 2:
            reply = relay.dispatch({**payload, 'req_id': 'child-shared-id'})
            assert reply['req_id'] == 'child-shared-id'
        else:
            reply = bridge.emit_and_wait(payload, wait_op='ack_read_cell_files')
            assert reply['req_id'] != 'child-shared-id'
        assert reply['echo'] == index
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(request, range(30)))
        assert len({frame['req_id'] for frame in requests}) == 30
        assert all(frame['req_id'] != 'child-shared-id' for frame in requests)
    finally:
        relay.close()
        bridge.finish(success=True)
        peer.join(timeout=3)
    assert not peer.is_alive() and not failures


def test_forward_waits_for_real_finish_and_close_unblocks_without_success_D23():
    from concurrent.futures import ThreadPoolExecutor
    from collimate.testing.forward import Forwarder
    relay = Forwarder(Parent())
    assert relay.wait_finished(0) is False
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(relay.wait_finished, 3)
        relay.dispatch({'op': 'finish', 'success': False, 'error_message': 'child failed'})
        assert pending.result(timeout=3) is True
    assert relay.result['success'] is False
    relay.close()
    abandoned = Forwarder(Parent())
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(abandoned.wait_finished, 3)
        abandoned.close()
        assert pending.result(timeout=3) is False


def test_default_forwarder_borrows_current_bridge_without_closing_it__P6F01(native_bridge):
    from collimate.testing.forward import Forwarder
    relay = Forwarder()
    assert relay.bridge is native_bridge
    relay.close()
    assert native_bridge._stop_pipe_r >= 0
    assert native_bridge._stop_pipe_w >= 0
