# SPDX-License-Identifier: MIT
"""pydantic-ai: Pydantic AI's own ACP adapter serving its ``Coder`` agent,
as a process on this machine (``server.py``, in the adapter's own
environment: ``host_adapters.ensure_python_adapter``).

Collimate builds the model connection — the selected account's provider at
its registry endpoint, or the account's custom endpoint (the one harness that
takes one) — and hands it to the server in the environment, with the key on a
pipe the server alone inherits and reads at start. The commands level is
the gate (``policy.py``): ``deny`` gives Coder no shell tool, ``ask`` makes
each shell call a ``session/request_permission`` the client gate cards,
``allow`` runs it unattended. Under ``docker: full`` the host places the
run's sandbox container (``image/``, the workspace mounted at ``/workspace``
as for every harness container) and Coder's workspace is that container —
recommended, above all for ``allow``; otherwise the shell runs on this
machine in the run's workspace, as claude-code's chat does. Collimate's
tools reach it over the MCP bridge (``mcpServers``), and the harness home —
outside the workspace the sandbox mounts — holds the session store, which
the cell keeps between runs so ``session/load`` reopens a session, and the
system prompt's instruction file, read per session."""
from __future__ import annotations

import asyncio
import json
import os

import gen

from .. import CommandsPolicy, PreparedHarness
from . import policy
from ...acp_client import SpawnProfile, spawn_host_adapter
from ...adapter_artifacts import PYTHON_ADAPTERS
from ...host_adapters import ensure_python_adapter, host_env

ADAPTER_NAME = "pydantic-ai-harness"
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
INSTRUCTIONS_FILE = "instructions.md"
SESSIONS_DIR = "sessions"

# An account's wire protocol → the model class the server builds.
_WIRES = {"openai": "openai", "openai-compatible": "openai", "openai-chat": "openai",
          "openai-responses": "openai-responses", "anthropic": "anthropic", "gemini": "gemini"}


def model_wire(account: dict) -> str:
    """The wire the account's endpoint speaks (by its protocol, never the
    provider's name: an OpenRouter, Groq or Cerebras account is OpenAI-wire)."""
    protocol = str(account.get("protocol") or "")
    try:
        return _WIRES[protocol]
    except KeyError:
        raise ValueError(f"Unsupported model connection protocol {protocol!r}") from None


# The variables that select and reach a Docker daemon, which the sandbox
# workspace's ``docker`` client needs beside the replacing host env.
_DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


def server_env(*, wire: str, endpoint: str, model: str, key_fd: int, provider: str,
               commands: str, file_tools: bool, private: str, sandbox: str = "") -> dict:
    """The server's whole configuration (``server.py``'s docstring).
    ``private`` is the harness home, OUTSIDE the workspace (the sandbox
    mounts the workspace): the instruction file and the session store live
    there, and the key arrives on an inherited pipe (``key_fd``), never
    on disk."""
    return {
        "COLLIMATE_PAI_SANDBOX": sandbox,
        **({name: os.environ[name] for name in _DOCKER_ENV if os.environ.get(name)} if sandbox else {}),
        "COLLIMATE_PAI_WIRE": wire,
        "COLLIMATE_PAI_ENDPOINT": endpoint,
        "COLLIMATE_PAI_MODEL": model,
        "COLLIMATE_PAI_KEY_FD": str(key_fd),
        # Cerebras refuses OpenAI's strict tool schemas.
        "COLLIMATE_PAI_STRICT_TOOLS": "0" if provider == "cerebras" else "1",
        "COLLIMATE_PAI_COMMANDS": commands,
        "COLLIMATE_PAI_FILE_TOOLS": "1" if file_tools else "0",
        "COLLIMATE_PAI_INSTRUCTIONS": os.path.join(private, INSTRUCTIONS_FILE),
        "COLLIMATE_PAI_SESSIONS": os.path.join(private, SESSIONS_DIR),
        "COLLIMATE_PAI_VERSION": json.dumps(PYTHON_ADAPTERS[ADAPTER_NAME].version),
    }


def _key_pipe(key: str) -> int:
    """A pipe holding the key, its read end returned for the child to
    inherit: the server reads it once at start, and nothing is on disk."""
    read, write = os.pipe()
    try:
        os.write(write, key.encode("utf-8"))
    finally:
        os.close(write)
    return read


def private_dir() -> str:
    """This run's home for the server's files — the instruction file and the
    session store — which ``harness_homes`` keeps outside the workspace and
    every container mount (the adapter's commands are sandboxed), seeded from
    and copied back to the cell's session store."""
    from ...harness_homes import harness_home
    return harness_home("pydantic-ai")


def write_instructions(private: str, prompt: str) -> bool:
    """The session context in the harness home (removed when empty):
    the server reads it for every session."""
    path = os.path.join(private, INSTRUCTIONS_FILE)
    text = (prompt or "").strip()
    if not text:
        if os.path.exists(path):
            os.remove(path)
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    return True


async def prepare(ctx: dict, *, leg: str = "acp") -> PreparedHarness:
    """The host adapter's spawn profile for this run's selection and grant."""
    from ...accounts import model_account, model_endpoint
    from ...agent_config import resolve_model

    if leg != "acp":
        raise ValueError("pydantic-ai has no terminal")
    account = await asyncio.to_thread(model_account)
    endpoint = model_endpoint(account)
    model = ctx.get("model") or resolve_model("pydantic-ai", ctx["provider"], "", auth=ctx["auth"])
    python = await asyncio.to_thread(ensure_python_adapter, ADAPTER_NAME)
    private = await asyncio.to_thread(private_dir)
    written = await asyncio.to_thread(write_instructions, private, ctx.get("system_prompt") or "")
    commands = ctx["commands"]
    docker = gen.grant().docker
    notice = policy.host_shell_note(commands, docker)
    sandbox = ""
    if policy.wants_sandbox(commands, docker):
        # The host places the sandbox like any harness container (docker:
        # full allows it), the run workspace mounted at /workspace.
        from ...cli_container import ensure_env
        env_obj = await ensure_env("pydantic-ai", build_dir="pydantic-ai")
        sandbox = env_obj.container_id
    cwd = gen.workspace_path()
    base_env = await asyncio.to_thread(host_env)
    key = ctx["credentials"].key

    async def _spawn():
        # The key on a fresh pipe per spawn, inherited by the server alone.
        key_fd = _key_pipe(key)
        try:
            env = {**base_env, **server_env(
                wire=model_wire(account), endpoint=endpoint, model=model, key_fd=key_fd,
                provider=ctx["provider"], commands=commands,
                file_tools=bool(ctx.get("enable_file_tools", True)), private=private, sandbox=sandbox)}
            return await spawn_host_adapter("pydantic-ai", [python, "-I", SERVER], env=env, cwd=cwd,
                                            pass_fds=(key_fd,))
        finally:
            os.close(key_fd)

    profile = SpawnProfile(
        locus="host", cwd=cwd, spawn=_spawn, commands=commands, commands_notice=notice,
        adapter_version=PYTHON_ADAPTERS[ADAPTER_NAME].version,
        # The bridge on this machine's loopback: the server is a host process.
        bind_host="127.0.0.1",
        # The model is the server's own (no session config option to set).
        model_applied=True, model_value=model,
        # The home's instruction file carried the system prompt.
        fold_system_prompt=not written,
    )
    return PreparedHarness(CommandsPolicy(commands, {}, notice), profile, None, (python, SERVER), {}, model)
