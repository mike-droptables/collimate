# SPDX-License-Identifier: MIT
"""Unit tests for the goose backend's pure helpers (``goose_backend``).

Only the bridge-free, docker-free helpers are exercised here: the env dict,
the ``goose run`` argv, the stdout reply extraction, and the session-jsonl →
trace normalization. Live container round-trips are covered by the end-to-end
smoke, not here. No generator NAMES or shipped-asset filenames are asserted on
— only synthetic inputs.
"""
from __future__ import annotations

import json
import shlex

import pytest

# The backend is generator-side shared code; import it the way the `agent`
# generator does (sibling on the generators path).
from _agent_shared import harness_homes
import _agent_shared as eng  # noqa: E402
from _agent_shared.harnesses.goose import backend as gb  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/goose.terminal"



def _env(**over):
    """goose_env with sensible defaults, overridable per test; ``provider``
    is the Collimate provider, mapped through goose's adapter."""
    from _agent_shared.harnesses.goose.adapter import ADAPTER
    base = dict(provider="openai", model="gpt-5", tools_enabled=True, commands="allow")
    base.update(over)
    provider = base.pop("provider")
    return gb.goose_env(native=ADAPTER.native_provider(provider).id, **base)


# ── goose_env ─────────────────────────────────────────────────────────

def test_goose_env_leaves_sampling_to_the_model():
    """No sampling or output knob reaches goose: the model's own defaults
    apply, so goose_env takes none and sets no GOOSE_TEMPERATURE or
    GOOSE_MAX_TOKENS."""
    import inspect

    params = inspect.signature(gb.goose_env).parameters
    assert "temperature" not in params and "max_tokens" not in params
    env = _env()
    assert "GOOSE_TEMPERATURE" not in env
    assert "GOOSE_MAX_TOKENS" not in env
    assert env["GOOSE_MODE"] == "auto"


def test_goose_env_gemini_maps_to_google_provider_and_key():
    env = _env(provider="gemini", model="gemini-2.5-flash")
    assert env["GOOSE_PROVIDER"] == "google"
    assert env["GOOSE_MODEL"] == "gemini-2.5-flash"
    assert "GOOGLE_API_KEY" not in env
    # never the anthropic var, never a GEMINI_API_KEY
    assert "ANTHROPIC_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


@pytest.mark.parametrize("provider,keyvar", [
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("gemini", "GOOGLE_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("cerebras", "CEREBRAS_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
])
def test_goose_native_file_key_per_provider(provider, keyvar, tmp_path):
    env = _env(provider=provider)
    assert keyvar not in env
    path = tmp_path / "secrets.yaml"
    gb.write_api_credentials(str(tmp_path), "secrets.yaml", provider, "secret")
    assert json.loads(path.read_text()) == {keyvar: "secret"}
    # the provider id passes through unchanged except the gemini→google remap
    assert env["GOOSE_PROVIDER"] == ("google" if provider == "gemini" else provider)


def test_goose_refuses_a_provider_it_does_not_bill():
    with pytest.raises(ValueError, match="cannot bill provider"):
        _env(provider="unsupported-provider")


def test_goose_env_keyring_and_telemetry_always_set():
    env = _env()
    assert env["GOOSE_DISABLE_KEYRING"] == "1"
    assert env["GOOSE_TELEMETRY_ENABLED"] == "false"


def test_goose_env_mode_auto_when_tools_enabled():
    assert _env(tools_enabled=True)["GOOSE_MODE"] == "auto"


def test_goose_env_mode_chat_when_tools_disabled():
    assert _env(tools_enabled=False)["GOOSE_MODE"] == "chat"


# ── goose_argv ────────────────────────────────────────────────────────


# ── extract_goose_reply ───────────────────────────────────────────────


# ── trace_from_goose_session ──────────────────────────────────────────

def test_trace_from_goose_session_roles_text_and_tools():
    record = json.dumps({"messages": [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll list them."},
            {"type": "toolRequest",
             "toolCall": {"value": {"name": "shell", "arguments": {"command": "ls"}}}},
        ]},
        {"role": "user", "content": [
            {"type": "toolResponse", "name": "shell",
             "toolResult": {"value": "a.txt\nb.txt"}},
        ]},
        [1, 2, 3],    # not a dict → skipped
        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
    ]})
    trace = gb.trace_from_goose_session(record)
    assert [e["type"] for e in trace] == \
        ["user", "text", "tool_use", "tool_result", "text"]
    assert trace[0]["text"] == "list the files"
    assert trace[1]["text"] == "I'll list them."
    assert trace[2]["tool"] == "shell"
    assert trace[2]["input"] == {"command": "ls"}
    assert trace[3]["tool"] == "shell"
    assert "a.txt" in trace[3]["output"]
    assert trace[4]["text"] == "Done."


