# SPDX-License-Identifier: MIT
"""Real native OpenCode TUI grant and private credential-route gates."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import gen

from tests_live.conftest import (bind_model_account, has_key, model_account, model_for,
                      requires_docker, run_on_test_loop)
from tests_live.test_live_acp_conformance import _probe_prompt, _write_probe
from tests_live._host_terminal import _HostTerminal, _type_line
from _agent_shared.harnesses.opencode import backend as oc
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

pytestmark = [requires_docker, pytest.mark.usefixtures("one_loop_per_test"),
              pytest.mark.skipif(not has_key("openrouter"), reason="needs OPENROUTER_API_KEY")]


def prepared(bridge, level):
    bridge.permissions = {"docker": "full", "internet": "full", "commands": level}
    bridge.packages = {"opencode": "1.19.4"}
    model = model_for("openrouter")
    bind_model_account(model_account("openrouter", model=model, harness="opencode"))
    return run_on_test_loop(_pipeline.prepare(_HARNESS['opencode'], {
        "auth": "api_key", "provider": "openrouter", "model": model,
        "commands": level, "websearch": False, "system_prompt": ""}, leg="terminal"))


def export_session(env, before):
    sessions = oc.harness_session_ids(env) - before
    if len(sessions) != 1:
        return None
    sid = next(iter(sessions))
    raw = oc.cli_output(env, ['export', sid])
    if "{" not in raw:
        return None
    document = json.loads(raw[raw.index("{"):])
    key = os.environ["OPENROUTER_API_KEY"]
    if key in raw:
        pytest.fail("Selected provider key leaked into the native session export")
    return document


def assert_environment_private(env):
    rc, output = env.exec(["env"])
    assert rc == 0
    key = os.environ["OPENROUTER_API_KEY"]
    if key in output or any("API_KEY=" in line for line in output.splitlines()):
        pytest.fail("Native container environment exposed a provider key")
    rc, output = env.exec(["python3", "-c",
        "import json,os,stat; p='/home/coder/.local/share/opencode/auth.json'; "
        "d=json.load(open(p)); print(d['openrouter']['type']); "
        "print(bool(d['openrouter'].get('key'))); print(oct(stat.S_IMODE(os.stat(p).st_mode)))"])
    assert rc == 0 and output.splitlines() == ["api", "True", "0o600"]


def test_opencode_container_environment_has_no_provider_key(docker_env_bridge):
    result = prepared(docker_env_bridge, "allow")
    assert_environment_private(result.env_obj)
    print("[credentials] opencode allow: native auth file mode0600; container env has no provider key")


@pytest.mark.parametrize("level", ["deny", "ask-reject", "ask-allow", "allow"])
def test_native_opencode_tui_commands_grant(docker_env_bridge, level):
    commands, _, answer = level.partition("-")
    digest = _write_probe()
    result = prepared(docker_env_bridge, commands)
    env = result.env_obj
    before = oc.harness_session_ids(env)
    term = _HostTerminal(env.container_id, list(result.argv), "/workspace",
                         {"TERM": "xterm-256color", **result.env})
    observed_permission = False
    finished = False
    transcript = None
    try:
        term.wait_for(b"Ask anything", b"Build", b"OpenCode", timeout=60)
        _type_line(term, _probe_prompt("sha256sum commands-probe.txt | tee commands-probe.ran"))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            screen = term.said().lower()
            if commands == "ask" and not observed_permission and "allow once" in screen and "reject" in screen:
                observed_permission = True
                term.send(b"\x1b" if answer == "reject" else b"\r")
            transcript = export_session(env, before)
            if transcript:
                assistant = [m for m in transcript.get("messages", [])
                             if (m.get("info") or {}).get("role") == "assistant"]
                if any((m.get("info") or {}).get("time", {}).get("completed") for m in assistant):
                    finished = True
                    break
            time.sleep(1)
        if not finished:
            pytest.fail(f"Native TUI turn did not complete: {term.recent(1600)}")
        marker = Path(gen.workspace_path()) / "commands-probe.ran"
        ran = marker.exists() and digest in marker.read_text()
        parts = [p for m in transcript.get("messages", [])
                 if (m.get("info") or {}).get("role") == "assistant" for p in m.get("parts", [])]
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text")
        tool_states = [p.get("state", {}) for p in parts if p.get("type") == "tool"]
        assert tool_states or "NO-SHELL" in text, "The native turn failed before attempting or refusing the probe"
        assert observed_permission is (commands == "ask")
        assert ran is (commands == "allow" or answer == "allow")
        if answer == "reject":
            assert any(s.get("status") == "error" and "reject" in s.get("error", "").lower()
                       for s in tool_states), "Native permission rejection did not reach the shell tool"
        assert_environment_private(env)
        print(f"[commands-native-tui] opencode/{level}: permission={observed_permission} shell_ran={ran}")
    finally:
        term.stop()
