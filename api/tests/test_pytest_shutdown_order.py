"""The CLI's real SIGTERM and shell shutdown both finish their owned cleanup."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request

import pytest


def test_actual_cli_sigterm_finishes_task_after_real_lifespan__P0F29(tmp_path):
    """Uvicorn's re-raised SIGTERM must unwind Runner before process exit."""
    api_root = Path(__file__).resolve().parents[1]
    # Only external container discovery is disabled. The actual app lifespan,
    # server socket, uvicorn signal capture and asyncio.Runner remain real.
    program = '''
import asyncio, contextlib, pathlib, sys
sys.path.insert(0, sys.argv.pop(1))
import sandbox
sandbox.reap_orphan_containers = lambda: None
import main
original = main.app.router.lifespan_context
async def pending_cleanup():
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(.02)
        pathlib.Path('task-finally').write_text('finished after lifespan')
@contextlib.asynccontextmanager
async def observed(app):
    async with original(app):
        task = asyncio.create_task(pending_cleanup())
        await asyncio.sleep(0)
        yield
    assert not task.done()
    pathlib.Path('lifespan-finished').write_text('done')
main.app.router.lifespan_context = observed
main.cli_main()
'''
    env = os.environ.copy()
    for name in list(env):
        if name.startswith('COLLIMATE_'):
            env.pop(name)
    home = tmp_path / 'home'
    home.mkdir()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'),
               XDG_CACHE_HOME=str(home / 'cache'),
               COLLIMATE_ACCOUNTS_CONFIG=str(home / 'accounts.json'),
               PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring')
    log_path = tmp_path / 'server.log'
    with log_path.open('w') as log:
        child = subprocess.Popen(
            [sys.executable, '-u', '-c', program, str(api_root), '--port', '0',
             '--data-dir', str(tmp_path)], cwd=tmp_path, env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            url = None
            while time.monotonic() < deadline:
                assert child.poll() is None, log_path.read_text()
                match = re.search(r'Uvicorn running on (http://127\.0\.0\.1:\d+)', log_path.read_text())
                if match:
                    url = match[1]
                    with urllib.request.urlopen(f'{url}/health', timeout=2) as response:
                        assert response.status == 200
                    break
                time.sleep(.025)
            assert url is not None, log_path.read_text()
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=15)
            assert (tmp_path / 'lifespan-finished').read_text() == 'done', log_path.read_text()
            assert (tmp_path / 'task-finally').is_file(), (
                f'SIGTERM skipped pending-task finally (exit {child.returncode}):\n{log_path.read_text()}'
            )
            assert (tmp_path / 'task-finally').read_text() == 'finished after lifespan'
            assert child.returncode == 0, log_path.read_text()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


@pytest.mark.parametrize('raises', [False, True])
def test_cli_restores_previous_sigterm_handler_on_return_or_error__P0F29(tmp_path, monkeypatch, raises):
    import main
    import uvicorn
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['collimate', '--data-dir', str(tmp_path)])
    previous = signal.getsignal(signal.SIGTERM)
    def original(signum, frame):
        pass
    signal.signal(signal.SIGTERM, original)
    def run(*args, **kwargs):
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler
        if raises:
            raise RuntimeError('launch failed')
    monkeypatch.setattr(uvicorn, 'run', run)
    try:
        if raises:
            with pytest.raises(RuntimeError, match='launch failed'):
                main.cli_main()
        else:
            main.cli_main()
        assert signal.getsignal(signal.SIGTERM) is original
    finally:
        signal.signal(signal.SIGTERM, previous)


async def test_shell_shutdown_closes_start_gate_before_flush_and_sweep__P0F29(monkeypatch):
    from fastapi import HTTPException
    import routes.runs as runs
    from routes.runs.create import _start_run
    import routes.system as system
    import run_registry
    from routes.runs.models import RunRequest
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    monkeypatch.setattr(run_registry, '_shutting_down', False)
    async def flush_pause(seconds):
        assert seconds == .3
        entered.set()
        await release.wait()
    async def stop_all():
        calls.append(('stop', run_registry.is_shutting_down()))
    monkeypatch.setattr(system.asyncio, 'sleep', flush_pause)
    monkeypatch.setattr(runs, 'stop_all_runs', stop_all)
    monkeypatch.setattr(system.os, 'kill', lambda pid, sig: calls.append(('signal', sig)))
    sequence = asyncio.create_task(system._run_shutdown_sequence())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert run_registry.is_shutting_down(), 'starts remained open during shutdown response flush'
        with pytest.raises(HTTPException) as caught:
            await _start_run('not-created', RunRequest(cell_id='cell', runner='runner'))
        assert caught.value.status_code == 503
    finally:
        release.set()
        await sequence
    assert calls == [('stop', True), ('signal', signal.SIGINT)]


async def test_ordinary_stop_all_keeps_start_gate_open__P0F29(monkeypatch):
    import routes.runs as runs
    import routes.system as system
    import run_registry
    monkeypatch.setattr(run_registry, '_shutting_down', False)
    async def stop_all():
        return {'requested': 0}
    monkeypatch.setattr(runs, 'stop_all_runs', stop_all)
    assert await system.stop_all() == {'status': 'stopped', 'requested': 0}
    assert not run_registry.is_shutting_down()