def test_trace_from_goose_session_wrapped_message_object():
    # A message wrapped under a "message" key parses the same.
    record = json.dumps({"messages": [{"message": {"role": "user", "content": "hi there"}}]})
    trace = gb.trace_from_goose_session(record)
    assert trace == [{"type": "user", "text": "hi there"}]


def test_trace_from_goose_session_reads_only_the_export_document():
    """The record is goose's ``session export --format json`` document; a
    record that is not one — empty, garbage, or one JSON object per line —
    holds no messages."""
    assert gb.trace_from_goose_session("") == []
    assert gb.trace_from_goose_session("not json\n{bad}\n") == []
    assert gb.trace_from_goose_session("\n   \n") == []
    per_line = '{"role":"user","content":"a"}\n{"role":"assistant","content":"b"}\n'
    assert gb.trace_from_goose_session(per_line) == []


# ── goose_cell_extension_args (pure) ──────────────────────────────────
#
# Cell MCP servers → repeatable `goose run` extension flags. goose 1.42 has no
# config.yaml here: http rides --with-streamable-http-extension (url only —
# headers unsupported, dropped with a note), stdio rides --with-extension with
# inline env. Secrets are already literals (run_agent pre-resolves).


def test_goose_record_entries_read_the_export_document():
    """The exporter reads ``session export --format json``: its messages sit
    at the top, under ``session``, or form a bare list."""
    from _agent_shared.harnesses.goose import backend as gb

    doc = json.dumps({"session": {"id": "s1"}, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]})
    assert [e["type"] for e in gb.trace_from_goose_session(doc)] == ["user", "text"]
    nested = json.dumps({"session": {"messages": [{"role": "user", "content": "in"}]}})
    assert gb.trace_from_goose_session(nested) == [{"type": "user", "text": "in"}]
    top = json.dumps([{"role": "user", "content": "plain"}])
    assert gb.trace_from_goose_session(top) == [{"type": "user", "text": "plain"}]
    assert gb.trace_from_goose_session("") == []


def testgoose_session_export_uses_the_cli(monkeypatch):
    """The export goes to a file in the container, read whole (an exec's
    output past the host's cap keeps only its tail) and then removed."""
    from _agent_shared.harnesses.goose import backend as gb

    calls: list = []

    class _Env:
        def __init__(self):
            self.files: dict = {}

        def exec(self, argv, **kw):
            calls.append(argv)
            if argv[:2] == ["rm", "-f"]:
                self.files.pop(argv[2])
            else:
                words = shlex.split(argv[-1])
                self.files[words[words.index(">") + 1]] = (
                    "some banner\n" + json.dumps({"messages": []}))
            return 0, ""

        def read_file(self, path, **kw):
            return self.files[path].encode()

    env = _Env()
    out = gb.goose_session_export(env, "20260830_1")
    assert out.startswith("{") and json.loads(out) == {"messages": []}
    assert len(calls) == 2
    assert "session export --session-id 20260830_1 --format json > /tmp/" in calls[0][-1]
    assert calls[1][:2] == ["rm", "-f"] and env.files == {}


class _GooseStore:
    """A goose container's session store (``{id: record}``, oldest first):
    ``session list --format json`` answers the rows newest first, ``export``
    writes one record to the file it redirects to (read back with
    ``read_file``); ``list_rc`` fails the listing."""

    def __init__(self, sessions, list_rc: int = 0):
        self.sessions = dict(sessions)
        self.list_rc = list_rc
        self.exported: list = []
        self.files: dict = {}

    def exec(self, argv, **kw):
        cmd = argv[-1]
        if "session list" in cmd:
            if self.list_rc:
                return self.list_rc, ""
            return 0, json.dumps([{"id": sid, "description": "x"}
                                  for sid in reversed(list(self.sessions))])
        if argv[:2] == ["rm", "-f"]:
            self.files.pop(argv[2], None)
            return 0, ""
        words = shlex.split(cmd)
        sid = words[words.index("--session-id") + 1]
        self.exported.append(sid)
        self.files[words[words.index(">") + 1]] = json.dumps(self.sessions[sid])
        return 0, ""

    def read_file(self, path, **kw):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path].encode()


