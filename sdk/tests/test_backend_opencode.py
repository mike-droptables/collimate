# SPDX-License-Identifier: MIT
"""Unit tests for the opencode backend (``generators/_agent_shared/
opencode_backend.py``).

Pure, container-free helpers — the config/argv builders, the export trace
normalizer (what the persisted ``_agent/trace.json`` is built from), the
model-id mapping — plus the credential arrangement: the shared PLAN login
store (chatgpt / github_copilot / xai), the readiness predicates the bind turns
on, the promote that fills it, and ``opencode_volumes``. The terminal leg's
container/TTY request is asserted here too, by stubbing ``gen`` and
``ensure_env``: it has to MATCH the ACP profile's mounts, because one cached
container serves both legs. The real container round trips need Docker + a
live model and are covered by the end-to-end smoke. No generator NAMES or
shipped-asset filenames are asserted on — only synthetic inputs, and never a
real credential.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import json
import os
import re

import pytest


# opencode is generator-side shared code; import it the way the engine does
# (sibling on the generators path). This pulls in gen + collimate, which resolve
# under the repo venv (see how tests/test_agent_engine.py imports the engine).
from _agent_shared import login_store as ls
import _agent_shared as eng  # noqa: E402
from _agent_shared.harnesses.opencode import backend as oc  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402

from tests._workspace import GENERATORS


async def _no_credentials(adapter, *, provider, plan):
    """The pipeline's credential step for a test whose renderer is a fake."""
    return _pipeline.Credentials(provider, plan, key="" if plan else "sk-test")


# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/opencode.terminal"


# ── opencode_config ───────────────────────────────────────────────────


# ── opencode_run_argv ─────────────────────────────────────────────────


# ── _opencode_model_id ────────────────────────────────────────────────

def test_opencode_model_id_gemini_maps_google():
    assert oc.native_id("gemini") == "google"
    assert oc._opencode_model_id("google", "gemini-3.8-flash") == "google/gemini-3.8-flash"
    # idempotent: an already-prefixed id is left alone.
    assert oc._opencode_model_id("google", "google/gemini-3.8-flash") == \
        "google/gemini-3.8-flash"


def test_opencode_model_id_prefix_once():
    # A bare model gets the provider prefixed once.
    assert oc._opencode_model_id("anthropic", "claude-sonnet-4-6") == \
        "anthropic/claude-sonnet-4-6"
    # An openrouter vendor/model slug still gets openrouter/ prepended (exactly once)…
    assert oc._opencode_model_id("openrouter", "anthropic/claude-sonnet-4.6") == \
        "openrouter/anthropic/claude-sonnet-4.6"
    # …and an already-routed openrouter id is unchanged.
    assert oc._opencode_model_id("openrouter", "openrouter/anthropic/claude-sonnet-4.6") == \
        "openrouter/anthropic/claude-sonnet-4.6"


# ── trace_from_opencode_export ────────────────────────────────────────

def test_trace_from_opencode_export_maps_reasoning_and_tool_parts():
    data = {"messages": [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "build it"}]},
        {"info": {"role": "assistant"}, "parts": [
            {"type": "reasoning", "text": "SQLite keeps costs low"},
            {"type": "tool", "tool": "bash", "state": {"input": {"cmd": "ls"}, "output": "a.py"}},
            {"type": "step-start"},
            {"type": "text", "text": "built"},
        ]},
    ]}
    trace = oc.trace_from_opencode_export(data)
    assert [ev["type"] for ev in trace] == ["user", "thinking", "tool_use", "tool_result", "text"]
    assert trace[1]["text"] == "SQLite keeps costs low"
    assert trace[2]["tool"] == "bash"
    assert trace[3]["output"] == "a.py"
    # Garbage in, empty trace out — the export is best-effort and never raises.
    assert oc.trace_from_opencode_export({}) == []
    assert oc.trace_from_opencode_export({"messages": ["nope", {"parts": "x"}]}) == []


# ── opencode_config: cell MCP merged into the mcp section (pure) ───────


# ── the terminal leg's context seam (agent-v2 §9.1) ───────────────────

class _FakeContainer:
    """The container under shared preparation: base64 writes through
    ``exec_run``, cat/rm through the same (``_EnvOver.exec``)."""

    def __init__(self):
        self.files: dict = {}

    def exec_run(self, argv, **kw):
        import base64
        import shlex as _shlex
        cmd = argv[-1]
        if cmd.startswith("rm -f "):
            self.files.pop(_shlex.split(cmd)[2], None)
        elif "base64 -d >" in cmd:
            b64 = cmd.split("echo ", 1)[1].split(" | base64", 1)[0]
            path = _shlex.split(cmd.rsplit("> ", 1)[1])[0]
            self.files[path] = base64.b64decode(
                _shlex.split(b64)[0]).decode("utf-8")

        class _R:
            exit_code = 0
            output = b""
        return _R()


