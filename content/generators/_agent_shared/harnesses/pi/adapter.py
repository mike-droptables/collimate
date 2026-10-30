# SPDX-License-Identifier: MIT
"""pi: the pi coding agent behind the third-party pi-acp adapter, in its container."""
from .. import (Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal,
                Tools, natives)
from ...adapter_artifacts import MCP_REMOTE, image_artifact

ADAPTER = HarnessAdapter(
    id="pi", label="pi", locus="container", acp_argv=("pi-acp",), host_binary="pi", host_twin=False,
    # pi calls Gemini ``google``; its ChatGPT plan is the oauth-only
    # ``openai-codex`` provider.
    auth=Auth(providers=natives("anthropic", ("gemini", "google"), "openai", "groq", "cerebras",
                                "openrouter"),
              plans={"chatgpt": "openai-codex", "xai": "xai"},
              login=Login("pi, then /login", ".pi/agent/auth.json", "credential_json", ("auth.json",),
                          "oauth_store", env="PI_CODING_AGENT_DIR", suffix="auth.json",
                          store_keys={"chatgpt": "openai-codex", "xai": "xai"})),
    model=Model(plan_defaults={"chatgpt": "gpt-5.5", "xai": "grok-4.6"},
                unchecked="pi refuses to open a session for an ID its list lacks"),
    # pi-acp has no instruction seam; the TTY takes --append-system-prompt.
    # The harness home's agent/collimate-context.md, which the wrapper appends
    # to pi's system prompt on the ACP leg (the terminal passes the flag).
    instructions=Instructions("home_file", "agent/collimate-context.md"),
    # pi-acp accepts but never wires session MCP servers: the bridge rides a
    # Collimate pi extension, and pi has no web tools.
    tools=Tools(collimate="native", cell_mcp=False, web="none"),
    commands=Commands("the root-owned wrapper's --exclude-tools and the gate extension",
                      mode_rule="acp_policy:_pi_mode_allowed",
                      recognisers=("acp_policy:_pi_confirm",)),
    session=Session(paths=("agent/sessions",)),
    terminal=Terminal("C", "harnesses.pi.backend:terminal_context", True, "Agent · pi"),
    hooks=Hooks(),
    prepare="harnesses.pi.backend:prepare",
    image="image",
    artifacts={"pi-acp": image_artifact("pi-acp"),
               "pi-coding-agent": {"source": "npm:@earendil-works/pi-coding-agent",
                                   "arg": "PI_CODING_AGENT_VERSION"},
               **MCP_REMOTE,
               "pi-mcp-sdk": {"source": "npm:@modelcontextprotocol/sdk", "arg": "MCP_SDK_VERSION"}},
    adapter_artifact="pi-acp", registry_id="pi-acp",
    summary="The pi coding agent in its own container, on the Annotation surface. Provider key, or your "
            "ChatGPT or SuperGrok subscription",
)
