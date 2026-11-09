# SPDX-License-Identifier: MIT
"""ACP run orchestration — spawn profiles + the per-run session cache.

:func:`run_acp_turn` is the engine's ONE dispatch target for every external
harness (claude-code on the host; every other harness in its container, or
on the host under docker: none for the harnesses with a host twin — each
declared in ``harnesses/<package>/adapter.py``): it opens (or reuses)
the run's persistent :class:`AcpSession`, prompts it, and returns the
``(text, trace, usage)`` triple ``run_agent`` expects. The session — adapter
child, container, MCP bridge — is cached for the whole run (chat turns reuse
it) and torn down by the run-level teardown (``close_acp_sessions``, with
``cli_container.close_all`` running the sync twin).

**Spawn profiles.** ACP deliberately has no opinion about model selection,
web-tool gating, or credentials, so each harness's renderer owns its leg —
the adapter argv, the env/config seam for model + websearch + auth, the
credential arrangement — behind the generic pipeline
(:mod:`.harnesses.pipeline`, which resolves the credential and writes a home
seam first), and the per-cell harness home (§2.5 — the harness's
own session store must outlive the container for ``session/load`` to ever
work). The pattern per store, REGARDLESS of auth: **the per-cell harness home
is the harness's config/data dir** — the user's real ``~/.claude`` /
``~/.codex`` / ``~/.gemini`` is never read or written, so no run inherits
their settings, memory, skills, MCP config or session history. The host owns a shared transient credential store per account generation:

* claude-code carries its selected subscription token in the process env;
* codex, opencode and pi bind the lease's auth.json into the vendor home;
  their pinned writers update the file in place;
* cline binds the credential directory, with its location set by
  CLINE_PROVIDER_SETTINGS_PATH; its atomic replacement writes must remain
  inside that directory;
* antigravity copies the selected Google login into vendor state and returns
  the refreshed copy at release.

Each plan leg refuses a missing login before starting the harness and names
Key manager as the place to supply it. Plan legs carry no competing API key.

**System prompt.** ACP's ``session/new`` carries none, so it rides each
harness's own instruction seam (shared/docs/agent-runtime.md, instruction context) — never the user's message:

* claude-code — ``_meta.claudeCode.options.systemPrompt.append``
* opencode — the global config's ``instructions`` array names a
  container-local context file
* goose — the container's global ``~/.config/goose/.goosehints``
* antigravity — ``~/.gemini/config/rules/collimate-context.md`` in the
  per-cell home (agy's GLOBAL rules dir)
* a ``home_file`` seam, written by the pipeline into the per-cell home:
  codex ``$CODEX_HOME/AGENTS.md``, qwen-code ``~/.qwen/QWEN.md``, hermes
  ``$HERMES_HOME/SOUL.md``, vibe ``$VIBE_HOME/AGENTS.md``, copilot
  ``$COPILOT_HOME/copilot-instructions.md``

Every one of those paths is Collimate-owned — the harness's own container or
its per-cell home — never the workspace and never anything of the user's. The
harnesses left over (cline / pi) keep the fallback: the prompt
folded ahead of the FIRST prompt of a new session, fenced in
``transcript_merge.FOLD_OPEN``/``FOLD_CLOSE`` so the terminal-leg exporters
can strip it back out. A profile that carried the text sets
``fold_system_prompt=False``; the seam file is rewritten on every open
(``session/load`` included) and cleared when the run carries no prompt.

**Transport failure** (§2.5): a dead adapter mid-run emits an error event and
a phase message, then ONE bounded respawn + ``session/load`` where advertised
— otherwise the run fails. Never park a dead session as alive.
"""
from __future__ import annotations

import asyncio
import functools
import contextlib
import time
from typing import Callable, Optional

import gen
from .acp_policy import mode_allowed, offered_options, option_gated
from .harnesses import HARNESS, resolve

from .acp_client import AcpSession, AcpTransportDied, _log, build_mcp_servers, read_session_file, resumable_acp_session_id
from .harness_homes import sync_harness_homes

# The run's open sessions, by (backend, conversation) — a run only ever uses
# one backend.
_ACP_SESSIONS: dict = {}