def test_prepare_puts_the_context_in_the_instructions_seam_never_the_workspace(stub_terminal):
    _run_terminal(auth="api_key", provider="openrouter", model="openai/gpt-4.1-mini",
                  extra_system="SYSTEM PROMPT + skills index")
    files = stub_terminal["files"]
    assert files[oc.CONTEXT_SEAM_PATH].startswith("SYSTEM PROMPT")
    cfg = json.loads(files[oc.OPENCODE_CONFIG_PATH])
    assert cfg["instructions"] == [oc.CONTEXT_SEAM_PATH]
    assert not any(p.startswith("/workspace") for p in files)


def test_prepare_without_a_system_prompt_names_no_instructions(stub_terminal):
    _run_terminal(auth="api_key", provider="anthropic", model="claude-sonnet-4-6")
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert "instructions" not in cfg
    assert oc.CONTEXT_SEAM_PATH not in stub_terminal["files"]


# ──────────────────────────────────────────────────────────────────────
# The shared PLAN logins (chatgpt / github_copilot / xai).
#
# The contract: the opencode data dir is Collimate's per-cell harness home
# under EVERY auth (the user's ~/.local/share/opencode is never read or
# written), and a plan leg shares nothing but ONE auth.json inode — once a
# sign-in exists to share.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def home(cache_root):
    from _agent_shared.harness_homes import harness_home

    return harness_home("opencode")


def _write_auth(path: str, content) -> str:
    """An auth.json at ``path`` — a dict is dumped, a string written raw (the
    truncated-mid-write and empty-mountpoint cases)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content if isinstance(content, str) else json.dumps(content))
    return path


def _oauth(**extra) -> dict:
    return {"type": "oauth", "access": "a", "refresh": "r",
            "expires": 9999999999999, **extra}


_CHATGPT = {"openai": _oauth(accountId="acct-1")}
_COPILOT = {"github-copilot": _oauth(expires=0)}
_XAI = {"xai": _oauth()}
# opencode keeps EVERY credential in the one flat map, API keys included —
# and an API-key entry is not a subscription login.
_API_ENTRY = {"openai": {"type": "api", "key": "sk-x"}}


def test_the_container_paths_are_opencodes_own_defaults(cache_root):
    """XDG_DATA_HOME and HOME are the only things that move opencode's
    credential file, and Collimate sets neither — so the data dir it resolves
    IS the mount, and auth.json sits inside it (which is what makes the
    single-file bind a NESTED one)."""
    assert oc.OPENCODE_DATA_DIR == "/home/coder/.local/share/opencode"
    assert oc.OPENCODE_AUTH_PATH == "/home/coder/.local/share/opencode/auth.json"
    assert oc.OPENCODE_AUTH_PATH.startswith(oc.OPENCODE_DATA_DIR + "/")


def test_the_plan_tables_agree_with_the_model_id_builder():
    """One wire vocabulary: the key a plan's credential lives
    under IS opencode's provider id, and prefixing a model with the Collimate
    provider the plan pins must produce exactly that id — or a plan leg would
    bind one provider's login and ask another to serve the turn."""
    from _agent_shared import PLAN_PROVIDERS

    for auth, wire in oc.OPENCODE_PLAN_KEYS.items():
        assert oc.native_id(PLAN_PROVIDERS[auth], auth) == wire
    # The plan-only copilot family has no API key and no models.dev name of
    # its own; "github-copilot" is opencode's built-in plugin provider.
    assert oc.native_id("copilot", "github_copilot") == "github-copilot"


@pytest.mark.parametrize("content, ready", [
    ({}, {"chatgpt": False, "github_copilot": False, "xai": False}),
    (_CHATGPT, {"chatgpt": True, "github_copilot": False, "xai": False}),
    (_COPILOT, {"chatgpt": False, "github_copilot": True, "xai": False}),
    (_XAI, {"chatgpt": False, "github_copilot": False, "xai": True}),
    ({**_CHATGPT, **_XAI},
     {"chatgpt": True, "github_copilot": False, "xai": True}),
    # An API key under the same provider key is not a subscription login.
    (_API_ENTRY, {"chatgpt": False, "github_copilot": False, "xai": False}),
])
def test_selected_login_ready_reads_the_plan_key(cache_root, monkeypatch, content, ready):
    for auth, expected in ready.items():
        _select_test_plan(monkeypatch, "opencode", auth)
        _write_auth(oc.shared_login_path(), content)
        assert ls.selected_login_ready("opencode", auth) is expected
    # api_key is never "ready": there is no plan key to look for.
    assert ls.selected_login_ready("opencode", "api_key") is False


def test_selected_login_ready_is_false_for_an_unreadable_store(cache_root):
    """Absent, or truncated by a concurrent in-place write: either way there is
    nothing to sign in with, and the caller's job is to say so — never to
    crash on the parse."""
    assert ls.selected_login_ready("opencode", "chatgpt") is False
    _write_auth(oc.shared_login_path(), '{"openai": {"type": "oau')
    assert ls.selected_login_ready("opencode", "chatgpt") is False
    _write_auth(oc.shared_login_path(), "")
    assert ls.selected_login_ready("opencode", "chatgpt") is False


