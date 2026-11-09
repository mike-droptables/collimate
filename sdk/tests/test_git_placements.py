# SPDX-License-Identifier: MIT
"""Each place a run's git runs gets the bound Git account the host put in the
run's environment: the Agent's host leg, its container leg, its ACP terminal
exec, Copilot's container, the Agent's terminal on this machine, Launch's
oneshot command and terminal, and the editor of Launch's app. Real git, given
the environment each leg hands its child (plus the PATH and HOME a container
has of its own; for the app, the HOME its one exec wrote), finds the header
under the account's base (``get-urlmatch``), commits as its author and
authenticates ``ls-remote`` against a ``git http-backend`` that answers 401
without it."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import stat
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from collimate import git_hosts
from _agent_shared import accounts, acp_client, terminal_runner
from _agent_shared.harnesses.copilot import backend as copilot
from tests._git_fixtures import carry_account
from tests._workspace import GENERATORS


class _Spawned(Exception):
    """Stops a spawn once its environment is captured."""


@pytest.fixture
def placed(http_git, monkeypatch, tmp_path):
    """The served repository's URL with the http account carried as the
    host carries a bound one, and the Agent's session reset."""
    url = http_git.seed("base/app.git")
    carry_account(monkeypatch, http_git.account)
    accounts.forget_git_session()
    monkeypatch.setattr(accounts, "_git", http_git.account)
    monkeypatch.setattr(accounts, "_old_git_told", True)
    monkeypatch.setattr(acp_client, "register_adapter_log", lambda backend: str(tmp_path / "adapter.log"))
    try:
        yield url
    finally:
        accounts.forget_git_session()


def _git(env: dict, home, *args) -> subprocess.CompletedProcess:
    run_env = {"PATH": os.environ["PATH"], "HOME": str(home), "LC_ALL": "C", **env}
    return subprocess.run([shutil.which("git"), *args], env=run_env, cwd=home, capture_output=True, text=True,
                          timeout=60)


def _assert_authenticates(env: dict, url: str, http_git, tmp_path, home=None) -> None:
    if home is None:
        home = tmp_path / f"home-{len(list(tmp_path.iterdir()))}"
        home.mkdir()
    header = f"Authorization: Basic {http_git.account.authorization()}"
    assert _git(env, home, "config", "--get-urlmatch", "http.extraHeader", url).stdout.strip() == header
    assert _git(env, home, "config", "--get-urlmatch", "http.extraHeader", http_git.url("other/app.git")).stdout == ""
    assert _git(env, home, "var", "GIT_AUTHOR_IDENT").stdout.startswith("Ada Lovelace <ada@example.org> ")
    listed = _git(env, home, "ls-remote", url)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.rstrip().endswith("refs/heads/main")
    assert http_git.seen[-1] == f"Basic {http_git.account.authorization()}"