# ──────────────────────────────────────────────────────────────────────
# The run's live options (gen.publish_options): what the status section
# shows in the run's expanded row and the user changes there. They are
# the run's, not one conversation's — every session the run opens (each
# chat thread's, a reopen after the idle park or a dead transport) runs
# under them.
#
# * The harness's own: its sessions' configOptions (mode, model, thought
#   level, its extras), held to the grant's commands level
#   (acp_policy.offered_options), known once a session is open.
# * Collimate's own (HarnessAdapter.live_options, e.g. claude's ultracode):
#   published before any session opens, carried into the spawn profile
#   (profile_options), and applied by reopening each session over
#   session/load at its next turn.
#
# Every pick is STICKY: a reopen rebuilds the session from the run config
# (the account model through the spawn seam), and the picks are re-applied
# over it before its first prompt (_reapply_sticky_config).
# ──────────────────────────────────────────────────────────────────────

ULTRACODE = "ultracode"
# The user's picks, by option id.
_STICKY_CONFIG: dict = {}
# The harness's options as the run's sessions last showed them (offered,
# normalized) — what a pick made with no session open is checked against.
_LAST_UI_OPTIONS: list = []
# The set last published ([] = none yet).
_PUBLISHED: list = [[]]


def clear_run_state() -> None:
    """Forget the run's option picks and option set, and the once-per-run
    log marks, at the run's end. NOT done by ``close_acp_sessions``: a
    terminal leg calls that mid-run, and the picks must survive it."""
    from . import harness_homes
    _STICKY_CONFIG.clear()
    _LAST_UI_OPTIONS.clear()
    _PUBLISHED[0] = []
    harness_homes._NO_SESSION_PATHS_LOGGED.clear()


def collimate_options(backend: str) -> list:
    """The options Collimate itself offers for ``backend``'s harness
    (``HarnessAdapter.live_options``), at the run's picks: what a run
    publishes before any session opens."""
    row = HARNESS.get(backend)
    if row is None or not row.live_options.ultracode:
        return []
    return [{
        "id": ULTRACODE, "name": "Ultracode", "category": "model_config", "type": "boolean",
        "description": ("Standing dynamic-workflow orchestration: Claude plans multi-agent "
                        "workflows for every request. Takes effect from the next message."),
        "currentValue": bool(_STICKY_CONFIG.get(ULTRACODE, False)),
    }]


def profile_options(backend: str) -> dict:
    """The Collimate-owned picks a spawn profile carries (``ctx["live"]``):
    ``{option id: value}`` for every option :func:`collimate_options`
    offers."""
    return {o["id"]: o["currentValue"] for o in collimate_options(backend)}


def run_options(backend: str) -> list:
    """The run's whole live option set: Collimate's, then the harness's."""
    return collimate_options(backend) + _LAST_UI_OPTIONS


def _text(value) -> "str | None":
    return value if isinstance(value, str) and value else None


def _run_option(option: dict) -> "dict | None":
    """A session option in the run-option shape (``gen.publish_options``),
    or None for one that does not fit it."""
    oid, kind, current = option.get("id"), option.get("type"), option.get("currentValue")
    if not _text(oid):
        return None
    shaped = {"id": oid, "name": _text(option.get("name")) or oid,
              **{key: option[key] for key in ("description", "category") if _text(option.get(key))},
              "type": kind, "currentValue": current}
    if kind == "boolean":
        return shaped if isinstance(current, bool) else None
    if kind != "select" or not isinstance(current, str):
        return None
    shaped["options"] = [
        {"value": v["value"], "name": _text(v.get("name")) or v["value"],
         **({"description": v["description"]} if _text(v.get("description")) else {})}
        for v in option.get("options") or [] if isinstance(v, dict) and _text(v.get("value"))]
    return shaped if shaped["options"] else None


def _absorb(backend: str, commands: str, session: AcpSession) -> None:
    """Take ``session``'s options as the harness's: the ones a run under
    ``commands`` may change (:func:`acp_policy.offered_options`), in the
    run-option shape (one that does not fit it is left out, logged)."""
    kept = []
    for option in offered_options(backend, commands, session.ui_config_options()):
        shaped = _run_option(option)
        if shaped is None:
            _log(f"acp[{backend}]: option {option.get('id')!r} does not fit the run's "
                 "options — left out")
        else:
            kept.append(shaped)
    _LAST_UI_OPTIONS[:] = kept