def _record(text: str) -> dict:
    return {"conversation": [{"role": "user", "content": [{"type": "text", "text": "q"}]},
                             {"role": "assistant", "content": [{"type": "text", "text": text}]}]}


@pytest.fixture
def goose_written(monkeypatch):
    from _agent_shared.harnesses.goose import backend as gb
    from _agent_shared import transcript_merge

    written: dict = {}
    monkeypatch.setattr(gb.gen, "update_file",
                        lambda path, content, **kw: written.__setitem__(path, content),
                        raising=False)
    monkeypatch.setattr(gb.gen, "log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(transcript_merge, "write_merged_transcript",
                        lambda msgs, **kw: written.__setitem__("transcript", msgs) or len(msgs))
    return written


def test_a_fresh_goose_terminal_never_exports_an_earlier_runs_session__F1528(goose_written):
    """F1528: a fresh TTY ended before it wrote a session of its own exports
    nothing — the store's newest session (``-l 1``) is an earlier run's."""
    from _agent_shared.harnesses.goose import backend as gb

    env = _GooseStore({"20260901_1": _record("earlier run")})
    before = gb.harness_session_ids(env)
    gb._finalize_goose_terminal(env, harness_session_id="", before=before)
    assert env.exported == [] and goose_written == {}


def test_a_fresh_goose_terminal_exports_the_one_session_it_wrote__F1528(goose_written):
    from _agent_shared.harnesses.goose import backend as gb

    env = _GooseStore({"20260901_1": _record("earlier run")})
    before = gb.harness_session_ids(env)
    env.sessions["20260928_1"] = _record("this run")
    gb._finalize_goose_terminal(env, harness_session_id="", before=before)
    assert env.exported == ["20260928_1"]
    assert goose_written["transcript"][-1].content == "this run"


@pytest.mark.parametrize("case", ["no-snapshot", "two-new", "unlistable"])
def test_no_single_newgoose_session_exports_nothing__F1528(goose_written, case):
    from _agent_shared.harnesses.goose import backend as gb

    env = _GooseStore({"20260901_1": _record("earlier run")})
    before = None if case == "no-snapshot" else gb.harness_session_ids(env)
    env.sessions["20260928_1"] = _record("a")
    if case == "two-new":
        env.sessions["20260928_2"] = _record("b")
    if case == "unlistable":
        env.list_rc = 1
    gb._finalize_goose_terminal(env, harness_session_id="", before=before)
    assert env.exported == [] and goose_written == {}


def test_a_goose_store_unlistable_at_leg_start_exports_nothing__F1528(goose_written):
    """F1528: a listing that fails when the leg starts leaves it with no
    snapshot (``_listed_before`` is None), so nothing is exported even once
    the store lists again. Read as an empty store, the failed listing would
    have made the earlier run's session look new."""
    env = _GooseStore({"20260901_1": _record("earlier run")}, list_rc=1)
    before = gb._listed_before(env)
    assert before is None
    env.list_rc = 0
    gb._finalize_goose_terminal(env, harness_session_id="", before=before)
    assert env.exported == [] and goose_written == {}


class _ListingEnv:
    """A container whose ``goose session list`` answers ``(rc, out)``."""

    def __init__(self, rc, out):
        self.rc, self.out = rc, out

    def exec(self, argv, **kw):
        return self.rc, self.out


def test_a_goose_listing_is_empty_only_when_blank_or_the_empty_notice__F1528():
    """F1528: an empty store's listing may be goose's plain notice rather
    than ``[]``, so a blank listing or ``No sessions found`` is an empty
    store, and a banner before the JSON is skipped. Any other output with no
    JSON in it raises, as do a failed listing and JSON that does not parse:
    read as an empty store, it would make an earlier run's session look
    new."""
    assert gb.harness_session_ids(_ListingEnv(0, "No sessions found\n")) == frozenset()
    assert gb.harness_session_ids(_ListingEnv(0, "")) == frozenset()
    assert gb.harness_session_ids(_ListingEnv(0, "a banner\n" + json.dumps(
        [{"id": "20260901_1"}]))) == {"20260901_1"}
    for rc, out in ((1, ""), (0, "[{\"id\": "),
                    (0, "20260901_1  x  2026-09-01\n"), (0, "warning: store busy\n")):
        with pytest.raises(Exception):
            gb.harness_session_ids(_ListingEnv(rc, out))


