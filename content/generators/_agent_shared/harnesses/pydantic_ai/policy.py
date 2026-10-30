# SPDX-License-Identifier: MIT
"""The pydantic-ai harness's grant rules, with no imports: read by the
generator (``backend.py``) and by the ACP server in its own environment
(``server.py``), so both sides decide the same way.

The commands level is the gate: ``deny`` gives Coder no shell tool, ``ask``
makes each shell call wait for the client's approval, ``allow`` runs it
unattended. The docker grant decides only where the shell runs: under
``docker: full`` in the run's sandbox container (a Pydantic AI Harness Docker
workspace the host placed, the run workspace mounted at ``/workspace``) —
recommended, above all for ``allow`` — otherwise on this machine, in the run
directory, as claude-code's chat does."""
from __future__ import annotations

SHELL_TOOL = "shell"
#: Where the sandbox mounts the run workspace — as every harness container does.
SANDBOX_MOUNT = "/workspace"
HOST_SHELL_NOTE = ("pydantic-ai runs commands on this machine under docker: none; docker: full sandboxes them "
                   "in a container and is recommended for commands: allow")


def shell_offered(commands: str) -> bool:
    """Coder has a shell tool above deny."""
    return commands in ("ask", "allow")


def wants_sandbox(commands: str, docker: str) -> bool:
    """Whether the run's shell runs in its sandbox container: a shell, under
    ``docker: full``."""
    return shell_offered(commands) and docker == "full"


def host_shell_note(commands: str, docker: str) -> str:
    """The notice a run whose shell runs on this machine carries."""
    return HOST_SHELL_NOTE if shell_offered(commands) and docker != "full" else ""


def delegation_offered(commands: str) -> bool:
    """``delegate_task`` only under allow: a delegate runs outside the
    session's approvals."""
    return commands == "allow"
