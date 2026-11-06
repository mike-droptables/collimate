"""Codex on the host locus, through the product path.

The Agent cell's generator, on its chat surface, is spawned by the process manager under its
rlimits, and every process it starts inherits them: codex-acp, this machine's
own codex, and whatever codex starts for a turn. The scenario asks that chain
to read a workspace file and checks the reply, so a limit that fails one of
those processes at startup (the address-space cap that killed Codex's V8
code-mode host on 2026-10-10) is caught here rather than in a user's chat.
Gated on this machine's codex and an OpenRouter key; the model is the suite's
OPENROUTER_MODEL under a codex model entry.
"""
from __future__ import annotations

import json
import shutil
import urllib.parse
import uuid

import pytest

from .conftest import OPENROUTER_MODEL, credential_account, delete_accounts, model_entry
from .test_live_generator_tool import ChatWire

pytestmark = pytest.mark.live


@pytest.fixture
def codex_host_runner(api, openrouter_key, keychain_ok):
    """The shipped Codex agent config on its chat surface, placed on the host
    (docker: none), bound to an OpenRouter model entry on the codex harness."""
    if shutil.which("codex") is None:
        pytest.skip("needs this machine's own `codex` on PATH (docker: none runs the host copy)")
    if not openrouter_key:
        pytest.skip("no OPENROUTER_API_KEY — not in the environment and "
                    + ("not in the OS keychain under the `collimate` service" if keychain_ok
                       else "the OS keychain did not answer, so it could not be read"))
    tag = uuid.uuid4().hex[:6]
    owner = credential_account(api, f"OpenRouter codex host {tag}", "openrouter", openrouter_key)
    model = model_entry(api, owner, f"codex host {tag}", OPENROUTER_MODEL, "codex")
    template = api.request("GET", "/api/runner_generators/agent/configs/codex")
    [main] = template["runner"]["settings"]
    runner = {**template["runner"], "id": f"live-codex-host-{tag}",
              "name": f"Codex host chat {tag}", "generator": "agent",
              "accounts": {"model": model["id"]},
              "settings": {main: {**template["runner"]["settings"][main], "surface": "chat"}},
              "permissions": {**template["runner"]["permissions"], "docker": "none"}}
    api.request("POST", "/api/runners", {"runner": runner, "files": template["files"]})
    entry = next(r for r in api.request("GET", "/api/runners") if r["id"] == runner["id"])
    assert not entry.get("invalid"), entry
    try:
        yield entry
    finally:
        api.stop_started_runs()
        try:
            api.request("DELETE", "/api/runners/" + urllib.parse.quote(runner["id"]))
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass
        delete_accounts(api, model["id"], owner["id"])


def test_codex_on_the_host_reads_a_workspace_file(api, codex_host_runner, evidence):
    sentinel = "SENTINEL-" + uuid.uuid4().hex[:8]
    cell = api.new_cell("Codex host", [{
        "name": "middle.md",
        "content": f"{sentinel}: the middle of the essay\n\nA second paragraph, not to be quoted.\n",
    }])
    run = api.start_runner(cell, codex_host_runner)
    api.wait_idle(run, timeout=240)
    wire = ChatWire(api, cell, run)
    try:
        frames = wire.turn("Read middle.md in the workspace and reply with exactly "
                           "its first line and nothing else.")
    finally:
        wire.close()
    messages = api.wait_assistant(cell, 1, run_id=run)
    replies = [m.get("content") or "" for m in messages if m.get("role") == "assistant"]
    evidence.text(f"codex-host-{run[:8]}.json", {"frames": frames, "messages": messages})
    assert any(sentinel in reply for reply in replies), replies
    # No process in the chain died at startup under the generator's limits.
    record = json.dumps(api.run_events(run), default=str) + json.dumps(frames, default=str)
    for symptom in ("code-mode host exited", "code-mode host closed", "SIGTRAP", "out of memory"):
        assert symptom not in record, symptom
