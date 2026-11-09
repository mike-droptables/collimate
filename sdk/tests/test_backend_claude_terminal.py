# SPDX-License-Identifier: MIT
"""Unit tests for the claude-terminal backend
(``generators/_agent_shared/harnesses/claude_code/backend.py``).

Two halves. The deterministic helpers: the ``.claude.json`` onboarding seed,
where a session's jsonl is looked for (the pinned project dir, with the
cwd-slug fallback), the content-block flattener, and the raw-session trace
normalizer that feeds the persisted ``_agent/trace.json``. Then
the selected Claude terminal entry's PREPARATION — the argv, the environment
and the homes it builds — which is where the isolation contract lives: this
machine's ``claude``, run out of the entry's per-account home under both
auths, never the user's ``~/.claude``, its session record filed in the cell's
harness home. Everything is faked (no PTY, no keystore, no network). Only
synthetic inputs, no on-disk generator names.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest

# claude_code_backend is generator-side shared code; import it the way the agent
# generator does (sibling on the generators path).
import _agent_shared as eng  # noqa: E402
from _agent_shared import harness_homes
from _agent_shared.harnesses.claude_code import backend as _claude_backend  # noqa: E402
from _agent_shared.harnesses.claude_code import backend as ct  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from tests._agent_run import in_cell  # noqa: E402

# The run's terminal pointer a leg is handed (synthetic run label).
_POINTER = "_runs/tester-0badc0de/terminal/claude-code.terminal"


# ── pull_claude_transcript ───────────────────────────────────────────
#
# A HOST read on every path: the config dir is always this cell's bind-mounted
# harness home now, so the reader opens a file. (There was a container-exec
# fallback, reading the cwd-slug path under the image's own ~/.claude; the
# project-dir pin means sessions no longer land there, so it was deleted
# rather than kept as a wrong answer — hence `store_dir` is required.)

_SESSION = "11111111-2222-3333-4444-555555555555"
# A workspace whose slug files nothing: these sessions sit under the pinned name.
_WS = "/a/workspace/never/used"


def _jsonl(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def _write_session(store, raw, harness_session_id=_SESSION,
                   name=None):
    """Put a raw session jsonl where claude files it: ``<store>/projects/
    <pinned name>/<id>.jsonl``. Returns the store path (what the mode passes
    as ``store_dir``)."""
    sess = store / "projects" / (name or _claude_backend.CLAUDE_PROJECT_DIR_NAME)
    sess.mkdir(parents=True, exist_ok=True)
    (sess / f"{harness_session_id}.jsonl").write_text(raw)
    return str(store)


def test_pull_transcript_reads_this_runs_session_from_the_host_store(tmp_path):
    raw = _jsonl(
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi there"}]}},
    )
    store = _write_session(tmp_path / "store", raw)
    messages, got_raw = ct.pull_claude_transcript(_SESSION, store_dir=store, project=_WS)

    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert got_raw == raw


def test_pull_transcript_skips_non_message_entries_and_garbage(tmp_path):
    raw = (
        _jsonl(
            {"type": "summary", "summary": "ignored"},
            {"type": "system", "message": {"role": "system", "content": "ignored"}},
            {"type": "assistant", "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "dropped"}]}},
            {"type": "user", "message": {"role": "user", "content": "kept"}},
        )
        + "not json at all\n\n   \n"
    )
    store = _write_session(tmp_path / "store", raw)
    messages, _raw = ct.pull_claude_transcript(_SESSION, store_dir=store, project=_WS)
    # Tool-only / thinking-only turns flatten to empty text and are dropped.
    assert messages == [{"role": "user", "content": "kept"}]


def test_pull_transcript_missing_session_is_empty_not_an_error(tmp_path):
    # A different session id in the same project dir must not be picked up —
    # that scoping is what keeps a concurrent host session (and a previous run
    # of this cell, whose sessions persist in the harness home) out of the
    # export.
    store = _write_session(tmp_path / "store", _jsonl(
        {"type": "user", "message": {"role": "user", "content": "someone else"}}),
        harness_session_id="other-session")
    assert ct.pull_claude_transcript(_SESSION, store_dir=store, project=_WS) == ([], None)
    # No store, nothing to read — never a container exec, which is what the
    # deleted fallback did (at a path the pin made wrong).
    assert ct.pull_claude_transcript(_SESSION, store_dir="", project=_WS) == ([], None)


# ── _extract_text ─────────────────────────────────────────────────────

def test_extract_text_variants():
    assert ct._extract_text("plain string") == "plain string"
    assert ct._extract_text([
        {"type": "text", "text": "first"},
        {"type": "tool_use", "name": "Edit", "input": {}},
        {"type": "text", "text": "second"},
        {"type": "text", "text": ""},
        "not a block",
    ]) == "first\nsecond"
    assert ct._extract_text([]) == ""
    assert ct._extract_text(None) == ""
    assert ct._extract_text({"type": "text", "text": "unwrapped"}) == ""


# ── trace_from_claude_session ─────────────────────────────────────────


def test_trace_from_claude_session_keeps_thinking_and_tools():
    lines = [
        {"type": "user", "message": {"role": "user", "content": "add rate limiting"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "assume one IP is one user"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "limiter.py"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "done"},
        ]}},
        {"type": "summary", "summary": "ignored bookkeeping line"},
    ]
    raw = "\n".join(json.dumps(x) for x in lines) + "\nnot json\n"
    trace = ct.trace_from_claude_session(raw)
    assert [ev["type"] for ev in trace] == ["user", "thinking", "tool_use", "tool_result", "text"]
    assert trace[1]["text"] == "assume one IP is one user"
    assert trace[2]["tool"] == "Write"
    assert trace[3]["output"] == "ok"
    # Garbage in, empty trace out — the export is best-effort and never raises.
    assert ct.trace_from_claude_session("") == []
    assert ct.trace_from_claude_session("{broken") == []


# ── the Tier-A resume arrangement's host-side helpers (agent-v2 §4.2) ──


def test_seed_host_config_merges_never_clobbers(tmp_path, native_bridge):
    """With ``CLAUDE_CONFIG_DIR`` set, claude's state file is
    ``$CLAUDE_CONFIG_DIR/.claude.json`` — the per-account home, which holds
    what the CLI itself recorded (its sign-in's account, earlier runs), so the
    seed must MERGE the onboarding/trust flags into whatever is already
    there."""
    cfg_dir = tmp_path / "store"
    cfg_dir.mkdir()
    elsewhere = str(tmp_path)            # an earlier workspace that still exists
    existing = {
        "theme": "dark",
        "projects": {elsewhere: {"hasTrustDialogAccepted": True}},
    }
    (cfg_dir / ".claude.json").write_text(json.dumps(existing))
    ct._seed_claude_config_host(str(cfg_dir), "/host/abs/workspace", "allow")
    state = json.loads((cfg_dir / ".claude.json").read_text())
    # The prior state survives untouched…
    assert state["theme"] == "dark"
    assert state["projects"][elsewhere] == {"hasTrustDialogAccepted": True}
    # …and the flags for THIS workspace are in.
    assert state["hasCompletedOnboarding"] is True
    assert set(state) == {"theme", "projects", "hasCompletedOnboarding"}
    ours = state["projects"]["/host/abs/workspace"]
    assert ours["hasTrustDialogAccepted"] is True
    assert ours["hasCompletedProjectOnboarding"] is True


def test_seed_host_config_creates_store_and_survives_garbage(tmp_path, native_bridge):
    # A fresh per-cell harness home (api_key auth) — no dir, no file.
    fresh = tmp_path / "harness-home"
    ct._seed_claude_config_host(str(fresh), "/ws", "allow")
    assert json.loads((fresh / ".claude.json").read_text())["projects"]["/ws"][
        "hasTrustDialogAccepted"] is True
    # A corrupt state file is replaced rather than crashing the leg.
    (fresh / ".claude.json").write_text("{broken")
    ct._seed_claude_config_host(str(fresh), "/ws", "allow")
    assert json.loads((fresh / ".claude.json").read_text())["hasCompletedOnboarding"] is True


def test_seed_host_config_preserves_a_file_it_cannot_parse(tmp_path, native_bridge):
    """The cell's host ACP leg writes this same file with its own live
    ``claude``, so an unparseable read may be a half-written state — and the
    file holds the cell's accumulated claude state either way. Keep it."""
    home = tmp_path / "harness-home"
    home.mkdir()
    (home / ".claude.json").write_text('{"projects": {"/ws": {"hasTr')
    ct._seed_claude_config_host(str(home), "/ws", "allow")
    assert json.loads((home / ".claude.json").read_text())[
        "hasCompletedOnboarding"] is True
    kept = [p for p in home.iterdir() if ".corrupt-" in p.name]
    assert len(kept) == 1, [p.name for p in home.iterdir()]
    assert kept[0].read_text() == '{"projects": {"/ws": {"hasTr'
    # An EMPTY file carries nothing to preserve — no copy, no clutter.
    (home / ".claude.json").write_text("")
    ct._seed_claude_config_host(str(home), "/ws", "allow")
    assert len([p for p in home.iterdir() if ".corrupt-" in p.name]) == 1
    # The write goes through a temp file + os.replace (a concurrent reader
    # never sees a truncated state), and the temp never survives it.
    assert not [p for p in home.iterdir() if ".tmp-" in p.name]


def test_pull_transcript_reads_the_host_store_when_resuming(tmp_path):
    """The resume arrangement's sessions live in the HOST store
    (``store_dir/projects/<slug-of-host-workspace>``) — the reader must go
    there directly, not through container exec."""
    # A path with '.' and '_' segments — claude sanitizes EVERY
    # non-alphanumeric char to '-', not just '/', and the reader must agree.
    project = str(tmp_path / ".collimate-tmp" / "ws_ab12_x9")
    slug = ct._claude_project_slug(project)
    assert "." not in slug and "_" not in slug and "/" not in slug
    sess_dir = tmp_path / "store" / "projects" / slug
    sess_dir.mkdir(parents=True)
    entry = {"type": "user", "message": {"role": "user", "content": "hi from acp"}}
    (sess_dir / "sid-1.jsonl").write_text(json.dumps(entry) + "\n")
    messages, raw = ct.pull_claude_transcript(
        "sid-1", store_dir=str(tmp_path / "store"), project=project)
    assert messages == [{"role": "user", "content": "hi from acp"}]
    assert raw and "hi from acp" in raw
    # A missing session file is empty, not an error.
    messages, raw = ct.pull_claude_transcript(
        "sid-missing", store_dir=str(tmp_path / "store"), project=project)
    assert (messages, raw) == ([], None)


# ── the project-dir pin: where a session's jsonl actually lands ────────


def test_session_paths_prefer_the_cwd_slug_then_the_reserved_pinned_name(tmp_path):
    """Neither leg pins CLAUDE_CODE_PROJECT_DIR_NAME yet (the adapter's bundled
    CLI ignores it; the legs must agree), so the cwd slug is where sessions
    land and comes first. The reserved pinned name is still tried so the day
    it is switched on cannot make the miss SILENT (the reader opens a path
    that never exists and exports an empty transcript)."""
    store, project = str(tmp_path / "store"), "/host/abs/ws"
    paths = ct.claude_session_paths(store, project, "sid-1")
    assert paths == [
        os.path.join(store, "projects", ct._claude_project_slug(project),
                     "sid-1.jsonl"),
        os.path.join(store, "projects", _claude_backend.CLAUDE_PROJECT_DIR_NAME, "sid-1.jsonl"),
    ]
    # Nothing to look for is not a path list.
    assert ct.claude_session_paths("", project, "sid-1") == []
    assert ct.claude_session_paths(store, project, "") == []


def test_transcript_is_found_under_either_project_dir(tmp_path):
    store, project = tmp_path / "store", "/host/abs/ws"
    entry = {"type": "user", "message": {"role": "user", "content": "from the tty"}}
    for name in (_claude_backend.CLAUDE_PROJECT_DIR_NAME, ct._claude_project_slug(project)):
        sess = store / "projects" / name
        sess.mkdir(parents=True)
        (sess / "sid-1.jsonl").write_text(json.dumps(entry) + "\n")
        messages, _raw = ct.pull_claude_transcript(
            "sid-1", store_dir=str(store), project=project)
        assert messages == [{"role": "user", "content": "from the tty"}]
        (sess / "sid-1.jsonl").unlink()
    messages, _raw = ct.pull_claude_transcript(
        "sid-1", store_dir=str(store), project=project)
    assert messages == []


def test_host_seed_carries_no_account_identity(tmp_path, native_bridge):
    """Identity comes from the token/key in the environment; a seeded account
    block would be a fabricated (and stale) login record."""
    ct._seed_claude_config_host(str(tmp_path / "home"), "/ws", "allow")
    blob = (tmp_path / "home" / ".claude.json").read_text().lower()
    for forbidden in ("oauthaccount", "email", "userid", "organization",
                      "subscription", "token", "credential"):
        assert forbidden not in blob, f"seed leaked identity field {forbidden!r}"


# ══════════════════════════════════════════════════════════════════════
# The selected Claude terminal: this machine's own `claude`, out of the
# entry's per-account home under BOTH auths. The user's ~/.claude is never
# read — under commands: allow the agent runs with
# --dangerously-skip-permissions, and their home holds their settings hooks
# and their credential file, whose single-use rotating refresh token the CLI
# replaces by rename (two processes on that chain revoke each other's login).
# ══════════════════════════════════════════════════════════════════════

_CLAUDE = {"path": "/opt/claude/bin/claude", "version": "2.1.281", "binary": "claude"}


@pytest.fixture
def terminal_run(request, monkeypatch, tmp_path, native_bridge):
    """Drive the selected Claude terminal entry with no PTY, no keystore and
    the host cache under ``tmp_path``: the per-account home and the cell's
    harness home land there, and the recorded ``(container id, terminal argv,
    terminal env, cwd)`` come back for assertion. Mutate ``rec["token"]``
    (``""``: no setup token) or ``rec["account"]`` before calling; pass
    ``resume_harness_session_id=`` to ``rec["run"]`` for a TTY on the run's
    minted session. ``rec["during"]`` records what the leg's homes held while
    the TTY was registered."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    in_cell(request, monkeypatch, tmp_path / "run-ws", "cell-tty")
    rec: dict = {"token": "sk-ant-oat-TTY", "key": "sk-ant-api-TTY", "account": "entry-1"}

    async def _api_key(provider, **kw):
        rec["api_key_provider"] = provider
        return rec["key"]

    @contextlib.asynccontextmanager
    async def _serve_terminal(container_id, command, **kw):
        rec["container_id"] = container_id
        rec["command"] = list(command)
        rec["term_env"] = dict(kw.get("env") or {})
        rec["cwd"] = kw.get("cwd")
        rec["buttons"] = list(kw.get("buttons") or [])
        home = rec["term_env"]["CLAUDE_CONFIG_DIR"]
        link = os.path.join(home, "projects", ct._claude_project_slug(kw.get("cwd") or ""))
        rec["during"] = {"link": os.path.islink(link) and os.readlink(link)}
        yield {"terminal_id": "tty-1"}

    @contextlib.asynccontextmanager
    async def _live_files(**kw):
        yield None

    async def _serve_tty_until(*a, **kw):
        return "stopped"

    def _account(role):
        secrets = {"oauth_token": rec["token"]} if rec["token"] else {}
        return {"id": rec["account"], "service": "anthropic", "metadata": {"harness": "claude-code"},
                "secrets": secrets}

    monkeypatch.setattr(ct.gen, "account", _account)
    monkeypatch.setattr(_pipeline, "account_api_key", _api_key, raising=False)
    monkeypatch.setattr(ct.gen, "serve_terminal", _serve_terminal, raising=False)
    monkeypatch.setattr(ct.gen, "live_files", _live_files, raising=False)
    monkeypatch.setattr(eng, "serve_tty_until", _serve_tty_until, raising=False)

    def _run(resume_harness_session_id: str = "", on_ready=None, **cfg_over):
        cfg = {"backend": "claude-code", "model": "sonnet", "auth": "api_key", "commands": "allow",
               "host_harness": dict(_CLAUDE)}
        cfg.update(cfg_over)
        asyncio.run(eng.surface_runner("claude-code", "terminal")(
            cfg, pointer=_POINTER, label="test", resume_harness_session_id=resume_harness_session_id,
            on_ready=on_ready))
        return rec

    rec["run"] = _run
    return rec


def _account_home(tmp_path, account="entry-1"):
    return str(tmp_path / "cache" / "collimate" / "host-homes" / "claude-code" / account)


@pytest.mark.parametrize("auth", ["claude_code", "api_key"])
def test_terminal_runs_this_machines_claude_out_of_the_entrys_own_home(terminal_run, tmp_path, auth):
    """The CLI is the machine's own, on this machine (no container), in the
    cell's real workspace, and its config dir is the entry's per-account
    home, owner-only from the cache root down: never the user's ~/.claude."""
    import gen

    rec = terminal_run["run"](auth=auth)
    assert rec["container_id"] is None
    assert rec["command"][0] == _CLAUDE["path"]
    assert rec["cwd"] == gen.workspace_path()
    home = rec["term_env"]["CLAUDE_CONFIG_DIR"]
    assert home == _account_home(tmp_path)
    level = home
    while level != str(tmp_path / "cache"):
        assert stat_mode(level) == 0o700, level
        level = os.path.dirname(level)
    assert os.path.expanduser("~/.claude") not in json.dumps({k: v for k, v in rec.items() if k != "run"})


def stat_mode(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize("auth", ["claude_code", "api_key"])
def test_terminal_points_the_cli_at_its_home_with_the_agent_flags(terminal_run, auth):
    """CLAUDE_CONFIG_DIR rides the TTY's own environment (the only place the
    CLI reads some variables), with the agent flags. The project-dir pin is
    deliberately absent — see _claude_backend.CLAUDE_PROJECT_DIR_NAME."""
    env = terminal_run["run"](auth=auth)["term_env"]
    assert "CLAUDE_CODE_PROJECT_DIR_NAME" not in env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["TERM"] == "xterm-256color"
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-5-5"


def test_the_terminal_environment_replaces_the_childs(terminal_run, monkeypatch):
    """Nothing of this process's environment reaches the CLI but PATH, HOME
    and the locale: no other account's key, no server variable."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-SOMEONE-ELSE")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-ambient")
    monkeypatch.setenv("COLLIMATE_IPC_TOKEN", "server-secret")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = terminal_run["run"](auth="api_key")["term_env"]
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "COLLIMATE_IPC_TOKEN"} & set(env)
    assert "sk-ant-api-SOMEONE-ELSE" not in json.dumps(env)
    assert env["HOME"] == os.environ["HOME"] and env["PATH"] and env["LANG"] == "C.UTF-8"


def test_terminal_subscription_with_a_setup_token_carries_it(terminal_run):
    rec = terminal_run["run"](auth="claude_code")
    assert rec["term_env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-TTY"
    assert "ANTHROPIC_API_KEY" not in rec["term_env"]
    assert "api_key_provider" not in rec
    assert "sk-ant-oat-TTY" not in json.dumps(rec["command"])


def test_terminal_subscription_without_a_token_signs_in_inside_the_tty(terminal_run):
    """No setup token: nothing from Collimate — no token, no endpoint
    override — the CLI's own sign-in in its home (`/login` in the TTY)
    serves. The leg starts; it is not refused."""
    terminal_run["token"] = ""
    rec = terminal_run["run"](auth="claude_code")
    assert rec["command"][0] == _CLAUDE["path"]
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"} & set(rec["term_env"])
    assert "apiKeyHelper" not in _settings_arg(rec["command"])


def test_terminal_api_key_auth_uses_its_private_helper_in_the_cell_home(terminal_run):
    """The key is a private file in the CELL's harness home (disposable, per
    run), read by a helper the CLI's own settings name: never in the argv or
    the environment, never in the per-account home that outlives the run."""
    import sys

    rec = terminal_run["run"](auth="api_key")
    cell = harness_homes.harness_home("claude-code")
    assert Path(cell, ct._API_KEY_FILE).read_text() == "sk-ant-api-TTY"
    assert _settings_arg(rec["command"])["apiKeyHelper"] == f"{sys.executable} {cell}/{ct._API_KEY_HELPER}"
    assert "sk-ant-api-TTY" not in json.dumps([rec["command"], rec["term_env"]])
    assert not os.path.exists(os.path.join(rec["term_env"]["CLAUDE_CONFIG_DIR"], ct._API_KEY_FILE))
    assert rec["api_key_provider"] == "anthropic"


def test_the_entrys_home_persists_across_cells_and_is_its_own(terminal_run, request, monkeypatch, tmp_path):
    """One sign-in per Key manager entry: a second cell on the same entry
    runs out of the same home; another entry out of its own."""
    first = terminal_run["run"](auth="claude_code")["term_env"]["CLAUDE_CONFIG_DIR"]
    harness_homes.close_harness_homes()
    in_cell(request, monkeypatch, tmp_path / "run-ws-2", "cell-other")
    assert terminal_run["run"](auth="claude_code")["term_env"]["CLAUDE_CONFIG_DIR"] == first
    terminal_run["account"] = "entry-2"
    other = terminal_run["run"](auth="claude_code")["term_env"]["CLAUDE_CONFIG_DIR"]
    assert other == _account_home(tmp_path, "entry-2") != first


def test_terminal_seeds_onboarding_into_the_entrys_home(terminal_run, tmp_path):
    """The flags claude consults are $CLAUDE_CONFIG_DIR/.claude.json, in the
    per-account home; a merge that keeps what the CLI recorded (its sign-in's
    account) and drops the entries of workspaces that no longer exist."""
    import gen

    home = _account_home(tmp_path)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"oauthAccount": {"emailAddress": "kept@example.com"},
                   "projects": {"/gone/run/workspace": {"hasTrustDialogAccepted": True},
                                str(tmp_path): {"hasTrustDialogAccepted": True}}}, f)
    terminal_run["run"](auth="claude_code")
    state = json.loads(open(os.path.join(home, ".claude.json")).read())
    assert state["hasCompletedOnboarding"] is True
    assert state["oauthAccount"] == {"emailAddress": "kept@example.com"}
    assert state["projects"][gen.workspace_path()]["hasTrustDialogAccepted"] is True
    assert "/gone/run/workspace" not in state["projects"] and str(tmp_path) in state["projects"]


def test_the_session_record_lands_in_the_cells_harness_home(terminal_run):
    """While the TTY runs, the per-account projects/<workspace> folder links
    to the cell's harness home, so the live tail, the export and the cell's
    session store read one record; the link goes with the leg."""
    import gen

    rec = terminal_run["run"](resume_harness_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    slug = ct._claude_project_slug(gen.workspace_path())
    cell = harness_homes.harness_home("claude-code")
    assert rec["during"]["link"] == os.path.join(cell, "projects", slug)
    link = os.path.join(rec["term_env"]["CLAUDE_CONFIG_DIR"], "projects", slug)
    assert not os.path.lexists(link)


def test_a_dangling_link_a_killed_run_left_is_swept(terminal_run, tmp_path):
    home = _account_home(tmp_path)
    os.makedirs(os.path.join(home, "projects"), exist_ok=True)
    stale = os.path.join(home, "projects", "-a-killed-runs-workspace")
    os.symlink(str(tmp_path / "gone"), stale)
    terminal_run["run"]()
    assert not os.path.lexists(stale)


def test_terminal_on_the_minted_session_runs_on_it(terminal_run):
    """The TTY runs on the run's minted ACP session (``--session-id``), so the
    live tail follows exactly that conversation."""
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ready = []
    rec = terminal_run["run"](resume_harness_session_id=sid, on_ready=lambda s, **kw: ready.append(s))
    command = list(rec["command"])
    assert command[command.index("--session-id") + 1] == sid
    assert "--resume" not in command and "--plugin-dir" not in command
    assert ready == [sid]
    # Claude's terminal declares Capture Chat, its one toolbar button.
    assert rec["buttons"] == [{"id": "capture_chat", "label": "Capture Chat"}]


def test_a_fresh_terminal_names_its_own_session_and_the_tail_follows_it(terminal_run):
    """No minted session (a subscription with no token cannot mint one):
    the TTY runs on a session id of its own, and the ready hook hands that
    id on, so the tail follows the TTY's record all the same."""
    ready = []
    rec = terminal_run["run"](on_ready=lambda s, **kw: ready.append(s))
    sid = rec["command"][rec["command"].index("--session-id") + 1]
    assert len(sid) == 36 and ready == [sid]


def test_a_slow_teardown_never_holds_a_cancelled_leg(terminal_run, monkeypatch):
    """The leg's teardown (the exports) runs off the event loop, so the run
    end's bounded wait can cut it short: once the Stop's cancel has started
    the teardown, the bound's own cancel ends the wait at the bound, however
    long the export takes (a loop blocked in a synchronous teardown could not
    fire the timeout)."""
    import time

    monkeypatch.setattr(ct, "pull_claude_transcript", lambda *a, **kw: (time.sleep(1.5), ([], None))[1])

    async def _forever(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(eng, "serve_tty_until", _forever, raising=False)

    async def flow():
        task = asyncio.ensure_future(eng.surface_runner("claude-code", "terminal")(
            {"backend": "claude-code", "model": "sonnet", "auth": "api_key", "commands": "allow",
             "host_harness": dict(_CLAUDE)}, pointer=_POINTER, label="test"))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        task.cancel()                       # the Stop: the teardown starts
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.3)   # the run end's bound
        return time.monotonic() - t0

    assert asyncio.run(flow()) < 1.0


def _settings_arg(command: list) -> dict:
    return json.loads(command[command.index("--settings") + 1])


def test_terminal_commands_allow_runs_unattended(terminal_run, tmp_path):
    """allow: unattended on this machine — skip the prompts, and pre-accept
    the bypass dialog that flag would otherwise raise."""
    rec = terminal_run["run"](commands="allow")
    assert rec["command"][:2] == [_CLAUDE["path"], "--dangerously-skip-permissions"]
    assert set(_settings_arg(rec["command"])) == {"apiKeyHelper", "skipDangerousModePermissionPrompt"}
    state = json.loads(open(os.path.join(_account_home(tmp_path), ".claude.json")).read())
    assert state["hasCompletedOnboarding"] is True


def test_terminal_commands_ask_keeps_claudes_own_prompt(terminal_run, tmp_path):
    """ask: no skip flag, the default mode, the cell's project and local
    settings loaded (the user tier is the per-account home), and the
    flag-settings tier asks about the shell tools every time, with bypass and
    auto off. A grant a previous leg saved for this workspace is withdrawn.
    (deny never reaches the host TTY: placement refuses it.)"""
    import gen

    home = _account_home(tmp_path)
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"projects": {gen.workspace_path(): {"allowedTools": ["Bash(rm:*)"]}}}, f)
    command = terminal_run["run"](commands="ask")["command"]
    assert "--dangerously-skip-permissions" not in command
    assert command[command.index("--permission-mode") + 1] == "default"
    assert "--setting-sources=project,local" in command and "--restricted" not in command
    settings = _settings_arg(command)
    perms = settings["permissions"]
    assert perms["ask"] == ["Bash", "Monitor", "PowerShell"] and "deny" not in perms
    assert perms["disableBypassPermissionsMode"] == "disable"
    assert perms["disableAutoMode"] == "disable"
    assert settings["sandbox"] == {"enabled": False, "autoAllowBashIfSandboxed": False}
    assert "--disallowedTools" not in command
    state = json.loads(open(os.path.join(home, ".claude.json")).read())
    assert state["projects"][gen.workspace_path()]["allowedTools"] == []


