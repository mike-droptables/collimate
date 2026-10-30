# SPDX-License-Identifier: MIT
"""One owner-authorized OpenCode Gemini native private-file ACP leg (P4R08)."""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from tests_live.conftest import (bind_model_account, eng, has_key, model_account,
                                requires_docker, run_on_test_loop)
from tests_live.fixtures.acp_capture import Capture, observed_profile

MODEL = "gemini-3.5-flash-lite"
VERSION = "1.18.30"
ALIASES = ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY")
pytestmark = [requires_docker, pytest.mark.usefixtures("one_loop_per_test"),
              pytest.mark.skipif(not has_key("gemini"), reason="needs authorized GEMINI_API_KEY"),
              pytest.mark.skipif(os.environ.get("COLLIMATE_LIVE_P4R08") != "1",
                                 reason="needs reviewed single-call COLLIMATE_LIVE_P4R08=1")]


def _write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)


def test_opencode_gemini_private_file_round_trip_P4R08(docker_env_bridge, monkeypatch):
    """Drive one actual engine turn, retaining failure without retry or reload."""
    import gen
    import docker
    from _agent_shared import acp_run, harness_homes
    from _agent_shared.cli_container import close_all

    evidence = Path(os.environ["COLLIMATE_LIVE_P4R08_EVIDENCE"])
    assert evidence.is_absolute() and evidence.is_dir()
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o700
    bridge = docker_env_bridge
    bridge.permissions = {"docker": "full", "internet": "full", "commands": "deny"}
    bridge.packages = {"opencode": VERSION}
    bind_model_account(model_account("gemini", model=MODEL, harness="opencode"))
    selected_key = os.environ["GEMINI_API_KEY"].strip()
    capture = Capture(secrets=(selected_key,))
    receipt = {"row": "P4R08", "harness": "opencode", "version": VERSION,
               "provider": "gemini", "model": "google/" + MODEL,
               "native_completion": False, "cleanup": False}
    owned = {}
    original = acp_run._PROFILES["opencode"]

    async def prepare(context):
        assert context["provider"] == "gemini" and context["model"] == MODEL
        profile = await original(context)
        owned["profile"] = profile
        owned["container_id"] = profile.env_obj.container_id
        owned["client"] = profile.env_obj.container.client
        home = Path(harness_homes.harness_home("opencode"))
        owned["home"] = home
        auth = home / "auth.json"
        # Compare privately; neither the document nor its key is evidence.
        assert json.loads(auth.read_text()) == {"google": {"type": "api", "key": selected_key}}
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
        assert profile.locus == "container" and profile.model_value == "google/" + MODEL
        rc, version = await asyncio.to_thread(profile.env_obj.exec, ["opencode", "--version"])
        assert rc == 0 and version.strip() == VERSION
        # The actual container environment is separate from pytest's selected account key.
        rc, environment = await asyncio.to_thread(profile.env_obj.exec, ["env"])
        assert rc == 0
        assert selected_key not in environment
        assert not any(line.startswith(name + "=") for name in ALIASES for line in environment.splitlines())
        receipt.update(auth_file_mode="0600", selected_native_auth_only=True,
                       container_aliases_absent=True, observed_version=version.strip())
        return observed_profile(profile, capture)

    monkeypatch.setitem(acp_run._PROFILES, "opencode", prepare)
    try:
        result = run_on_test_loop(asyncio.wait_for(eng.run_agent(
            prompt="Reply with exactly: P4R08-GEMINI-OK",
            backend="opencode", auth="api_key", provider="gemini", model=MODEL,
            stream=True, enable_file_tools=False, websearch=False,
            commands="deny"), timeout=240))
        assert (result.text or "").strip() == "P4R08-GEMINI-OK"
        session = json.loads(gen.read_file("_agent/session.json"))
        assert session["backend"] == "opencode" and session["acp_session_id"]
        assert len(capture.prompt_ids) == 1
        requests = [row for row in capture.records if row.get("method") == "session/prompt"]
        responses = [row["message"] for row in capture.records if "message" in row
                     and row["message"].get("id") == capture.prompt_ids[0]
                     and type(row["message"].get("id")) is type(capture.prompt_ids[0])
                     and "method" not in row["message"]]
        assert len(requests) == len(responses) == 1
        assert requests[0]["sessionId"] == session["acp_session_id"]
        assert responses[0].get("result", {}).get("stopReason") == "end_turn"
        # Inspect the real native execution identity as its own UID. Read the
        # comparison key from its existing private file, never scanner argv/env.
        scanner = """
import json, os
from pathlib import Path
key = json.loads(Path('/home/coder/.local/share/opencode/auth.json').read_text())['google']['key'].encode()
found = 0
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        argv = (entry / 'cmdline').read_bytes().split(b'\\0')
        if len(argv) < 2 or Path(os.fsdecode(argv[0])).name != 'opencode' or argv[1] != b'acp':
            continue
        values = (entry / 'environ').read_bytes().split(b'\\0')
    except FileNotFoundError:
        continue
    assert not any(value.startswith((b'GEMINI_API_KEY=', b'GOOGLE_GENERATIVE_AI_API_KEY=')) or key in value for value in values)
    assert not any(key in value for value in argv)
    found += 1
assert found > 0
print(json.dumps({'native_acp_processes': found, 'native_aliases_absent': True, 'native_key_absent_from_argv_env': True}))
"""
        rc, proof = owned["profile"].env_obj.exec(
            ["python3", "-c", scanner], user="coder", timeout=20)
        assert rc == 0, "Native OpenCode private-environment scan failed"
        receipt.update(json.loads(proof))
        receipt.update(native_completion=True, native_prompt_count=1,
                       session_id=session["acp_session_id"])
    finally:
        # Close actual owners even when the native call or an assertion fails.
        try:
            run_on_test_loop(acp_run.close_acp_sessions())
        finally:
            close_all()
            harness_homes.close_harness_homes()
            try:
                if "profile" in owned:
                    try:
                        owned["client"].containers.get(owned["container_id"])
                    except docker.errors.NotFound:
                        receipt["container_removed"] = True
                    else:
                        pytest.fail("Owned OpenCode container survived cleanup")
                assert "home" not in owned or not owned["home"].exists()
                receipt["private_home_removed"] = True
                receipt["cleanup"] = True
            finally:
                capture.write_private(evidence / "native-acp.json")
                _write_private(evidence / "receipt.json", receipt)