def test_volumes_bind_the_login_on_a_plan_only(cache_root, home, monkeypatch):
    """A plan leg binds the leased store over opencode's auth.json (the
    pipeline already refused a lease without that plan's login); an API-key
    leg never does."""
    mount = {home: {"bind": oc.OPENCODE_DATA_DIR, "mode": "rw"}}
    assert oc.opencode_volumes("api_key") == mount
    _select_test_plan(monkeypatch, "opencode", "github_copilot")
    _write_auth(oc.shared_login_path(), _COPILOT)
    assert oc.opencode_volumes("github_copilot") == {
        **mount,
        oc.shared_login_path(): {"bind": oc.OPENCODE_AUTH_PATH, "mode": "rw"},
    }
    assert oc.opencode_volumes("api_key") == mount
    # Never the user's own opencode data dir, under any auth.
    real = os.path.expanduser("~/.local/share/opencode")
    assert not any(src == real or src.startswith(real + os.sep)
                   for src in oc.opencode_volumes("github_copilot"))


# ── the terminal leg's container + TTY request ────────────────────────


class _Stop(Exception):
    """Ends the TTY runner at the terminal-surface boundary — everything after
    it is the PTY and the renderer."""


class _RecordingContainer:
    def __init__(self, rec):
        self.rec = rec

    def exec_run(self, argv, **kw):
        cmd = argv[-1]
        self.rec["execs"].append(cmd)
        m = re.search(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > (?:'([^']+)'|(\S+))", cmd)
        if m:
            self.rec["files"][m.group(2) or m.group(3)] = base64.b64decode(m.group(1)).decode()

        class _Res:
            exit_code = 0
            output = b""
        return _Res()


class _FakeEnv:
    container_id = "container-1"

    def __init__(self, rec):
        self.rec = rec
        self.container = _RecordingContainer(rec)

    def exec(self, argv, **kw):
        if argv[0] == 'mktemp':
            return 0, '/tmp/opencode-fixture-output'
        result = self.container.exec_run(argv, **kw)
        # Config staging still lands when this fixture swaps in its session
        # store recorder, whose separate scope does not record file writes.
        match = re.search(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > (?:'([^']+)'|(\S+))", argv[-1])
        if match:
            self.rec['files'][match.group(2) or match.group(3)] = base64.b64decode(match.group(1)).decode()
        self.captured_output = result.output
        return result.exit_code, result.output.decode()

    def read_file(self, path, **kw):
        if path == '/tmp/opencode-fixture-output':
            return self.captured_output
        return self.rec["files"][path].encode()


@pytest.fixture
def stub_terminal(monkeypatch, cache_root, selected_account_runtime):
    """Drive the selected OpenCode terminal entry as far as its terminal registration,
    with the container and the run surface stubbed out. Returns
    the recorded calls."""
    target = selected_account_runtime / 'opencode/auth.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'openai': {'type': 'oauth', 'refresh': 'r'}, 'github-copilot': {'type': 'oauth', 'refresh': 'r'}, 'xai': {'type': 'oauth', 'refresh': 'r'}}))
    from _agent_shared import cli_container as cc

    rec: dict = {"logs": [], "execs": [], "files": {}, "ensure": {}, "serve": {}}
    rec["env"] = _FakeEnv(rec)

    async def _ensure_env(name, **kw):
        rec["ensure"] = {"name": name, **kw}
        return rec["env"]

    def _serve_terminal(container_id, command, **kw):
        rec["serve"] = {"container_id": container_id, "command": list(command), **kw}
        raise _Stop()

    async def _api_key(provider, **kw):
        rec["api_key_provider"] = provider
        return "key-1"

    async def _update(**kw):
        return None


    # Native bridge intake is covered by the D23 carrier tests; this fixture
    # stops at terminal registration and must not start a real server.
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def no_bridge(**kwargs):
        yield None
    monkeypatch.setattr(cc, "leg_bridge", no_bridge)
    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(oc.gen, "commit", _update, raising=False)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(oc.gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(oc.gen, "phase", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc.gen, "update_file", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc.gen, "log", lambda m: rec["logs"].append(m),
                        raising=False)
    return rec


def _select_account_plan(plan):
    pytest.fail('selected host account fixture was not installed')


def _run_terminal(extra_system="", **cfg):
    base = {"backend": "opencode", "auth": "chatgpt", "provider": "openai",
            "model": "gpt-5.5", "commands": "allow", "websearch": True}
    base.update(cfg)
    _select_account_plan(base["auth"])
    with pytest.raises(_Stop):
        asyncio.run(eng.surface_runner("opencode", "terminal")(base, pointer=_POINTER, extra_system=extra_system))


def test_terminal_leg_with_a_signed_in_store_says_so_and_mounts_it(
        stub_terminal, home):
    _write_auth(oc.shared_login_path(), _XAI)
    _run_terminal(auth="xai", provider="xai", model="grok-4.6")
    assert oc.shared_login_path() in stub_terminal["ensure"]["volumes"]
    assert not any("SuperGrok Subscription" in m for m in stub_terminal["logs"])


# ── which conversation a terminal leg exports (F1528) ──────────────────


def _export_doc(text: str) -> dict:
    return {"info": {"id": "x"}, "messages": [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "q " + text}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": text}]},
    ]}


