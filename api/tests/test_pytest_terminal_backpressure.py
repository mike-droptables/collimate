"""P4F08 real WebSocket/ping/PTY backpressure and detach safety."""

from sessions import terminal as terminal_sessions
import asyncio
import contextlib
import os
import socket
import sys
import tty

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

import routes.terminal as terminal
from test_pytest_terminal_keep_alive import _app, _wait_for
import test_pytest_terminal_keep_alive as support

_clean_registry = support._clean_registry


@pytest.mark.parametrize("real_bound", [False, True])
async def test_stdin_pause_closes_1013_and_same_pty_reattaches__P4F08(monkeypatch, tmp_path, real_bound):
    # Scale both independent server timers, retaining their production ordering.
    if not real_bound:
        monkeypatch.setattr(terminal, '_STDIN_PAUSE_SECONDS', .12, raising=False)
    else:
        assert 20 <= terminal._STDIN_PAUSE_SECONDS <= 40
    pause = terminal._STDIN_PAUSE_SECONDS
    monkeypatch.setattr(terminal, '_STDIN_PENDING_CAP', 65536)
    gate = tmp_path / 'read'
    received = tmp_path / 'received'
    source = '''import os,select,time,pathlib
p=pathlib.Path(os.environ['GATE'])
while not p.exists(): time.sleep(.01)
body=b''
while select.select([0],[],[],.3)[0]: body+=os.read(0,65536)
pathlib.Path(os.environ['RESULT']).write_bytes(body)
print('read-complete',flush=True)
time.sleep(30)
'''
    async def spawn(session):
        import pty
        master, slave = pty.openpty()
        tty.setraw(slave)
        try:
            session.proc = await asyncio.create_subprocess_exec(sys.executable, '-c', source,
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                env={**os.environ, 'GATE': str(gate), 'RESULT': str(received)})
        finally:
            os.close(slave)
        session.master_fd = master
    monkeypatch.setattr(terminal, '_spawn_pty', spawn)
    info = terminal_sessions.register_session('run-1', 'cell-1', 'synthetic', ['synthetic'], keep_alive=True)
    server = uvicorn.Server(uvicorn.Config(_app(), host='127.0.0.1', port=0,
        log_level='error', ws='websockets-sansio', ws_ping_interval=.025, ws_ping_timeout=40 if real_bound else .35,
        timeout_graceful_shutdown=1))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.setblocking(False)
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _wait_for(lambda: server.started)
        url = f"ws://127.0.0.1:{listener.getsockname()[1]}{info['ws_path']}?token={info['token']}"
        async with connect(url, ping_interval=None, compression=None) as ws:
            started = asyncio.get_running_loop().time()
            # Numbered frames let us prove the surviving prefix is neither
            # reordered nor replayed. The program reads no PTY input yet.
            payload = b''.join(bytes([n]) * 32768 for n in range(32))
            for start in range(0, len(payload), 32768):
                await ws.send(payload[start:start+32768])
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), pause + 5)
            assert closed.value.rcvd.code == 1013
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed >= pause - .1
            print({'real_bound': real_bound, 'pause_seconds': pause, 'actual_close_seconds': elapsed, 'code': 1013})
        session = terminal_sessions._sessions[info['terminal_id']]
        await _wait_for(lambda: not session.attached)
        proc = session.proc
        assert proc is not None and proc.returncode is None
        async with connect(url, ping_interval=None) as reattached:
            gate.touch()
            output = b''
            while b'read-complete' not in output:
                output += await asyncio.wait_for(reattached.recv(), 3)
            assert session.proc is proc
        kept = received.read_bytes()
        assert kept and len(kept) < len(payload)
        assert kept == payload[:len(kept)], 'stdin must never be replayed across attaches'
    finally:
        terminal_sessions.unregister_session(info['terminal_id'])
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serving, 5)
        listener.close()
