# SPDX-License-Identifier: MIT
"""Codex: OpenAI's CLI behind codex-acp, in its container or on this machine."""
from .. import (Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal,
                Tools, natives)
from ...adapter_artifacts import MCP_REMOTE, image_artifact

# What OpenAI documents for a ChatGPT plan (learn.chatgpt.com/docs/auth,
# checked 2026-09-22): sign in on a machine with a browser, then copy
# $CODEX_HOME/auth.json to the headless place — the ONLY route for a Plus/Pro
# plan; Business/Enterprise workspaces may instead mint a Codex access token
# and `codex login --with-access-token`, which writes the same file. The store
# is `file` by default (codex-rs defaults.toml), but an install may keep the
# login in the OS keyring (cli_auth_credentials_store keyring/auto), and a Snap
# install relocates CODEX_HOME without telling anyone else.
_HINT = (
    "Sign in with `codex login` (or `codex login --device-auth` once device-code login is enabled in your "
    "ChatGPT security settings), then paste the whole contents of {store} — codex's `$CODEX_HOME/auth.json`; open it in "
    "an editor or `cat` it in a wide terminal and copy from the opening {{ to the closing }} (a narrow terminal cuts the "
    "long token lines) "
    "(a Snap install keeps it under ~/snap/codex/current). If there is no such file, codex keeps the login in your "
    "OS keyring: add `cli_auth_credentials_store = \"file\"` to `$CODEX_HOME/config.toml`, sign in again, and paste "
    "the file. Business and Enterprise workspaces can instead paste the file `codex login --with-access-token` "
    "writes from a Codex access token."
)

ADAPTER = HarnessAdapter(
    id="codex", label="Codex", locus="container", acp_argv=("codex-acp",),
    host_binary="codex", host_twin=True,
    # codex speaks the Responses wire only: openai natively, openrouter and
    # groq as model_providers tables.
    auth=Auth(providers=natives("openai", "openrouter", "groq"), default_provider="openai",
              plans={"chatgpt": "openai"},
              login=Login("codex login", ".codex/auth.json", "credential_json", ("auth.json",),
                          "codex_auth", env="CODEX_HOME", suffix="auth.json",
                          also=("snap/codex/current/auth.json",), hint=_HINT),
              plan_default_for=("chatgpt",)),
    # The ChatGPT account's Codex catalogue rotates and codex's bundled one
    # dropped the API default, so a blank model is codex's own pick on
    # openai either way; openrouter and groq keep the provider default.
    model=Model(plan_defaults={"chatgpt": ""}, key_defaults={"openai": ""}),
    instructions=Instructions("home_file", "AGENTS.md"),
    tools=Tools(),
    commands=Commands("the root-owned requirements.toml lock, the session mode preset and the shell tool",
                      mode_rule="acp_policy:_codex_mode_allowed",
                      recognisers=("acp_policy:_codex_sandbox_widening",),
                      persisting_options=frozenset({"allow_permissions_turn_strict_auto_review"})),
    session=Session(paths=("sessions", "archived_sessions", "history.jsonl", "session_index.jsonl")),
    terminal=Terminal("A", "harnesses.codex.backend:terminal_context", True, "Agent · Codex",
                      tail="terminal_tail:codex_tail"),
    hooks=Hooks(normalize_update="harnesses.codex.backend:drop_startup_warning",
                tool_approval="harnesses.codex.backend:is_mcp_tool_approval",
                commands_note="harnesses.codex.backend:commands_note"),
    prepare="harnesses.codex.backend:prepare",
    image="image",
    artifacts={"codex": {"source": "npm:@openai/codex", "arg": "CODEX_VERSION"},
               "codex-acp": image_artifact("codex-acp"), **MCP_REMOTE},
    adapter_artifact="codex-acp", registry_id="codex-acp",
    summary="OpenAI's Codex CLI in its own container (or this machine's codex under docker: none), on the "
            "Annotation surface. openai, openrouter or groq key, or ChatGPT sign-in",
)