class _StoreContainer:
    """An opencode container over a session store (``{id: export document}``,
    oldest first): ``session list`` prints the ids newest first, ``export``
    answers one document; ``list_rc`` fails the listing."""

    def __init__(self, sessions, list_rc: int = 0):
        self.sessions = dict(sessions)
        self.list_rc = list_rc
        self.exported: list = []
        self.listed = 0

    def exec_run(self, argv, **kw):
        import shlex as _shlex
        cmd = argv[-1]

        class _R:
            exit_code = 0
            output = b""
        r = _R()
        if cmd.startswith("opencode session list"):
            self.listed += 1
            r.exit_code = self.list_rc
            r.output = ("Error: store locked" if self.list_rc else "\n".join(
                f"{sid}  a title  2026-09-28" for sid in reversed(list(self.sessions)))).encode()
        elif cmd.startswith("opencode export"):
            sid = _shlex.split(cmd)[2]
            self.exported.append(sid)
            r.output = json.dumps(self.sessions[sid]).encode()
        return r

    def exec(self, argv, **kw):
        if argv[0] == 'mktemp':
            return 0, '/tmp/opencode-fixture-output'
        result = self.exec_run(argv, **kw)
        self.captured_output = result.output
        return result.exit_code, result.output.decode()

    def read_file(self, path, **kw):
        return self.captured_output


@pytest.fixture
def export_rec(cache_root, monkeypatch):
    """The files a finalize writes (path → content)."""
    import gen

    files: dict = {}
    monkeypatch.setattr(gen, "update_file",
                        lambda path, content, **kw: files.__setitem__(path, content),
                        raising=False)
    monkeypatch.setattr(gen, "log", lambda *a, **k: None, raising=False)
    from _agent_shared import transcript_merge

    monkeypatch.setattr(transcript_merge, "write_merged_transcript",
                        lambda msgs, **kw: files.__setitem__("transcript", msgs) or len(msgs))
    return files


def test_a_fresh_terminal_never_exports_an_earlier_runs_session__F1528(export_rec):
    """F1528: a fresh TTY (no session to run on) in a store that holds this
    cell's earlier conversation, ended before it wrote one of its own,
    exports NOTHING — the store's newest session is an earlier run's."""
    c = _StoreContainer({"ses_OLD": _export_doc("earlier run")})
    before = oc.harness_session_ids(c)
    oc._finalize(c, harness_session_id="", before=before)
    assert c.exported == [] and export_rec == {}


def test_a_fresh_terminal_exports_the_one_session_it_wrote__F1528(export_rec):
    """F1528: the session that appeared after the leg's snapshot is the
    TTY's, whatever the store's order says — it is exported, the earlier one
    never is."""
    c = _StoreContainer({"ses_OLD": _export_doc("earlier run")})
    before = oc.harness_session_ids(c)
    c.sessions["ses_NEW"] = _export_doc("this run")
    c.sessions = {"ses_NEW": c.sessions["ses_NEW"], "ses_OLD": c.sessions["ses_OLD"]}
    oc._finalize(c, harness_session_id="", before=before)
    assert c.exported == ["ses_NEW"]
    assert [m.content for m in export_rec["transcript"]] == ["q this run", "this run"]


@pytest.mark.parametrize("case", ["no-snapshot", "two-new", "unlistable"])
def test_no_single_new_session_exports_nothing__F1528(export_rec, case):
    """No snapshot (the store could not be listed when the leg started), two
    new sessions (which the TTY ended on is unknowable), or a store that
    cannot be listed at the end: nothing is exported."""
    c = _StoreContainer({"ses_OLD": _export_doc("earlier run")})
    before = None if case == "no-snapshot" else oc.harness_session_ids(c)
    c.sessions.update(ses_A=_export_doc("a"), ses_B=_export_doc("b"))
    if case != "two-new":
        c.sessions.pop("ses_B")
    if case == "unlistable":
        c.list_rc = 1
    oc._finalize(c, harness_session_id="", before=before)
    assert c.exported == [] and export_rec == {}


