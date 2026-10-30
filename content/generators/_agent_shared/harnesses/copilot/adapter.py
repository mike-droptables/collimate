# SPDX-License-Identifier: MIT
"""GitHub Copilot CLI: GitHub's agent, ACP-native (``copilot --acp``), in its container."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal, Tools, natives
from ...adapter_artifacts import MCP_REMOTE

_HINT = ("Create a fine-grained personal access token at https://github.com/settings/personal-access-tokens/new "
         "on your personal account with the account permission \"Copilot Requests\", and paste it here. "
         "Classic tokens (ghp_…) are not accepted.")

ADAPTER = HarnessAdapter(
    id="copilot", label="GitHub Copilot CLI", locus="container", acp_argv=("copilot", "--acp"),
    host_binary="copilot", host_twin=False,
    # The Copilot plan is a GitHub token with the Copilot Requests
    # permission (COPILOT_GITHUB_TOKEN); a provider key runs through
    # Copilot's own BYOK (COPILOT_PROVIDER_*), with no GitHub login.
    auth=Auth(providers=natives("openai", "anthropic", "groq", "cerebras", "openrouter", "xai", "mistral"),
              default_provider="openai", plans={"github_copilot": "copilot"},
              login=Login("a fine-grained personal access token with the Copilot Requests permission",
                          "", "credential_file", ("token",), "github_token", hint=_HINT),
              plan_default_for=("github_copilot",)),
    # A blank model is Copilot's own pick for the plan.
    model=Model(plan_defaults={"github_copilot": ""}),
    # $COPILOT_HOME/copilot-instructions.md: the global custom instructions.
    instructions=Instructions("home_file", "copilot-instructions.md"),
    tools=Tools(),
    commands=Commands("server-wide tool flags: the shell tool excluded and denied under deny, no shell "
                      "allow rule under ask, --allow-tool shell under allow; hooks, MCP servers, custom agents "
                      "and skills a file in the workspace or harness home could name are locked by the "
                      "image's managed settings (folder trust does not gate them under --acp)",
                      mode_rule="acp_policy:_copilot_mode_allowed",
                      recognisers=("harnesses.copilot.recognise:write_bash_input",)),
    session=Session(paths=("session-state", "session-store.db")),
    terminal=Terminal("C", "harnesses.copilot.backend:terminal_context", True, "Agent · Copilot"),
    hooks=Hooks(),
    prepare="harnesses.copilot.backend:prepare",
    image="image",
    artifacts={"copilot": {"source": "npm:@github/copilot", "arg": "COPILOT_VERSION"}, **MCP_REMOTE},
    registry_id="github-copilot-cli",
    note="With commands allow, permitted commands run with the token or key stripped from their environment. "
         "Copilot loads a workspace's hooks and MCP servers under --acp whether or not the folder is trusted, "
         "so no folder is trusted and the image's root-owned managed settings lock hooks, MCP servers, custom "
         "agents and skills to plugins: files the agent writes there never run a command, at any commands level.",
    summary="GitHub Copilot CLI in its own container, on the Annotation surface. Your GitHub Copilot "
            "subscription through a fine-grained token, or a provider key through Copilot's BYOK",
)
