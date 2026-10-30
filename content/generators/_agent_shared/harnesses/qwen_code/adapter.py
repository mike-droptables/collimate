# SPDX-License-Identifier: MIT
"""Qwen Code: the Gemini-CLI-lineage agent, ACP-native, in its container."""
from .. import Auth, Commands, HarnessAdapter, Hooks, Instructions, Model, Session, Terminal, Tools, natives
from ...adapter_artifacts import MCP_REMOTE

ADAPTER = HarnessAdapter(
    id="qwen-code", label="Qwen Code", locus="container",
    acp_argv=("qwen", "--acp", "--experimental-skills"), host_binary="qwen", host_twin=False,
    # qwen selects a protocol, not a vendor: anthropic and gemini natively,
    # every OpenAI-compatible provider as ``openai`` with its base URL.
    auth=Auth(providers=natives("anthropic", "gemini", "openai", ("groq", "openai"),
                                ("cerebras", "openai"), ("openrouter", "openai"))),
    model=Model(matches="harnesses.qwen_code.backend:model_matches"),
    instructions=Instructions("home_file", "QWEN.md"),
    tools=Tools(),
    commands=Commands("root-owned system settings, --exclude-tools under deny and the approval mode",
                      mode_rule="acp_policy:_qwen_mode_allowed",
                      mode_switch_options={"proceed_always": "auto-edit"}),
    session=Session(paths=("tmp",), one_per_process=True),
    terminal=Terminal("C", "harnesses.qwen_code.backend:terminal_context", True, "Agent · Qwen Code"),
    hooks=Hooks(),
    prepare="harnesses.qwen_code.backend:prepare",
    image="image",
    artifacts={"qwen-code": {"source": "npm:@qwen-code/qwen-code", "arg": "QWEN_CODE_VERSION"}, **MCP_REMOTE},
    registry_id="qwen-code",
    summary="Qwen Code in its own container, on the Annotation surface. Anthropic, OpenAI, Gemini, Groq, "
            "Cerebras or OpenRouter key",
)
