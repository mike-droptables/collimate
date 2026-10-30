"""P4 nonstream harness operations use the host-mediated environment plane."""
import subprocess
import sys
import json
import asyncio
from pathlib import Path

import pytest

from _agent_shared import cli_container as cc  # noqa: E402


class ShellEnv:
    def __init__(self):
        self.calls = []

    @property
    def container(self):
        raise AssertionError('nonstream operation reached the raw Docker handle')

    def exec(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = subprocess.run(argv, capture_output=True)
        return result.returncode, (result.stdout + result.stderr).decode('utf-8', 'replace')

    def read_file(self, path, **kwargs):
        return Path(path).read_bytes()


@pytest.mark.parametrize('kind', ['context', 'policy', 'root'])
@pytest.mark.parametrize('large', [False, True])
def test_real_writes_use_only_environment_exec_F1505_F1532(tmp_path, kind, large):
    env = ShellEnv()
    target = tmp_path / 'folder with spaces' / "body '$.txt"
    content = 'whole λ body\n' * (100000 if large else 1)
    if kind == 'context':
        assert cc.write_container_file(env, str(target), content, mkdir=str(target.parent))
    elif kind == 'policy':
        cc.write_policy_file(env, str(target), content, mkdir=str(target.parent), what='policy')
    else:
        cc.write_root_file(env, str(target), content, what='root file')
    assert target.read_text() == content
    assert list(target.parent.iterdir()) == [target]
    assert env.calls
    assert all(kw.get('user') == ('root' if kind == 'root' else 'coder') for _, kw in env.calls)


def test_workspace_home_setup_uses_environment_plane_D05_F1402(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    home = workspace / 'home'
    home.mkdir(parents=True)
    target = tmp_path / 'container-home'
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(workspace))
    env = ShellEnv()
    cc.stage_workspace_links(env, {str(home): {'bind': str(target), 'mode': 'rw'}}, mount=str(workspace))
    assert target.is_symlink() and target.resolve() == home
    assert env.calls[0][1] == {'user': 'root'}


def test_writability_refusal_uses_environment_plane_D05_F1402():
    env = ShellEnv()
    env.exec = lambda argv, **kwargs: (0, '/workspace\n')
    with pytest.raises(RuntimeError, match='cannot write /workspace'):
        cc.require_writable_bindings('fixture', env, {})


def test_opencode_tail_reads_whole_export_through_environment_F1669_F1402(tmp_path, monkeypatch):
    from _agent_shared import terminal_tail as tail
    binary = tmp_path / 'opencode'
    record = {'messages': [{'info': {'role': 'assistant'}, 'parts': [{'type': 'text', 'text': 'complete ' * 1000}]}]}
    payload = json.dumps(record)
    binary.write_text('#!/bin/sh\ncat <<\'PAYLOAD\'\n' + payload + '\nPAYLOAD\n')
    binary.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + ':' + __import__('os').environ['PATH'])
    env = ShellEnv()
    original = env.exec
    def capped(argv, **kwargs):
        code, output = original(argv, **kwargs)
        return code, output if len(output) <= 128 else '[truncated]' + output[-64:]
    env.exec = capped
    messages = tail._OpencodeTail(env, 'ses_TEST').read()
    assert messages and messages[-1].content == ('complete ' * 1000).strip()
    assert any('opencode export' in str(argv) for argv, _ in env.calls)


def test_adapter_teardown_uses_environment_plane_F1583_F1402(monkeypatch):
    from _agent_shared import acp_client as acp

    env = ShellEnv()
    env.container_id = 'attached-fixture'
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    pidfiles = []

    def attached(container_id, argv, pidfile, **kwargs):
        assert container_id == env.container_id
        Path(pidfile).write_text(str(child.pid))
        pidfiles.append(Path(pidfile))
        return None, None, object()

    monkeypatch.setattr(acp, '_attached_exec', attached)
    monkeypatch.setattr(acp, 'DockerExecTransport', lambda *a, **kw: object())
    monkeypatch.setattr(acp, 'register_adapter_log', lambda backend: '')
    try:
        adapter = asyncio.run(acp.spawn_container_adapter('fixture', env, ['adapter'], env_vars={}))
        adapter.kill()
        assert child.wait(timeout=3) != 0
        assert env.calls and all(not path.exists() for path in pidfiles)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        for path in pidfiles:
            path.unlink(missing_ok=True)


def test_native_live_gate_export_uses_full_environment_read_F1402(tmp_path, monkeypatch):
    from tests_live.test_live_opencode_commands import export_session

    payload = {'messages': [{'info': {'role': 'assistant'}, 'parts': [
        {'type': 'text', 'text': 'complete export ' * 1000}]}]}
    binary = tmp_path / 'opencode'
    binary.write_text('#!/bin/sh\nif [ "$1" = session ]; then echo ses_P4; else\n'
                      + "cat <<'PAYLOAD'\n" + json.dumps(payload) + '\nPAYLOAD\nfi\n')
    binary.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + ':' + __import__('os').environ['PATH'])
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-credential-not-in-content')
    env = ShellEnv()
    original = env.exec

    def capped(argv, **kwargs):
        code, output = original(argv, **kwargs)
        return code, output if len(output) <= 128 else '[truncated]' + output[-64:]

    env.exec = capped
    assert export_session(env, frozenset()) == payload
