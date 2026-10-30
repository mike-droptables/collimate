# SPDX-License-Identifier: MIT
"""Antigravity: Google's official ACP server (antigravity-acp) in its container."""
from .. import (Auth, Commands, HarnessAdapter, Hooks, Instructions, Login, Model, Session, Terminal,
                Tools, natives)
from ...adapter_artifacts import MCP_REMOTE

# The pasted document serves the ACP server only (the Annotation and Chat
# surfaces); Google's terms restrict third-party use of consumer sign-ins, and
# the hint says so where it is pasted. The CLI surface needs no document: it
# runs Google's own agy on this machine, which signs in through Google's own
# browser flow. {store} is the file found or expected on the machine.
_HINT = ("Optional, for the Annotation and Chat surfaces: the whole contents of {store}, from the opening "
         "{{ to the closing }}. Mint it by signing in once through the official antigravity-acp server in "
         "Zed or JetBrains; on macOS set AGY_ACP_FORCE_FILE_STORAGE=1 first, or the token goes to the "
         "keychain instead of this file. At your own risk: Google's terms restrict third-party use of "
         "consumer Google sign-ins, and a Gemini API key is the supported route. The CLI surface needs "
         "no document: it runs agy on this machine, which signs in through Google's own browser flow.")


def terminal_locus(auth: str) -> str:
    """The CLI surface on the Google plan is the unmodified agy on this
    machine, signed in through Google's own loopback browser flow, which can
    complete only where the browser is; on a Gemini key it runs in the
    harness's container."""
    return "host" if auth == "google" else "container"

ADAPTER = HarnessAdapter(
    id="antigravity", label="Antigravity", locus="container",
    acp_argv=("/opt/agy-acp/agy_acp_server.par", "--uid="),
    host_binary="agy", host_twin=False,
    auth=Auth(providers=natives("gemini"), default_provider="gemini", plans={"google": "gemini"},
              login=Login("the antigravity-acp server (in Zed or JetBrains)",
                          ".gemini/antigravity-acp/acp_token.json", "credential_json",
                          ("acp_token.json",), "google_authorized_user", copy=True, hint=_HINT),
              plan_default_for=("google",)),
    # The server lists its models at run time (a Gemini API id under a key, its
    # own catalogue under the sign-in), so nothing is pinned here.
    model=Model(default="default", plan_defaults={"google": "default"},
                unchecked="antigravity-acp refuses to start a session on an ID it does not list",
                matches="harnesses.antigravity.backend:model_matches"),
    instructions=Instructions("meta", "_meta.antigravity.systemPrompt.append"),
    tools=Tools(),
    commands=Commands("the ACP client's permission gate on the server's session/request_permission; below "
                      "allow the tools that run code without asking (run_workflow, manage_task, schedule, the "
                      "subagent tools) are withheld and a PreToolUse hook denies them", mode_rule="acp_policy:_antigravity_mode_allowed",
                      recognisers=("harnesses.antigravity.recognise:server_exec_tool",)),
    # The server's conversation store is separate from the CLI's (G4), so a chat
    # session cannot be continued in the terminal nor the reverse; both are kept.
    session=Session(paths=("antigravity-acp/conversations", "antigravity-cli/conversations",
                           "antigravity/conversations")),
    terminal=Terminal("A", "harnesses.antigravity.backend:terminal_context", True, "Agent · Antigravity",
                      tail="terminal_tail:antigravity_tail",
                      harness_session_ids="harnesses.antigravity.backend:harness_session_ids",
                      locus="harnesses.antigravity.adapter:terminal_locus"),
    hooks=Hooks(),
    prepare="harnesses.antigravity.backend:prepare",
    image="image",
    artifacts={"agy": {"source": "github:google-antigravity/antigravity-cli", "arg": "AGY_VERSION"},
               **MCP_REMOTE},
    registry_id="antigravity-acp",
    note="With commands allow, permitted commands can read the provider key from the harness process "
         "environment. The CLI surface runs Google's agy: on the Google plan it is this machine's agy, "
         "which signs in through Google's own browser flow once per Key manager entry and needs docker: "
         "none or full with commands ask or allow; on a Gemini key it runs in its container. The chat "
         "server's conversation store is separate from the CLI's: a chat session is not continued in the "
         "terminal. Cell MCP servers other than the Collimate bridge load lazily, behind a schema read the "
         "withheld view_file tool would do.",
    summary="Google's Antigravity ACP server (antigravity-acp) in its own container, on the Annotation "
            "surface; the agy CLI on the terminal, this machine's under your Google sign-in or in the "
            "container on a Gemini key. Gemini key or your Google account",
)
