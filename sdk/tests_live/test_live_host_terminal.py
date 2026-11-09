# SPDX-License-Identifier: MIT
"""LIVE: the CLI surface on this machine — the unmodified vendor CLI in a
PTY the way the app's terminal route serves a host session
(:class:`_host_terminal._HostTerminal`, ``container_id=None``), driven
through the real terminal leg (``surface_runner(..., "terminal")``).

Each leg asserts what the design promises: placement puts the CLI on this
machine; the PTY child is the machine's own binary, in the cell's real
workspace, with the leg's own environment (no credential of Collimate's on
the Google plan); its home is the entry's per-account home under the host
cache, owner-only; the CLI opens at its own prompt or its own sign-in, with
none of the first-run screens Collimate answers; and once the leg ends, the
CLI's process group is gone.

Claude Code needs ``COLLIMATE_LIVE_CLAUDE_CODE=1`` and a ``claude`` on this
machine; Antigravity a ``agy`` on this machine (``host_harness.probe``).
Neither needs Docker. A fresh per-account home (under the test's tmp cache)
has no sign-in, so the leg is driven to the CLI's own sign-in screen and no
further: the sign-in is the user's, in a browser (``README.md``, the manual
checklist). A Claude entry carrying a setup token (``CLAUDE_CODE_OAUTH_TOKEN``)
is signed in through it, and runs one short turn. ``COLLIMATE_LIVE_HOST_ACCOUNT_CLAUDE_CODE`` /
``COLLIMATE_LIVE_HOST_ACCOUNT_ANTIGRAVITY`` name a Key manager entry id
whose home under the real host cache holds a completed sign-in: the leg
then runs one short turn on it, billed to that subscription.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
import uuid

import pytest

from collimate import host_harness
from collimate.harness_placement import placement
from collimate.permissions import Grant

from .conftest import CLAUDE_CODE_ENABLED, bind_model_account, model_account, run_on_test_loop
from ._host_terminal import _host_terminals, _type_line

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("one_loop_per_test")]


def _found(harness: str) -> dict:
    found = host_harness.probe(harness)
    if found is None or not found.usable:
        pytest.skip(f"no usable {host_harness.HARNESS_HOST_BINARY[harness]} on this machine")
    return {"path": found.path, "version": found.version, "binary": found.binary}


def _entry(harness: str, monkeypatch, tmp_path) -> "tuple[str, bool]":
    """``(entry id, signed in)``: a named entry whose real home holds a
    sign-in, or a fresh entry whose home is under the test's tmp cache."""
    named = (os.environ.get("COLLIMATE_LIVE_HOST_ACCOUNT_" + harness.upper().replace("-", "_")) or "").strip()
    if named:
        return named, True
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return f"live-{harness}-{uuid.uuid4().hex[:8]}", False


def _gone(pids: list, timeout: float = 15.0) -> list:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if os.path.exists(f"/proc/{pid}")
                 and open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"]
        time.sleep(0.1)
    return alive


def _private_from_cache_down(home: str, cache: str) -> None:
    level = home
    while os.path.commonpath([level, cache]) == cache and level != cache:
        assert stat.S_IMODE(os.stat(level).st_mode) == 0o700, level
        level = os.path.dirname(level)


async def _leg(harness: str, cfg: dict, terms: dict, *, until_up: float = 120.0):
    """Start the real terminal leg; return ``(task, term)`` once its PTY
    is up."""
    import gen
    from _agent_shared import surface_runner

    task = asyncio.create_task(surface_runner(harness, "terminal")(
        cfg, pointer=gen.run_path(f"terminal/{harness}.terminal")))
    deadline = time.monotonic() + until_up
    while not terms and not task.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if task.done():
        task.result()
    assert len(terms) == 1, "the leg never served its terminal"
    return task, next(iter(terms.values()))


async def _end(task, term) -> list:
    """End the leg as the host's destroy does; return what is left of the
    CLI's process group."""
    group = term.group()
    await asyncio.to_thread(term.stop)
    left = await asyncio.to_thread(_gone, group)
    await asyncio.wait_for(task, 60)
    return left


# One short turn whose answer is not in its prompt.
_BACKWARDS_PROMPT = "Reply with only this word written backwards: HSAH-TSOH"
_BACKWARDS_ANSWER = "HOST-HASH"