def publish_run_options(backend: str, *, force: bool = False) -> None:
    """Publish the run's live options when they differ from the set last
    published — or always (``force``), the answer to a change the user
    made, applied or not, which the status section waits on."""
    options = run_options(backend)
    if options == _PUBLISHED[0] and not force:
        return
    _PUBLISHED[0] = options
    try:
        gen.publish_options(options)
    except Exception as exc:  # noqa: BLE001 — the run goes on without them
        _log(f"acp[{backend}]: the run's options could not be published ({exc})")


def _options_listener(backend: str, commands: str) -> Callable[[AcpSession], None]:
    """An ``AcpSession.on_options`` for a run under ``commands``: the agent
    reported a new option set or mode — take it and republish."""
    def changed(session: AcpSession) -> None:
        _absorb(backend, commands, session)
        publish_run_options(backend)
    return changed


# How long a harness's last wall-clock line stays fresh before the next
# prompt carries a new one. The line is folded into the PROMPT (ACP has no
# per-turn system channel), and every harness stores prompt text verbatim —
# its own TUI and session export show it back — so it must not ride every
# turn: once at session open (inside the system prompt) and then only when
# stale. 20 minutes bounds the drift the model can suffer when it dates
# something, and a normal chat sees the line once.
_CLOCK_REFRESH_S = 20 * 60

# The in-container stdio↔HTTP proxy (carrier B — §2.3): installed globally in
# every adapter image via its _IMAGE_ARTIFACTS row. stdio is the protocol's
# only MANDATORY transport, so a harness that advertises no
# ``mcpCapabilities.http`` still reaches the bridge through this.
STDIO_PROXY_ARGV = ("mcp-remote",)

# Generous per-turn ceiling — the stop signal and the run's hard wallclock are
# the real bounds; this only stops a wedged adapter from pinning a turn forever.
_TURN_TIMEOUT_S = 1800.0


async def close_acp_sessions() -> None:
    """Close every cached session (adapter children included), concurrently.
    Idempotent; called from the same run-level teardown that closes the
    containers.

    The sessions leave the cache first, so no other reaper can reach them: a
    cancel or stop that lands mid-close still kills every child, through the
    sync ``discard`` (a no-op for a session already closed)."""
    sessions = list(_ACP_SESSIONS.values())
    _ACP_SESSIONS.clear()
    try:
        # Concurrently, and best-effort: one close raising or dawdling never
        # holds up the others.
        await asyncio.gather(*(s.close() for s in sessions),
                             return_exceptions=True)
    finally:
        for s in sessions:
            with contextlib.suppress(Exception):
                s.discard()


def close_acp_sessions_sync() -> None:
    """The sync twin for ``cli_container.close_all`` (atexit-safe): runs each
    session's ``discard``, which kills its adapter children and command
    terminals directly — the transports die with the process."""
    sessions = list(_ACP_SESSIONS.values())
    _ACP_SESSIONS.clear()
    for s in sessions:
        with contextlib.suppress(Exception):
            s.discard()


# ──────────────────────────────────────────────────────────────────────
# Spawn profiles: every harness's leg is prepared by the generic pipeline
# (harnesses.pipeline) from its adapter declaration. A host twin's standard
# runtime drives the machine's own CLI the way its container twin is driven —
# Collimate never installs or updates that CLI, and the argv[0] it spawns is
# exactly the path ``host_harness.probe`` found — writing the same seams into
# the per-cell harness home on disk instead of the image and reaching the
# tool bridge on the loopback URL. There is no container boundary there: the
# harness's file and shell tools act on this machine.
# ──────────────────────────────────────────────────────────────────────


async def prepare_profile(ctx: dict, *, harness: str):
    from .harnesses.pipeline import prepare
    return (await prepare(HARNESS[harness], ctx, leg="acp")).profile


_PROFILES = {name: functools.partial(prepare_profile, harness=name)
             for name, row in HARNESS.items() if row.acp_argv}