def test_a_leg_start_listing_with_no_json_exports_nothing__F1528(goose_written):
    """F1528: when the leg starts, the listing exits 0 with output that is
    neither JSON nor the empty-store notice while the store holds an earlier
    run's session; when it ends, the listing is JSON. The start listing is no
    snapshot, so the TTY, which wrote no session, exports nothing (read as
    an empty store, it would have made the earlier session look new)."""
    env = _GooseStore({"20260901_1": _record("earlier run")})
    before = gb._listed_before(_ListingEnv(0, "20260901_1  earlier run\n"))
    assert before is None
    gb._finalize_goose_terminal(env, harness_session_id="", before=before)
    assert env.exported == [] and goose_written == {}


def test_the_goose_runner_snapshots_the_store_before_a_fresh_tty__F1528(
        goose_written, monkeypatch):
    """A fresh goose TTY: the store is listed before the terminal starts, the
    snapshot reaches ``on_ready`` (the live tail follows only the one new
    session), and the teardown exports only the session the TTY wrote."""
    import asyncio
    import contextlib

    import _agent_shared as eng
    from _agent_shared import cli_container as cc
    from _agent_shared import mcp_bridge

    class _Env(_GooseStore):
        container_id = "cid"

        def exec(self, argv, **kw):
            if "session list" in argv[-1] or "session export" in argv[-1]:
                return super().exec(argv, **kw)
            return 0, ""

    env = _Env({"20260901_1": _record("earlier run")})

    async def _ensure_env(name, **kw):
        return env

    async def _key(provider, **kw):
        return "k"

    async def _noop(**kw):
        return None

    @contextlib.asynccontextmanager
    async def _serve_terminal(container_id, command, **kw):
        yield {"terminal_id": "tty-1"}

    @contextlib.asynccontextmanager
    async def _live_files(**kw):
        yield None

    async def _until(**kw):
        env.sessions["20260928_1"] = _record("this run")     # the user chats

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(harness_homes, "harness_home", lambda backend: f"/tmp/home-{backend}")
    monkeypatch.setattr(mcp_bridge, "start_bridge", lambda **k: None)
    monkeypatch.setattr(cc, "write_container_file", lambda *a, **k: None)
    monkeypatch.setattr(cc, "write_policy_file", lambda *a, **k: None)
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(gb.gen, "commit", _noop, raising=False)
    for name in ("phase",):
        monkeypatch.setattr(gb.gen, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb.gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(gb.gen, "live_files", _live_files, raising=False)
    monkeypatch.setattr(eng, "serve_tty_until", _until, raising=False)
    ready: list = []
    asyncio.run(eng.surface_runner("goose", "terminal")(
        {"backend": "goose", "provider": "openrouter", "model": "m",
         "commands": "allow", "websearch": False}, pointer=_POINTER,
        on_ready=lambda sid, **kw: ready.append((sid, kw))))
    assert ready == [("", {"before": frozenset({"20260901_1"})})]
    assert env.exported == ["20260928_1"]
    assert goose_written["transcript"][-1].content == "this run"


# ── the commands grant (deny < ask < allow) ───────────────────────────

_NEVER_KEPT = ("summon", "Extension Manager", "extensionmanager", "scheduler",
               "code_execution", "orchestrator", "computercontroller")


@pytest.mark.parametrize("commands, mode", [("allow", "auto"), ("ask", "approve"), ("deny", "auto")])
def test_goose_mode_per_level(commands, mode):
    assert _env(commands=commands)["GOOSE_MODE"] == mode
    assert _env(commands=commands, tools_enabled=False)["GOOSE_MODE"] == "chat"


def test_allow_keeps_gooses_own_extensions():
    assert "EXTENSIONS" not in _env(commands="allow")


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_extension_allowlist_below_allow(commands):
    """deny drops developer's shell (available_tools filters listing AND
    dispatch); ask keeps it. Neither keeps summon (auto-mode delegates), the
    extension manager (enables more extensions) or the scheduler (recipes in
    auto mode)."""
    ext = json.loads(_env(commands=commands)["EXTENSIONS"])
    dev = ext["developer"]
    assert dev["type"] == "platform" and dev["enabled"] is True
    assert ("shell" in dev["available_tools"]) is (commands == "ask")
    assert {"write", "edit", "tree"} <= set(dev["available_tools"])
    assert not set(ext) & set(_NEVER_KEPT)
    assert all(e["type"] == "platform" and e["enabled"] for e in ext.values())


def _entry(ext: dict, key: str = "") -> dict:
    return {"extension": ext, "extensionKey": key or ext.get("name", "")}


def _session_entries(commands: str) -> list:
    """What goose lists for a session opened under ``commands`` (its
    EXTENSIONS allowlist) plus the bridge this leg handed it."""
    out = [_entry(dict(ext)) for ext in gb.goose_extensions(commands).values()]
    out.append(_entry({"type": "mcp", "server": {
        "name": "collimate", "type": "http", "url": "http://h/mcp"}}, "collimate"))
    return out


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_a_session_opened_at_the_level_passes_its_check(commands):
    assert gb.extension_violations(_session_entries(commands), commands,
                                   servers=("collimate",)) == []


def test_a_session_from_a_looser_level_fails_the_check():
    """session/load restores the stored extensions, not the leg's allowlist:
    an ask session's shell under deny, allow's summon / extension manager /
    scheduler and a stored stdio server are all caught."""
    ask = _session_entries("ask")
    assert gb.extension_violations(ask, "deny", servers=("collimate",)) == [
        "developer (shell)"]
    looser = [_entry({"type": "platform", "name": "developer"}),
              _entry({"type": "platform", "name": "summon"}),
              _entry({"type": "platform", "name": "extensionmanager"}),
              _entry({"type": "platform", "name": "scheduler"}),
              _entry({"type": "mcp", "server": {"name": "pwn", "command": "sh",
                                                "args": ["-c", "id"]}})]
    assert gb.extension_violations(looser, "ask", servers=("collimate",)) == [
        "developer (all tools)", "summon", "extensionmanager", "scheduler", "pwn"]
    assert gb.extension_violations(looser, "allow") == []


_TUI_BRIDGE = "http://host.docker.internal:41234/mcp"


def _tui_bridge_entry(url: str = _TUI_BRIDGE) -> dict:
    """What goose's CLI saves for ``--with-streamable-http-extension <url>``:
    an http MCP server named from the URL (goose's ``name_to_key``)."""
    return _entry({"type": "mcp", "server": {
        "type": "http", "name": "host_docker_internal_41234_mcp", "url": url,
        "headers": []}}, "host_docker_internal_41234_mcp")


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_the_terminal_legs_live_bridge_passes_the_check(commands):
    """After a terminal leg: the TTY's bridge, saved by goose's CLI under a
    URL-derived name, is this run's own live bridge, so a later session/load
    of the TTY's conversation keeps it. Anything else still fails:
    a stdio server under that name, an http server at another URL, or that
    URL once its bridge is gone."""
    live = frozenset({_TUI_BRIDGE, "http://127.0.0.1:41234/mcp"})
    entries = _session_entries(commands) + [_tui_bridge_entry()]
    assert gb.extension_violations(entries, commands, servers=("collimate",),
                                   bridge_urls=live) == []
    assert gb.extension_violations(entries, commands, servers=("collimate",)) == [
        "host_docker_internal_41234_mcp"]
    stranger = _tui_bridge_entry("http://evil.example:41234/mcp")
    stdio = _entry({"type": "mcp", "server": {
        "name": "host_docker_internal_41234_mcp", "command": "sh",
        "args": ["-c", "id"], "url": _TUI_BRIDGE}})
    unknown = _entry({"type": "mcp", "server": {"name": "pwn", "type": "sse",
                                                "url": _TUI_BRIDGE}})
    for extra in (stranger, stdio, unknown):
        assert gb.extension_violations(_session_entries(commands) + [extra], commands,
                                       servers=("collimate",), bridge_urls=live) != []


def test_run_bridge_urls_are_every_bridge_this_run_started(monkeypatch):
    """The cached bridges' endpoints and every terminal leg's — a leg's
    bridge stops with its TTY, but the session it wrote still names it
    (F1526: the terminal's conversation stays loadable)."""
    from types import SimpleNamespace

    from _agent_shared import cli_container as cc

    monkeypatch.setattr(cc, "_BRIDGES", {
        "a": SimpleNamespace(url_container="http://host.docker.internal:6/mcp",
                             url_host="http://172.17.0.1:6/mcp"),
        "b": None,
        "c": SimpleNamespace(url_container="http://host.docker.internal:5/mcp",
                             url_host="http://127.0.0.1:5/mcp")})
    monkeypatch.setattr(cc, "_LEG_BRIDGE_URLS", {
        _TUI_BRIDGE, "http://172.17.0.1:41234/mcp"})
    assert cc.run_bridge_urls() == {
        _TUI_BRIDGE, "http://172.17.0.1:41234/mcp",
        "http://host.docker.internal:6/mcp", "http://172.17.0.1:6/mcp",
        "http://host.docker.internal:5/mcp", "http://127.0.0.1:5/mcp"}


def test_the_profiles_check_reads_gooses_own_extension_list():
    """The ACP leg asks goose which extensions the loaded session carries
    (``_goose/unstable/session/extensions/list``) and names the extras."""
    import asyncio
    from types import SimpleNamespace

    calls: list = []

    class _Conn:
        async def ext_method(self, method, params):
            calls.append((method, params))
            return {"extensions": _session_entries("ask")}

    session = SimpleNamespace(conn=_Conn(), acp_session_id="g1", commands="deny",
                              mcp_server_names=("collimate",))
    reason = asyncio.run(gb.verify_loaded(session))
    assert calls == [("goose/unstable/session/extensions/list", {"sessionId": "g1"})]
    assert "developer (shell)" in reason
    session.commands = "ask"
    assert asyncio.run(gb.verify_loaded(session)) == ""


@pytest.mark.parametrize("commands", ["deny", "ask"])
def test_the_profiles_check_accepts_this_runs_terminal_bridge(monkeypatch, commands):
    """F1526: a session/load of the TTY's conversation lists the TTY's
    bridge; the check takes this run's own bridges
    (``cli_container.run_bridge_urls``), a stopped terminal leg's included.
    The leg's bridge runs through ``leg_bridge`` and has stopped before the
    check; the cached gateway bridges are empty."""
    import asyncio
    from types import SimpleNamespace

    from _agent_shared import cli_container as cc
    from _agent_shared import mcp_bridge

    class _Conn:
        async def ext_method(self, method, params):
            return {"extensions": _session_entries(commands) + [_tui_bridge_entry()]}

    class _LegBridge:
        url_container = _TUI_BRIDGE
        url_host = "http://172.17.0.1:41234/mcp"
        stopped = False

        def stop(self):
            self.stopped = True

    session = SimpleNamespace(conn=_Conn(), acp_session_id="g1", commands=commands,
                              mcp_server_names=("collimate",))
    monkeypatch.setattr(cc, "_BRIDGES", {})
    monkeypatch.setattr(cc, "_LEG_BRIDGE_URLS", set())
    assert "host_docker_internal_41234_mcp" in asyncio.run(
        gb.verify_loaded(session))

    monkeypatch.setattr(mcp_bridge, "start_bridge", lambda **kw: _LegBridge())

    async def _terminal_leg():
        async with cc.leg_bridge(extra_entries=[("web_fetch", "web", False, "", lambda: None)]) as bridge:
            return bridge

    bridge = asyncio.run(_terminal_leg())
    assert bridge.stopped and cc._BRIDGES == {}
    assert asyncio.run(gb.verify_loaded(session)) == ""


@pytest.mark.parametrize("commands, ask_before", [("ask", ["shell"]), ("deny", []), ("allow", [])])
def test_permission_yaml_is_fresh_and_grants_nothing(commands, ask_before):
    import yaml
    doc = yaml.safe_load(gb.permission_yaml(commands))
    assert doc == {"user": {"always_allow": [], "ask_before": ask_before, "never_allow": []}}


class _PolicyEnv:
    """A container whose execs report ``exit_code``; records (script, user)."""

    def __init__(self, exit_code=0):
        self.container = self
        self.exit_code = exit_code
        self.runs: list = []

    def exec_run(self, argv, user=None, **kw):
        self.runs.append((argv[-1], user))

        class _R:
            exit_code = self.exit_code
            output = b"no space left on device"
        return _R()

    def exec(self, argv, **kwargs):
        result = self.exec_run(argv, user=kwargs.get("user"))
        return result.exit_code, result.output.decode("utf-8")


def test_permission_file_lands_in_gooses_config_dir_as_goose_own_file():
    """The fresh permission.yaml is written as goose's own user (goose saves
    an "Always Allow" answer there during a leg) in its config dir."""
    import base64
    import re

    env = _PolicyEnv()
    gb.write_permission_file(env, "ask")
    [(script, user)] = env.runs
    assert user == "coder"
    m = re.search(r"mkdir -p (\S+) && echo '?([A-Za-z0-9+/=]*)'? \| base64 -d > (\S+)", script)
    assert m and m.group(1) == "/home/coder/.config/goose"
    assert m.group(3) == gb.GOOSE_PERMISSION_PATH
    assert base64.b64decode(m.group(2)).decode() == gb.permission_yaml("ask")


def test_a_permission_file_that_does_not_land_stops_the_leg():
    """A stale permission.yaml would carry an "Always Allow" answer into the
    leg, so a write that fails raises instead of passing silently."""
    with pytest.raises(RuntimeError, match="permission store"):
        gb.write_permission_file(_PolicyEnv(exit_code=1), "ask")


@pytest.mark.parametrize("version", ["1.50.0", "1.51.2", "goose 2.0.0"])
def test_host_goose_new_enough_keeps_the_level(version):
    for commands in ("deny", "ask", "allow"):
        assert gb.host_commands_level(commands, version.split()[-1]) == (commands, "")


@pytest.mark.parametrize("commands", ["deny", "ask"])
@pytest.mark.parametrize("version", ["1.49.9", "", "garbage"])
def test_host_goose_too_old_is_refused_below_allow(commands, version):
    with pytest.raises(ValueError, match="1.50.0"):
        gb.host_commands_level(commands, version)
    assert gb.host_commands_level("allow", version) == ("allow", "")


@pytest.mark.parametrize("commands, value, ok", [
    ("ask", "approve", True), ("ask", "chat", True), ("ask", "auto", False),
    ("ask", "smart_approve", False),
    ("deny", "auto", True), ("allow", "auto", True), ("allow", "smart_approve", True),
])
def test_goose_mode_rule(commands, value, ok):
    from _agent_shared import acp_run
    assert acp_run.mode_allowed("goose", commands, value) is ok


class _Stop(Exception):
    pass


@pytest.mark.parametrize("commands, mode", [
    ("deny", "auto"), ("ask", "approve"), ("allow", "auto")])
def test_terminal_leg_runs_the_levels_mode_with_a_fresh_permission_file(monkeypatch, commands, mode):
    """The TTY runs the level's mode from a permission.yaml with no saved
    "always allow": under ask goose's own prompt asks the human before every
    command; under deny the agent has no shell tool."""
    import asyncio

    from _agent_shared import cli_container as cc

    written: dict = {}
    seen: dict = {}

    class _Env:
        container_id = "cid"

        def exec(self, argv, **kw):
            return 0, ""

    async def _ensure_env(name, **kw):
        seen["volumes"] = kw.get("volumes")
        seen["env_vars"] = kw.get("env_vars")
        return _Env()

    async def _key(provider, **kw):
        return "k"

    async def _noop(**kw):
        return None

    class _Serve:
        def __init__(self, cid, command, **kw):
            seen.update(command=command, term_env=kw["env"])

        async def __aenter__(self):
            raise _Stop()

        async def __aexit__(self, *a):
            return False

    from _agent_shared import mcp_bridge

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(harness_homes, "harness_home", lambda backend: f"/tmp/home-{backend}")
    monkeypatch.setattr(mcp_bridge, "start_bridge", lambda **k: None)
    monkeypatch.setattr(cc, "write_container_file",
                        lambda env, path, content, **kw: written.update({path: content}))
    monkeypatch.setattr(cc, "write_policy_file",
                        lambda env, path, content, **kw: written.update({path: content}))
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(gb.gen, "commit", _noop, raising=False)
    monkeypatch.setattr(gb.gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb.gen, "log", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb.gen, "serve_terminal", _Serve, raising=False)
    with pytest.raises(_Stop):
        asyncio.run(eng.surface_runner("goose", "terminal")(
            {"backend": "goose", "provider": "openrouter", "model": "google/gemini-3.8-flash",
             "commands": commands, "websearch": False}, pointer=_POINTER))
    assert seen["term_env"]["GOOSE_MODE"] == mode
    # Session store and private native config mounts match ACP; no exec key env.
    assert seen["volumes"] == {"/tmp/home-goose": {
        "bind": "/home/coder/.local/share/goose", "mode": "rw"},
        "/tmp/home-goose/native-config": {"bind": "/home/coder/.config/goose", "mode": "rw"}}
    assert not seen["env_vars"]
    assert "OPENROUTER_API_KEY" not in seen["term_env"]
    assert written[gb.GOOSE_PERMISSION_PATH] == gb.permission_yaml(commands)
    assert ("EXTENSIONS" in seen["term_env"]) is (commands != "allow")
    if commands != "allow":
        tools = json.loads(seen["term_env"]["EXTENSIONS"])["developer"]["available_tools"]
        assert ("shell" in tools) is (commands == "ask")


def test_the_terminal_leg_stops_its_own_bridge__F1526(monkeypatch):
    """F1526: the TTY's workspace and web tools ride its own bridge
    (``cli_container.leg_bridge``), handed to goose as its streamable-http
    extension and stopped when the leg ends — here, when the terminal
    registration fails — not left serving on a live capability URL until the
    run ends. Its URL stays one of the run's own, so a later session/load
    still accepts the extension goose saved."""
    import asyncio

    from _agent_shared import cli_container as cc
    from _agent_shared import mcp_bridge

    seen: dict = {}

    class _Env:
        container_id = "cid"

        def exec(self, argv, **kw):
            return 0, ""

    class _Bridge:
        url_container = "http://host.docker.internal:41234/cap/mcp"
        url_host = "http://172.17.0.1:41234/cap/mcp"

        def stop(self):
            seen["stopped"] = True

    async def _ensure_env(name, **kw):
        return _Env()

    async def _key(provider, **kw):
        return "k"

    async def _noop(**kw):
        return None

    class _Serve:
        def __init__(self, cid, command, **kw):
            seen["command"] = command

        async def __aenter__(self):
            raise _Stop()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cc, "_LEG_BRIDGE_URLS", set())
    monkeypatch.setattr(harness_homes, "harness_home", lambda backend: f"/tmp/home-{backend}")
    monkeypatch.setattr(mcp_bridge, "start_bridge", lambda **k: _Bridge())
    monkeypatch.setattr(cc, "write_container_file", lambda *a, **k: None)
    monkeypatch.setattr(cc, "write_policy_file", lambda *a, **k: None)
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(gb.gen, "commit", _noop, raising=False)
    for name in ("phase", "log"):
        monkeypatch.setattr(gb.gen, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb.gen, "serve_terminal", _Serve, raising=False)
    with pytest.raises(_Stop):
        asyncio.run(eng.surface_runner("goose", "terminal")(
            {"backend": "goose", "provider": "openrouter", "model": "m",
             "commands": "ask", "websearch": True}, pointer=_POINTER))
    i = seen["command"].index("--with-streamable-http-extension")
    assert seen["command"][i + 1] == _Bridge.url_container
    assert seen.get("stopped") is True
    assert _Bridge.url_container in cc.run_bridge_urls()


