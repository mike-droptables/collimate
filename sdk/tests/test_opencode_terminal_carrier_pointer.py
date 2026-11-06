"""D23 live fixture pointer passes actual SDK recording-host admission."""
import sys
from pathlib import Path

import gen
import pytest

from collimate.testing import run_generator_callable
from tests_live.test_live_opencode_generator_tool import terminal_pointer, hosted_terminal_identity, install_forward_recording_ack


def test_native_terminal_fixture_pointer_admitted_and_cleaned_D23():
    async def run():
        pointer = terminal_pointer()
        async with gen.serve_terminal("synthetic-container", ["synthetic-program"], pointer=pointer) as terminal:
            assert terminal["terminal_id"] == "rec-term-1"
            assert gen.has_file(pointer)
        assert not gen.has_file(pointer)
    result = run_generator_callable(run)
    assert result.ok, result.error
    operations = [row["op"] for row in result.tier_b_calls]
    assert operations.count("start_terminal") == 1
    assert operations.count("stop_terminal") == 1


def test_native_terminal_fixture_forwarded_child_has_hosted_identity_D23(native_bridge):
    import json
    import os
    import subprocess

    from collimate.testing.forward import Forwarder, prepare_child

    workspace = Path(native_bridge.workspace)
    draft = workspace / 'draft'
    draft.mkdir()
    (draft / 'probe.generator').write_text('entrypoint: main.py\n')
    (draft / 'main.py').write_text('import gen\n')
    original_run = native_bridge.run_id
    assert not native_bridge.channel
    hosted_terminal_identity(native_bridge)
    with Forwarder(native_bridge) as relay:
        child = prepare_child(relay, 'draft/probe.generator')
        try:
            program = ('import json; from collimate._host.bridge import HostBridge; '
                       'b=HostBridge(); print(json.dumps({"run_id":b.run_id,'
                       '"channel":b.channel,"cell_id":b.cell_id})); '
                       'b.finish(success=True)')
            result = subprocess.run([sys.executable, '-c', program],
                env={**os.environ, **child.env}, capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                'run_id': original_run, 'channel': 'p4-native-opencode',
                'cell_id': 'p4-native-opencode-cell'}
            assert child.env['COLLIMATE_CHANNEL'] == native_bridge.channel
            assert child.env['COLLIMATE_CELL_ID'] == native_bridge.cell_id
            assert child.env['COLLIMATE_RUN_ID'] == original_run
        finally:
            child.close()


def test_native_child_update_records_committed_snapshot_before_finish_D23(native_bridge, monkeypatch):
    import os
    import subprocess

    from collimate.testing.forward import Forwarder, prepare_child

    marker = 'committed-native-child-ack'
    draft = Path(native_bridge.workspace) / 'draft'
    draft.mkdir()
    (draft / 'probe.generator').write_text('entrypoint: main.py\n')
    (draft / 'main.py').write_text(
        'import gen\nfrom collimate._host import bridge as _host\n'
        '_host._DEFAULT_ACK_TIMEOUT_SECONDS = .3\n'
        'async def run():\n'
        f'    gen.update_file("native-child.txt", {marker!r})\n'
        '    await gen.commit(name="Native child committed")\n'
        'gen.main(run)\n')
    hosted_terminal_identity(native_bridge)
    install_forward_recording_ack(native_bridge, monkeypatch)
    with Forwarder(native_bridge) as relay:
        child = prepare_child(relay, 'draft/probe.generator')
        try:
            result = subprocess.run([sys.executable, str(draft / 'main.py')],
                cwd=child.cwd, env={**os.environ, **child.env},
                capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result.stderr
            assert relay.wait_finished(.5)
            assert relay.result['success'] is True
            assert native_bridge.read_cell_files(paths=['native-child.txt']) == {'native-child.txt': marker}
            assert native_bridge.updates == [{'name': 'Native child committed',
                                             'files': {'native-child.txt': marker}}]
        finally:
            child.close()


@pytest.mark.parametrize('damage', [
    'operation', 'wait_op', 'deleted_files', 'unknown_artifact',
    'different_local_path', 'log_artifact', 'unknown_metadata', 'metadata_value',
])
def test_recording_child_ack_refuses_unsupported_raw_shapes_D23(native_bridge, monkeypatch, damage):
    payload = {'op': 'update_cell',
               'operation_id':'probe-operation', 'writer_id':'probe-writer', 'edit_seq':1,
               'base_revisions':{'native-child.txt':0}, 'resolves':[],
               'artifacts': [{'type': 'workspace', 'cell_path': 'native-child.txt',
                              'local_path': '.collrun/forward-test/persistence/probe-operation/0',
                              'encoding':'utf-8'}], 'metadata': {}}
    wait_op = 'ack_update'
    if damage == 'operation':
        payload['op'] = 'account'
    elif damage == 'wait_op':
        wait_op = 'ack_account'
    elif damage == 'deleted_files':
        payload['deleted_files'] = ['other.txt']
    elif damage == 'unknown_artifact':
        payload['artifacts'][0]['cell_path'] = 'other.txt'
    elif damage == 'different_local_path':
        payload['artifacts'][0]['local_path'] = 'elsewhere.txt'
    elif damage == 'log_artifact':
        payload['artifacts'][0]['category'] = 'log'
    elif damage == 'unknown_metadata':
        payload['metadata']['settings'] = {}
    elif damage == 'metadata_value':
        payload['metadata']['name'] = []
    install_forward_recording_ack(native_bridge, monkeypatch)
    with pytest.raises(ValueError, match='unsupported'):
        native_bridge.emit_and_wait(payload, wait_op=wait_op)
    assert native_bridge.updates == []
    assert native_bridge.read_cell_files() == {}


def test_expired_bridge_route_refused_when_listener_port_is_reused_D23():
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from tests_live.test_live_opencode_generator_tool import dead, tool_names

    class Handler(BaseHTTPRequestHandler):
        active_path = '/first/mcp'
        inactive_status = 404

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert request['method'] == 'tools/list'
            status = 200 if self.path == self.active_path else self.inactive_status
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                               'result': {'tools': [{'name': 'test_generator'}]}}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'

    async def run():
        assert await tool_names(base + '/first/mcp') == {'test_generator'}
        Handler.active_path = '/replacement/mcp'
        await dead(base + '/first/mcp')
        assert await tool_names(base + '/replacement/mcp') == {'test_generator'}
        with pytest.raises(AssertionError):
            await dead(base + '/replacement/mcp')
        for status in (200, 401, 403, 500):
            Handler.inactive_status = status
            with pytest.raises(AssertionError):
                await dead(base + '/first/mcp')

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
    asyncio.run(dead(base + '/first/mcp'))
