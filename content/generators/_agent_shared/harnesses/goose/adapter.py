# SPDX-License-Identifier: MIT
"""goose: the AAIF agent, ACP-native, in its container or on this machine."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Native, Session, Terminal, Tools, natives
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="goose", label="goose", locus="container", acp_argv=("goose", "acp"),
    host_binary="goose", host_twin=True,
    # goose calls Gemini ``google`` and reads its key from GOOGLE_API_KEY.
    auth=Auth(providers=natives("anthropic", ("gemini", Native("google", "GOOGLE_API_KEY")),
                                "openai", "groq", "cerebras", "openrouter")),
    model=Model(),
    instructions=Instructions("container_file", "~/.config/goose/.goosehints"),
    tools=Tools(),
    commands=Commands("the EXTENSIONS allowlist and GOOSE_MODE (approve under ask)",
                      mode_rule="acp_policy:_goose_mode_allowed",
                      recognisers=("acp_policy:_goose_shell",)),
    session=Session(paths=("sessions", "sessions.db", "share/goose/sessions"),
                    verify_loaded="harnesses.goose.backend:verify_loaded"),
    terminal=Terminal("B", "harnesses.goose.backend:terminal_context", True, "Agent · Goose",
                      tail="terminal_tail:goose_tail"),
    hooks=Hooks(host_commands_level="harnesses.goose.backend:host_commands_level"),
    prepare="harnesses.goose.backend:prepare",
    image="image",
    artifacts={"goose": {"source": "github:aaif-goose/goose", "arg": "GOOSE_VERSION"}, **MCP_REMOTE},
    registry_id="goose",
    summary="goose (AAIF) in its own container (or this machine's goose under docker: none), on the "
            "Annotation surface. Anthropic, OpenAI, Gemini, Groq, Cerebras or OpenRouter key",
)
