# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "pyyaml",
#   # The pydantic-ai harness runs in an environment of its own
#   # (host_adapters.ensure_python_adapter): no model SDK is the generator's.
#   "agent-client-protocol==0.12.1",
#   "docker",
#   # The native harnesses use MCP2026-07-28. Keep the tested server/client
#   # pair together: MCPServer supports that protocol and earlier peers.
#   "mcp==2.3.0",
#   "fastmcp==4.1.3",
#   "uvicorn",
#   "starlette",
#   "httpx",
#   # Floor on the model SDKs' HTTP stack (httpx2 pins its httpcore2 twin):
#   # httpcore2 < 2.13 closes an abandoned response stream through a
#   # generator-based context manager that fails at event-loop teardown: a
#   # user Stop mid-reply prints "generator didn't stop after athrow()"
#   # tracebacks into the run log.
#   "httpx2>=2.13",
#   "trafilatura",
#   "ddgs",
# ]
# ///
"""Agent: the Agent cell's one generator.

A session over a selectable harness on the surface its ``surface`` setting
picks: annotations (the user's messages, answered in place), a chat, or the
harness's own TTY (CLI). The runtime is ``_agent_shared.session``.
"""
import os
import sys

import gen

# The shared _* cell folders (_agent_shared, _env_shared) are SIBLINGS of
# this generator's folder at run time (materialised into the cell); in the
# repo/tests they live at the asset root, one level above the cell folder.
# Insert both.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))
from _agent_shared import session  # noqa: E402


async def run():
    """The run: the session over this run's surface."""
    await session.run(here=_here)


if __name__ == "__main__":
    gen.main(run)
