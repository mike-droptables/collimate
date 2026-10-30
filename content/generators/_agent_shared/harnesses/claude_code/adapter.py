# SPDX-License-Identifier: MIT
"""Claude Code: Anthropic's CLI behind its ACP adapter, a host process."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Session, Terminal, Tools, natives

ADAPTER = HarnessAdapter(
    id="claude-code", label="Claude Code", locus="host", acp_argv=("claude-agent-acp",),
    host_binary="claude", host_twin=False,
    auth=Auth(providers=natives("anthropic"), plans={"claude_code": "anthropic"},
              plan_default_for=("claude_code",)),
    model=Model(default="sonnet"),
    instructions=Instructions("meta", "_meta.claudeCode.options.systemPrompt.append"),
    tools=Tools(),
    commands=Commands("--settings permission rules, the tools allowlist and strict permission kinds",
                      mode_rule="acp_policy:_claude_mode_allowed",
                      recognisers=("acp_policy:_claude_monitor",), strict_kinds=True),
    session=Session(paths=("projects", "todos")),
    terminal=Terminal("A", "harnesses.claude_code.backend:terminal_context", False,
                      "Agent · Claude terminal", tail="terminal_tail:claude_tail",
                      prepare_with_request=True, locus="host"),
    hooks=Hooks(explain_failure="harnesses.claude_code.backend:explain_failure"),
    prepare="harnesses.claude_code.backend:prepare",
    adapter_artifact="claude-agent-acp", registry_id="claude-acp",
    preset_permissions={"docker": "none", "internet": "full", "commands": "deny"},
    summary="Claude Code behind its ACP adapter, a process on this machine, on the Annotation surface; "
            "its CLI terminal is this machine's claude, signed in once per Key manager entry. Anthropic "
            "key or Claude subscription",
)