async def mint_acp_session(cfg: dict) -> str:
    """A terminal run mints the ACP session FIRST. Open the session (which
    writes the ``_agent/session.json`` continuity record), take its id, close
    the adapter, and hand the id to the TTY (``claude --resume`` / ``codex
    resume`` / the goose/opencode session flags). The TTY then writes its
    conversation into that session, so the live tail follows that
    conversation. (A harness whose TTY names its own conversation —
    ``BackendSpec.harness_session_ids`` — is not minted: its tail follows the
    one conversation that appears after the store's earlier ones.)

    Minimal surface on purpose (no file tools, web rows or cell MCP): the session is closed immediately, and a later
    ``session/load`` re-sends the real leg's own ``mcpServers``."""
    session = await _open_session(
        backend=cfg["backend"], auth=cfg["auth"], provider=cfg["provider"],
        model=cfg["model"], system_prompt="",
        mcp_decls=[],
        enable_file_tools=False, websearch=False, turn_skills=None,
        approvals="auto", commands=cfg["commands"],
    )
    sid = session.acp_session_id
    await session.close()
    return sid


# ──────────────────────────────────────────────────────────────────────
# The one dispatch target.
# ──────────────────────────────────────────────────────────────────────


async def _reapply_sticky_config(session: AcpSession, backend: str, commands: str) -> None:
    """Re-apply the run's picks of the harness's options to a freshly
    (re)opened session — best-effort, one log line per option either way.
    An option the reopened session already shows at the picked value (a new
    session built from the run config's seam, a load that restored it) is
    skipped — re-applying would only re-run the harness's switch command.
    A pick beyond this turn's ``commands`` level (:func:`mode_allowed`) is
    dropped, never re-applied: the session-level switch checks no grant.
    Collimate's own picks rode the spawn profile already."""
    current = {str(o.get("id")): o for o in session.ui_config_options()}
    for cid, value in list(_STICKY_CONFIG.items()):
        if cid in session.opened_with:
            continue
        option = current.get(cid)
        if option is None:
            _log(f"acp[{backend}]: the reopened session offers no {cid!r} — "
                 f"the pick {value!r} waits")
            continue
        if option_gated(option) and not mode_allowed(backend, commands, value):
            _log(f"acp[{backend}]: dropped the sticky {cid}={value!r} — beyond "
                 f"this runner's commands permission ({commands})")
            continue
        if option.get("currentValue") == value:
            continue
        try:
            await session.apply_config_change(cid, value)
            _log(f"acp[{backend}]: re-applied {cid}={value!r} on the reopened session")
        except Exception as exc:  # noqa: BLE001 — the turn still runs
            _log(f"acp[{backend}]: could not re-apply {cid}={value!r} on "
                 f"the reopened session ({exc})")


