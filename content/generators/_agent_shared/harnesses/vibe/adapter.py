# SPDX-License-Identifier: MIT
"""Mistral Vibe: Mistral's CLI, ACP-native (``vibe-acp``), in its container."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Session, Terminal, Tools, natives
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="vibe", label="Mistral Vibe", locus="container", acp_argv=("vibe-acp",),
    host_binary="vibe", host_twin=False,
    # Mistral on its own backend; the OpenAI-compatible providers as declared
    # [[providers]] entries.
    auth=Auth(providers=natives("mistral", "openai", "groq", "cerebras", "openrouter"),
              default_provider="mistral"),
    # A blank model on Mistral is Vibe's own default (mistral-vibe-cli-latest).
    model=Model(key_defaults={"mistral": ""}),
    # $VIBE_HOME/AGENTS.md: the user doc, appended for every session whatever
    # the cwd or its trust.
    instructions=Instructions("home_file", "AGENTS.md"),
    tools=Tools(),
    commands=Commands("policy in the VIBE_* environment the agent cannot rewrite: bash disabled under "
                      "deny, asking with an empty allowlist under ask, auto-approve disabled below allow",
                      mode_rule="acp_policy:_vibe_mode_allowed",
                      persisting_options=frozenset({"allow_always_permanent", "allow_permanent_exact",
                                                    "allow_permanent_directory_recursive"})),
    session=Session(paths=("logs/session",)),
    terminal=Terminal("C", "harnesses.vibe.backend:terminal_context", True, "Agent · Mistral Vibe"),
    hooks=Hooks(),
    prepare="harnesses.vibe.backend:prepare",
    image="image",
    artifacts={"mistral-vibe": {"source": "pypi:mistral-vibe", "arg": "VIBE_VERSION"}, **MCP_REMOTE},
    registry_id="mistral-vibe",
    note="With commands allow, permitted commands can read the provider key from the harness process environment.",
    summary="Mistral Vibe in its own container, on the Annotation surface. Mistral key, or an OpenAI, "
            "Groq, Cerebras or OpenRouter key",
)