def test_harness_listing_preserves_vendor_json_identity__F0287():
    raw = json.dumps([{"session_id": "vendor-conversation"}])
    assert gb.harness_session_ids(_ListingEnv(0, raw)) == {"vendor-conversation"}


# ── provider endpoint (the registry endpoint, in goose's own shape) ─────

@pytest.mark.parametrize("provider,expected", [
    ("anthropic", {"ANTHROPIC_HOST": "https://api.anthropic.com"}),
    ("gemini", {"GOOGLE_HOST": "https://generativelanguage.googleapis.com"}),
    ("openai", {"OPENAI_HOST": "https://api.openai.com", "OPENAI_BASE_PATH": "v1/chat/completions"}),
    ("openrouter", {"OPENROUTER_HOST": "https://openrouter.ai"}),
    ("groq", {}),
    ("cerebras", {}),
])
def test_goose_env_points_each_provider_at_the_registry_endpoint(provider, expected):
    env = _env(provider=provider)
    hosts = {k: v for k, v in env.items() if k.endswith(("_HOST", "_BASE_PATH"))}
    assert hosts == expected


def test_goose_env_host_follows_a_replaced_registry_endpoint(monkeypatch):
    from collimate import providers
    monkeypatch.setitem(providers.STANDARD_MODEL_ENDPOINTS, "openrouter", "http://h:9/api/v1")
    assert _env(provider="openrouter")["OPENROUTER_HOST"] == "http://h:9"


@pytest.mark.parametrize("provider,url", [
    ("groq", "https://api.groq.com/openai/v1/chat/completions"),
    ("cerebras", "https://api.cerebras.ai/v1/chat/completions"),
])
def test_goose_groq_cerebras_override_file_carries_the_registry_endpoint(provider, url):
    body = json.loads(gb.custom_provider_json(provider, "m"))
    assert body["name"] == provider and body["base_url"] == url
    assert body["api_key_env"] == f"{provider.upper()}_API_KEY"
    assert gb.custom_provider_json("openrouter", "m") is None