async def run_acp_turn(
    *, backend: str, prompt: str, auth: str, provider: str, model: str,
    system_prompt: str, mcp_decls,
    stream: bool, emit: Callable[[dict], None],
    should_stop: "Optional[Callable[[], bool]]" = None,
    enable_file_tools: bool = True, websearch: bool = True,
    attachments: "list | None" = None, turn_skills: "list | None" = None,
    approvals: str = "auto",
    commands: str = "deny",
    session_key: str = "",
    runtime: str = "docker", host_harness: "dict | None" = None,
    annotation_tools: bool = False,
) -> tuple:
    """One prompt turn against the run's persistent ACP session. Returns
    ``(text, trace, usage)`` — ``usage`` is ``{}`` by contract: ACP's
    ``usage_update`` is a context meter (emitted live), not per-turn tokens.

    The session is cached per ``(backend, session_key)``: chat threads are
    independent conversations (``session_key`` is the chat thread id), so each
    gets its own ACP session — otherwise switching threads would bleed one
    thread's harness-side history into another's. The spawn profiles carry
    the model and the websearch toggle through each harness's own seams.
    ``annotation_tools`` adds the Annotation surface's annotate and
    list_annotations rows to the session's tool server."""
    key = (backend, session_key)
    open_kwargs = dict(
        backend=backend, auth=auth, provider=provider, model=model,
        system_prompt=system_prompt,
        mcp_decls=mcp_decls,
        enable_file_tools=enable_file_tools, websearch=websearch,
        turn_skills=turn_skills,
        approvals=approvals, commands=commands,
        runtime=runtime, host_harness=host_harness,
        annotation_tools=annotation_tools,
    )

    async def _reopen(load_id: str = "") -> AcpSession:
        # Open, cache, carry the run's picks over, and publish what the
        # session offers.
        live = profile_options(backend)
        s = await _open_session(load_acp_session_id=load_id, live=live, **open_kwargs)
        s.opened_with = live
        _ACP_SESSIONS[key] = s
        await _reapply_sticky_config(s, backend, commands)
        s.on_options = _options_listener(backend, commands)
        _absorb(backend, commands, s)
        publish_run_options(backend)
        return s

    session = _ACP_SESSIONS.get(key)
    first_turn = session is None
    if session is not None and session.loop is not asyncio.get_running_loop():
        # The cached session's transport lives on another (likely closed)
        # event loop — driving it from here would hang silently on a reply
        # its receive task can never process. Reprovision on THIS loop and
        # carry the conversation over session/load where advertised.
        _ACP_SESSIONS.pop(key, None)
        old_id = session.acp_session_id if session.agent_caps.get("loadSession") else ""
        await asyncio.to_thread(session.discard)   # its kill blocks; keep it off the loop
        session = await _reopen(old_id)
        # When the load failed this is effectively a fresh session — the
        # first-turn system-prompt fold must still happen for the harnesses
        # whose profile has no real seam.
        first_turn = not session.loaded
    elif session is not None and session.opened_with != profile_options(backend):
        # The user changed a Collimate-owned option (ultracode) the spawn
        # profile carries: reopen the session with it, over session/load
        # where advertised, so the conversation goes on under the new value.
        _ACP_SESSIONS.pop(key, None)
        old_id = session.acp_session_id if session.agent_caps.get("loadSession") else ""
        _log(f"acp[{backend}]: reopening the session for the run's new options")
        await session.close()
        session = await _reopen(old_id)
        first_turn = not session.loaded
    if session is None:
        session = await _reopen()

    # Staged ONCE per turn: staging writes bytes-only attachments into the
    # cell, and a re-fold for a respawned session must not write them twice.
    refs = _staged_attachment_refs(attachments)

    def _turn_text(sess: AcpSession, first: bool) -> str:
        return _fold_prompt(sess, prompt, system_prompt=system_prompt,
                            first_turn=first, refs=refs)

    async def _send(sess: AcpSession) -> tuple:
        return await asyncio.wait_for(
            sess.prompt(
                text, emit=emit, stream=stream, should_stop=should_stop,
                attachments=attachments,
            ),
            timeout=_TURN_TIMEOUT_S,
        )

    text = _turn_text(session, first_turn)
    try:
        reply, trace, _stop = await _send(session)
        return reply, trace, {}
    except AcpTransportDied as e:
        # §2.5: a phase message, then ONE bounded respawn + session/load
        # where advertised; otherwise the run fails — a dead session is never
        # parked as alive. NOT an "error" event: the surface ends the live
        # stream on one, and this turn goes on. Instead the caller drops the
        # dead attempt's partial reply (text_reset) so the retry's answer is
        # the whole reply, not glued onto the fragment it replaces.
        emit({"type": "text_reset", "reason": f"agent transport lost: {e}"})
        try:
            gen.phase("running", "agent transport lost — reprovisioning")
        except Exception:  # noqa: BLE001
            pass
        _log(f"acp[{backend}]: transport died ({e}) — one respawn")
        old = _ACP_SESSIONS.pop(key, None)
        old_id = old.acp_session_id if old is not None else ""
        can_load = bool(old is not None and old.agent_caps.get("loadSession"))
        if old is not None:
            await old.close()
        session = await _reopen(old_id if can_load else "")
        # The text was folded for the dead session: a NEW replacement (no
        # loadSession, or the load fell back) needs the first-turn fold, a
        # loaded one its own delta/clock handling.
        text = _turn_text(session, not session.loaded)
        reply, trace, _stop = await _send(session)
        # The durable record says the turn was re-sent (the dead attempt's
        # own events never reached a trace).
        note = {"type": "system_note",
                "text": f"agent transport lost ({e}); respawned and re-sent the prompt"}
        return reply, [note, *trace], {}
    finally:
        # Persist the harness's session store at every turn boundary: a
        # SIGKILL (Force Stop, or Stop's escalation) never reaches the run
        # end's close_harness_homes, and the cell's store would then miss
        # this run's session.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(sync_harness_homes)