def test_managed_rules_only_refuses_ask_on_this_machine(terminal_run, monkeypatch):
    """allowManagedPermissionRulesOnly drops the ask rule the leg rides on:
    this machine's claude would run read-only commands unasked, so the CLI
    surface refuses ask (allow is unaffected)."""
    monkeypatch.setattr(ct, "claude_managed_rules_only", lambda: True)
    with pytest.raises(RuntimeError, match="allowManagedPermissionRulesOnly"):
        terminal_run["run"](commands="ask")
    assert "command" not in terminal_run
    assert terminal_run["run"](commands="allow")["command"][1] == "--dangerously-skip-permissions"


def test_the_terminal_needs_claude_on_this_machine(terminal_run):
    with pytest.raises(RuntimeError, match="this machine's claude, which was not found"):
        terminal_run["run"](host_harness=None)
    assert "command" not in terminal_run


@pytest.fixture(autouse=True)
def selected_account_runtime(account_runtime):
    snapshot = {"id": "entry-1", "metadata": {"harness": "claude-code"},
                "secrets": {"oauth_token": "test-subscription"}}
    with account_runtime(snapshot) as (root, _select_plan):
        yield root


def test_a_non_claude_model_fails_the_leg_instead_of_ending_it(
        terminal_run, monkeypatch):
    """A wrong model raises like every other startup failure (the terminal
    leg's failed path writes error.md and ends the run failed); the runner
    itself writes nothing and never reaches the TTY."""
    written: list = []
    monkeypatch.setattr(ct.gen, "update_file",
                        lambda path, *a, **kw: written.append(path))
    with pytest.raises(RuntimeError, match="Claude models"):
        terminal_run["run"](model="openai/gpt-4o")
    assert "command" not in terminal_run
    assert "error.md" not in written


