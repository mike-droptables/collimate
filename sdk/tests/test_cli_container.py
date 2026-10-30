# SPDX-License-Identifier: MIT
"""Unit tests for the harness container cache (``cli_container``): a cached
env whose container died is replaced, not handed back. Docker-free — the
envs are stand-ins and provisioning is stubbed."""
from __future__ import annotations


import pytest

from _agent_shared import cli_container as cc  # noqa: E402


class NotFound(Exception):
    """Named like docker.errors.NotFound — the check matches by name, since
    docker-py is a generator-side dependency."""


class _Container:
    def __init__(self, status):
        self.status = status


class _Env:
    def __init__(self, container=None, error=None):
        self._container, self._error = container, error
        self.closed = False
        self.mount = "/workspace"

    @property
    def container(self):
        if self._error is not None:
            raise self._error
        return self._container

    def close(self):
        self.closed = True

    def exec(self, argv, **kwargs):
        result = self.container.exec_run(argv, **kwargs)
        return result.exit_code, result.output.decode()


@pytest.fixture
def envs(monkeypatch):
    fresh = _Env(_Container("running"))
    monkeypatch.setattr(cc, "_ENVS", {})
    monkeypatch.setattr(cc, "_require_docker", lambda name, build: fresh)
    monkeypatch.setattr(cc, "_ATEXIT_REGISTERED", True)
    return fresh


@pytest.mark.parametrize("dead", [
    _Env(error=NotFound("No such container")),
    _Env(_Container("exited")),
])
def test_a_dead_cached_container_is_replaced(envs, dead):
    """F34: after a dockerd restart or a `docker kill`, every later turn used
    to reopen through the cached env and die on a raw 409."""
    cc._ENVS["qwen-code"] = dead
    got = cc.container_env("qwen-code", build_dir="qwen-code")
    assert got is envs and cc._ENVS["qwen-code"] is envs
    assert dead.closed


@pytest.mark.parametrize("alive", [
    _Env(_Container("running")),
    _Env(error=RuntimeError("cannot reach the daemon")),   # no verdict → keep
])
def test_a_live_or_unjudgeable_cached_container_is_reused(envs, alive):
    cc._ENVS["qwen-code"] = alive
    assert cc.container_env("qwen-code", build_dir="qwen-code") is alive
    assert not alive.closed


class _ProbeContainer(_Container):
    def __init__(self, denied=(), exit_code=0):
        super().__init__("running")
        self.denied, self.exit_code, self.argv = list(denied), exit_code, None

    def exec_run(self, argv, **kw):
        import types
        self.argv = argv
        out = "".join(f"{d}\n" for d in self.denied if d in argv)
        return types.SimpleNamespace(exit_code=self.exit_code, output=out.encode())


def test_a_bind_the_harness_cannot_write_fails_by_name(monkeypatch, tmp_path):
    """F65: a host uid other than 1000 made every containerized harness lose
    its writes silently; the container is now asked, and says so by name."""
    fresh = _Env(_ProbeContainer(denied=["/workspace", "/home/coder/.qwen"]))
    monkeypatch.setattr(cc, "_ENVS", {})
    monkeypatch.setattr(cc, "_require_docker", lambda name, build: fresh)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cc.gen, "workspace_path", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match=r"cannot write /workspace, /home/coder/.qwen"):
        cc.container_env("qwen-code", build_dir="qwen-code", volumes={
            str(home): {"bind": "/home/coder/.qwen", "mode": "rw"}})
    assert fresh.closed and "qwen-code" not in cc._ENVS


@pytest.mark.parametrize("container", [
    _ProbeContainer(),                                   # all writable
    _ProbeContainer(denied=["/workspace"], exit_code=126),   # probe failed: no verdict
])
def test_a_writable_or_unprobeable_container_is_kept(monkeypatch, container):
    fresh = _Env(container)
    monkeypatch.setattr(cc, "_ENVS", {})
    monkeypatch.setattr(cc, "_require_docker", lambda name, build: fresh)
    monkeypatch.setattr(cc, "_ATEXIT_REGISTERED", True)
    assert cc.container_env("qwen-code", build_dir="qwen-code") is fresh
    assert not fresh.closed


def test_gateway_bridge_is_cached_per_event_loop(monkeypatch):
    """The bridge bounces its main_loop tools onto the loop it started on, so
    a second loop in one process gets its own bridge, while one loop reuses
    its bridge across calls."""
    import asyncio

    from _agent_shared import mcp_bridge

    monkeypatch.setattr(mcp_bridge, "start_bridge", lambda **kw: object())
    monkeypatch.setattr(cc, "_BRIDGES", {})

    async def _twice():
        return (cc.gateway_bridge(extra_entries=[("web_fetch", "web", False, "", lambda: None)]),
                cc.gateway_bridge(extra_entries=[("web_fetch", "web", False, "", lambda: None)]))

    a1, a2 = asyncio.run(_twice())
    b1, _ = asyncio.run(_twice())
    assert a1 is a2
    assert b1 is not a1