def test_a_store_unlistable_at_leg_start_exports_nothing__F1528(export_rec):
    """F1528: a listing that fails when the leg starts leaves it with no
    snapshot (``_listed_before`` is None), so neither the teardown nor the
    live tail picks a session, even once the store lists again. Read as an
    empty store, the failed listing would have made the earlier run's
    session look new."""
    from _agent_shared import terminal_tail as tt

    c = _StoreContainer({"ses_OLD": _export_doc("earlier run")}, list_rc=1)
    before = oc._listed_before(c)
    assert before is None
    c.list_rc = 0
    assert tt._OpencodeTail(c, "", before).read() == []
    oc._finalize(c, harness_session_id="", before=before)
    assert c.exported == [] and export_rec == {}


def test_a_terminal_on_the_minted_session_exports_exactly_it__F1528(export_rec):
    """With the session the TTY ran on, that session is exported — no
    listing, whatever else the store holds."""
    c = _StoreContainer({"ses_RUN": _export_doc("minted"),
                         "ses_LATER": _export_doc("a /new session")})
    oc._finalize(c, harness_session_id="ses_RUN", before=None)
    assert c.exported == ["ses_RUN"] and c.listed == 0


def test_the_runner_snapshots_the_store_before_a_fresh_tty__F1528(stub_terminal, monkeypatch):
    """A fresh TTY: the store is listed before the terminal starts, the
    snapshot reaches ``on_ready`` (the live tail follows only the one new
    session), and the teardown exports only the session the TTY wrote."""
    import contextlib

    import _agent_shared as eng

    c = _StoreContainer({"ses_OLD": _export_doc("earlier run")})
    stub_terminal["env"].container = c

    @contextlib.asynccontextmanager
    async def _serve_terminal(container_id, command, **kw):
        stub_terminal["serve"] = {"command": list(command), **kw}
        yield {"terminal_id": "tty-1"}

    @contextlib.asynccontextmanager
    async def _live_files(**kw):
        yield None

    async def _until(**kw):
        c.sessions["ses_NEW"] = _export_doc("this run")     # the user chats

    ready: list = []
    monkeypatch.setattr(oc.gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(oc.gen, "live_files", _live_files, raising=False)
    monkeypatch.setattr(eng, "serve_tty_until", _until, raising=False)
    # The snapshot is taken under this leg's config: after the seed.
    original_prepare = oc.prepare

    async def recorded_prepare(*a, **k):
        result = await original_prepare(*a, **k)
        ready.append(("seed", c.listed))
        return result

    monkeypatch.setattr(oc, "prepare", recorded_prepare)
    monkeypatch.setattr(_pipeline, 'credentials', _no_credentials)
    monkeypatch.setattr(oc, "_export_chatlog",
                        lambda container, sid: stub_terminal.setdefault("exported", sid))
    asyncio.run(eng.surface_runner("opencode", "terminal")(
        {"backend": "opencode", "auth": "api_key", "provider": "anthropic",
         "model": "claude-sonnet-4-6", "commands": "allow", "websearch": True},
        pointer=_POINTER,
        on_ready=lambda sid, **kw: ready.append((sid, kw))))
    assert stub_terminal["serve"]["command"] == ["opencode"]
    assert ready == [("seed", 0), ("", {"before": frozenset({"ses_OLD"})})]
    assert stub_terminal["exported"] == "ses_NEW"


def test_a_plan_leg_injects_no_api_key_at_all(stub_terminal):
    """A stored oauth entry HIJACKS the provider id in opencode (the plugin
    loader sets the client's apiKey itself and rewrites the endpoint), so a
    key handed to a plan container would be inert — but the run log would then
    name a key that billed nothing. The modal is never opened either: there is
    nothing to ask for."""
    _run_terminal(auth="github_copilot", provider="copilot", model="gpt-6-luna")
    assert "api_key_provider" not in stub_terminal
    assert stub_terminal["serve"]["env"] == {"TERM": "xterm-256color"}
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "github-copilot/gpt-6-luna"


def test_the_plan_pins_the_provider_over_a_residual_dropdown_value(stub_terminal):
    """The schema hides the provider field under a plan, but a cell can still
    carry a stale value. The plan pins it — a ChatGPT subscription can only
    ever bill openai, whatever the config says."""
    _run_terminal(auth="chatgpt", provider="anthropic", model="gpt-5.5")
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "openai/gpt-5.5"


def test_api_key_leg_fetches_selected_key_without_exporting_it(stub_terminal):
    _run_terminal(auth="api_key", provider="anthropic",
                  model="claude-sonnet-4-6")
    assert stub_terminal["api_key_provider"] == "anthropic"
    assert "ANTHROPIC_API_KEY" not in stub_terminal["serve"]["env"]
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "anthropic/claude-sonnet-4-6"


def test_gemini_terminal_uses_native_google_auth_file(stub_terminal):
    _run_terminal(auth="api_key", provider="gemini", model="gemini-2.5-flash")
    env = stub_terminal["serve"]["env"]
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_GENERATIVE_AI_API_KEY" not in env
    assert json.loads((Path(oc.harness_home("opencode")) / "auth.json").read_text()) == {
        "google": {"type": "api", "key": "key-1"}}


def test_a_blank_model_falls_back_to_the_plans_own_default(stub_terminal):
    """A plan's catalogue is not the API's — opencode's Codex plugin FILTERS
    the openai catalogue down to four ids under OAuth, and the provider
    default (gpt-5) is not among them — so the blank-model default is the
    per-(backend, plan) one."""
    _run_terminal(model="")
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "openai/gpt-5.5"
    _run_terminal(auth="xai", provider="xai", model="")
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "xai/grok-4.6"
    _run_terminal(auth="github_copilot", provider="copilot", model="")
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["model"] == "github-copilot/gpt-6-luna"


# ── the review's three store fixes (2026-09-11) ──────────────────────


@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime, monkeypatch):
    snapshot = {"service": "openai", "auth_type": "cli", "metadata": {"harness": "opencode", "plan": "chatgpt"}, "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot, selectable=True) as (root, select_plan):
        monkeypatch.setitem(globals(), '_select_account_plan', select_plan)
        yield root


# ──────────────────────────────────────────────────────────────────────
# The grant's commands axis (deny < ask < allow) on every leg: the ACP
# container profile, the ACP host twin and the TUI.
# ──────────────────────────────────────────────────────────────────────

_REPO = os.path.dirname(os.path.dirname(__file__))


@pytest.mark.parametrize("commands, acp, websearch, expected", [
    ("deny", True, True, {"permission": {"bash": "deny"}, "snapshot": False}),
    ("ask", True, True, {"permission": {"bash": "ask", "task": "deny"},
                         "snapshot": False}),
    # The TUI shows a subagent's ask itself — only the ACP leg denies task.
    ("ask", False, True, {"permission": {"bash": "ask"}, "snapshot": False}),
    ("allow", True, True, {"permission": {"bash": "allow"}}),
    ("allow", False, False, {"permission": {
        "bash": "allow", "webfetch": "deny", "websearch": "deny"}}),
    ("deny", False, False, {
        "permission": {"bash": "deny", "webfetch": "deny", "websearch": "deny"},
        "snapshot": False}),
])
def test_grant_config_per_level(commands, acp, websearch, expected):
    """The permission block, and snapshots off under deny and ask: opencode's
    own shell, formatter and LSP stay as the harness (and the cell's config)
    set them."""
    assert oc.grant_config(commands, websearch=websearch, acp=acp) == expected


@pytest.mark.parametrize("acp", [True, False])
@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_snapshots_are_off_below_allow(commands, acp):
    """opencode's per-turn snapshots run git on a git-dir in the harness home,
    whose own ``config`` GIT_CONFIG_GLOBAL does not cover and whose filter or
    fsmonitor settings would run commands unasked. Below allow they are off;
    under allow opencode's default (on) stands."""
    grant = oc.grant_config(commands, websearch=True, acp=acp)
    if commands == "allow":
        assert "snapshot" not in grant
    else:
        assert grant["snapshot"] is False


class _UserRecordingContainer(_FakeContainer):
    """``_FakeContainer`` that also records which user wrote each file."""

    def __init__(self):
        super().__init__()
        self.users: dict = {}

    def exec_run(self, argv, **kw):
        import shlex as _shlex
        cmd = argv[-1]
        if "base64 -d >" in cmd:
            self.users[_shlex.split(cmd.rsplit("> ", 1)[1])[0]] = kw.get("user")
        return super().exec_run(argv, **kw)


def test_prepare_writes_the_grant_as_root(stub_terminal):
    """The TUI's config — grant keys included — and its seam are written as
    root into the root-owned config dir, where the agent cannot edit them."""
    c = _UserRecordingContainer()
    stub_terminal["env"].container = c
    _run_terminal(auth="api_key", provider="anthropic", model="claude-sonnet-4-6",
                  commands="deny", websearch=False, extra_system="be brief")
    cfg = json.loads(c.files[oc.OPENCODE_CONFIG_PATH])
    assert cfg["permission"]["bash"] == "deny"
    assert c.users[oc.OPENCODE_CONFIG_PATH] == "root"
    assert c.users[oc.CONTEXT_SEAM_PATH] == "root"


def _git_config_env(env: dict) -> dict:
    return {k: v for k, v in env.items() if k.startswith("GIT_CONFIG")}


def _snapshot_expected(commands: str):
    """``snapshot: false`` below allow; allow leaves opencode's default."""
    return None if commands == "allow" else False


def _git_expected(commands: str) -> dict:
    """Under deny and ask the git opencode's git service spawns reads no
    global or system git config: that config sits in the agent-writable
    harness home, and its filter/textconv/fsmonitor settings run commands
    with no shell tool and no ask."""
    if commands == "allow":
        return {}
    return {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_terminal_leg_carries_the_grant(stub_terminal, commands):
    """The TUI leg carries the same grant keys as the chat leg (task stays
    with the TUI's own prompt) and the web denial; the cell's own project
    config loads."""
    _run_terminal(auth="api_key", provider="anthropic", model="claude-sonnet-4-6",
                  commands=commands, websearch=False)
    cfg = json.loads(stub_terminal["files"][oc.OPENCODE_CONFIG_PATH])
    assert cfg["permission"] == {"bash": commands, "webfetch": "deny",
                                 "websearch": "deny"}
    assert not {"shell", "formatter", "lsp"} & set(cfg)
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in stub_terminal["serve"]["env"]
    assert stub_terminal["serve"]["command"] == ["opencode"]
    assert cfg.get("snapshot") is _snapshot_expected(commands)
    assert _git_config_env(stub_terminal["serve"]["env"]) == _git_expected(commands)


def test_image_ships_a_root_owned_config():
    """opencode runs plugins, custom tools and command hooks from its global
    config dir and ``~/.opencode`` unasked, so the image keeps both
    root-owned (755) inside a root-owned, sticky (1777) home and
    ``~/.config``: coder can add its own files there but cannot write into,
    rename or replace the two directories. The root chown comes after the
    blanket ``chown -R coder`` that would otherwise undo it."""
    with open(os.path.join(GENERATORS, "_agent_shared", "harnesses", "opencode", "image",
                           "Dockerfile")) as fh:
        text = fh.read()
    run = next(block for block in text.replace("\\\n", " ").split("\nRUN ")
               if block.startswith("useradd ")).split("\n", 1)[0]
    steps = [" ".join(step.split()) for step in run.split("&&")]
    homes = "/home/coder /home/coder/.config"
    dirs = "/home/coder/.config/opencode /home/coder/.opencode"
    root = steps.index(f"chown root:root {homes} {dirs}")
    assert root > steps.index("chown -R coder:coder /home/coder /workspace")
    assert f"chmod 1777 {homes}" in steps[root:]
    assert f"chmod 755 {dirs}" in steps[root:]


# ── the ACP legs ──────────────────────────────────────────────────────

class _ProfileEnv:
    """The container a profile writes into: path → (content, user)."""

    def __init__(self):
        self.files: dict = {}
        self.container = self
        self.container_id = "cid"

    def exec(self, argv, **kw):
        result = self.exec_run(argv, **kw)
        return result.exit_code, result.output.decode()

    def exec_run(self, argv, user=None, **kw):
        # cli_container.write_root_file: base64 through sh, as ``user``.
        for m in re.finditer(r"echo '?([A-Za-z0-9+/=]+)'? \| base64 -d > '?([^' ]+)'?",
                             argv[-1]):
            self.files[m.group(2)] = (base64.b64decode(m.group(1)).decode(), user)

        class _R:
            exit_code = 0
            output = b""
        return _R()


@pytest.fixture
def profile_env(monkeypatch, cache_root):
    from _agent_shared import acp_run as ar
    from _agent_shared import cli_container as cc

    env = _ProfileEnv()

    async def _ensure_env(name, **kw):
        return env

    def _write(target, path, content, *, mkdir=None, user="coder"):
        target.files[path] = (content, user)

    async def _key(provider, **kw):
        return "test-key"

    monkeypatch.setattr(cc, "ensure_env", _ensure_env)
    monkeypatch.setattr(cc, "write_container_file", _write)
    monkeypatch.setattr(_pipeline, "account_api_key", _key, raising=False)
    monkeypatch.setattr(ar.gen, "log", lambda *_a, **_k: None, raising=False)
    return env


def _acp_ctx(**over) -> dict:
    ctx = {"auth": "api_key", "provider": "openrouter", "model": "openai/gpt-4.1-mini",
           "system_prompt": "be brief", "enable_file_tools": True,
           "websearch": True, "commands": "deny"}
    ctx.update(over)
    return ctx


def _spawn_env(profile) -> dict:

    seen: dict = {}

    async def _spawn(backend, env_obj, argv, *, env_vars, cwd="/workspace"):
        seen.update(env_vars=env_vars, argv=argv)
        return object()
    old = oc.spawn_container_adapter
    oc.spawn_container_adapter = _spawn
    try:
        asyncio.run(profile.spawn())
    finally:
        oc.spawn_container_adapter = old
    return seen


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_container_profile_writes_the_grant_as_root(profile_env, commands):
    from _agent_shared import acp_run as ar

    profile = asyncio.run(ar._PROFILES["opencode"](_acp_ctx(commands=commands)))
    content, user = profile_env.files[oc.OPENCODE_CONFIG_PATH]
    cfg = json.loads(content)
    assert user == "root"
    assert profile_env.files[oc.CONTEXT_SEAM_PATH][1] == "root"
    assert cfg["permission"]["bash"] == commands
    assert cfg["permission"].get("task") == ("deny" if commands == "ask" else None)
    assert cfg.get("snapshot") is _snapshot_expected(commands)
    assert not {"shell", "formatter", "lsp"} & set(cfg)
    spawned = _spawn_env(profile)
    assert spawned["argv"] == ["opencode", "acp"]
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in spawned["env_vars"]
    assert _git_config_env(spawned["env_vars"]) == _git_expected(commands)


@pytest.mark.parametrize("commands", ["deny", "ask", "allow"])
def test_host_twin_holds_the_grant_in_its_own_config_home(monkeypatch, tmp_path, commands):
    """The host twin: XDG_CONFIG_HOME in the per-cell home (the user's own
    ~/.config/opencode — plugins and all — is never read), the grant in the
    config there, and the cell's project config left to load."""
    from _agent_shared import acp_run as ar

    home = tmp_path / "home"
    spawned = []

    async def _spawn(backend, argv, *, env, cwd):
        spawned.append(env)
        return object()

    async def _key(provider, **kw):
        return "k"
    monkeypatch.setattr(_pipeline, "account_api_key", _key)
    monkeypatch.setattr(oc, "harness_home", lambda backend: str(home))
    monkeypatch.setattr(oc, "spawn_host_adapter", _spawn)
    from _agent_shared import host_adapters
    monkeypatch.setattr(host_adapters, "host_node_bin_dir", lambda: "")
    monkeypatch.setattr(ar.gen, "workspace_path", lambda: str(tmp_path / "ws"), raising=False)
    monkeypatch.setattr(ar.gen, "log", lambda *_a, **_k: None, raising=False)
    ctx = _acp_ctx(commands=commands, runtime="standard",
                   host_harness={"path": "/usr/bin/opencode", "version": "1.18.30",
                                 "binary": "opencode"})
    profile = asyncio.run(ar._PROFILES["opencode"](ctx))
    asyncio.run(profile.spawn())
    env = spawned[0]
    assert env["XDG_CONFIG_HOME"] == str(home / "config")
    assert "OPENCODE_CONFIG_DIR" not in env
    assert "OPENCODE_DISABLE_PROJECT_CONFIG" not in env
    assert _git_config_env(env) == _git_expected(commands)
    cfg = json.loads((home / "config" / "opencode" / "opencode.json").read_text())
    assert cfg["permission"]["bash"] == commands
    assert cfg.get("snapshot") is _snapshot_expected(commands)
    assert "shell" not in cfg
    assert not (home / "bin").exists()


@pytest.mark.parametrize("commands, mode, allowed", [
    ("deny", "build", True), ("ask", "plan", True), ("ask", "build", True),
    ("allow", "build", True),
])
def test_every_opencode_mode_keeps_the_grant(commands, mode, allowed):
    from _agent_shared import acp_run as ar

    assert ar.mode_allowed("opencode", commands, mode) is allowed


def test_acp_config_spells_the_model_per_auth_and_names_the_seam():
    """Both ACP legs (container profile and host twin) write the config
    acp_config builds: the key leg prefixes the provider's opencode id, a plan
    prefixes the plan's own provider (and leaves a blank model out), and the
    instruction file rides only when given."""
    base = {"$schema": "https://opencode.ai/config.json"}
    grant = oc.grant_config("allow", websearch=True, acp=True)
    assert oc.acp_config(auth="api_key", provider="gemini", model="gemini-x",
                         commands="allow", websearch=True) == {
        **base, "model": "google/gemini-x",
        "provider": {"google": {"options": {"baseURL": "https://generativelanguage.googleapis.com/v1beta"}}},
        **grant}
    plan = next(iter(oc.OPENCODE_PLAN_KEYS))
    oc_id = oc._opencode_model_id(oc.native_id("", plan), "m1")
    assert oc.acp_config(auth=plan, provider="anthropic", model="m1",
                         commands="allow", websearch=True,
                         instructions_path="/ctx.md") == {
        **base, "model": oc_id, **grant, "instructions": ["/ctx.md"]}
    assert oc.acp_config(auth=plan, provider="anthropic", model="",
                         commands="allow", websearch=True) == {**base, **grant}


def test_api_key_file_keeps_provider_keys_out_of_terminal_env_F1402(stub_terminal):
    _run_terminal(auth="api_key", provider="anthropic", model="claude-sonnet-4-6")
    assert "ANTHROPIC_API_KEY" not in stub_terminal["serve"]["env"]
    home = oc.harness_home("opencode")
    path = Path(home) / "auth.json"
    assert json.loads(path.read_text()) == {"anthropic": {"type": "api", "key": "key-1"}}
    assert path.stat().st_mode & 0o777 == 0o600


def _select_test_plan(monkeypatch, harness, plan):
    """Explicitly select a separate host account before acquiring its lease."""
    import gen
    from collimate.account_taxonomy import PLAN_PROVIDERS
    ls.close_account_credentials()
    monkeypatch.setattr(gen, "account", lambda role: {
        "service": PLAN_PROVIDERS[plan], "auth_type": "cli",
        "metadata": {"harness": harness, "plan": plan},
        "secrets": {"oauth_token": "test-subscription"}})
