# SPDX-License-Identifier: MIT
"""opencode: sst's agent, ACP-native, in its container or on this machine."""
from .. import (Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal,
                Tools, natives)
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="opencode", label="opencode", locus="container", acp_argv=("opencode", "acp"),
    host_binary="opencode", host_twin=True,
    auth=Auth(providers=natives("anthropic", ("gemini", "google"), "openai", "groq", "cerebras", "openrouter"),
              plans={"chatgpt": "openai", "github_copilot": "github-copilot", "xai": "xai"},
              login=Login("opencode auth login", ".local/share/opencode/auth.json", "credential_json",
                          ("auth.json",), "oauth_store", env="XDG_DATA_HOME", suffix="opencode/auth.json",
                          store_keys={"chatgpt": "openai", "github_copilot": "github-copilot", "xai": "xai"}),
              plan_default_for=("xai",)),
    # Under OAuth the openai catalogue is filtered to the plan's models;
    # grok-4.6 is opencode's own xAI default.
    model=Model(plan_defaults={"chatgpt": "gpt-5.5", "xai": "grok-4.6"}),
    instructions=Instructions("config_list", "~/.config/opencode/collimate-context.md"),
    tools=Tools(),
    commands=Commands("the root-owned global permission.bash rule, snapshots off and a pinned git env",
                      mode_rule="acp_policy:_opencode_mode_allowed",
                      recognisers=("acp_policy:_opencode_shell_directory",)),
    session=Session(paths=("storage", "opencode.db", "data/opencode/storage", "data/opencode/opencode.db")),
    terminal=Terminal("B", "harnesses.opencode.backend:terminal_context", True, "Agent · OpenCode",
                      tail="terminal_tail:opencode_tail"),
    hooks=Hooks(),
    prepare="harnesses.opencode.backend:prepare",
    image="image",
    artifacts={"opencode": {"source": "npm:opencode-ai", "arg": "OPENCODE_VERSION"}, **MCP_REMOTE},
    registry_id="opencode",
    summary="opencode in its own container (or this machine's opencode under docker: none), on the "
            "Annotation surface. Provider key, or your ChatGPT, GitHub Copilot or SuperGrok subscription",
)