def _claude_replied(store: str, workspace: str, sid: str, timeout: float = 180.0) -> None:
    from _agent_shared.harnesses.claude_code import backend as claude

    deadline = time.monotonic() + timeout
    messages: list = []
    while time.monotonic() < deadline:
        messages, _raw = claude.pull_claude_transcript(sid, store_dir=store, project=workspace)
        if any(m["role"] == "assistant" and _BACKWARDS_ANSWER in m["content"] for m in messages):
            return
        time.sleep(1)
    raise AssertionError(f"claude never answered {_BACKWARDS_ANSWER} in session {sid}: {messages[-3:]}")


# Claude Code's screens: its prompt footer, the first-run screens the seed
# answers, and its sign-in method menu.
_CLAUDE_PROMPT = re.compile(rb"\? for shortcuts")
_CLAUDE_FIRST_RUN = (b"Choose the text style", b"Do you trust the files in this folder")
_CLAUDE_LOGIN = re.compile(rb"Select login method|Claude account with subscription|Anthropic Console")


@pytest.mark.skipif(not CLAUDE_CODE_ENABLED, reason="set COLLIMATE_LIVE_CLAUDE_CODE=1 to run this machine's claude")
def test_claude_code_runs_on_this_machine_in_its_entrys_own_home(native_bridge, monkeypatch, tmp_path):
    import gen
    from _agent_shared import harness_homes
    from _agent_shared.cli_host import account_home, homes_root
    from _agent_shared.harnesses.claude_code import backend as claude

    found = _found("claude-code")
    entry, signed_in = _entry("claude-code", monkeypatch, tmp_path)
    account = model_account("anthropic", model="haiku", harness="claude-code",
                            auth_type="subscription", plan="claude_code")
    account["id"] = entry
    # An entry carrying a setup token (CLAUDE_CODE_OAUTH_TOKEN) signs the TTY
    # in through it, a fresh home or not.
    signed_in = signed_in or bool((account.get("secrets") or {}).get("oauth_token"))
    bind_model_account(account)
    native_bridge.permissions = {"docker": "none", "internet": "full", "commands": "ask"}
    placed = placement("claude-code", Grant(docker="none", internet="full", commands="ask"), "terminal",
                       auth="claude_code")
    assert placed.ok and placed.runtime == "host" and placed.host["path"] == found["path"]
    terms = _host_terminals(native_bridge, monkeypatch)
    user_credentials = os.path.expanduser("~/.claude/.credentials.json")
    before = os.stat(user_credentials).st_mtime_ns if os.path.exists(user_credentials) else None
    cfg = {"backend": "claude-code", "auth": "claude_code", "provider": "anthropic", "model": "haiku",
           "commands": "ask", "websearch": False, "host_harness": found}

    async def run():
        task, term = await _leg("claude-code", cfg, terms)
        try:
            assert term.host and term.proc.args[0] == found["path"]
            with open(f"/proc/{term.proc.pid}/environ", "rb") as fh:
                environ = dict(item.split(b"=", 1) for item in fh.read().split(b"\0") if b"=" in item)
            home = account_home("claude-code", account)
            assert environ[b"CLAUDE_CONFIG_DIR"] == home.encode()
            assert os.readlink(f"/proc/{term.proc.pid}/cwd") == os.path.realpath(gen.workspace_path())
            assert b"ANTHROPIC_API_KEY" not in environ
            assert home.startswith(homes_root()) and os.path.isdir(home)
            _private_from_cache_down(home, os.path.dirname(homes_root()))
            first = await asyncio.to_thread(term.wait_for, _CLAUDE_PROMPT, *_CLAUDE_FIRST_RUN, timeout=120)
            assert first is _CLAUDE_PROMPT, f"claude showed a first-run screen; it shows {term.recent()!r}"
            if signed_in:
                # The reply is read from claude's own session record (in the
                # cell's harness home), never the screen, which echoes the prompt.
                argv = term.proc.args
                sid = argv[argv.index("--session-id") + 1]
                await asyncio.to_thread(_type_line, term, _BACKWARDS_PROMPT)
                await asyncio.to_thread(_claude_replied, harness_homes.harness_home("claude-code"),
                                        gen.workspace_path(), sid)
            else:
                # The sign-in is the user's, inside the CLI's own TTY: its menu
                # is one /login away (nothing is chosen, so no browser opens).
                await asyncio.to_thread(_type_line, term, "/login")
                await asyncio.to_thread(term.wait_for, _CLAUDE_LOGIN, timeout=60)
                term.send(b"\x1b")
            # The session record is filed in the cell's harness home.
            slug = claude._claude_project_slug(gen.workspace_path())
            link = os.path.join(home, "projects", slug)
            assert os.readlink(link) == os.path.join(harness_homes.harness_home("claude-code"), "projects", slug)
        finally:
            left = await _end(task, term)
        assert left == [], f"the CLI's process group outlived its leg: {left}"
        assert not os.path.lexists(link)
        state = json.loads(open(os.path.join(home, ".claude.json")).read())
        assert state["projects"][gen.workspace_path()]["hasTrustDialogAccepted"] is True

    run_on_test_loop(run())
    after = os.stat(user_credentials).st_mtime_ns if os.path.exists(user_credentials) else None
    assert after == before, "the user's own ~/.claude credentials were touched"


