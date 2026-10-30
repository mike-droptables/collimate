# SPDX-License-Identifier: MIT
"""Pure ACP adapter artifacts shared by host and container preparation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterArtifact:
    package: str
    executable: str
    fallback_version: str
    build_arg: str
    host_install: bool


ADAPTERS = {
    'claude-agent-acp': AdapterArtifact('@agentclientprotocol/claude-agent-acp',
        'claude-agent-acp', '0.97.0', '', True),
    'codex-acp': AdapterArtifact('@agentclientprotocol/codex-acp',
        'codex-acp', '2.3.0', 'CODEX_ACP_VERSION', True),
    'pi-acp': AdapterArtifact('pi-acp', 'pi-acp', '0.0.34', 'PI_ACP_VERSION', False),
}


@dataclass(frozen=True)
class PythonAdapter:
    """A host ACP adapter that is a Python program in an environment of its
    own (its dependencies conflict with the generator's), pinned exact:
    ``requirements`` are installed into a venv under Collimate's cache and
    ``module`` is the distribution whose version the preflight checks."""
    requirements: tuple[str, ...]
    module: str
    version: str


#: The pydantic-ai harness: Pydantic AI's ACP adapter (``experimental.acp``)
#: serving its ``Coder`` agent. Its ``acp`` extra pins the ACP SDK below the
#: generator's, so it cannot share the generator's environment.
PYTHON_ADAPTERS = {
    'pydantic-ai-harness': PythonAdapter(
        ('pydantic-ai-harness[acp,coder]==0.61.0',
         'pydantic-ai-slim[openai,anthropic,google,mcp]==2.61.0'),
        'pydantic-ai-harness', '0.61.0'),
}


# The in-container stdio↔HTTP MCP proxy every adapter image carries: plumbing,
# exact-pinned (cli_container._PINNED_PLUMBING), never floated.
MCP_REMOTE = {'mcp-remote': {'source': 'npm:mcp-remote', 'arg': 'MCP_REMOTE_VERSION'}}


def host_adapter(name: str) -> AdapterArtifact:
    """Only the declared host adapters may reach Collimate's npm installer."""
    artifact = ADAPTERS[name]
    if not artifact.host_install:
        raise KeyError(name)
    return artifact


def image_artifact(name: str) -> dict[str, str]:
    artifact = ADAPTERS[name]
    if not artifact.build_arg:
        raise ValueError(f'Adapter {name!r} has no container build argument')
    return {'source': 'npm:' + artifact.package, 'arg': artifact.build_arg}