class _LegBridge:
    def __init__(self, port: int = 5):
        self.url_container = f"http://host.docker.internal:{port}/cap/mcp"
        self.url_host = f"http://172.17.0.1:{port}/cap/mcp"
        self.stops = 0
        self.stopped_on_loop: list = []

    def stop(self):
        # The stop joins the server thread: it must not block the event loop.
        import asyncio

        try:
            asyncio.get_running_loop()
            self.stopped_on_loop.append(True)
        except RuntimeError:
            self.stopped_on_loop.append(False)
        self.stops += 1


def test_leg_bridge_is_stopped_when_the_leg_ends__F1526(monkeypatch):
    """F1526: every TTY runner's one lifetime rule. The leg's bridge is its
    own (never the cached gateway bridge), handed the grant's flags on the
    running loop, stopped when the ``async with`` exits — normally or by an
    exception — and its endpoints stay among the run's own bridge URLs
    until close_all."""
    import asyncio

    from _agent_shared import mcp_bridge

    started: list = []

    def _start(**kw):
        started.append(kw)
        return _LegBridge(len(started))

    monkeypatch.setattr(mcp_bridge, "start_bridge", _start)
    monkeypatch.setattr(cc, "_BRIDGES", {})
    monkeypatch.setattr(cc, "_ENVS", {})
    monkeypatch.setattr(cc, "_LEG_BRIDGE_URLS", set())
    extra = [("web_fetch", "web", False, "", lambda: None)]

    async def _leg():
        async with cc.leg_bridge(extra_entries=extra) as bridge:
            assert started[0]["loop"] is asyncio.get_running_loop()
            assert bridge.stops == 0
        return bridge

    ended = asyncio.run(_leg())
    assert ended.stops == 1
    assert ended.stopped_on_loop == [False]           # off the loop
    assert [row[0] for row in started[0]["extra_entries"]] == ["list_handlers", "handler_skills", "validate_file", "web_fetch"]
    assert started[0]["extra_entries"][-1:] == extra
    assert cc._BRIDGES == {}                        # never the cached one

    async def _failing_leg():
        async with cc.leg_bridge(extra_entries=extra) as bridge:
            box.append(bridge)
            raise RuntimeError("the seed failed")

    box: list = []
    with pytest.raises(RuntimeError, match="the seed failed"):
        asyncio.run(_failing_leg())
    assert box[0].stops == 1
    assert cc.run_bridge_urls() == {ended.url_container, ended.url_host,
                                    box[0].url_container, box[0].url_host}
    cc.close_all()
    assert cc.run_bridge_urls() == frozenset()


def test_leg_bridge_never_raises_on_start__F1526(monkeypatch):
    """Handler tools always start; a bridge that cannot start is a leg
    without those tools (logged), never a failed terminal."""
    import asyncio

    from _agent_shared import mcp_bridge

    calls: list = []
    logs: list = []

    def _boom(**kw):
        calls.append(kw)
        raise OSError("no docker gateway")

    monkeypatch.setattr(mcp_bridge, "start_bridge", _boom)
    monkeypatch.setattr(cc, "safe_log", logs.append)

    async def _leg(entries):
        async with cc.leg_bridge(extra_entries=entries) as bridge:
            return bridge

    assert asyncio.run(_leg([])) is None and len(calls) == 1
    assert asyncio.run(_leg([("web_fetch", "web", False, "", lambda: None)])) is None and len(calls) == 2
    assert any("no docker gateway" in m for m in logs)


def test_harness_home_uses_the_workspace_mount_only__D05(monkeypatch, tmp_path):
    import _env_shared
    from types import SimpleNamespace

    home = tmp_path / '.collrun' / 'homes' / 'qwen'
    home.mkdir(parents=True)
    placed = []
    commands = []
    class Container:
        def exec_run(self, argv, **kwargs):
            commands.append((argv, kwargs))
            return SimpleNamespace(exit_code=0, output=b'')
    env = _Env(Container())
    monkeypatch.setattr(cc, '_ENVS', {})
    monkeypatch.setattr(cc, '_ATEXIT_REGISTERED', True)
    monkeypatch.setattr(cc, 'image_spec', lambda *_: {'image': 'fixture'})
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(tmp_path))
    monkeypatch.setattr(_env_shared, 'DockerEnvironment', lambda **kw: placed.append(kw) or env)
    cc.container_env('qwen-code', build_dir='qwen-code', volumes={
        str(home): {'bind': '/home/coder/.qwen', 'mode': 'rw'},
    })
    assert placed[0].get('volumes', {}) == {}
    assert any('/workspace/.collrun/homes/qwen' in argv for argv, _ in commands)


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'readonly'])
def test_workspace_links_reject_unsafe_sources_before_exec__D05(monkeypatch, tmp_path, kind):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    source = outside
    if kind == 'symlink':
        source = workspace / 'link'
        source.symlink_to(outside, target_is_directory=True)
    elif kind == 'readonly':
        source = workspace / 'home'
        source.mkdir()
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(workspace))
    container = _ProbeContainer()
    with pytest.raises(ValueError):
        cc.stage_workspace_links(_Env(container), {
            str(source): {'bind': '/home/coder/private', 'mode': 'ro' if kind == 'readonly' else 'rw'},
        })
    assert container.argv is None


