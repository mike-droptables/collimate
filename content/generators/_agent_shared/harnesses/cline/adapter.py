# SPDX-License-Identifier: MIT
"""Cline: the Cline CLI, ACP-native, in its container."""
from .. import (Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal,
                Tools, natives)
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="cline", label="Cline", locus="container", acp_argv=("cline", "--acp"),
    host_binary="cline", host_twin=False,
    # Cline's models.dev provider ids match Collimate's; the ChatGPT plan is
    # its oauth-only ``openai-codex`` provider.
    auth=Auth(providers=natives("anthropic", "gemini", "openai", "groq", "cerebras", "openrouter"),
              plans={"chatgpt": "openai-codex"},
              login=Login("cline auth openai-codex", ".cline/data/providers.json", "credential_json",
                          ("chatgpt", "providers.json"), "cline_providers",
                          env="CLINE_PROVIDER_SETTINGS_PATH")),
    # Under the plan cline resolves its own default from the live catalogue.
    model=Model(plan_defaults={"chatgpt": ""}, verify=True,
                unchecked="cline silently runs the provider's default instead of an ID its catalogue lacks"),
    instructions=Instructions("fold"),
    # cline ignores ACP mcpServers: the bridge rides its own MCP settings
    # file, and the cell's declared servers cannot be attached; it has no web
    # off-switch, so a fetch is refused at the permission gate instead.
    tools=Tools(collimate="native", cell_mcp=False, web="refusal"),
    commands=Commands("root-owned global settings disabledTools and the auto-approve switch",
                      mode_rule="acp_policy:_cline_mode_allowed"),
    session=Session(paths=("data/tasks", "tasks")),
    terminal=Terminal("C", "harnesses.cline.backend:terminal_context", True, "Agent · Cline",
                      lifetime_context="harnesses.cline.backend:terminal_lifetime"),
    hooks=Hooks(),
    prepare="harnesses.cline.backend:prepare",
    image="image",
    artifacts={"cline": {"source": "npm:cline", "arg": "CLINE_VERSION"}, **MCP_REMOTE},
    registry_id="cline",
    note="With commands allow, permitted commands can read the provider key from the harness process environment.",
    summary="Cline in its own container, on the Annotation surface. Provider key, or your ChatGPT "
            "subscription",
)