# backend → ``(commands, mode value) -> bool`` — see mode_allowed. Every ACP
# backend has a row (tests/test_agent_matrix pins it).
_MODE_RULES = {name: resolve(row.commands.mode_rule) for name, row in HARNESS.items() if row.acp_argv}


async def apply_config_change(backend: str, config_id: str, value, *, commands: str) -> None:
    """Apply the user's change to one of the run's live options and record
    it as the run's pick (re-applied on every reopen). Callers report
    failures, never crash the run, and publish the run's options after
    either way.

    * A Collimate-owned option (:func:`collimate_options`) is recorded only:
      each open session is reopened with it at its next turn.
    * A harness option goes to every session the run has open on this loop
      (each chat thread's). With none open — a terminal leg or the idle
      park closed them — it is recorded for the next open and the last set
      shown is patched, so the pane shows the value the next turn runs
      with.

    Refused: an id the run does not offer or a value its option does not
    (``ValueError``) — the host checked the change against the set published
    then, and the harness may have offered another set since — and a value
    of an option held to the grant (:func:`acp_policy.option_gated`) that
    would loosen its ``commands`` level (``PermissionError``)."""
    cid = str(config_id or "")
    if any(o["id"] == cid for o in collimate_options(backend)):
        if not isinstance(value, bool):
            raise ValueError(f"{cid} takes true or false")
        _STICKY_CONFIG[cid] = value
        return
    option = next((o for o in _LAST_UI_OPTIONS if o["id"] == cid), None)
    if option is None:
        raise ValueError(f"the run offers no option {cid!r}")
    if not (isinstance(value, bool) if option["type"] == "boolean"
            else any(v["value"] == value for v in option["options"])):
        raise ValueError(f"{option['name']} offers no value {value!r}")
    if option_gated(option) and not mode_allowed(backend, commands, value):
        raise PermissionError(
            f"{option['name']} {value!r} would run commands beyond this runner's "
            f"commands permission ({commands}) — change the runner's grant instead")
    loop = asyncio.get_running_loop()
    sessions = [s for (b, _key), s in list(_ACP_SESSIONS.items())
                if b == backend and s.loop is loop]
    for session in sessions:
        await session.apply_config_change(cid, value)
    _STICKY_CONFIG[cid] = value
    if sessions:
        _absorb(backend, commands, sessions[-1])
    else:
        _LAST_UI_OPTIONS[:] = [{**o, "currentValue": value} if o["id"] == cid else o
                               for o in _LAST_UI_OPTIONS]


def _fold_prompt(
    session: AcpSession, prompt: str, *, system_prompt: str,
    first_turn: bool, refs: str = "",
) -> str:
    """The prompt text for this turn: the system prompt folded ahead of a NEW
    session's first turn (harnesses whose profile has no real seam), a
    wall-clock refresh when the harness's last one is stale, plus the turn's
    staged-attachment references (``refs``, from :func:`_staged_attachment_refs`).

    The clock does NOT ride every turn. ``run_agent`` already appends the
    wall-clock line to the system prompt, so a new session gets it once —
    inside the folded system prompt here, or through the profile's own seam
    (claude's ``_meta`` options). Every harness stores the prompt text
    verbatim and shows it back (its TUI on a terminal leg, its session
    export), so repeating the line per turn littered every conversation
    with timestamps — and on the first turn of the folding harnesses the
    line appeared TWICE. It comes back only when ``_CLOCK_REFRESH_S`` has
    passed (or the session was loaded and its history's clock is unknown),
    and then as one line ahead of the prompt.

    The preamble is wrapped in the ``FOLD_OPEN`` / ``FOLD_CLOSE`` markers so
    the terminal-leg exporters can strip it back out of the user's message
    when they backfill the transcript (``transcript_merge``)."""
    from .agent_events import clock_line
    from .transcript_merge import FOLD_CLOSE, FOLD_OPEN

    parts: list = []
    now = time.monotonic()
    profile_folds = session.profile.fold_system_prompt
    delivered = getattr(session, "delivered_system", None)
    if delivered is None and session.loaded and profile_folds:
        # A loaded session's history holds the standing instructions it ran
        # under before (an idle park's reopen): measure against the prompt
        # as it stands. A seam profile rewrote its seam at this open, so it
        # already has them.
        delivered = system_prompt
    # What the harness was told at open — through the profile's seam, the
    # first-turn fold below, or (a loaded session) its own history — is the
    # baseline later turns' directive changes are measured against.
    session.delivered_system = system_prompt
    if first_turn and system_prompt and profile_folds and not session.loaded:
        parts.append(FOLD_OPEN)
        parts.append(system_prompt)
        parts.append(FOLD_CLOSE)
        session.clock_at = now
    else:
        delta = (_system_prompt_delta(delivered, system_prompt)
                 if delivered is not None else "")
        if delta:
            parts += [FOLD_OPEN, delta, FOLD_CLOSE]
        clock_at = float(getattr(session, "clock_at", 0.0) or 0.0)
        if not clock_at or now - clock_at >= _CLOCK_REFRESH_S:
            parts.append(clock_line())
            session.clock_at = now
    if refs:
        parts.append(refs)
    parts.append(prompt)
    return "\n\n".join(p for p in parts if p)