def _without_account(env: dict, url: str, tmp_path) -> None:
    """The same leg's environment without the block meets the 401."""
    bare = {k: v for k, v in env.items() if not k.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY", "GIT_CONFIG_VALUE"))}
    home = tmp_path / "bare-home"
    home.mkdir(exist_ok=True)
    refused = _git({**bare, "GIT_TERMINAL_PROMPT": "0"}, home, "ls-remote", url)
    assert refused.returncode != 0


@pytest.mark.parametrize("commands", ["deny", "allow"])
def test_the_agents_host_leg(placed, http_git, monkeypatch, tmp_path, commands):
    seen: dict = {}

    async def create_subprocess_exec(*argv, env, **kwargs):
        seen["env"] = env
        raise _Spawned

    monkeypatch.setattr(accounts, "_commands", commands)
    monkeypatch.setattr(acp_client.asyncio, "create_subprocess_exec", create_subprocess_exec)
    with pytest.raises(_Spawned):
        asyncio.run(acp_client.spawn_host_adapter("claude-code", ["adapter"],
                                                  env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                                                  cwd=str(tmp_path)))
    env = seen["env"]
    assert git_hosts.git_config_entries(env)[-1] == ("credential.helper", "")
    _assert_authenticates(env, placed, http_git, tmp_path)
    _without_account(env, placed, tmp_path)


def test_the_agents_container_leg(placed, http_git, monkeypatch, tmp_path):
    seen: dict = {}

    def attached_exec(container_id, argv, pidfile, *, stdin, environment, workdir):
        seen["env"] = environment
        raise _Spawned

    monkeypatch.setattr(acp_client, "_attached_exec", attached_exec)
    with pytest.raises(_Spawned):
        asyncio.run(acp_client.spawn_container_adapter("goose", SimpleNamespace(container_id="c1"), ["adapter"],
                                                       env_vars={"PATH": "/usr/bin"}))
    assert git_hosts.git_config_entries(seen["env"])[-1] == ("safe.directory", "*")
    assert not {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"} & seen["env"].keys()
    _assert_authenticates(seen["env"], placed, http_git, tmp_path)
    _without_account(seen["env"], placed, tmp_path)


def test_the_agents_acp_terminal_exec(placed, http_git, monkeypatch, tmp_path):
    seen: dict = {}

    def attached_exec(container_id, argv, pidfile, *, stdin, environment, workdir):
        seen["env"] = environment
        raise _Spawned

    monkeypatch.setattr(acp_client, "_attached_exec", attached_exec)
    session = SimpleNamespace(backend="goose", agent_cwd="/workspace",
                              profile=SimpleNamespace(env_obj=SimpleNamespace(container_id="c1")))
    term = acp_client._CommandTerminal("t1", byte_limit=1024)
    with pytest.raises(_Spawned):
        asyncio.run(acp_client._spawn_container_terminal(session, term, ["git", "ls-remote"],
                                                         {"GIT_CONFIG_COUNT": "0"}, None))
    assert ("safe.directory", "*") in git_hosts.git_config_entries(seen["env"])
    _assert_authenticates(seen["env"], placed, http_git, tmp_path)


def test_copilots_container(placed, http_git, monkeypatch, tmp_path):
    seen: dict = {}

    def attached_exec(container_id, argv, pidfile, *, stdin, environment, workdir):
        seen["env"] = environment
        raise _Spawned

    async def ensure_env(*args, **kwargs):
        return SimpleNamespace(container_id="c")

    import _agent_shared.cli_container as cli_container
    monkeypatch.setattr(cli_container, "ensure_env", ensure_env)
    monkeypatch.setattr(copilot, "harness_home", lambda backend: str(tmp_path))
    monkeypatch.setattr(copilot, "write_native_file", lambda root, name, body: None)
    monkeypatch.setattr(acp_client, "_attached_exec", attached_exec)
    ctx = {"credentials": SimpleNamespace(plan=None, key="sk", login=""), "model": "m", "commands": "ask",
           "provider": "openai", "websearch": False}
    prepared = asyncio.run(copilot.prepare(ctx, leg="acp"))
    with pytest.raises(_Spawned):
        asyncio.run(prepared.profile.spawn())
    argv = list(prepared.argv)
    names = argv[argv.index("--secret-env-vars") + 1].split(",")
    assert [n for n in names if n.startswith("GIT_")] == [
        n for n, v in seen["env"].items() if http_git.account.authorization() in v]
    _assert_authenticates(seen["env"], placed, http_git, tmp_path)


@pytest.mark.parametrize("locus", ["host", "container"])
def test_the_agents_terminal(placed, http_git, monkeypatch, tmp_path, locus):
    """What the terminal runner hands serve_terminal for a CLI on this
    machine and in its container."""
    from _agent_shared.harnesses import Terminal, pipeline
    seen: dict = {}
    launch = terminal_runner.TerminalLaunch(None if locus == "host" else "c1", ("cli",), str(tmp_path),
                                            {"PATH": os.environ["PATH"], "TERM": "xterm-256color"}, "CLI", [],
                                            "ready", "")

    @asynccontextmanager
    async def native_context(prepared, request, bridge):
        yield launch

    @asynccontextmanager
    async def serve_terminal(container_id, argv, *, env, **kwargs):
        seen["env"] = env
        raise _Spawned
        yield

    async def prepare(row, cfg, **kwargs):
        return SimpleNamespace()

    async def commit(**kwargs):
        return None

    facet = Terminal(tier="C", native_context="fake:native", bridge_enabled=False, default_label="CLI", locus=locus)
    monkeypatch.setattr(accounts, "_commands", "ask")
    monkeypatch.setattr(terminal_runner, "HARNESS", {"claude-code": SimpleNamespace(id="claude-code", terminal=facet)})
    monkeypatch.setattr(terminal_runner, "resolve", lambda target: native_context)
    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(terminal_runner.gen, "commit", commit)
    monkeypatch.setattr(terminal_runner.gen, "phase", lambda *args: None)
    monkeypatch.setattr(terminal_runner.gen, "serve_terminal", serve_terminal)
    with pytest.raises(_Spawned):
        asyncio.run(terminal_runner.run_terminal({"backend": "claude-code"}, pointer="own.terminal"))
    env = seen["env"]
    assert git_hosts.git_config_entries(env)[-1] == (
        ("credential.helper", "") if locus == "host" else ("safe.directory", "*"))
    _assert_authenticates(env, placed, http_git, tmp_path)
    _without_account(env, placed, tmp_path)


def _launch():
    if str(GENERATORS) not in sys.path:
        sys.path.insert(0, str(GENERATORS))
    spec = importlib.util.spec_from_file_location("_launch_placement_under_test",
                                                  GENERATORS / "launch" / "launch" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launchs_oneshot_command_and_terminal(placed, http_git, tmp_path):
    """Oneshot's command exec and the terminal's docker exec run with
    _git_env (wired in test_run_generator)."""
    launch = _launch()
    for env in (launch._git_env(), launch._git_env({"TERM": "xterm-256color"})):
        assert ("safe.directory", "*") in git_hosts.git_config_entries(env)
        _assert_authenticates(env, placed, http_git, tmp_path)


class _Here:
    """Launch's container execs, run on this machine with ``home`` as the
    editor user's HOME."""

    def __init__(self, home) -> None:
        self.home = home

    def exec(self, command, *, cwd=None, env=None, timeout=None, user=None):
        done = subprocess.run(command, env={"PATH": os.environ["PATH"], "HOME": str(self.home), **(env or {})},
                              capture_output=True, timeout=60)
        return done.returncode, done.stdout + done.stderr


def test_launchs_app_editor(placed, http_git, monkeypatch, tmp_path):
    """The app's editor started with the container's environment, which
    holds no secret; the login execs (_configure_git, _install_header) give
    its user the author and the header file its global config includes."""
    launch = _launch()
    logs: list = []
    monkeypatch.setattr(launch, "gen", SimpleNamespace(log=logs.append))
    home = tmp_path / "coder"
    home.mkdir()
    launch._configure_git(_Here(home), account=http_git.account, user="coder")
    launch._install_header(_Here(home), account=http_git.account, user="coder")
    assert not any("not configured" in line for line in logs), logs
    assert stat.S_IMODE((home / ".collimate_git_config").stat().st_mode) == 0o600
    assert http_git.account.authorization() not in (home / ".gitconfig").read_text()
    _assert_authenticates({}, placed, http_git, tmp_path, home=home)
    _without_account({}, placed, tmp_path)
