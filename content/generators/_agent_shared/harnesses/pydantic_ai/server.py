# SPDX-License-Identifier: MIT
"""The pydantic-ai harness: Pydantic AI's own ACP adapter serving its `Coder`
agent, as Collimate runs it.

A standalone program, run by the interpreter of the harness's own environment
(``host_adapters.ensure_python_adapter``: ``pydantic-ai-harness[acp,coder]``
pinned exact, whose ACP SDK differs from the one the Agent generator speaks),
so it imports nothing from the generator. Everything a run decides arrives in
the environment, read once at start:

* ``COLLIMATE_PAI_WIRE`` — ``anthropic``, ``gemini``, ``openai`` (chat
  completions) or ``openai-responses``; ``COLLIMATE_PAI_ENDPOINT`` the base
  URL the provider registry (or the account's custom endpoint) names;
  ``COLLIMATE_PAI_MODEL`` the model id; ``COLLIMATE_PAI_KEY_FD`` an inherited
  pipe holding the key, read once at start so the key is on no disk and in
  neither this process's environment nor its commands'; ``COLLIMATE_PAI_STRICT_TOOLS``
  ``0`` for a provider that refuses strict tool schemas (Cerebras);
* ``COLLIMATE_PAI_COMMANDS`` — the commands level, the gate (``policy.py``):
  ``deny`` gives no shell tool, ``ask`` makes every shell call wait for the
  client's approval (``session/request_permission``), ``allow`` runs it.
  ``COLLIMATE_PAI_SANDBOX`` — the id of the run's sandbox container under
  ``docker: full``: Coder's workspace (its shell and its file tools) is then
  that container, a Pydantic AI Harness ``DockerSandbox`` workspace attached
  to the container the host placed (the run workspace mounted at
  ``/workspace``); without one the workspace is the run directory on this
  machine. ``COLLIMATE_PAI_FILE_TOOLS`` ``0`` removes Coder's file tools;
* ``COLLIMATE_PAI_INSTRUCTIONS`` — the file holding the run's system prompt,
  read for every session, and ``COLLIMATE_PAI_SESSIONS`` the directory each
  session is saved in (so ``session/load`` reopens it after the process is
  gone): both in the harness home, which is outside the workspace the
  sandbox container mounts.

The client's MCP servers (Collimate's tool bridge, the cell's own) become
toolsets prefixed with their names, beside Coder's own tools. Delegation
(``delegate_task``) is on only under ``allow``: a delegate runs outside this
session's approval flow.
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path

# The shared grant rules beside this file (the interpreter runs isolated, -I).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import policy  # noqa: E402

from pydantic_ai_harness.experimental import HarnessExperimentalWarning

warnings.filterwarnings("ignore", category=HarnessExperimentalWarning)

from acp import schema  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.capabilities import AbstractCapability, Capability, PrepareTools  # noqa: E402
from pydantic_ai.mcp import MCPToolset  # noqa: E402
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef  # noqa: E402
from pydantic_ai_harness.docker_sandbox import DockerSandbox  # noqa: E402
from pydantic_ai_harness.coder import FILE_TOOL_NAMES, Coder  # noqa: E402
from pydantic_ai_harness.experimental.acp import (  # noqa: E402
    AcpSession, AcpSessionConfig, StoredSession, run_acp_stdio_sync,
)

SHELL_TOOL = policy.SHELL_TOOL
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _read_key() -> str:
    """The key from the pipe the generator handed this process, read once."""
    fd = os.environ.pop("COLLIMATE_PAI_KEY_FD", "")
    if not fd:
        return ""
    with os.fdopen(int(fd), "rb") as handle:
        return handle.read().decode("utf-8").strip()


def build_model(wire: str, endpoint: str, model: str, key: str, *, strict_tools: bool = True):
    """The Pydantic AI model for one provider connection, by its wire."""
    if wire in ("openai", "openai-responses"):
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
        from pydantic_ai.providers.openai import OpenAIProvider
        provider = OpenAIProvider(base_url=endpoint, api_key=key or "unauthenticated")
        cls = OpenAIResponsesModel if wire == "openai-responses" else OpenAIChatModel
        profile: dict = {}
        if not strict_tools:
            profile["openai_supports_strict_tool_definition"] = False
        if cls is OpenAIChatModel:
            # One leading system message: Coder's prompt and the run's
            # instructions merged. An open-weight model's chat template can
            # refuse a second one (Cerebras's qwen-3.8-27b: "System message
            # must be at the beginning"); a single one every provider takes.
            profile["openai_chat_supports_multiple_system_messages"] = False
        return cls(model, provider=provider, profile=profile or None)
    if wire == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider
        return AnthropicModel(model, provider=AnthropicProvider(base_url=endpoint.removesuffix("/v1"),
                                                                api_key=key))
    if wire == "gemini":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider
        return GoogleModel(model, provider=GoogleProvider(
            api_key=key, base_url=endpoint.removesuffix("/v1beta").removesuffix("/v1")))
    raise ValueError(f"unsupported model wire {wire!r}")


class _AskBeforeShell(AbstractCapability):
    """``commands: ask``: every shell call pauses for the client's approval,
    before the tool body runs."""

    def get_wrapper_toolset(self, toolset):
        return toolset.approval_required(lambda ctx, tool_def, args: tool_def.name == SHELL_TOOL)


def build_agent(model, *, commands: str, file_tools: bool) -> Agent:
    """Coder over the commands level: a shell tool above deny
    (``policy.shell_offered``), the approval wrapper under ask, the file
    tools unless they are off. Where the shell runs is the session's
    workspace (:func:`session_workspace`)."""
    shell = policy.shell_offered(commands)
    removed = set() if shell else {SHELL_TOOL}
    if not file_tools:
        removed.update(FILE_TOOL_NAMES)

    async def hide(ctx, tool_defs):
        return [t for t in tool_defs if t.name not in removed]

    capabilities: list = [Coder(sub_agents=policy.delegation_offered(commands))]
    if removed:
        capabilities.append(PrepareTools(hide))
    if shell and commands == "ask":
        capabilities.append(_AskBeforeShell())
    return Agent(model, name="pydantic-ai", capabilities=capabilities)


def session_workspace(cwd: str, sandbox: str):
    """The workspace Coder works in: the sandbox container the host placed
    (attached through Pydantic AI Harness's ``DockerSandbox``, by the label its
    image carries), else the run directory on this machine."""
    if sandbox:
        return DockerSandbox(working_dir=policy.SANDBOX_MOUNT).backend(WorkspaceRef(provider="docker", id=sandbox))
    return LocalWorkspaceBackend(cwd)


class FileSessionStore:
    """One JSON file per session under ``root`` (owner-only), so a session
    survives the process: ``session/load`` reopens it."""

    _ADAPTER = TypeAdapter(StoredSession)

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, session_id: str) -> Path:
        if not _SAFE_ID.match(session_id):
            raise ValueError(f"invalid session id {session_id!r}")
        return self.root / f"{session_id}.json"

    async def save(self, session_id: str, session: StoredSession) -> None:
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(self._ADAPTER.dump_json(session))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    async def load(self, session_id: str) -> "StoredSession | None":
        path = self._path(session_id)
        if not path.is_file():
            return None
        return self._ADAPTER.validate_json(path.read_bytes())


def _instructions(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip() if path else ""
    except FileNotFoundError:
        return ""


def _mcp_toolsets(servers) -> list:
    """The client's MCP servers as toolsets, each prefixed with its name so
    its tools never collide with Coder's own (``collimate_list_files``)."""
    toolsets = []
    for server in servers:
        if isinstance(server, (schema.HttpMcpServer, schema.SseMcpServer)):
            headers = {h.name: h.value for h in server.headers or []}
            toolset = MCPToolset(server.url, headers=headers or None, id=f"mcp_{server.name}")
        elif isinstance(server, schema.McpServerStdio):
            from fastmcp.client.transports import StdioTransport
            env = {e.name: e.value for e in server.env or []}
            toolset = MCPToolset(StdioTransport(command=server.command, args=list(server.args or []),
                                                env=env or None), id=f"mcp_{server.name}")
        else:
            continue
        toolsets.append(toolset.prefixed(re.sub(r"[^A-Za-z0-9_]", "_", server.name)))
    return toolsets


