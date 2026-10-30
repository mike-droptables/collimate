# SPDX-License-Identifier: MIT
"""Hermes Agent: NousResearch's agent, ACP-native (``hermes acp``), in its container."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Session, Terminal, Tools, natives
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="hermes", label="Hermes", locus="container", acp_argv=("hermes", "acp"),
    host_binary="hermes", host_twin=False,
    # Hermes's own provider ids; Groq, Cerebras and Mistral are named
    # OpenAI-compatible providers (``custom:<name>``) Collimate declares in
    # its config.
    auth=Auth(providers=natives("anthropic", "gemini", ("openai", "openai-api"),
                                ("groq", "custom:groq"), ("cerebras", "custom:cerebras"),
                                "openrouter", "xai", ("mistral", "custom:mistral"))),
    model=Model(),
    # $HERMES_HOME/SOUL.md: the global persona slot, read for every session
    # whatever the cwd.
    instructions=Instructions("home_file", "SOUL.md"),
    tools=Tools(),
    # Hermes asks only before the commands its own pattern list flags
    # dangerous, so "ask before every command" is not a level it can hold.
    commands=Commands("root-owned managed config (/etc/hermes): the terminal and code-execution "
                      "toolsets removed under deny, approvals off under allow",
                      levels=("deny", "allow"), mode_rule="acp_policy:_any_mode"),
    session=Session(paths=("state.db", "state.db-wal", "state.db-shm", "sessions")),
    terminal=Terminal("B", "harnesses.hermes.backend:terminal_context", True, "Agent · Hermes",
                      tail="terminal_tail:hermes_tail",
                      harness_session_ids="harnesses.hermes.backend:harness_session_ids"),
    hooks=Hooks(),
    prepare="harnesses.hermes.backend:prepare",
    image="image",
    artifacts={"hermes": {"source": "github:NousResearch/hermes-agent", "arg": "HERMES_VERSION"},
               **MCP_REMOTE},
    unregistered="Hermes Agent ships native ACP but is not in the ACP registry (checked 2026-10-25)",
    summary="NousResearch's Hermes Agent in its own container, on the Annotation surface. Anthropic, "
            "OpenAI, Gemini, Groq, Cerebras, OpenRouter, xAI or Mistral key",
)