def _agy_replied(gemini: str, before, timeout: float = 240.0) -> None:
    from _agent_shared.harnesses.antigravity import backend as agy

    deadline = time.monotonic() + timeout
    messages: list = []
    while time.monotonic() < deadline:
        new = sorted(agy.conversation_ids(gemini) - before)
        if len(new) == 1:
            messages = agy.messages_from_conversation_db(agy.conversation_db_path(gemini, new[0]))
            if any(getattr(m, "role", "") == "assistant" and _BACKWARDS_ANSWER in str(getattr(m, "content", ""))
                   for m in messages):
                return
        time.sleep(1)
    raise AssertionError(f"agy never answered {_BACKWARDS_ANSWER}: {messages[-3:]}")


# agy's screens: its sign-in (first run in a fresh home), and its prompt.
_AGY_SIGN_IN = re.compile(rb"Select login method|You are currently not signed in")
_AGY_PROMPT = re.compile(rb"\? for shortcuts")


def test_antigravity_on_the_google_plan_runs_this_machines_agy_to_its_own_sign_in(
        native_bridge, monkeypatch, tmp_path):
    import gen
    from _agent_shared.cli_host import account_home, homes_root

    found = _found("antigravity")
    entry, signed_in = _entry("antigravity", monkeypatch, tmp_path)
    account = model_account("gemini", model="", harness="antigravity", auth_type="cli", plan="google")
    account["id"] = entry
    bind_model_account(account)
    native_bridge.permissions = {"docker": "none", "internet": "full", "commands": "ask"}
    placed = placement("antigravity", Grant(docker="none", internet="full", commands="ask"), "terminal",
                       auth="google")
    assert placed.ok and placed.runtime == "host" and placed.host["path"] == found["path"]
    terms = _host_terminals(native_bridge, monkeypatch)
    cfg = {"backend": "antigravity", "auth": "google", "provider": "gemini", "model": "",
           "commands": "ask", "websearch": False, "host_harness": found}

    async def run():
        task, term = await _leg("antigravity", cfg, terms)
        try:
            assert term.host and term.proc.args[0] == found["path"]
            with open(f"/proc/{term.proc.pid}/environ", "rb") as fh:
                environ = dict(item.split(b"=", 1) for item in fh.read().split(b"\0") if b"=" in item)
            home = account_home("antigravity", account)
            assert environ[b"HOME"] == home.encode()
            assert not {b"GEMINI_API_KEY", b"GOOGLE_API_KEY", b"GOOGLE_GEMINI_BASE_URL"} & set(environ)
            assert os.readlink(f"/proc/{term.proc.pid}/cwd") == os.path.realpath(gen.workspace_path())
            _private_from_cache_down(home, os.path.dirname(homes_root()))
            if signed_in:
                from _agent_shared.harnesses.antigravity import backend as agy

                before = agy.conversation_ids(os.path.join(home, ".gemini"))
                await asyncio.to_thread(term.wait_for, _AGY_PROMPT, timeout=180)
                await asyncio.to_thread(_type_line, term, _BACKWARDS_PROMPT)
                await asyncio.to_thread(_agy_replied, os.path.join(home, ".gemini"), before)
            else:
                # A fresh home: agy's own sign-in, which only the user's
                # browser completes. Nothing is chosen, so no flow starts.
                await asyncio.to_thread(term.wait_for, _AGY_SIGN_IN, timeout=120)
                assert "Google" in term.said()
            gemini = os.path.join(home, ".gemini")
            settings = json.load(open(os.path.join(gemini, "antigravity-cli", "settings.json")))
            assert "modelProvider" not in settings
            assert settings["trustedWorkspaces"] == [gen.workspace_path()]
            assert settings["permissions"] == {"ask": ["command(*)"]}
            servers = json.load(open(os.path.join(gemini, "config", "mcp_config.json")))["mcpServers"]
            assert servers["collimate"]["serverUrl"].startswith("http://127.0.0.1:")
        finally:
            left = await _end(task, term)
        assert left == [], f"agy's process group outlived its leg: {left}"

    run_on_test_loop(run())
