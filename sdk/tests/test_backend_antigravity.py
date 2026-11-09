# SPDX-License-Identifier: MIT
"""Unit tests for the antigravity backend's PURE helpers
(``antigravity_backend``) — the ACP server's token copy and the terminal leg's
``agy`` CLI.

The credential arrangement (the per-cell harness home and the Google login's
per-run copy), the config-file builders (the CLI's ``settings.json`` merge rule
and the exact ``mcp_config.json`` shape ``agy mcp add`` writes), the sign-in
guard, and the conversation-store reader (SQLite + hand-decoded protobuf) are
all bridge-free and deterministic, so they test in isolation against synthetic
inputs. The docker / ``gen`` / network paths are covered by the end-to-end
smoke — except for the container arrangement the TTY leg asks for, which is
asserted here by stubbing ``gen`` and ``ensure_env``: it has to MATCH the ACP
profile's, because one cached container serves both legs. Nothing here touches
docker, subprocess, or the network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import stat
import sys

import pytest
import gen


# The engine is generator-side shared code; import it the way the `agent`
# generator does (sibling on the generators path).
import _agent_shared as eng
from _agent_shared import harness_homes, login_store as ls
from _agent_shared.harnesses.antigravity import backend as ab  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from tests._workspace import GENERATORS


# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/antigravity.terminal"


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path):
    """The run's host workspace (``gen.workspace_path``)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(gen, "workspace_path", lambda: str(ws), raising=False)
    return ws


@pytest.fixture
def home(cache_root):
    from _agent_shared.harness_homes import harness_home

    return harness_home("antigravity")