def test_failed_workspace_link_setup_closes_container__D05(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    fresh = _Env(_ProbeContainer(exit_code=1))
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(tmp_path))
    monkeypatch.setattr(cc, '_ENVS', {})
    monkeypatch.setattr(cc, '_require_docker', lambda *_: fresh)
    with pytest.raises(RuntimeError, match='cannot prepare harness workspace link'):
        cc.container_env('fixture', build_dir='fixture', volumes={
            str(home): {'bind': '/home/coder/private', 'mode': 'rw'},
        })
    assert fresh.closed
    assert 'fixture' not in cc._ENVS


def test_host_account_mount_survives_home_link_setup__D05(monkeypatch, tmp_path):
    import _env_shared

    workspace = tmp_path / 'workspace'
    home = workspace / '.collrun' / 'homes' / 'codex'
    home.mkdir(parents=True)
    account = tmp_path / 'host-account' / 'auth.json'
    account.parent.mkdir()
    account.write_text('shared credentials')
    placed = []
    commands = []
    class Container:
        def exec_run(self, argv, **kwargs):
            from types import SimpleNamespace
            commands.append((argv, kwargs))
            return SimpleNamespace(exit_code=0, output=b'')
    env = _Env(Container())
    monkeypatch.setattr(cc, '_ENVS', {})
    monkeypatch.setattr(cc, '_ATEXIT_REGISTERED', True)
    monkeypatch.setattr(cc, 'image_spec', lambda *_: {'image': 'fixture'})
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(workspace))
    monkeypatch.setattr(_env_shared, 'DockerEnvironment', lambda **kw: placed.append(kw) or env)
    cc.container_env('codex', build_dir='codex', volumes={
        str(home): {'bind': '/home/coder/.codex', 'mode': 'rw'},
        str(account): {'bind': '/home/coder/.codex/auth.json', 'mode': 'rw'},
    }, readonly_files={'/protected/hooks.json': '{"guard":true}'})
    assert placed[0]['volumes'] == {
        str(account): {'bind': '/workspace/.collrun/homes/codex/auth.json', 'mode': 'rw'},
    }
    assert placed[0]['readonly_files'] == {'/protected/hooks.json': '{"guard":true}'}
    links = [argv for argv, _ in commands if argv[:2] == ['sh', '-ec']]
    assert len(links) == 1
    assert links[0][-2:] == ['/workspace/.collrun/homes/codex', '/home/coder/.codex']


def test_native_workspace_mount_identity_replaces_cached_environment(envs):
    previous = _Env(_Container('running'))
    previous.mount = '/host/exact/workspace'
    cc._ENVS['claude-code'] = previous
    assert cc.container_env('claude-code', build_dir='claude-code') is envs
    assert previous.closed and cc._ENVS['claude-code'] is envs


@pytest.mark.parametrize('replacement', [False, True])
def test_own_container_close_never_evicts_another_legs_replacement(envs, replacement):
    own = _Env(_Container('running'))
    cc._ENVS['claude-code'] = envs if replacement else own
    cc.close_env('claude-code', own)
    assert own.closed
    if replacement:
        assert cc._ENVS['claude-code'] is envs and not envs.closed
    else:
        assert 'claude-code' not in cc._ENVS


def test_failed_native_close_retains_run_level_cleanup_backstop(envs, monkeypatch):
    def fail():
        raise RuntimeError('own close failed')
    monkeypatch.setattr(envs, 'close', fail)
    cc._ENVS['claude-code'] = envs
    with pytest.raises(RuntimeError, match='own close failed'):
        cc.close_env('claude-code', envs)
    assert cc._ENVS['claude-code'] is envs


def test_resumed_workspace_binding_keeps_exact_host_mount_for_nested_account(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    home = workspace / '.collrun' / 'homes' / 'claude-code'
    home.mkdir(parents=True)
    account = tmp_path / 'selected-account'
    account.mkdir()
    monkeypatch.setattr(cc.gen, 'workspace_path', lambda: str(workspace))
    bindings = {str(home): {'bind': '/home/coder/.claude', 'mode': 'rw'},
                str(account): {'bind': '/home/coder/.claude/account', 'mode': 'ro'}}
    links, external = cc._split_bindings(bindings, mount=str(workspace))
    assert links == {str(home): bindings[str(home)]}
    assert external == {str(account): {'bind': str(home / 'account'), 'mode': 'ro'}}
    assert bindings[str(account)]['bind'] == '/home/coder/.claude/account'
