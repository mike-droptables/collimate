# SPDX-License-Identifier: MIT
"""Collimate's PreToolUse hook (``harnesses/antigravity/image/agy_hook.py``) and
the ACP sign-in guard of ``harnesses/antigravity/backend.py``.

The hook is the terminal's commands gate (deny; ask = agy's own prompt) and a
no-op beside the ACP server, whose permission requests reach the client gate.
The guard ends a run the server has parked on a browser sign-in."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests._workspace import GENERATORS


_HOOK = os.path.join(GENERATORS, "_agent_shared",
                     "harnesses", "antigravity", "image", "agy_hook.py")


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("_agy_hook_under_test", _HOOK)
    module = importlib.util.module_from_spec(spec)
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True      # that directory is the image's build context
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


def _event(name: str, **args) -> dict:
    return {"toolCall": {"name": name, "args": args}, "conversationId": "c", "stepIdx": 3}


def test_the_hook_denies_every_exec_tool_under_deny(hook):
    for name in hook.EXEC_TOOLS:
        assert hook.decide("deny", _event(name, Action="send_input"))["decision"] == "deny"
    assert "shell" in hook.EXEC_TOOLS and "run_command" in hook.EXEC_TOOLS


@pytest.mark.parametrize("level", ["deny", "ask"])
def test_manage_task_is_gated_only_where_it_types_into_a_task(hook, level):
    """``send_input`` delivers stdin to a running command, so it is a command;
    list / status / kill run nothing new. An action the hook does not know (or
    none) is gated like send_input. agy shows no prompt for this auto-allowed
    tool, so ask refuses it too."""
    send = _event("manage_task", Action="send_input", TaskId="t1", Input="rm -rf x\n")
    assert hook.decide(level, send)["decision"] == "deny"
    for action in ("list", "status", "kill"):
        assert hook.decide(level, _event("manage_task", Action=action))["decision"] == "allow"
    for unknown in (_event("manage_task", Action="exec", Input="id\n"), _event("manage_task", Input="id\n")):
        assert hook.decide(level, unknown)["decision"] == "deny"


def test_the_hook_forces_agys_prompt_in_the_tui(hook):
    assert hook.decide("ask", _event("run_command", CommandLine="ls"))["decision"] == "force_ask"
    assert hook.decide("ask", _event("shell", command="ls"))["decision"] == "force_ask"


@pytest.mark.parametrize("level", ["deny", "ask"])
def test_beside_the_acp_server_the_hook_leaves_the_decision_to_the_client_gate(hook, level):
    """The server asks the client for every command, and the client's gate
    refuses under deny and cards under ask — showing the attempt either way. A
    hook answering deny there would hide it, so the server's hook says
    allow (no objection; the server's own permission flow still runs)."""
    assert hook.decide(level, _event("run_command", CommandLine="ls"), acp_server=True)["decision"] == "allow"
    assert hook.decide("", _event("run_command"), acp_server=True)["decision"] == "deny"


def test_the_hook_fails_closed(hook):
    assert hook.decide("", _event("run_command"))["decision"] == "deny"
    assert hook.decide("ask", _event("view_file"))["decision"] == "deny"


def test_the_hook_prints_one_decision_and_denies_unreadable_input():
    """End to end: the script prints a decision for any stdin, never nothing
    (empty output is "no opinion", which agy runs)."""
    for stdin, argv in (("not json", ["ask"]), ("{}", ["deny"]), (json.dumps(_event("run_command")), [])):
        out = subprocess.run([sys.executable, "-B", _HOOK, *argv], input=stdin, text=True,
                             capture_output=True, check=True).stdout
        assert json.loads(out)["decision"] == "deny"
    out = subprocess.run([sys.executable, "-B", _HOOK, "ask"], input=json.dumps(_event("run_command")),
                         text=True, capture_output=True, check=True).stdout
    assert json.loads(out)["decision"] == "force_ask"


def test_the_hook_recognises_the_server_by_its_parent_process(hook, monkeypatch):
    monkeypatch.setattr(hook.os, "getppid", lambda: 4242)

    def opener(path, mode="r", *a, **kw):
        assert path == "/proc/4242/cmdline"
        import io
        return io.BytesIO(b"/opt/agy-acp/agy_acp_server.par\0--uid=\0")

    monkeypatch.setattr("builtins.open", opener)
    assert hook.started_by_acp_server() is True

    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").BytesIO(b"/bin/bash\0-lc\0"))
    assert hook.started_by_acp_server() is False

    def missing(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", missing)
    assert hook.started_by_acp_server() is False


# ── the sign-in guard ─────────────────────────────────────────────────


class _Transport:
    """The server's transport: what the client sends, and what it says."""

    def __init__(self):
        self.sent, self.incoming, self.closed = [], asyncio.Queue(), False

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


def _guard(tmp_path, transport):
    from _agent_shared.harnesses.antigravity import backend as ab
    killed = []
    process = SimpleNamespace(transport=transport, kill=lambda: killed.append(True))
    log = tmp_path / "acp.log"
    return ab, ab.SignInGuard(process, str(log), poll_s=0.01), log, killed


def test_the_guard_passes_everything_through_until_the_server_asks_for_a_browser(tmp_path):
    async def run():
        transport = _Transport()
        ab, guard, log, killed = _guard(tmp_path, transport)
        await guard.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert transport.sent == [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}]
        transport.incoming.put_nowait({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert await guard.receive() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        log.write_text("INFO normal server noise\n")
        await asyncio.sleep(0.1)
        assert killed == []
        await guard.close()
        assert transport.closed
    asyncio.run(run())


def test_the_guard_answers_every_outstanding_request_with_the_actionable_error(tmp_path):
    async def run():
        transport = _Transport()
        ab, guard, log, killed = _guard(tmp_path, transport)
        await guard.send({"jsonrpc": "2.0", "id": 7, "method": "session/new", "params": {}})
        await guard.send({"jsonrpc": "2.0", "id": 8, "method": "session/prompt", "params": {}})
        transport.incoming.put_nowait({"jsonrpc": "2.0", "id": 8, "result": {"stopReason": "end_turn"}})
        assert (await guard.receive())["id"] == 8                  # answered: no longer outstanding
        # The marker may arrive split across two reads of the log.
        half = len(ab.BROWSER_SIGN_IN_MARKER) // 2
        log.write_text("noise " + ab.BROWSER_SIGN_IN_MARKER[:half])
        await asyncio.sleep(0.1)
        with open(log, "a") as fh:
            fh.write(ab.BROWSER_SIGN_IN_MARKER[half:] + ": https://accounts.google.com/...\n")
        error = await asyncio.wait_for(guard.receive(), 5)
        assert error["id"] == 7 and error["error"]["code"] == -32000
        assert error["error"]["message"] == ab.BROWSER_SIGN_IN_REFUSED
        assert await asyncio.wait_for(guard.receive(), 5) is None   # then the server is gone
        assert killed == [True]
        await guard.close()
    asyncio.run(run())