def _prompt_paragraphs(text: str) -> list:
    from .transcript_merge import CLOCK_HEAD   # clock_line()'s fixed opening
    return [p.strip() for p in (text or "").split("\n\n")
            if p.strip() and not p.strip().startswith(CLOCK_HEAD)]


def _system_prompt_delta(before: str, after: str) -> str:
    """The session instructions that changed since the harness last got them.

    ACP has no per-turn system channel: the system prompt reaches a harness
    once, at open (its seam) or in the first-turn fold. But a caller may
    rebuild it every turn — a paragraph added for some turns, dropped for
    others — and the SAME session carries on. Without this, a later turn's
    instructions would never reach a session opened before them.
    Paragraph-level, the wall-clock line excluded (it changes every turn and
    has its own refresh); a withdrawn instruction is named by its opening, not
    repeated; a one-turn note is never reported withdrawn."""
    old, new = _prompt_paragraphs(before), _prompt_paragraphs(after)
    added = [p for p in new if p not in old]
    removed = [p for p in old if p not in new]
    out: list = []
    if removed:
        def _opening(p: str) -> str:
            line = p.splitlines()[0]
            return line if len(line) <= 160 else line[:157] + "…"
        out.append("These earlier session instructions NO LONGER apply:\n"
                   + "\n".join(f"- {_opening(p)}" for p in removed))
    if added:
        out.append("Session instructions updated — these now apply:\n\n"
                   + "\n\n".join(added))
    return "\n\n".join(out)


def _staged_attachment_refs(attachments) -> str:
    """Reference the turn's attachments in the prompt by their CELL path.

    A chat attachment already lives in the cell (the renderer uploads it to
    ``attachments/<name>`` and the turn carries that ``path``), so the
    harness is pointed at exactly that file — never a staged copy under a
    different name. An attachment that arrives as bare bytes (a caller's input) is written to ``attachments/`` first so the same rule
    holds. Every attachment is listed, images included: an image may
    additionally ride INLINE as a content block where the agent's
    promptCapabilities allow (AcpSession adds it), but the model still
    needs the cell location to refer back to the file."""
    from .cli_container import stage_attachments

    if not attachments:
        return ""
    refs: list = []
    to_stage: list = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "")
        mt = str(a.get("media_type") or "")
        if path and gen.has_file(path):
            refs.append((path, mt))
        else:
            to_stage.append(a)
    refs += stage_attachments(to_stage) if to_stage else []
    from .agent_events import attachment_ref_lines
    return attachment_ref_lines([{"path": p, "media_type": mt} for p, mt in refs])


def _profile_ctx(
    *, auth: str, provider: str, model: str, system_prompt: str,
    enable_file_tools: bool, websearch: bool, runtime: str,
    host_harness: "dict | None", commands: str,
) -> dict:
    """Normalize configuration once before any harness profile selects a path.

    Profiles receive the same auth spelling and runtime/command values;
    direct run_agent callers have the same behavior as saved configurations.
    """
    return {
        "auth": (auth or "api_key").strip().lower(),
        "provider": provider, "model": model, "system_prompt": system_prompt,
        "enable_file_tools": enable_file_tools, "websearch": websearch,
        "runtime": (runtime or "docker").strip().lower(),
        "host_harness": host_harness,
        "commands": (commands or "deny").strip().lower(),
    }


