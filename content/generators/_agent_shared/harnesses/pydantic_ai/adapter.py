# SPDX-License-Identifier: MIT
"""pydantic-ai: Pydantic AI's own ACP adapter serving its Coder agent, a
process on this machine, on any provider or a custom endpoint; under docker:
full its shell runs in a Docker sandbox the host places."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Session, Tools, natives

ADAPTER = HarnessAdapter(
    id="pydantic-ai", label="Pydantic AI", locus="host",
    acp_argv=("python", "-I", "harnesses/pydantic_ai/server.py"),
    host_binary="", host_twin=False,
    unregistered="Pydantic AI's ACP adapter ships in pydantic-ai-harness and is not in the ACP registry (checked 2026-10-25)",
    auth=Auth(providers=natives("anthropic", "gemini", "openai", "groq", "cerebras",
                                "openrouter", "xai", "mistral"),
              custom_endpoint=True),
    model=Model(),
    # The instruction file in the harness home, outside the workspace the
    # sandbox mounts, which the server reads per session.
    instructions=Instructions("meta", "COLLIMATE_PAI_INSTRUCTIONS"),
    # Coder has no web tools: the bridge's web rows follow the grant.
    tools=Tools(web="none"),
    # The level is the gate; the docker grant picks where the shell runs: in a
    # Pydantic AI Harness Docker sandbox the host places (image/) under
    # docker: full, else on this machine.
    commands=Commands("Coder's own tools: no shell tool under deny, each shell call a "
                      "session/request_permission under ask, unattended under allow; under docker: full "
                      "the shell runs in a Docker sandbox, otherwise on this machine", sandboxed=True),
    # The saved ACP sessions, in the harness home outside the workspace (the
    # commands are sandboxed), kept in the cell between runs.
    session=Session(paths=("sessions",)),
    terminal=None,
    hooks=Hooks(),
    prepare="harnesses.pydantic_ai.backend:prepare",
    image="image",
    note="docker: full sandboxes its commands in a container with the workspace mounted, and is "
         "recommended for commands: allow; under docker: none they run on this machine.",
    preset_permissions={"docker": "none", "internet": "full", "commands": "deny"},
    summary="Pydantic AI's Coder agent behind its own ACP adapter, a process on this machine, on the "
            "Annotation surface; docker: full sandboxes its commands and is recommended for commands: allow. "
            "Any provider key, or a custom endpoint",
)