def _write(path: str, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


_TOKEN = json.dumps({"type": "authorized_user", "client_id": "id", "client_secret": "secret",
                     "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token"}).encode()


# ── the home + the shared Google login ────────────────────────────────
#
# The contract: ~/.gemini inside the container is Collimate's per-cell harness
# home under BOTH auths (the user's real ~/.gemini is never read or written),
# and the google leg shares nothing but the LOGIN — as per-cell COPIES, because
# Google's refresh tokens do not rotate.


def test_ready_predicate_reads_the_servers_token_document(home):
    """The lease holds the ACP server's own ``acp_token.json``: an EMPTY file
    is no login (docker's mountpoint artifact, a truncated write), nor is a
    document without the keys the server refreshes with."""
    assert ls.selected_login_ready("antigravity", "google") is False

    _write(ab.shared_login_path(), b"")
    assert ls.selected_login_ready("antigravity", "google") is False
    _write(ab.shared_login_path(), b'{"refresh_token": "only"}')
    assert ls.selected_login_ready("antigravity", "google") is False
    _write(ab.shared_login_path(), _TOKEN)
    assert ls.selected_login_ready("antigravity", "google") is True


def test_refusals_say_what_to_do():
    assert not hasattr(ab, "TERMINAL_GOOGLE_REFUSED")
    assert "browser sign-in" in ab.BROWSER_SIGN_IN_REFUSED
    assert "Key manager" in ab.BROWSER_SIGN_IN_REFUSED and "AGY_ACP_FORCE_FILE_STORAGE=1" in ab.BROWSER_SIGN_IN_REFUSED


# ── promote: the first sign-in becomes everyone's ─────────────────────


# ── seed: the shared login becomes this cell's copy ───────────────────


# ── the container arrangement ─────────────────────────────────────────


def test_volumes_mount_only_the_per_cell_home_under_both_auths(home):
    """Both auths expose only vendor state through the workspace mount: the
    CLI's directory and the ACP server's, both legs binding the same two."""
    mount = {os.path.join(home, directory): {"bind": f"{ab.ANTIGRAVITY_HOME}/{directory}", "mode": "rw"}
             for directory in (ab.STATE_DIR, ab.ACP_STATE_DIR)}
    assert ab.antigravity_volumes("api_key") == mount
    _write(ab.shared_login_path(), _TOKEN)
    assert ab.antigravity_volumes("google") == mount
    real = os.path.expanduser("~/.gemini")
    assert not any(src == real or src.startswith(real + os.sep)
                   for src in ab.antigravity_volumes("google"))


def test_google_volumes_copy_the_token_0600_into_the_acp_directory(home):
    _write(ab.shared_login_path(), _TOKEN)
    ab.antigravity_volumes("google")
    copied = os.path.join(home, ab.ACP_STATE_DIR, ab.ACP_TOKEN_FILE)
    assert open(copied, "rb").read() == _TOKEN and stat.S_IMODE(os.stat(copied).st_mode) == 0o600
    assert not os.path.exists(os.path.join(home, ab.STATE_DIR, ab.ACP_TOKEN_FILE))


# ── settings.json ─────────────────────────────────────────────────────


def _settings(existing=None, *, commands="deny", model=""):
    return ab.settings_json(existing, commands=commands, model=model)


def test_settings_json_merges_and_sets_the_quiet_flags():
    """agy writes its own keys into that file while it runs (a colour scheme
    picked in the TUI) — we own the quiet flags, ``modelProvider``, the
    trusted folder and the commands keys, and merge into everything else.
    ``permissions`` is REPLACED: an "always allow" agy persisted there must not
    outlive the leg."""
    existing = {"colorScheme": "dark", "altScreenMode": True,
                "trustedWorkspaces": ["/workspace"],
                "permissions": {"allow": ["command(ls)"]}}
    out = _settings(existing)
    assert out["colorScheme"] == "dark"
    assert out["altScreenMode"] is True
    assert out["trustedWorkspaces"] == ["/workspace"]
    assert "command(ls)" not in json.dumps(out["permissions"])
    assert out["showTips"] is False
    assert out["showFeedbackSurvey"] is False
    assert out["notifications"] is False
    # agy 1.2.11 deletes enableTelemetry at startup whatever its value: it is
    # not a setting, so it is not written as if it were an opt-out.
    assert "enableTelemetry" not in out
    # Pure — the caller's dict is not mutated.
    assert "showTips" not in existing
    assert existing["permissions"] == {"allow": ["command(ls)"]}


def test_settings_json_trusts_the_workspace_and_nothing_else():
    """agy's folder-trust screen is answered "Yes, I trust this folder" for
    the workspace, as every file in the cell is trusted: ``trustedWorkspaces``
    names ``/workspace`` alone, REPLACING whatever the file held, so a wider
    path (``/``, the home) never rides along."""
    fresh = _settings()
    assert fresh["trustedWorkspaces"] == ["/workspace"]
    widened = _settings({"trustedWorkspaces": ["/", "/home/coder", "/workspace"]})
    assert widened["trustedWorkspaces"] == ["/workspace"]


def test_settings_json_leaves_the_colour_scheme_at_agys_default():
    """agy stores its default colour scheme (``terminal``, the first option)
    as NO key, so a fresh file carries none and agy's default applies."""
    assert "colorScheme" not in _settings()


def test_onboarding_json_is_the_record_agy_writes_when_the_screens_are_done():
    """Exactly the file agy 1.2.11 writes at ``cache/onboarding.json`` once a
    user finishes its first-run screens (probed: the same bytes whether the
    data-use box was ticked or not). With it, agy opens at its prompt: no
    colour screen and no data-use screen with its pre-ticked box."""
    assert ab.onboarding_json() == {"consumerOnboardingComplete": True,
                                    "enterpriseOnboardingComplete": False,
                                    "onboardingComplete": True}
    # A fresh dict each call: a caller that edits it cannot change the next.
    ab.onboarding_json()["onboardingComplete"] = False
    assert ab.onboarding_json()["onboardingComplete"] is True


def test_settings_json_model_provider_is_the_api_key_switch():
    """Verified live: ``GEMINI_API_KEY`` ALONE is ignored by the CLI
    ("authentication failed or timed out"); with this key it logs
    "ChainedAuth: authenticated via gemini_api_key". The terminal leg takes a
    key only, so the switch is always set."""
    assert _settings()["modelProvider"] == "gemini"
    assert _settings({"modelProvider": "other"})["modelProvider"] == "gemini"


def test_write_settings_is_private_atomic_and_idempotent(home):
    ab.write_settings(home, commands="deny")
    path = os.path.join(home, "antigravity-cli", "settings.json")
    assert json.load(open(path))["modelProvider"] == "gemini"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # The CONTAINER path the runner/profile name is the same file.
    assert ab.SETTINGS_PATH == "/home/coder/.gemini/antigravity-cli/settings.json"

    # A second write leaves the file untouched (no churn on every leg of
    # every run), and keeps what agy added to it.
    before = os.stat(path).st_mtime_ns
    ab.write_settings(home, commands="deny")
    assert os.stat(path).st_mtime_ns == before
    with open(path, "w") as fh:
        json.dump({"modelProvider": "gemini", "colorScheme": "dark"}, fh)
    ab.write_settings(home, commands="deny")
    assert json.load(open(path))["colorScheme"] == "dark"
    # Nothing half-written left behind by the temp + replace.
    state = os.path.dirname(path)
    assert sorted(os.listdir(state)) == ["cache", "settings.json"]
    assert os.listdir(os.path.join(state, "cache")) == ["onboarding.json"]


def test_write_settings_answers_the_first_run_screens(home):
    """The terminal leg's start writes the completed onboarding record beside the
    settings, 0600 like agy's own, so the TUI opens at its prompt in a home
    that is new each run. An incomplete or unreadable record is replaced; a
    right one is left alone."""
    path = os.path.join(home, "antigravity-cli", "cache", "onboarding.json")
    ab.write_settings(home, commands="ask")
    assert json.load(open(path)) == ab.onboarding_json()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    settings = json.load(open(os.path.join(home, "antigravity-cli",
                                           "settings.json")))
    assert settings["trustedWorkspaces"] == ["/workspace"]

    before = os.stat(path).st_mtime_ns
    ab.write_settings(home, commands="ask")
    assert os.stat(path).st_mtime_ns == before

    for stale in ('{"consumerOnboardingComplete": false, '
                  '"enterpriseOnboardingComplete": false, '
                  '"onboardingComplete": false}', "not json"):
        with open(path, "w") as fh:
            fh.write(stale)
        ab.write_settings(home, commands="deny")
        assert json.load(open(path)) == ab.onboarding_json()


# ── the commands grant: settings, hooks, read-only guard ──────────────


@pytest.mark.parametrize("commands, permissions", [
    ("deny", {"deny": ["command(*)"]}),
    ("ask", {"ask": ["command(*)"]}),
    ("allow", {}),
])
def test_terminal_leg_permissions_follow_the_commands_level(commands,
                                                             permissions):
    """The TUI's human answers agy's own prompts, so it gets only the level's
    rule: an ask rule outranks any "always allow" persisted from a prompt."""
    out = _settings(commands=commands)
    assert out["permissions"] == permissions


def test_deny_and_ask_pin_the_review_mode_and_turn_the_sandbox_off():
    """``always-proceed`` runs commands past an ask rule and past a hook's
    force_ask (verified in the TUI), and ``proceed-in-sandbox`` auto-approves
    sandboxed commands — so under deny / ask the mode is pinned to
    request-review and the sandbox (with its auto-approve) is off. Under allow
    a stale pin is dropped."""
    stale = {"toolPermission": "always-proceed", "enableTerminalSandbox": True}
    for commands in ("deny", "ask"):
        out = _settings(dict(stale), commands=commands)
        assert out["toolPermission"] == "request-review"
        assert out["enableTerminalSandbox"] is False
    out = _settings(dict(stale), commands="allow")
    assert "toolPermission" not in out and "enableTerminalSandbox" not in out


def test_hooks_json_gates_every_exec_tool_per_level():
    """Deny and ask register Collimate's hook on each exec tool (anchored
    matchers, one group per tool, the ACP server's ``shell`` among them);
    allow registers none. The hook answers at once, so its timeout is short."""
    assert "shell" in ab.EXEC_TOOLS
    assert ab.hooks_json("allow") == {}
    for level in ("deny", "ask"):
        groups = ab.hooks_json(level)["collimate-commands"]["PreToolUse"]
        assert [g["matcher"] for g in groups] == [
            f"^{name}$" for name in ab.EXEC_TOOLS]
        for group in groups:
            (handler,) = group["hooks"]
            assert handler["command"] == f"/usr/local/bin/collimate-agy-hook {level}"
            assert handler["type"] == "command"
            assert handler["timeout"] == ab.HOOK_TIMEOUT_S


def test_the_exec_tool_list_is_the_hooks_own():
    import importlib.util

    path = os.path.join(GENERATORS,
                        "_agent_shared", "harnesses", "antigravity", "image", "agy_hook.py")
    spec = importlib.util.spec_from_file_location("agy_hook_list", path)
    hook = importlib.util.module_from_spec(spec)
    # No .pyc beside it: that directory is the image's docker build context.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(hook)
    finally:
        sys.dont_write_bytecode = written
    assert hook.EXEC_TOOLS == ab.EXEC_TOOLS


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_command_guard_declares_readonly_content_outside_workspace__D05(home, commands):
    volumes = ab.antigravity_volumes("api_key")
    assert set(volumes) == {os.path.join(home, ab.STATE_DIR), os.path.join(home, ab.ACP_STATE_DIR)}
    policy = ab.command_guard_files(commands)
    assert policy == {ab.HOOKS_PATH: json.dumps(ab.hooks_json(commands)) + "\n"}
    assert not os.path.exists(os.path.join(home, "config"))


# ── mcp_config.json ───────────────────────────────────────────────────


def test_terminal_mcp_config_only_names_the_collimate_http_bridge_F1474():
    from _agent_shared.mcp_bridge import SERVER_NAME
    url = "http://host.docker.internal:9/private/mcp"
    text = ab.mcp_config_json(url)
    assert json.loads(text) == {"mcpServers": {
        SERVER_NAME: {"disabled": False, "serverUrl": url}}}
    assert text.endswith("\n")
    assert json.loads(ab.mcp_config_json(None)) == {"mcpServers": {}}
    with pytest.raises(TypeError, match="URL"):
        ab.mcp_config_json([{"name": "retired-list", "url": url}])


# ── the conversation store ────────────────────────────────────────────
#
# agy keeps one SQLite database per conversation and stores each step as a
# protobuf blob for which no .proto is published. The fixtures below encode
# those blobs BY HAND (wire format: varint key = field << 3 | wire type) so the
# reader is tested against the real byte layout rather than against itself.


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _v(num: int, value: int) -> bytes:
    """A varint field."""
    return _varint(num << 3) + _varint(value)


def _m(num: int, blob: bytes) -> bytes:
    """A length-delimited field (a nested message, or a string)."""
    return _varint((num << 3) | 2) + _varint(len(blob)) + blob


def _s(num: int, text: str) -> bytes:
    return _m(num, text.encode("utf-8"))


def _user_step(text: str, *, nested: bool = False) -> bytes:
    # {1: 14, 19: {2: text}} — or the {19: {3: {1: text}}} spelling.
    body = _m(3, _s(1, text)) if nested else _s(2, text)
    return _v(1, 14) + _m(19, body)


def _agent_step(text: str) -> bytes:
    return _v(1, 15) + _m(20, _s(1, text))


def _tool_step(name: str, raw_input: str) -> bytes:
    # {5: {4: {1: callId, 2: name, 3: rawInputJson}}} on a step whose type is
    # not one of the text ones.
    call = _s(1, "call-1") + _s(2, name) + _s(3, raw_input)
    return _v(1, 7) + _m(5, _m(4, call))


def _make_db(path: str, payloads: list, *, types: "list | None" = None) -> str:
    """A conversation database with agy's observed ``steps`` schema. WAL, like
    the real store — the reader must cope with the journal mode the CLI sets."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
        "status INTEGER, has_subtrajectory INTEGER, metadata BLOB, "
        "error_details BLOB, permissions BLOB, task_details BLOB, "
        "render_info BLOB, step_payload BLOB, step_format INTEGER)")
    for i, payload in enumerate(payloads):
        step_type = types[i] if types is not None else None
        con.execute("INSERT INTO steps (idx, step_type, step_payload) "
                    "VALUES (?, ?, ?)", (i, step_type, payload))
    con.commit()
    con.close()
    return path


def test_trace_from_conversation_db_decodes_a_real_layout(tmp_path):
    """The whole reader end to end: a user prompt (both text spellings), an
    assistant message split across consecutive rows, a tool call, and the two
    row types that are not conversation."""
    path = _make_db(str(tmp_path / "conv.db"), [
        _user_step("what is here?"),
        _agent_step("Let me "),
        _agent_step("look."),
        _tool_step("view_file", '{"path": "a.py"}'),
        _v(1, 23) + _m(30, _s(1, "a title")),      # title update — ignored
        _agent_step("It is a module."),
        _v(1, 17) + _m(24, _m(3, _s(1, "boom"))),  # error step — ignored
        _user_step("thanks", nested=True),
    ], types=[14, 15, 15, 7, 23, 15, 17, 14])

    assert ab.trace_from_conversation_db(path) == [
        {"type": "user", "text": "what is here?"},
        # Consecutive 15-rows are stream segments of ONE message.
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "tool": "view_file", "input": {"path": "a.py"}},
        {"type": "text", "text": "It is a module."},
        {"type": "user", "text": "thanks"},
    ]
    # …and the transcript messages are user + assistant TEXT only.
    assert [(m.role, m.content) for m in ab.messages_from_conversation_db(path)] == [
        ("user", "what is here?"), ("assistant", "Let me look."),
        ("assistant", "It is a module."), ("user", "thanks"),
    ]


def test_trace_reads_the_step_type_from_the_payload_when_the_column_is_null(tmp_path):
    """The column is the CLI's own index; the payload's field 1 is the truth.
    A build that stops filling the column must not blank the whole export."""
    path = _make_db(str(tmp_path / "c.db"),
                    [_user_step("hi"), _agent_step("hello")])
    assert ab.trace_from_conversation_db(path) == [
        {"type": "user", "text": "hi"}, {"type": "text", "text": "hello"}]


def test_trace_keeps_unparseable_tool_arguments_as_text(tmp_path):
    path = _make_db(str(tmp_path / "c.db"),
                    [_tool_step("run_command", "not json {")], types=[7])
    assert ab.trace_from_conversation_db(path) == [
        {"type": "tool_use", "tool": "run_command", "input": "not json {"}]


@pytest.mark.parametrize("make", [
    lambda p: p,                                        # nothing there at all
    lambda p: _write(p, b"not a database"),             # garbage
    lambda p: _write(p, b""),                           # caught mid-create
])
def test_trace_never_raises(tmp_path, make):
    """Best-effort by contract: a store that is absent, locked, half-written or
    laid out by a build whose field numbers moved yields [] — the leg then
    reconciles less, it does not fail."""
    assert ab.trace_from_conversation_db(make(str(tmp_path / "c.db"))) == []
    assert ab.messages_from_conversation_db(str(tmp_path / "c.db")) == []


def test_trace_reads_a_live_store_while_agy_still_holds_it_open(tmp_path):
    """The property the LIVE tail rests on: agy keeps the conversation open in
    WAL mode for the whole turn, so the export has to read a database with a
    hot ``-wal`` and an active writer — read-only, never checkpointing it — and
    see each committed step as it lands."""
    path = str(tmp_path / "live.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, "
                "step_payload BLOB)")
    try:
        con.execute("INSERT INTO steps VALUES (?, ?, ?)", (0, 14, _user_step("q")))
        con.commit()
        assert os.path.exists(path + "-wal")     # still hot: nothing checkpointed
        assert ab.trace_from_conversation_db(path) == [{"type": "user", "text": "q"}]

        con.execute("INSERT INTO steps VALUES (?, ?, ?)", (1, 15, _agent_step("a")))
        con.commit()
        assert ab.trace_from_conversation_db(path) == [
            {"type": "user", "text": "q"}, {"type": "text", "text": "a"}]
    finally:
        con.close()


def test_trace_survives_a_row_whose_payload_is_not_protobuf(tmp_path):
    """One unreadable row must not cost the rows around it."""
    path = _make_db(str(tmp_path / "c.db"), [
        _user_step("q"), b"\xff\xff\xff\xff", _agent_step("a")],
        types=[14, 15, 15])
    assert ab.trace_from_conversation_db(path) == [
        {"type": "user", "text": "q"}, {"type": "text", "text": "a"}]


def test_trace_does_not_glue_text_across_an_undecoded_row(tmp_path):
    """Only CONSECUTIVE 15-rows are one message: a row between them that
    decodes to nothing still ends the first message."""
    path = _make_db(str(tmp_path / "c.db"), [
        _agent_step("Let me look."), _v(1, 99), _agent_step("It is a module.")],
        types=[15, 99, 15])
    assert ab.trace_from_conversation_db(path) == [
        {"type": "text", "text": "Let me look."},
        {"type": "text", "text": "It is a module."}]


def test_trace_reads_a_store_under_a_path_with_a_percent_sign(tmp_path):
    """SQLite percent-decodes URI filenames, so a literal ``%`` must be
    escaped or the open resolves a different path and exports nothing."""
    path = _make_db(str(tmp_path / "pct%41dir" / "c.db"), [_user_step("q")])
    assert ab.trace_from_conversation_db(path) == [{"type": "user", "text": "q"}]


# ── locating a conversation ───────────────────────────────────────────


def test_conversation_paths_search_both_store_directories(home):
    """agy has used two store layouts and a per-cell home outlives a CLI
    upgrade, so both are searched — and the path of a conversation that has no
    record yet is still the one the CURRENT build would create, which is what
    makes ``os.path.exists`` on it the "has this conversation happened" test."""
    current = os.path.join(home, "antigravity-cli", "conversations")
    legacy = os.path.join(home, "antigravity", "conversations")
    assert ab.conversation_db_path(home, "c1") == os.path.join(current, "c1.db")
    assert ab.conversation_db_path(home, "") == ""

    _make_db(os.path.join(legacy, "old.db"), [_user_step("q")])
    assert ab.conversation_db_path(home, "old.db"[:-3]) == \
        os.path.join(legacy, "old.db")
    # A conversation the current build wrote wins its own path back.
    _make_db(os.path.join(current, "new.db"), [_user_step("q")])
    assert ab.conversation_db_path(home, "old") == os.path.join(legacy, "old.db")


def test_the_store_lists_every_conversation_record(home):
    """A conversation is its ``<id>.db``, in either store directory (a
    per-cell home outlives a CLI upgrade); the ``-wal`` / ``-shm`` siblings
    are not conversations, and a home with no store lists none."""
    assert ab.conversation_ids(home) == frozenset()
    current = os.path.join(home, "antigravity-cli", "conversations")
    legacy = os.path.join(home, "antigravity", "conversations")
    _make_db(os.path.join(current, "new-conv.db"), [_user_step("this run")])
    _make_db(os.path.join(legacy, "old-conv.db"), [_user_step("last run")])
    _write(os.path.join(current, "new-conv.db-wal"), b"x")
    _write(os.path.join(current, "orphan.db-shm"), b"x")
    assert ab.conversation_ids(home) == {"new-conv", "old-conv"}


# ── the terminal leg's container request ──────────────────────────────


class _Stop(Exception):
    """Ends the TTY runner at the terminal-surface boundary — everything after
    it is the PTY and the renderer."""


class _FakeContainer:
    def __init__(self, rec):
        self.rec = rec

    def exec_run(self, argv, user=None):
        cmd = " ".join(argv)
        self.rec["execs"].append(cmd)
        # Decode what write_container_file just wrote, so a test can assert on
        # the FILE rather than on the shell line that carried it.
        m = re.search(r"echo '?([A-Za-z0-9+/=]*)'? \| base64 -d > '?([^' ]+)'?", cmd)
        if m:
            self.rec["files"][m.group(2)] = base64.b64decode(m.group(1)).decode()

        class _Res:
            exit_code = 0
            output = b""
        return _Res()


class _FakeEnv:
    container_id = "container-1"

    def __init__(self, rec):
        self.rec = rec
        self.container = _FakeContainer(rec)

    def exec(self, argv, **kwargs):
        result = self.container.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


@pytest.fixture
def stub_terminal(monkeypatch, cache_root, selected_account_runtime):
    """Drive the selected Antigravity terminal entry as far as its terminal registration,
    with the container and the run surface stubbed out. Returns the
    recorded calls."""
    target = selected_account_runtime / 'antigravity' / ab.ACP_TOKEN_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_TOKEN)
    from _agent_shared import cli_container as cc

    rec: dict = {"logs": [], "execs": [], "files": {}, "ensure": {}, "serve": {},
                 "env": _FakeEnv({})}
    rec["env"] = _FakeEnv(rec)

    async def _ensure_env(name, **kw):
        rec["ensure"] = {"name": name, **kw}
        return rec["env"]

    def _serve_terminal(container_id, command, **kw):
        rec["serve"] = {"container_id": container_id, "command": list(command), **kw}
        raise _Stop()

    async def _api_key(provider, **kw):
        rec["api_key_provider"] = provider
        return "gemini-key-1"

    async def _update(**kw):
        return None


    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    @asynccontextmanager
    async def _own_bridge(**kwargs):
        try:
            yield SimpleNamespace(url_container='http://host.docker.internal:9876/owned-native-mcp',
                                  url_host='http://127.0.0.1:9876/owned-native-mcp')
        finally:
            rec['bridge_closed'] = True
    monkeypatch.setattr(cc, "leg_bridge", _own_bridge)
    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(gen, "commit", _update, raising=False)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gen, "update_file", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gen, "log", lambda m: rec["logs"].append(m),
                        raising=False)
    return rec


def _run_terminal(*, resume_harness_session_id: str = "", **cfg):
    base = {"backend": "antigravity", "auth": "api_key", "provider": "gemini",
            "model": "", "commands": "ask"}
    base.update(cfg)
    with pytest.raises(_Stop):
        asyncio.run(eng.surface_runner("antigravity", "terminal")(
            base, pointer=_POINTER, extra_system="be brief", resume_harness_session_id=resume_harness_session_id))


def test_terminal_leg_writes_settings_before_the_container_starts(
        stub_terminal, home):
    """agy reads settings.json at startup and it decides HOW the first turn
    authenticates, so it is written host-side on our half of the mount — never
    through an exec into the container it is there to configure."""
    _run_terminal()
    data = json.load(open(os.path.join(home, "antigravity-cli", "settings.json")))
    assert data["modelProvider"] == "gemini"
    assert data["trustedWorkspaces"] == ["/workspace"]
    onboarding = os.path.join(home, "antigravity-cli", "cache",
                              "onboarding.json")
    assert json.load(open(onboarding)) == ab.onboarding_json()
    assert not any("settings.json" in cmd for cmd in stub_terminal["execs"])


def test_api_key_leg_carries_the_key_and_names_the_provider(stub_terminal, home):
    """The selected key rides only the native TTY env, and settings.json names
    ``modelProvider`` — without which agy ignores GEMINI_API_KEY entirely."""
    _run_terminal(auth="api_key")
    assert stub_terminal["api_key_provider"] == "gemini"
    assert "env_vars" not in stub_terminal["ensure"]
    assert stub_terminal["serve"]["env"]["GEMINI_API_KEY"] == "gemini-key-1"
    assert stub_terminal["serve"]["env"]["TERM"] == "xterm-256color"
    data = json.load(open(os.path.join(home, "antigravity-cli", "settings.json")))
    assert data["modelProvider"] == "gemini"
    # No login bookkeeping on this leg at all.
    assert not os.path.exists(os.path.join(home, ab.ACP_STATE_DIR, ab.ACP_TOKEN_FILE))


def test_terminal_leg_plants_the_seam_and_the_mcp_config_where_both_legs_look(
        stub_terminal):
    """The seams land at fixed paths in the shared container (the ACP server
    takes the prompt and the bridge over ACP and ignores both)."""
    _run_terminal()
    files = stub_terminal["files"]
    assert files[ab.CONTEXT_SEAM_PATH].strip() == "be brief"
    assert json.loads(files[ab.MCP_CONFIG_PATH]) == {"mcpServers": {"collimate": {
        "disabled": False, "serverUrl": "http://host.docker.internal:9876/owned-native-mcp"}}}
    assert stub_terminal["bridge_closed"] is True
    # The workspace fallback is NOT used when the real seam carried the text.
    assert "/workspace/GEMINI.md" not in files


def test_terminal_leg_replaces_previous_acp_mcp_config_with_own_bridge__D02(
        stub_terminal, monkeypatch):
    """Replace stale ACP config with only the owned native terminal bridge."""
    stub_terminal["files"][ab.MCP_CONFIG_PATH] = json.dumps({"mcpServers": {
        "foreign": {"serverUrl": "http://old-stale-server"}}})
    _run_terminal()
    assert json.loads(stub_terminal["files"][ab.MCP_CONFIG_PATH]) == {
        "mcpServers": {"collimate": {"disabled": False,
            "serverUrl": "http://host.docker.internal:9876/owned-native-mcp"}}}


def test_the_terminal_starts_a_fresh_conversation_the_tail_follows(
        stub_terminal, home):
    """agy's TTY names its own conversation: it is never started on an id of
    ours — not even one whose record exists — and the live tail follows the
    conversation it writes once it appears. The pointer declares no toolbar
    button."""
    _make_db(ab.conversation_db_path(home, "conv-xyz"), [_user_step("q")])
    for resume in ("", "conv-xyz"):
        _run_terminal(resume_harness_session_id=resume)
        assert stub_terminal["serve"]["command"] == ["agy"]
        assert stub_terminal["serve"]["buttons"] == []


# ── the conversation an ended TTY wrote (the spec's harness_session_ids) ─────


def test_the_tty_sessions_lookup_lists_this_cells_seeded_store_on_a_key(
        cache_root, monkeypatch):
    """On a Gemini key the spec's lookup lists the store of this cell's
    harness home — which the home's first use seeds from the cell's persisted
    store, so a listing taken before the TTY starts already holds every
    earlier conversation."""
    import _agent_shared as eng

    spec = eng.backend_spec("antigravity")
    assert spec.terminal_session_tier == "A"
    from _agent_shared.harnesses import resolve
    assert resolve(spec.harness_session_ids) is ab.harness_session_ids
    monkeypatch.setattr(gen, "account", lambda role: {
        "id": "key-entry", "service": "gemini", "auth_type": "api_key", "protocol": "gemini",
        "metadata": {"harness": "antigravity"}, "secrets": {"api_key": "k"}})

    home = harness_homes.harness_home("antigravity")
    _make_db(ab.conversation_db_path(home, "conv-earlier"), [_user_step("q")])
    harness_homes.close_harness_homes()        # persisted into the cell's store
    assert ab.harness_session_ids() == {"conv-earlier"}
    harness_homes.close_harness_homes()


def test_the_tty_sessions_lookup_lists_the_entrys_own_agy_home_on_the_google_plan(cache_root):
    """On the Google plan the TTY is this machine's agy, whose conversations
    live in the entry's per-account home (HOME/.gemini), the one its sign-in
    is in: that is the store listed before it starts."""
    from _agent_shared.cli_host import account_home

    gemini = os.path.join(account_home("antigravity"), ab.HOST_GEMINI_DIR)
    assert ab.terminal_home() == gemini
    _make_db(ab.conversation_db_path(gemini, "conv-signed-in"), [_user_step("q")])
    assert ab.harness_session_ids() == {"conv-signed-in"}


def test_the_one_conversation_that_appeared_is_the_ttys():
    """``new_tty_session``: the TTY's conversation is the one id the store
    gained while it ran — none gained is none written."""
    from _agent_shared import new_tty_session

    before = frozenset({"conv-old"})
    assert new_tty_session(before, {"conv-old", "conv-tty"}) == "conv-tty"
    assert new_tty_session(before, {"conv-old"}) == ""
    assert new_tty_session(frozenset(), set()) == ""


def test_several_new_conversations_are_none_of_them():
    """A subagent's own conversation, or a second one started in the TUI:
    which one the TTY ended on is unknowable, so none is taken."""
    from _agent_shared import new_tty_session

    assert new_tty_session({"conv-old"},
                           {"conv-old", "conv-main", "conv-sub"}) == ""


def test_an_id_the_store_held_is_never_the_ttys_whatever_its_mtime(home):
    """The per-cell store copy keeps file times, so an earlier conversation
    can carry an mtime past the leg's start (a clock stepped back, a
    ``touch -d`` from an earlier run's shell): it was in the listing taken
    before the TTY started, so it is never taken for the TTY's — alone, or
    beside the one the TTY wrote."""
    import time

    from _agent_shared import new_tty_session

    old = _make_db(ab.conversation_db_path(home, "conv-old"), [_user_step("q")])
    os.utime(old, (time.time() + 86400, time.time() + 86400))
    before = ab.conversation_ids(home)
    assert new_tty_session(before, ab.conversation_ids(home)) == ""
    _make_db(ab.conversation_db_path(home, "conv-tty"), [_user_step("q")])
    assert new_tty_session(before, ab.conversation_ids(home)) == "conv-tty"


def _finalize(home, before, monkeypatch) -> dict:
    """Run the terminal teardown's export; returns what it wrote."""
    written: dict = {}
    monkeypatch.setattr(gen, "update_file",
                        lambda path, content, **kw: written.update({path: content}),
                        raising=False)
    monkeypatch.setattr(gen, "log", lambda m: None, raising=False)
    from _agent_shared import transcript_merge
    monkeypatch.setattr(transcript_merge, "write_merged_transcript",
                        lambda messages, path: 0)
    ab._finalize_antigravity_terminal(home, before=before)
    return written


def test_the_teardown_exports_the_conversation_the_leg_adopts(home, monkeypatch):
    """The runner's teardown picks by the leg's rule, so the raw record names
    the conversation the TTY wrote: never an earlier one — however recent its
    file times — and nothing when several appeared."""
    import time

    old = _make_db(ab.conversation_db_path(home, "conv-old"),
                   [_user_step("last run")])
    os.utime(old, (time.time() + 86400, time.time() + 86400))
    before = ab.conversation_ids(home)
    assert _finalize(home, before, monkeypatch) == {}

    _make_db(ab.conversation_db_path(home, "conv-tty"), [_user_step("this run")])
    written = _finalize(home, before, monkeypatch)
    assert json.loads(written[ab._SESSION_FILE])["conversation_id"] == "conv-tty"

    _make_db(ab.conversation_db_path(home, "conv-sub"), [_user_step("sub")])
    assert _finalize(home, before, monkeypatch) == {}


def test_terminal_command_omits_model_for_the_default_sentinel(stub_terminal):
    """Blank or the sentinel means "agy picks" — and agy has no way to spell
    that other than passing no ``--model`` (an empty slug is rejected outright,
    before init)."""
    _run_terminal(model=ab.DEFAULT_MODEL_SENTINEL)
    assert stub_terminal["serve"]["command"] == ["agy"]
    assert stub_terminal["serve"]["label"] == "Antigravity · default"

    _run_terminal(model="gemini-3.8-flash-high")
    assert stub_terminal["serve"]["command"] == \
        ["agy", "--model", "collimate:model:gemini-3.8-flash-high"]
    assert stub_terminal["serve"]["label"] == "Antigravity · gemini-3.8-flash-high"


def test_terminal_under_deny_denies_the_agents_commands(stub_terminal, home):
    """The terminal opens under deny: agy's deny rule and the hook refuse
    every command the agent chooses. A line the human types after `!` is the
    human's own."""
    _run_terminal(commands="deny")
    assert stub_terminal["serve"]["command"] == ["agy"]
    data = json.load(open(os.path.join(home, "antigravity-cli", "settings.json")))
    assert data["permissions"] == {"deny": ["command(*)"]}
    assert data["toolPermission"] == "request-review"
    assert data["enableTerminalSandbox"] is False
    assert json.loads(stub_terminal["ensure"]["readonly_files"][ab.HOOKS_PATH]) == ab.hooks_json("deny")
    assert all(spec["mode"] == "rw" for spec in
               stub_terminal["ensure"]["volumes"].values())


def test_terminal_skips_permissions_only_under_allow(stub_terminal, home):
    """allow is unattended; ask keeps agy's own prompts (the ask rule and the
    hook's force_ask) for the human in the TTY."""
    _run_terminal(commands="allow")
    assert "--dangerously-skip-permissions" in stub_terminal["serve"]["command"]
    _run_terminal(commands="ask")
    assert "--dangerously-skip-permissions" not in stub_terminal["serve"]["command"]
    data = json.load(open(os.path.join(home, "antigravity-cli", "settings.json")))
    assert data["permissions"] == {"ask": ["command(*)"]}
    assert data["toolPermission"] == "request-review"
    assert json.loads(stub_terminal["ensure"]["readonly_files"][ab.HOOKS_PATH]) == ab.hooks_json("ask")


_HOST_AGY = {"path": "/opt/agy/bin/agy", "version": "1.4.7", "binary": "agy"}


def _run_google_terminal(**cfg):
    _run_terminal(auth="google", host_harness=dict(_HOST_AGY), **cfg)


def test_the_google_plan_terminal_is_this_machines_agy_with_no_credential(
        stub_terminal, cache_root, monkeypatch):
    """The Google plan's CLI is the unmodified agy on this machine: no
    container, no lease, no key; HOME is the entry's per-account home, where
    agy keeps the sign-in its own browser flow writes; the bridge is on
    loopback; the workspace is the cell's real path."""
    from _agent_shared.cli_host import account_home

    bound = {}
    from _agent_shared import cli_container as cc
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def _bridge(**kwargs):
        bound.update(kwargs)
        yield SimpleNamespace(url_container="http://host.docker.internal:1/x",
                              url_host="http://127.0.0.1:9876/owned-native-mcp")
    monkeypatch.setattr(cc, "leg_bridge", _bridge)
    materialized = []
    monkeypatch.setattr(gen, "materialize_account", lambda role: materialized.append(role))
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-key-never-passed")
    _run_google_terminal(commands="ask", model="")
    serve = stub_terminal["serve"]
    home = account_home("antigravity")
    assert serve["container_id"] is None
    assert serve["command"] == [_HOST_AGY["path"]]
    assert serve["cwd"] == gen.workspace_path()
    assert serve["env"]["HOME"] == home and serve["env"]["TERM"] == "xterm-256color"
    assert not {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL"} & set(serve["env"])
    assert stub_terminal["ensure"] == {} and materialized == []
    assert "api_key_provider" not in stub_terminal
    assert bound["bind_host"] == "127.0.0.1"
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    gemini = os.path.join(home, ab.HOST_GEMINI_DIR)
    settings = json.load(open(os.path.join(gemini, ab.STATE_DIR, "settings.json")))
    assert "modelProvider" not in settings and "customModelsConfig" not in settings
    assert settings["trustedWorkspaces"] == [gen.workspace_path()]
    assert settings["permissions"] == {"ask": ["command(*)"]}
    # The first-run screens (terms, data use) are agy's own on this plan.
    assert not os.path.exists(os.path.join(gemini, ab.STATE_DIR, "cache", "onboarding.json"))
    hooks = json.load(open(os.path.join(gemini, "config", "hooks.json")))
    assert hooks == ab.hooks_json("ask", ab.host_hook())
    assert ab.host_hook().endswith("image/agy_hook.py")
    assert json.load(open(os.path.join(gemini, "config", "mcp_config.json"))) == {"mcpServers": {
        "collimate": {"disabled": False, "serverUrl": "http://127.0.0.1:9876/owned-native-mcp"}}}
    assert open(os.path.join(gemini, "config", "rules", "collimate-context.md")).read().strip() == "be brief"


def test_the_google_plan_terminal_keeps_its_home_across_cells_and_runs_per_entry(
        stub_terminal, cache_root, monkeypatch):
    """One sign-in per Key manager entry: the same entry's home in another
    cell, another entry's elsewhere."""
    _run_google_terminal()
    first = stub_terminal["serve"]["env"]["HOME"]
    harness_homes.close_harness_homes()
    monkeypatch.setattr(gen, "run_info", lambda: type("Info", (), {"cell_id": "another-cell",
                                                                 "run_id": "r2"})())
    _run_google_terminal()
    assert stub_terminal["serve"]["env"]["HOME"] == first
    other = {"id": "another-entry", "service": "gemini", "auth_type": "cli", "protocol": "cli",
             "metadata": {"harness": "antigravity", "plan": "google"}, "secrets": {}}
    monkeypatch.setattr(gen, "account", lambda role: dict(other))
    _run_google_terminal()
    assert stub_terminal["serve"]["env"]["HOME"] != first
    assert os.path.basename(stub_terminal["serve"]["env"]["HOME"]) == "another-entry"


@pytest.mark.parametrize("commands", ["ask", "allow"])
def test_the_google_plan_terminal_passes_the_level_into_agys_settings(stub_terminal, cache_root, commands):
    from _agent_shared.cli_host import account_home

    _run_google_terminal(commands=commands, model="gemini-3-pro")
    command = stub_terminal["serve"]["command"]
    assert command[:3] == [_HOST_AGY["path"], "--model", "gemini-3-pro"]
    assert ("--dangerously-skip-permissions" in command) is (commands == "allow")
    gemini = os.path.join(account_home("antigravity"), ab.HOST_GEMINI_DIR)
    settings = json.load(open(os.path.join(gemini, ab.STATE_DIR, "settings.json")))
    assert settings["permissions"] == ({"ask": ["command(*)"]} if commands == "ask" else {})
    assert json.load(open(os.path.join(gemini, "config", "hooks.json"))) == (
        ab.hooks_json("ask", ab.host_hook()) if commands == "ask" else {})


def test_the_google_plan_terminal_needs_agy_on_this_machine(stub_terminal, cache_root):
    with pytest.raises(RuntimeError, match="this machine's agy, which was not found"):
        _run_terminal(auth="google", host_harness=None)
    assert stub_terminal["serve"] == {}


def test_a_gemini_key_terminal_stays_in_its_container(stub_terminal, home):
    _run_terminal(auth="api_key")
    assert stub_terminal["serve"]["container_id"] == _FakeEnv.container_id
    assert stub_terminal["ensure"]["name"] == "antigravity"
    assert stub_terminal["serve"]["env"]["GEMINI_API_KEY"] == "gemini-key-1"


def test_oauth_settings_name_no_provider_and_declare_no_model():
    data = ab.settings_json({"modelProvider": "gemini", "theme": "kept"}, commands="ask",
                            model="gemini-3-pro", auth="google", workspace="/real/ws")
    assert "modelProvider" not in data and data["theme"] == "kept"
    assert "customModelsConfig" not in data
    assert data["trustedWorkspaces"] == ["/real/ws"]
    key = ab.settings_json(None, commands="ask", model="gemini-3-pro")
    assert key["modelProvider"] == "gemini" and key["trustedWorkspaces"] == ["/workspace"]


def test_terminal_leg_serves_the_pointer_it_is_handed(stub_terminal):
    _run_terminal()
    assert stub_terminal["serve"]["pointer"] == _POINTER
    assert stub_terminal["serve"]["keep_alive"] is True
    assert stub_terminal["serve"]["cwd"] == "/workspace"


@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime):
    snapshot = {"id": "google-entry", "service": "gemini", "auth_type": "cli", "protocol": "cli",
                "metadata": {"harness": "antigravity", "plan": "google"}, "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot) as (root, _select_plan):
        yield root



# ── the server's tools that run code without asking (security review 1, 9) ──


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_below_allow_the_tools_that_run_code_without_asking_are_withheld(commands):
    from _agent_shared.harnesses.antigravity import backend as agy
    disabled = agy.session_meta("", websearch=True, commands=commands)["antigravity"]["disabledTools"]
    unasked = set(agy.SERVER_UNASKED_TOOLS)
    assert {"run_workflow", "manage_task", "schedule", "invoke_subagent"} <= unasked
    if commands == "allow":
        assert not unasked & set(disabled) and "run_command" not in disabled
    else:
        assert unasked <= set(disabled)
        assert ("run_command" in disabled) is (commands == "deny")
    assert "view_file" in disabled


def test_the_recogniser_names_the_same_tools_as_the_backend():
    from types import SimpleNamespace
    from _agent_shared.harnesses.antigravity import backend as agy, recognise
    from _agent_shared.harnesses import HARNESS
    assert recognise.UNASKED == frozenset(agy.SERVER_UNASKED_TOOLS)
    assert "harnesses.antigravity.recognise:server_exec_tool" in HARNESS["antigravity"].commands.recognisers
    for name in agy.SERVER_UNASKED_TOOLS:
        call = SimpleNamespace(raw_input={"toolName": name}, kind="other", title=name)
        assert recognise.server_exec_tool(None, call, name)
    read = SimpleNamespace(raw_input={"toolName": "client_view_file"}, kind="read", title="client_view_file")
    assert not recognise.server_exec_tool(None, read, "client_view_file")


def test_a_request_naming_an_unasked_tool_is_a_command_for_the_gate():
    from types import SimpleNamespace
    from _agent_shared import acp_policy
    session = SimpleNamespace(backend="antigravity")
    call = SimpleNamespace(kind="other", title="run_workflow", raw_input={"toolName": "run_workflow"})
    assert acp_policy._is_command_request(session, call)


@pytest.mark.parametrize("level", ["deny", "ask"])
def test_on_the_acp_leg_the_hook_denies_what_the_server_never_asks_about(level):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path as _Path
    from _agent_shared.harnesses.antigravity import backend as agy
    spec = spec_from_file_location("agy_hook_under_test", _Path(agy.__file__).parent / "image" / "agy_hook.py")
    hook = module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert set(hook.EXEC_TOOLS) == set(agy.EXEC_TOOLS)

    def decide(name, **args):
        return hook.decide(level, {"toolCall": {"name": name, "args": args}}, acp_server=True)["decision"]

    # The server's shell asks the client itself: the hook defers to that gate.
    assert decide("run_command") == "allow" and decide("shell") == "allow"
    for name in ("run_workflow", "schedule", "notebook_execution", "execute_browser_javascript"):
        assert decide(name) == "deny", name
    assert decide("manage_task", Action="send_input") == "deny"
    assert decide("manage_task", Action="list") == "allow"