def session_config(session: AcpSession) -> AcpSessionConfig:
    capabilities = []
    text = _instructions(os.environ.get("COLLIMATE_PAI_INSTRUCTIONS", ""))
    if text:
        capabilities.append(Capability(id="collimate_instructions", instructions=[text]))
    return AcpSessionConfig(deps=None, capabilities=capabilities, toolsets=_mcp_toolsets(session.mcp_servers),
                            workspace=session_workspace(session.cwd, os.environ.get("COLLIMATE_PAI_SANDBOX", "")))


def main() -> None:
    env = os.environ
    model = build_model(env["COLLIMATE_PAI_WIRE"], env.get("COLLIMATE_PAI_ENDPOINT", ""),
                        env["COLLIMATE_PAI_MODEL"], _read_key(),
                        strict_tools=env.get("COLLIMATE_PAI_STRICT_TOOLS", "1") != "0")
    agent = build_agent(model, commands=env.get("COLLIMATE_PAI_COMMANDS", "deny"),
                        file_tools=env.get("COLLIMATE_PAI_FILE_TOOLS", "1") != "0")
    sessions = env.get("COLLIMATE_PAI_SESSIONS", "")
    run_acp_stdio_sync(
        agent,
        name="pydantic-ai",
        version=json.loads(env.get("COLLIMATE_PAI_VERSION", '"0"')),
        session_config=session_config,
        session_store=FileSessionStore(sessions) if sessions else None,
        mcp_capabilities=schema.McpCapabilities(http=True, sse=True),
        prompt_capabilities=schema.PromptCapabilities(image=True, embedded_context=True),
    )


if __name__ == "__main__":
    main()
