# SPDX-License-Identifier: MIT
"""Actual native Goose terminal permissions and private secret-file route."""
import json
import os
from pathlib import Path
import time

import gen
import pytest

from tests_live.conftest import (bind_model_account, has_key, model_account, model_for,
                                 requires_docker, run_on_test_loop)
from tests_live.test_live_acp_conformance import _probe_prompt, _write_probe
from tests_live._host_terminal import _HostTerminal
from _agent_shared.harnesses.goose import backend as goose
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

pytestmark = [requires_docker, pytest.mark.usefixtures("one_loop_per_test"),
              pytest.mark.skipif(not has_key("openrouter"), reason="needs OPENROUTER_API_KEY")]


def prepared(bridge, level):
    bridge.permissions = {"docker": "full", "internet": "full", "commands": level}
    bridge.packages = {"goose": "1.56.0"}
    model = model_for("openrouter")
    bind_model_account(model_account("openrouter", model=model, harness="goose"))
    return run_on_test_loop(_pipeline.prepare(_HARNESS['goose'], {"provider": "openrouter", "model": model,
        "commands": level, "websearch": False, "system_prompt": ""}, leg="terminal"))


def assert_private(result):
    key = os.environ["OPENROUTER_API_KEY"]
    if key in json.dumps(result.env) or any(name.endswith("API_KEY") for name in result.env):
        pytest.fail("Provider key exposed in terminal process environment")
    env = result.env_obj
    rc, output = env.exec(["env"])
    if rc or key in output or any("API_KEY=" in line for line in output.splitlines()):
        pytest.fail("Native container environment exposed a provider key")
    rc, output = env.exec(["python3", "-c",
        "import json,os,stat; p='/home/coder/.config/goose/secrets.yaml'; "
        "d=json.load(open(p)); print(sorted(d)==['OPENROUTER_API_KEY']); "
        "print(bool(d['OPENROUTER_API_KEY'])); print(oct(stat.S_IMODE(os.stat(p).st_mode)))"])
    assert rc == 0 and output.splitlines() == ["True", "True", "0o600"]


def trace_since(env, before):
    ids = goose.harness_session_ids(env) - before
    if len(ids) != 1:
        return []
    raw = goose.goose_session_export(env, next(iter(ids)))
    if os.environ["OPENROUTER_API_KEY"] in raw:
        pytest.fail("Provider key exposed in native session record")
    return goose.trace_from_goose_session(raw)


def test_goose_native_secret_file_and_container_environment(docker_env_bridge):
    assert_private(prepared(docker_env_bridge, "allow"))
    print("[credentials] goose: selected native secrets.yaml mode0600; no provider key environment")


@pytest.mark.parametrize("level", ["deny", "ask-reject", "ask-allow", "allow"])
def test_native_goose_terminal_commands_grant(docker_env_bridge, level):
    commands, _, answer = level.partition("-")
    digest = _write_probe()
    result = prepared(docker_env_bridge, commands)
    env = result.env_obj
    before = goose.harness_session_ids(env)
    term = _HostTerminal(env.container_id, list(result.argv), "/workspace",
                         {"TERM": "xterm-256color", **result.env})
    observed = False
    trace = []
    try:
        term.wait_for(b"Enter to send", timeout=60)
        term.send(_probe_prompt("sha256sum commands-probe.txt | tee commands-probe.ran").encode())
        term.settle()
        prompt_checkpoint = len(term.said())
        term.send(b"\r")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            screen = term.said().lower()
            if commands == "ask" and not observed and ("do you allow" in screen or "do you allow?" in screen):
                observed = True
                if answer == "reject":
                    term.send(b"\x1b[B" * (2 if "always allow" in screen else 1) + b"\r")
                else:
                    term.send(b"\r")
            trace = trace_since(env, before)
            text = " ".join(row.get("text", "") for row in trace if row["type"] == "text")
            # Native prompt return marks completion; refusal prose is model-owned.
            if text and "enter to send" in screen[prompt_checkpoint:]:
                break
            time.sleep(1)
        else:
            recent = term.recent(1600).replace(os.environ["OPENROUTER_API_KEY"], "[redacted]")
            pytest.fail(f"Native Goose turn did not complete: {recent}")
        marker = Path(gen.workspace_path()) / "commands-probe.ran"
        ran = marker.exists() and digest in marker.read_text()
        assert observed is (commands == "ask")
        assert ran is (commands == "allow" or answer == "allow")
        if answer == "reject":
            responses = [json.loads(row["output"]) for row in trace if row["type"] == "tool_result"]
            assert any(row.get("isError") is True and any(
                "the user has declined" in part.get("text", "").lower()
                for part in row.get("content", [])) for row in responses)
        assert_private(result)
        print(f"[commands-native-terminal] goose/{level}: permission={observed} shell_ran={ran}")
    finally:
        term.stop()