async def _open_session(
    *, backend: str, auth: str, provider: str, model: str, system_prompt: str,
    mcp_decls, enable_file_tools: bool, websearch: bool,
    turn_skills, approvals: str = "auto", commands: str = "deny",
    load_acp_session_id: str = "",
    runtime: str = "docker", host_harness: "dict | None" = None,
    annotation_tools: bool = False, live: "dict | None" = None,
) -> AcpSession:
    """Build the profile, start the bridge, spawn the adapter, and mint (or
    load) the session. An in-run reopen (an idle park's re-provision, a
    respawn after a dead transport): with no explicit ``load_acp_session_id``, this run's
    ``_agent/session.json`` supplies one when its backend matches and the
    host-side record holds it at a commands level no looser than this one
    (``resumable_acp_session_id``) — ``AcpSession.start`` falls back to a new
    session when the load can't happen. A run's first open finds no record:
    the run deletes it at start. ``live`` is the run's picks of
    Collimate-owned options (:func:`profile_options`), which the harness's
    profile carries (claude's ``settings.ultracode``)."""
    from .cli_container import gateway_bridge

    ctx = _profile_ctx(
        auth=auth, provider=provider, model=model, system_prompt=system_prompt,
        enable_file_tools=enable_file_tools, websearch=websearch,
        runtime=runtime, host_harness=host_harness, commands=commands,
    )
    ctx["live"] = dict(live or {})
    profile = await _PROFILES[backend](ctx)
    commands = profile.commands
    if profile.commands_notice:
        _log(f"warning: {profile.commands_notice}")
        with contextlib.suppress(Exception):
            gen.phase("spinning_up", profile.commands_notice)

    # One tool server for the whole run: the workspace rows (read/write excluded — those are the two ACP fs
    # capabilities now). No exec rows here — every ACP harness has its own
    # shell, gated by the grant's commands level (its native settings plus
    # the client's permission gate).
    from .workspace_tools import tool_entries
    from .generator_test import CommandContext

    ws_rows = tool_entries(
        enable_file_tools=enable_file_tools, websearch=websearch,
        skills=turn_skills or [], commands=commands,
        command_context=CommandContext(profile.locus, env_obj=profile.env_obj) if commands != 'deny' else None,
        annotation_tools=annotation_tools,
    )
    bridge = gateway_bridge(
        extra_entries=ws_rows or None,
        bind_host=profile.bind_host,
        cache_scope=(commands, profile.locus, id(profile.env_obj)),
    )
    if bridge is None:
        # Handler tools are always requested. Make missing bridge capability
        # visible to the session as well as the underlying startup diagnostic.
        _log('acp: the collimate tool server is NOT attached to this session '
             '— the harness runs without handler/workspace tools; see the '
             'earlier "MCP bridge unavailable" line for the cause')

    if profile.bind_bridge is not None:
        await profile.bind_bridge(bridge)

    def _mcp_servers_for(agent_caps: dict) -> tuple:
        http_ok = bool(
            (agent_caps.get("mcpCapabilities") or {}).get("http"))
        return build_mcp_servers(
            bridge=bridge, mcp_decls=mcp_decls, locus=profile.locus,
            http_ok=http_ok, stdio_proxy_argv=list(STDIO_PROXY_ARGV),
        )

    session = AcpSession(
        backend=backend, profile=profile, mcp_servers_for=_mcp_servers_for,
        auth=ctx["auth"], model=model, file_tools_enabled=enable_file_tools,
        websearch_enabled=websearch, approvals=approvals,
    )
    if not load_acp_session_id:
        load_acp_session_id = resumable_acp_session_id(
            read_session_file(), backend=backend, commands=commands)
    await session.start(load_acp_session_id=load_acp_session_id)
    # A NEW session just received the wall-clock line (inside the system
    # prompt the profile seam or the first-turn fold carries); a LOADED one
    # resumes a history whose last clock may be days old — leave it unknown
    # so the next prompt refreshes it (see _fold_prompt).
    session.clock_at = 0.0 if session.loaded else time.monotonic()
    return session
