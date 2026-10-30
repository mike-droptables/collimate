"""The single POSIX group signal boundary, including already-reaped leaders."""
import os
import signal
import subprocess
import sys

import pytest


@pytest.mark.parametrize('error', [ProcessLookupError, PermissionError, OSError])
def test_live_group_and_pid_fallback_errors__F0192(monkeypatch, error):
    from process_signals import signal_process_group
    calls = []
    monkeypatch.setattr(os, 'getpgid', lambda pid: pid + 1)
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: calls.append(('group', pgid, sig)))
    monkeypatch.setattr(os, 'kill', lambda pid, sig: calls.append(('pid', pid, sig)))
    assert signal_process_group(123, signal.SIGTERM)
    assert calls == [('group', 124, signal.SIGTERM)]
    def missing(*args):
        raise error()
    monkeypatch.setattr(os, 'killpg', missing)
    assert signal_process_group(123, signal.SIGKILL)
    assert calls[-1] == ('pid', 123, signal.SIGKILL)
    monkeypatch.setattr(os, 'kill', missing)
    assert not signal_process_group(123, signal.SIGTERM)


def test_known_group_never_looks_up_or_signals_reused_pid__F0352(monkeypatch):
    from process_signals import signal_process_group
    calls = []
    def forbidden(*args):
        pytest.fail('a known group must never resolve or fall back to a bare PID')
    monkeypatch.setattr(os, 'getpgid', forbidden)
    monkeypatch.setattr(os, 'kill', forbidden)
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: calls.append((pgid, sig)))
    assert signal_process_group(123, signal.SIGTERM, known_group=True)
    assert calls == [(123, signal.SIGTERM)]
    def missing(*args):
        raise ProcessLookupError()
    monkeypatch.setattr(os, 'killpg', missing)
    assert not signal_process_group(123, signal.SIGTERM, known_group=True)


@pytest.mark.parametrize('leader_exits', [False, True])
def test_actual_owned_group_reaches_descendant_after_leader_reap__F0344(tmp_path, leader_exits):
    from process_signals import signal_process_group
    # The child records its signal receipt in an owned file. In the
    # reaped variant it remains in its original group after the leader exits.
    marker = tmp_path / 'child-term'
    child = ('import signal,time,sys\n'
             'from pathlib import Path\n'
             f'def stop(*_): Path({str(marker)!r}).write_text("term"); sys.exit(0)\n'
             'signal.signal(signal.SIGTERM, stop)\n'
             'print("ready",flush=True)\n'
             'while True: time.sleep(1)\n')
    leader = ('import subprocess,sys\n'
              f'p=subprocess.Popen([sys.executable,"-c",{child!r}],stdout=subprocess.PIPE,text=True)\n'
              'assert p.stdout.readline().strip()=="ready"\n'
              'import signal\nsignal.signal(signal.SIGTERM,lambda *_:None)\n'
              'print(p.pid,flush=True)\n'
              + ('sys.exit(0)\n' if leader_exits else
                 'p.wait()\n'))
    proc = subprocess.Popen([sys.executable, '-c', leader], start_new_session=True,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        import select
        assert select.select([proc.stdout], [], [], 5)[0], 'leader never reported its ready child'
        assert int(proc.stdout.readline()) > 0
        if leader_exits:
            assert proc.wait(timeout=5) == 0
        assert signal_process_group(proc.pid, signal.SIGTERM, known_group=leader_exits)
        if not leader_exits:
            assert proc.wait(timeout=5) == 0
        import time
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.read_text() == 'term'
    finally:
        signal_process_group(proc.pid, signal.SIGKILL, known_group=True)
        proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