def test_harness_resume_parameter_preserves_cli_invocation__F0287(terminal_run):
    harness_session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rec = terminal_run["run"](resume_harness_session_id=harness_session_id)
    argv = rec["command"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert set(settings) == {"apiKeyHelper", "skipDangerousModePermissionPrompt"}
    assert argv[argv.index("--session-id") + 1] == harness_session_id


@pytest.mark.parametrize("commands", ["allow", "ask", "deny"])
def test_terminal_native_bypass_acceptance_uses_current_settings_only_for_allow_P4F45(commands):
    """Pinned2.1.281 lA() reads this flag-settings key after legacy migration.

    The owned launch carries the current setting in its flag-settings tier,
    so native startup acceptance does not depend on persisted state migration.
    """
    argv = ct.claude_command_argv(commands, api_key_helper="/private/helper.py")
    settings = _settings_arg(argv)
    assert (settings.get("skipDangerousModePermissionPrompt") is True) == (commands == "allow")
    assert settings["apiKeyHelper"] == "/private/helper.py"
    assert ("--dangerously-skip-permissions" in argv) == (commands == "allow")
    if commands != "allow":
        assert "skipDangerousModePermissionPrompt" not in settings
        assert settings["permissions"]["disableBypassPermissionsMode"] == "disable"
