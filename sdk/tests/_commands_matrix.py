# SPDX-License-Identifier: MIT
"""Recording boundaries for the commands matrix; policy code stays real."""
from __future__ import annotations
from _agent_shared import harness_homes

import base64
import os
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests._agent_run import in_cell


class TerminalRecorded(BaseException):
    """Stop after the real leg has prepared and registered its terminal."""


class RecordingEnvironment:
    def __init__(self):
        self.container = self
        self.container_id = "matrix-container"
        self.mount = "/workspace"
        self.files = {}
        self.execs = []
        self.ensures = []
        self.spawn = {}
        self.terminal = {}

    def exec_run(self, argv, **kwargs):
        self.execs.append((list(argv), kwargs))
        script = argv[-1]
        for match in re.finditer(
            r"echo '?([A-Za-z0-9+/=]*)'? \| base64 -d > '?([^' ;&]+)'?", script
        ):
            self.files[match[2]] = base64.b64decode(match[1]).decode()
        return SimpleNamespace(exit_code=0, output=b"[]")

    def exec(self, argv, **kwargs):
        if argv == ["cline", "hub", "status"]:
            self.execs.append((list(argv), kwargs))
            return 0, '{"running": false}'
        result = self.exec_run(argv, **kwargs)
        return result.exit_code, result.output.decode()

    def read_file(self, path, **kwargs):
        return self.files.get(path, "").encode()

    def close(self):
        pass


@pytest.fixture
def recording_runtime(request, monkeypatch, tmp_path):
    from _agent_shared import cli_container as cc
    from _agent_shared.harnesses.claude_code import backend as ct
    from _agent_shared import login_store as ls
    import _env_shared
    import gen

    harness_homes.close_harness_homes()
    ls.close_account_credentials()
    in_cell(request, monkeypatch, tmp_path / "workspace", "matrix-cell")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    env = RecordingEnvironment()

    async def ensure(name, **kwargs):
        env.ensures.append((name, kwargs))
        env.mount = kwargs.get("mount", "/workspace")
        return env

    async def key(*args, **kwargs):
        return "matrix-fake-key"

    async def update(*args, **kwargs):
        return None

    async def container_spawn(backend, target, argv, **kwargs):
        env.spawn = {"backend": backend, "argv": list(argv), **kwargs}

    async def host_spawn(backend, argv, **kwargs):
        # A descriptor the child alone inherits (pydantic-ai's key pipe) is
        # read here, as the child would: it is closed once the spawn returns.
        inherited = {fd: os.read(fd, 65536).decode() for fd in kwargs.get("pass_fds") or ()}
        env.spawn = {"backend": backend, "argv": list(argv), "inherited": inherited, **kwargs}

    def terminal(container_id, command, **kwargs):
        env.terminal = {"container_id": container_id, "argv": list(command), **kwargs}
        raise TerminalRecorded()

    @asynccontextmanager
    async def bridge(**kwargs):
        yield None

    monkeypatch.setattr(cc, "ensure_env", ensure)
    monkeypatch.setattr(cc, "leg_bridge", bridge)
    monkeypatch.setattr(ct, "spawn_host_adapter", host_spawn)
    from _agent_shared.harnesses.opencode import backend as oc
    monkeypatch.setattr(oc, "spawn_container_adapter", container_spawn)
    monkeypatch.setattr(oc, "spawn_host_adapter", host_spawn)
    from _agent_shared.harnesses.goose import backend as gb
    monkeypatch.setattr(gb, "spawn_container_adapter", container_spawn)
    monkeypatch.setattr(gb, "spawn_host_adapter", host_spawn)
    monkeypatch.setattr(ct, "resolve_claude_adapter_version", lambda: "0.70.0")
    monkeypatch.setattr(ct, "ensure_claude_adapter", lambda version: "/fake/claude-acp")
    from _agent_shared import host_adapters as ha
    monkeypatch.setattr(ha, "resolve_adapter_version", lambda *args: "1.0")
    monkeypatch.setattr(ha, "ensure_adapter", lambda *args: "/fake/adapter")
    from _agent_shared.harnesses.antigravity import backend as ab
    monkeypatch.setattr(ab, "spawn_container_adapter", container_spawn)
    from _agent_shared.harnesses.qwen_code import backend as qb
    monkeypatch.setattr(qb, "spawn_container_adapter", container_spawn)
    from _agent_shared.harnesses.cline import backend as clb
    monkeypatch.setattr(clb, "spawn_container_adapter", container_spawn)
    from _agent_shared.harnesses.pi import backend as pb
    monkeypatch.setattr(pb, "spawn_container_adapter", container_spawn)
    from _agent_shared.harnesses.codex import backend as cb
    monkeypatch.setattr(cb, "spawn_container_adapter", container_spawn)
    monkeypatch.setattr(cb, "spawn_host_adapter", host_spawn)
    for name in ("hermes", "vibe", "copilot"):
        import importlib
        module = importlib.import_module(f"_agent_shared.harnesses.{name}.backend")
        monkeypatch.setattr(module, "spawn_container_adapter", container_spawn)
    from _agent_shared import host_adapters
    monkeypatch.setattr(host_adapters, "host_node_bin_dir", lambda: "")
    monkeypatch.setattr(ct, "claude_managed_rules_only", lambda: False)
    monkeypatch.setattr(gen, "commit", update)
    from _agent_shared.harnesses.pydantic_ai import backend as pyb
    monkeypatch.setattr(pyb, "spawn_host_adapter", host_spawn)
    from _agent_shared import accounts
    monkeypatch.setattr(accounts, "model_account", lambda: {
        "kind": "model", "service": "openrouter", "protocol": "openai-compatible",
        "model": "openai/gpt-4.1-mini"})
    monkeypatch.setattr(pyb, "ensure_python_adapter", lambda name: "/fake/pydantic-ai/python")
    monkeypatch.setattr(gen, "serve_terminal", terminal)
    for name in ("phase", "log", "update_file"):
        monkeypatch.setattr(gen, name, lambda *args, **kwargs: None)
    from _agent_shared.harnesses import pipeline
    monkeypatch.setattr(pipeline, "account_api_key", key)
    monkeypatch.setattr(_env_shared, "DockerEnvironment", lambda **kwargs: env)
    monkeypatch.setattr(cc, "image_spec", lambda *args: {})
    monkeypatch.setattr(cc, "stage_workspace_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(cc, "require_writable_bindings", lambda *args, **kwargs: None)
    monkeypatch.setattr(cc, "register_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(ct, "pull_claude_transcript", lambda *args, **kwargs: ([], None))
    yield env
    ls.close_account_credentials()
    harness_homes.close_harness_homes()
