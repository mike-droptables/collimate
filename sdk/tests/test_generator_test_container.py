"""P4F31 actual command supervisor and parent request-lock regressions.

These local subprocess tests exercise the Linux container command program;
only the Docker placement is substituted. Real app publication is a separate
explicit live test, not inferred from the synthetic transport ACK below.
"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest



def module():
    return importlib.import_module('_agent_shared.generator_test')


class LocalEnvironment:
    def __init__(self):
        self.calls = []
        self.detached = []

    def exec(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        options = {'cwd': kwargs['cwd'],
                   'env': {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'], **kwargs['env']},
                   'stdin': subprocess.DEVNULL, 'stderr': subprocess.STDOUT}
        if kwargs.get('detach'):
            process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, **options)
            self.detached.append(process)
            threading.Thread(target=process.wait, daemon=True).start()
            return 0, ''
        result = subprocess.run(argv, stdout=subprocess.PIPE, timeout=kwargs['timeout'], **options)
        for process in self.detached:
            process.wait(timeout=3)
        return result.returncode, result.stdout.decode()


def child_at(tmp_path, source, env=None):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    script = tmp_path / 'child.py'
    script.write_text(source)
    return SimpleNamespace(argv=[sys.executable, str(script)], cwd=str(tmp_path),
        env=env or {}, run_dir=str(root), host_run_dir=str(root))


def test_child_forward_progress_while_container_command_runs_P4F31(tmp_path, monkeypatch):
    from collimate._host.bridge import HostBridge
    from collimate.testing.forward import Forwarder
    monkeypatch.setenv('COLLIMATE_WORKSPACE', str(tmp_path))
    monkeypatch.delenv('COLLIMATE_IPC_PORT', raising=False)
    bridge = HostBridge()
    client, server = socket.socketpair()
    bridge._socket = client
    bridge._sock_rfile = client.makefile('r', encoding='utf-8')
    bridge._sock_wfile = client.makefile('w', encoding='utf-8')
    operations, failures = [], []
    local = LocalEnvironment()
    stopped = threading.Event()
    def host():
        try:
            with server, server.makefile('rb') as reader:
                while raw := reader.readline():
                    frame = json.loads(raw)
                    operations.append(frame['op'])
                    if frame['op'] == 'env_exec':
                        code, output = local.exec(frame['command'],
                            cwd=frame['cwd'], env=frame['env'], timeout=5, detach=frame.get('detach', False))
                        ack = {'op': 'ack_env_exec', 'rc': code, 'output': output}
                    elif frame['op'] == 'update_cell':
                        ack = {'op': 'ack_update', 'ok': True, 'cell_id': 'synthetic-wire-only'}
                    else:
                        break
                    server.sendall((json.dumps({**ack, 'req_id': frame['req_id']}) + '\n').encode())
        except Exception as exc:
            failures.append(exc)
        finally:
            stopped.set()
    thread = threading.Thread(target=host, daemon=True)
    thread.start()
    class Environment:
        def exec(self, argv, **kwargs):
            ack = bridge.emit_and_wait({'op': 'env_exec', 'command': argv,
                'cwd': kwargs['cwd'], 'env': kwargs['env'], 'detach': kwargs.get('detach', False)}, wait_op='ack_env_exec', timeout=5)
            return ack['rc'], ack['output']
    try:
        with Forwarder(bridge) as relay:
            source = '''import json, os, socket
s = socket.create_connection(('127.0.0.1', int(os.environ['TEST_PORT'])), timeout=.7)
s.sendall((json.dumps({'op':'__auth__','token':os.environ['TEST_TOKEN']})+'\\n').encode())
s.sendall(b'{"op":"update_cell","req_id":"actual-child"}\\n')
reply=json.loads(s.makefile('rb').readline())
assert reply['op']=='ack_update' and reply['req_id']=='actual-child'
print('ACK-RECEIVED-BEFORE-CHILD-EXIT')
'''
            child = child_at(tmp_path, source, {'TEST_PORT': str(relay.port), 'TEST_TOKEN': relay.token})
            result = asyncio.run(module()._container_command(Environment(), child))
            assert result == (0, 'ACK-RECEIVED-BEFORE-CHILD-EXIT\n')
            assert operations[:2] == ['env_exec', 'update_cell']
    finally:
        bridge._sock_wfile.close()
        bridge._sock_rfile.close()
        client.close()
        stopped.wait(3)
    assert not failures


def test_output_tail_is_bounded_without_a_spool_log_P4F31(tmp_path):
    m = module()
    child = child_at(tmp_path, 'import sys\nsys.stdout.buffer.write(b"x"*2000000+b"TAIL")\n')
    code, output = asyncio.run(m._container_command(LocalEnvironment(), child))
    assert code == 0 and output.startswith('[earlier output truncated]\n')
    assert output.endswith('TAIL')
    assert len(output.encode()) == m.OUTPUT_BYTES + len('[earlier output truncated]\n')
    paths = list(Path(child.host_run_dir).iterdir())
    assert {p.name for p in paths} <= {'command.ready', 'command.start', 'command.result', 'command.cancel'}
    assert sum(p.stat().st_size for p in paths) < m.OUTPUT_BYTES * 2


@pytest.mark.parametrize('before_ack', [False, True])
def test_cancel_stops_group_and_never_arms_child_before_ack_P4F31(tmp_path, before_ack):
    m = module()
    heartbeat = tmp_path / 'heartbeat'
    child = child_at(tmp_path, 'import signal,time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        'while True:\n Path("heartbeat").write_text(str(time.monotonic_ns()))\n time.sleep(.01)\n')
    launched, release = threading.Event(), threading.Event()
    class Environment(LocalEnvironment):
        def exec(self, argv, **kwargs):
            result = super().exec(argv, **kwargs)
            if kwargs.get('detach'):
                launched.set()
                if before_ack:
                    assert release.wait(3)
            return result
    async def check():
        task = asyncio.create_task(m._container_command(Environment(), child))
        assert await asyncio.to_thread(launched.wait, 3)
        if not before_ack:
            for _ in range(150):
                if heartbeat.exists():
                    break
                await asyncio.sleep(.02)
            assert heartbeat.exists()
        task.cancel()
        await asyncio.sleep(.1)
        if before_ack:
            assert not heartbeat.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        last = heartbeat.read_bytes() if heartbeat.exists() else None
        await asyncio.sleep(.1)
        assert (heartbeat.read_bytes() if heartbeat.exists() else None) == last
    asyncio.run(check())


def test_child_spawn_failure_returns_bounded_failure_P4F31(tmp_path):
    child = child_at(tmp_path, '')
    child.argv = ['/no-such-d23-executable']
    code, output = asyncio.run(module()._container_command(LocalEnvironment(), child))
    assert code == 127 and 'FileNotFoundError' in output


def test_launcher_failure_does_not_wait_for_child_P4F31(tmp_path):
    child = child_at(tmp_path, 'raise AssertionError("must not start")')
    class Environment:
        def exec(self, *args, **kwargs):
            return 17, 'launcher refused'
    with pytest.raises(RuntimeError, match='launcher refused'):
        asyncio.run(module()._container_command(Environment(), child))
    assert not (Path(child.host_run_dir) / 'command.start').exists()


def test_normal_completion_stops_descendant_holding_pipe_P4F31(tmp_path):
    heartbeat = tmp_path / 'descendant-heartbeat'
    descendant = 'import time\nfrom pathlib import Path\nwhile True:\n Path("descendant-heartbeat").write_text(str(time.monotonic_ns()))\n time.sleep(.01)\n'
    source = ('import subprocess,sys,time\nfrom pathlib import Path\n'
              f'subprocess.Popen([sys.executable,"-c",{descendant!r}])\n'
              'while not Path("descendant-heartbeat").exists(): time.sleep(.01)\n'
              'print("parent done")\n')
    child = child_at(tmp_path, source)
    assert asyncio.run(module()._container_command(LocalEnvironment(), child)) == (0, 'parent done\n')
    last = heartbeat.read_bytes()
    import time
    time.sleep(.1)
    assert heartbeat.read_bytes() == last
    ready = json.loads(Path(child.host_run_dir, 'command.ready').read_text())
    proc = Path('/proc') / str(ready['pid']) / 'stat'
    assert not proc.exists()


def test_timeout_stops_actual_container_program_group_P4F31(tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    child = child_at(tmp_path, 'import time\nfrom pathlib import Path\n'
        'while True:\n Path("heartbeat").write_text(str(time.monotonic_ns()))\n time.sleep(.01)\n')
    async def check():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(module()._container_command(LocalEnvironment(), child), timeout=.3)
        assert heartbeat.exists()
        last = heartbeat.read_bytes()
        await asyncio.sleep(.1)
        assert heartbeat.read_bytes() == last
    asyncio.run(check())


@pytest.mark.parametrize('kind', ['oversized', 'fifo', 'symlink'])
def test_command_records_are_bounded_regular_files_P4F31(tmp_path, kind):
    path = tmp_path / 'record'
    if kind == 'oversized':
        path.write_bytes(b'x' * 2048)
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.symlink_to(tmp_path / 'target')
        (tmp_path / 'target').write_text('{}')
    with pytest.raises((ValueError, OSError)):
        module()._command_record(path, 1024)


@pytest.mark.parametrize('ending', ['normal', 'cancel', 'timeout'])
def test_supervisor_reaps_direct_and_adopted_session_descendants_P4F31(tmp_path, ending):
    descendant = ('import os,time\nfrom pathlib import Path\n'
                  'Path("descendant.pid").write_text(str(os.getpid()))\n'
                  'while True: time.sleep(.02)\n')
    source = ('import os,subprocess,sys,time\nfrom pathlib import Path\n'
              'Path("child.pid").write_text(str(os.getpid()))\n'
              f'subprocess.Popen([sys.executable,"-c",{descendant!r}],start_new_session=True)\n'
              'while not Path("descendant.pid").exists(): time.sleep(.01)\n'
              + ('print("normal complete")\n' if ending == 'normal' else 'while True: time.sleep(.02)\n'))
    child = child_at(tmp_path, source)
    async def check():
        task = asyncio.create_task(module()._container_command(LocalEnvironment(), child))
        for _ in range(150):
            if (tmp_path / 'descendant.pid').exists():
                break
            await asyncio.sleep(.02)
        assert (tmp_path / 'descendant.pid').exists()
        if ending == 'normal':
            assert await task == (0, 'normal complete\n')
        elif ending == 'cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=.05)
        ready = json.loads(Path(child.host_run_dir, 'command.ready').read_text())
        pids = [ready['pid'], int((tmp_path / 'child.pid').read_text()),
                int((tmp_path / 'descendant.pid').read_text())]
        for pid in pids:
            assert not (Path('/proc') / str(pid)).exists(), f'unreaped process {pid}'
    asyncio.run(check())
