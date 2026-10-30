# SPDX-License-Identifier: MIT
"""Orchestrate agent turns, run capabilities and lifecycle.

Configuration and event helpers live in agent_config and agent_events.
Every harness is declared one per package under harnesses/ (pydantic-ai,
Pydantic AI's own ACP adapter, among them) and prepared by its pipeline, with
terminal legs sharing session state where supported; workspace and web tools
live in workspace_tools and web_tools and reach every harness over the MCP
bridge.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import asyncio
import os
import shutil
import re
import time
from dataclasses import replace
from typing import Callable, Optional

import gen
from .harnesses import BackendSpec as BackendSpec
from .agent_config import (
    backend_spec as backend_spec,
    container_refusal as container_refusal,
    full_claude_model_id as full_claude_model_id,
    is_claude_model as is_claude_model,
    model_family as model_family,
    parse_agent_config as parse_agent_config,
    resolve_model as resolve_model,
    run_needs_container as run_needs_container,
    to_bool as to_bool,
)
from .agent_events import (
    ATTACH_BYTE_CAP as ATTACH_BYTE_CAP,
    AgentResult as AgentResult,
    TOOL_INPUT_CAP as TOOL_INPUT_CAP,
    TOOL_RESULT_CAP as TOOL_RESULT_CAP,
    TRACE_FILE as TRACE_FILE,
    attachment_ref_lines as attachment_ref_lines,
    clock_line as clock_line,
    explain_turn_failure as explain_turn_failure,
    TurnRefused as TurnRefused,
    service_refusal as service_refusal,
    iter_attachments as iter_attachments,
    safe_log as safe_log,
    tool_summary as tool_summary,
    trace_event as trace_event,
)

from collimate.account_taxonomy import (
    CLAUDE_MODEL_ALIASES as CLAUDE_MODEL_ALIASES,
    PLAN_DEFAULT_MODELS as PLAN_DEFAULT_MODELS,
    PLAN_PROVIDERS as PLAN_PROVIDERS,
    default_model_for as default_model_for,
)
from collimate.providers import (
    PROVIDER_DEFAULT_MODELS as PROVIDER_DEFAULT_MODELS,
    STANDARD_MODEL_ENDPOINTS as STANDARD_MODEL_ENDPOINTS,
)
from .accounts import account_secret

from .cell_caps import SKILLS_DIR as _SKILLS_DIR
from .cell_caps import CellCaps as _CellCaps
from .cell_caps import discover_cell_capabilities as _discover_cell_capabilities
from .cell_caps import skills_index as _cc_index

_EMPTY_CAPS = _CellCaps()

# The run's cell-carried capabilities, by workspace: discovered at the first
# turn and kept for the run. The config folders are the runner's declaration,
# staged before that turn — but they sit under the workspace, which a
# container harness mounts read-write, so its own file tools can write there.
# Nothing written later is ever attached (a planted ``_mcp`` stdio server would
# be a command the grant never allowed).
_RUN_CELL_CAPS: dict = {}


def _read_cell_caps() -> _CellCaps:
    """Read the runner's ``_skills`` / ``_mcp`` config folders — best-effort
    (a bridge-free/unit context, or a runner that provides no folders,
    returns empty)."""
    try:
        return _discover_cell_capabilities(
            gen.config_folder("_skills"), gen.config_folder("_mcp"),
        )
    except Exception as e:  # noqa: BLE001
        safe_log(f"cell-caps: discovery skipped ({e})")
        return _EMPTY_CAPS


def discover_cell_caps() -> _CellCaps:
    """The run's cell-carried capabilities: the first discovery for this
    workspace, reused on every later turn (see :data:`_RUN_CELL_CAPS`). A
    later read that differs is logged and ignored."""
    caps = _read_cell_caps()
    try:
        workspace = gen.workspace_path()
    except Exception:  # noqa: BLE001 — bridge-free: nothing to keep it for
        return caps
    first = _RUN_CELL_CAPS.setdefault(workspace, caps)
    if caps.digest != first.digest:
        safe_log("cell-caps: the runner's config folders changed during the "
                  "run — keeping the capabilities discovered at its first turn")
    return first


def stage_builtin_skills(source_dir: str, *, names=None) -> list:
    """Copy a generator's own skills into the run's staged ``_skills`` config
    folder, so every backend finds them the way it finds the runner's: by
    reading the folder under the workspace (``/workspace`` in a container). ``names`` stages only those skill
    folders (``None``: every one). A runner-provided folder of the same
    name is left alone — the runner's copy wins. Returns the names staged;
    ``[]`` in a bridge-free context or when nothing needed staging."""
    try:
        root = gen.config_folder_path(_SKILLS_DIR)
    except Exception:  # noqa: BLE001 — bridge-free / unit context
        return []
    staged: list = []
    for entry in sorted(os.listdir(source_dir)) if os.path.isdir(source_dir) else []:
        if names is not None and entry not in names:
            continue
        src = os.path.join(source_dir, entry)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        dst = os.path.join(root, entry)
        if os.path.exists(dst):
            continue
        shutil.copytree(src, dst)
        staged.append(entry)
    return staged


def _cell_mcp_decls(caps: _CellCaps, cell_mcp: str) -> list:
    """The MCP declarations to attach for a ``cell_mcp`` setting: none when
    off, http-only under ``http``, all under ``http_stdio``."""
    if cell_mcp == "off":
        return []
    if cell_mcp == "http":
        return [d for d in caps.mcp if d.transport == "http"]
    return list(caps.mcp)


# What makes a harness's own URL parser read a cell MCP URL differently from
# the urlsplit parse the SSRF gate judges. The container harnesses parse the
# WHATWG way (node's URL, Rust's url crate): a backslash ends the authority
# there, so ``http://169.254.169.254\@example.com/`` names the metadata host
# to them and example.com to urlsplit; tabs, newlines and other controls are
# dropped before parsing.
_MCP_URL_CONFUSABLE = re.compile(r"[\\\x00-\x20\x7f]")
# A host every parser reads alike: ASCII labels of letters, digits, - and _.
_MCP_PLAIN_HOST = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?")
# A last label that makes WHATWG read the host as an IPv4 number
# (``0x7f.1``, ``2130706433``, ``0177.0.0.1``, ``127.0.0.1.``).
_WHATWG_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")


def _mcp_url_problem(url: str) -> "str | None":
    """Why an http MCP declaration's URL is refused before the SSRF gate
    judges it, or ``None``. The gate reads the URL with urlsplit; a harness
    in its own process reads it with its own parser. So the URL must name
    the same host to every parser: no backslash, whitespace or control
    character; a port that parses; and a host that is an IP address or a
    plain ASCII name (not percent-encoded, not Unicode, and not a number
    WHATWG reads as an IPv4 address the canonical form would not spell).
    Credentials before the host are refused too: a declaration carries them
    in its headers, never in the URL."""
    import ipaddress
    from urllib.parse import urlsplit

    if _MCP_URL_CONFUSABLE.search(url or ""):
        return "the URL contains a backslash, space or control character"
    try:
        parts = urlsplit(url or "")
        parts.port                          # raises on a malformed port
    except ValueError as e:
        return f"the URL does not parse ({e})"
    host = parts.hostname or ""
    if parts.username is not None or parts.password is not None:
        return (f"the URL puts credentials before its host {host!r} "
                "(declare them as headers)")
    if not host:
        return None                          # the gate refuses a hostless URL
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if not _MCP_PLAIN_HOST.fullmatch(host):
        return f"the URL host {host!r} is not a plain ASCII name or IP address"
    if _WHATWG_NUMERIC_LABEL.fullmatch(host.rstrip(".").rsplit(".", 1)[-1]):
        return f"the URL host {host!r} is a non-canonical IP address"
    return None


def filter_mcp_decls(decls: list, *, containerized: bool) -> tuple:
    """``(kept, warnings)`` after the SSRF gate on http declarations.

    Cell files are shareable, so a declared URL is not automatically trusted.
    Every http URL must first read the same to every parser
    (:func:`_mcp_url_problem`), then pass the same :func:`vet_url` guard the
    web tools use. Every harness runs in its own process (a container, a
    host adapter, a host twin), so the check is a name-time one: that process
    resolves the name itself.

    The ONE exception is a host loopback URL when ``containerized`` — the
    harness runs in its container THIS run (:func:`run_needs_container`; a
    host twin under ``docker: none`` does not):
    :func:`cell_attach.is_container_loopback` (parsed, userinfo refused)
    marks the URLs the ACP mapper rewrites to ``host.docker.internal`` (the
    gateway mapping the workspace bridge already uses), which reaches only
    gateway-bound services, never arbitrary host loopback. stdio
    declarations carry no URL (their containment is the http_stdio consent
    gate)."""
    from .web_tools import vet_url
    from .cell_attach import is_container_loopback

    kept: list = []
    warnings: list = []
    for d in decls or []:
        if d.transport != "http":
            kept.append(d)
            continue
        reason = _mcp_url_problem(d.url)
        if reason is None and containerized and is_container_loopback(d.url):
            kept.append(d)              # becomes host.docker.internal
            continue
        if reason is None:
            reason, _pin = vet_url(d.url)
        if reason:
            warnings.append(f"mcp server {d.name!r} blocked: {reason}")
            continue
        kept.append(d)
    return kept, warnings


# Stop watchdog and turn dispatch remain the orchestration root.

_STOP_POLL_S = 0.25
_STOP_GRACE_S = 0.5


class TurnStopped(Exception):
    """Internal to run_agent: the stop watchdog cancelled the backend turn."""


def stopped_within(exc: BaseException) -> bool:
    """Whether ``exc`` is a :class:`TurnStopped`, or an exception group that
    carries one (a stop raised inside a library task group surfaces wrapped)."""
    if isinstance(exc, TurnStopped):
        return True
    return any(stopped_within(e) for e in getattr(exc, "exceptions", ()) or ())


async def _await_stoppable(coro, stop: Callable[[], bool]):
    """Await a backend turn while independently watching the stop signal.

    The streaming backends poll ``stop`` between model messages, but a turn
    parked inside a silent await — the CLI executing a tool, a framework
    opening its MCP session, a slow first token — never reaches that poll,
    which is exactly when a live-chat Stop press must still work. The turn
    runs as a task; the signal is polled on a timer; once it fires, the task
    gets ``_STOP_GRACE_S`` to finish on its own (the in-stream poll path,
    which returns a cleaner partial) and is then cancelled. Cancellation is
    safe teardown on every backend — each arm unwinds its own transport
    (the ACP arm additionally sends ``session/cancel`` from its own
    in-stream poll before this hard path is ever reached).

    Raises :class:`TurnStopped` when the turn was cancelled by the signal;
    any other outcome (result or exception) passes through unchanged.
    """
    body = asyncio.ensure_future(coro)
    stopped_at = None
    try:
        while True:
            done, _pending = await asyncio.wait({body}, timeout=_STOP_POLL_S)
            if done:
                return body.result()
            now = time.monotonic()
            if stopped_at is None:
                if stop():
                    stopped_at = now
            elif now - stopped_at >= _STOP_GRACE_S:
                break
    except BaseException:
        # The watchdog itself was cancelled or the stop callback raised —
        # take the turn task down with us so it can't outlive the run.
        body.cancel()
        try:
            await body
        except BaseException:  # noqa: BLE001 — teardown must not mask the cause
            pass
        raise
    body.cancel()
    try:
        await body
    except BaseException:  # noqa: BLE001 — a cancelled turn's unwind is expected
        me = asyncio.current_task()
        if me is not None and me.cancelling():
            raise  # the cancel is aimed at US, not the body: propagate it
    raise TurnStopped()


async def run_agent(
    *,
    prompt: str,
    backend: str = "pydantic-ai",
    auth: str = "api_key",
    provider: str = "anthropic",
    model: str = PROVIDER_DEFAULT_MODELS["anthropic"],
    system_prompt: str = "",
    enable_file_tools: bool = False,
    websearch: bool = True,
    attachments: Optional[list] = None,
    stream: bool = False,
    cell_mcp: str = "off",
    # Permission-request policy on the ACP path: "auto" answers from the
    # grant (allow what it already permits, refuse the rest); "ask" surfaces
    # each request as a card the human answers over the surface WebSocket
    # (shared/docs/agent-runtime.md, grants and tools).
    approvals: str = "auto",
    # The effective commands level (deny | ask | allow — parse_agent_config's
    # ``commands``): whether the agent may run shell commands it chooses.
    # The harness's own mechanism holds it, and the ACP client gate answers
    # execute requests per the level. Default deny: a direct caller that says
    # nothing gets no shell.
    commands: str = "deny",
    # The chat thread this turn continues (its own ACP session).
    session_key: str = "",
    runtime: str = "docker", host_harness: "dict | None" = None,
    # The Annotation surface's annotate/list_annotations rows (the run's
    # replies to the user's messages), offered on every backend whatever
    # ``enable_file_tools`` says.
    annotation_tools: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> AgentResult:
    """Run one agent turn with the selected backend. See module docstring.

    ``should_stop`` is polled by the streaming backends between chunks AND by
    an independent watchdog (:func:`_await_stoppable`) that covers the silent
    stretches the in-stream polls can't reach — a tool executing, a framework
    opening its session, a slow first token. Either way, when it fires the
    turn halts and returns the text accumulated so far (a live chat interrupt
    that actually stops generation, not just delta forwarding). The run-level
    graceful stop (``gen.cancelled``, set by SIGTERM) is folded into the same
    signal, so every mode — chat turn, oneshot, one-shot — aborts
    an in-flight model turn promptly on Stop.

    Sampling, thinking, output caps and the tool-loop bound are the model's
    and the harness's own defaults; a streaming turn emits
    ``{"type": "thinking_delta"}`` events where the backend streams thinking,
    and every backend records it in the trace as ``{"type": "thinking"}``.
    Skills from the runner's ``_skills`` folder are delivered wherever the
    backend's spec says it takes them (``BackendSpec.cell_skills``).

    ``websearch`` gates the web_fetch / web_search tools per backend
    (each harness's own config seam, and the bridge rows where a harness has
    no seam).

    ``attachments`` are multimodal INPUTS to the model — a list of
    ``{"data": bytes, "media_type": str, "name": str}`` (an image, PDF, audio or
    video clip): images go as inline prompt content blocks where the agent's
    prompt capabilities allow, other media as files staged into the
    workspace."""
    from .workspace_tools import safe_snapshot, restage_exec_writes
    raw_emit = on_event or (lambda ev: None)
    # Accumulated stream deltas — the partial reply when the stop watchdog
    # has to cancel the turn task (a cancelled backend never returns one).
    _partial: dict = {"text": [], "thinking": []}

    def emit(ev: dict) -> None:
        t = ev.get("type")
        if t == "text_delta" and ev.get("text"):
            _partial["text"].append(ev["text"])
        elif t == "thinking_delta" and ev.get("text"):
            _partial["thinking"].append(ev["text"])
        elif t == "text_reset":
            # The turn is being re-sent: the dead attempt's deltas are void.
            _partial["text"].clear()
            _partial["thinking"].clear()
        raw_emit(ev)

    caller_stop = should_stop

    def _stop_now() -> bool:
        """The caller's live-chat interrupt OR the run-level graceful stop
        (SIGTERM sets ``gen.cancelled``) — one signal, every backend, both
        the in-stream polls and the watchdog."""
        try:
            if caller_stop is not None and caller_stop():
                return True
        except Exception:  # noqa: BLE001 — a broken callback must not wedge the turn
            pass
        try:
            return bool(gen.cancelled)
        except Exception:  # noqa: BLE001 — bridge-free context (unit tests, tools)
            return False

    should_stop = _stop_now
    sys_prompt = (system_prompt or "").strip()
    # The model has no clock: anything it timestamps (a status comment,
    # a log line, a dated filename) is otherwise an invented date that renders
    # as "1d ago" beside the user's real ones. One line, refreshed per turn.
    _clock = clock_line()
    sys_prompt = (sys_prompt + "\n\n" + _clock).strip() if sys_prompt else _clock
    canonical = (backend or "pydantic-ai").strip().lower()
    spec = backend_spec(canonical)   # unknown backend → hard error

    # Trusted turn callers still cannot bypass the startup runtime grant.
    grant = gen.grant()
    from collimate.harness_placement import runtime_refusal
    refusal = runtime_refusal(canonical, replace(grant, commands=commands), runtime,
                              str((host_harness or {}).get("path") or ""))
    if refusal:
        raise RuntimeError(refusal)
    if cell_mcp == "http_stdio" and not (grant.docker == "full" and spec.containerized and runtime == "docker"):
        raise RuntimeError("mcp: http_stdio needs docker: full and a containerized harness")
    websearch = websearch and grant.internet != "none"

    # Cell-carried capabilities: discovered from the runner's _skills / _mcp
    # config folders (static for the run). Skills surface uniformly as an index
    # appended to the system prompt, path-rooted at the staged SKILL.md files
    # every harness reads with its own file tools. MCP declarations thread to
    # the backend, which attaches them to its own MCP client/config
    # (per-harness). Discovery is best-effort — a bridge-free/unit context (or
    # a runner providing no folders) yields empty.
    cell_skills = spec.cell_skills
    cell_caps = _EMPTY_CAPS
    if cell_skills or cell_mcp != "off":
        cell_caps = discover_cell_caps()
    if cell_skills and cell_caps.skills:
        # The /workspace mount in a container; the host workspace dir on a
        # host locus — the same relative path either way.
        from .cell_attach import container_skills_root
        idx = _cc_index(cell_caps.skills, root=container_skills_root())
        if idx:
            sys_prompt = (sys_prompt + "\n\n" + idx).strip() if sys_prompt else idx
    # The loopback exemption follows where THIS leg runs: the ACP mapper
    # rewrites loopback only for a harness in its container, and a host twin
    # (docker: none) would reach the host's own loopback.
    mcp_decls, _mcp_blocked = await asyncio.to_thread(
        filter_mcp_decls,
        _cell_mcp_decls(cell_caps, cell_mcp),
        containerized=run_needs_container(canonical, "chat", runtime),
    )
    if mcp_decls:
        # Resolve every ${account:role.secret_name} ref to a LITERAL once, awaited here —
        # the per-harness mappers are sync and can't await the async
        # account_secret (that would stringify a coroutine into the credential).
        from . import cell_attach
        mcp_decls = await cell_attach.resolve_decl_secrets(mcp_decls, account_secret)
    for w in cell_caps.warnings + _mcp_blocked:
        _secret_log(f"cell-caps: {w}")
    if cell_caps.skills or mcp_decls:
        _secret_log(
            f"cell-caps: {len(cell_caps.skills)} skill(s), "
            f"{len(mcp_decls)} mcp server(s) armed"
        )
    # The turn's skills, threaded to the tool registries.
    turn_skills = list(cell_caps.skills) if cell_skills else []

    async def _call_backend():
        # Every harness speaks ACP: the adapter runs as a host subprocess
        # (pydantic-ai, claude) or an attached docker exec (the container
        # harnesses); the session persists for the whole run (chat turns
        # reuse it — native history, no prompt re-serialization).
        from .acp_run import run_acp_turn
        return await run_acp_turn(
            backend=canonical, prompt=prompt, auth=auth, provider=provider,
            model=model, system_prompt=sys_prompt,
            mcp_decls=mcp_decls,
            stream=stream, emit=emit, should_stop=should_stop,
            enable_file_tools=enable_file_tools, websearch=websearch,
            attachments=attachments, turn_skills=turn_skills,
            approvals=approvals, commands=commands,
            # Chat threads are independent ACP sessions (see run_acp_turn).
            session_key=session_key,
            runtime=runtime, host_harness=host_harness,
            annotation_tools=annotation_tools,
        )

    # Snapshot the workspace around the turn: several harnesses write files
    # DIRECTLY (the containerized CLIs edit the bind-mounted workspace;
    # claude-code's native Write/Edit tools hit the cwd) rather than through
    # the staging file tools, and outside the interactive surfaces' live
    # mirror those writes would never reach the cell. The same snapshot-diff
    # restage the exec tools use makes every turn's direct writes durable;
    # tool-staged writes re-stage idempotently (same content, no extra
    # commit), so this needs no per-backend gate.
    ws_before = await asyncio.to_thread(safe_snapshot)
    try:
        res = await _await_stoppable(_call_backend(), _stop_now)
    except TurnStopped:
        # The watchdog cancelled the turn mid-await (typically inside a
        # tool call, where the backend's own between-message poll can't
        # run). Contract unchanged: return the text accumulated so far.
        text = "".join(_partial["text"])
        trace = [trace_event("user", text=prompt)]
        if _partial["thinking"]:
            trace.append(trace_event(
                "thinking", text="".join(_partial["thinking"]),
            ))
        if text:
            trace.append(trace_event("text", text=text))
        trace.append(trace_event(
            "system_note", text="turn interrupted by user",
        ))
        res = (text, trace, {})
    ws_after = (await asyncio.to_thread(safe_snapshot)) if ws_before is not None else None
    if ws_before is not None and ws_after is not None:
        for note in restage_exec_writes(ws_before, ws_after):
            _secret_log(f"[workspace] {note}")

    text, trace, usage = res[0], res[1], res[2]
    return AgentResult(text=text or "", backend=canonical, trace=trace, usage=usage)


async def serve_tty_until(*, on_action=None, poll: float = 0.5,
                          terminal_id: str = "") -> str:
    """Park a terminal leg until the run stops or — given the PTY's
    ``terminal_id`` — the terminal's PROGRAM exits (a CLI that quit, a
    crashed TUI): the run would otherwise sit on a dead terminal. Returns
    ``"stopped"`` / ``"exited"``.

    ``on_action`` maps a surface-scoped action id (e.g. the claude runner's
    ``capture_chat`` toolbar button) to an async zero-arg handler. Each id is
    consumed with a host-side filter (``wait_for_action_event(action_id=…)``)
    so the leg never steals an action another waiter owns."""
    while not gen.cancelled:
        if terminal_id:
            try:
                status = await asyncio.to_thread(gen.terminal_status, terminal_id)
            except Exception:  # noqa: BLE001 — a probe failure never ends the leg
                status = {}
            if status.get("ended"):
                code = status.get("exit_code")
                _secret_log(f"terminal program ended (exit {code}) — leaving the "
                        "terminal leg")
                return "exited"
        if on_action:
            for action_id, handler in on_action.items():
                ev = await gen.wait_for_action_event(action_id=action_id,
                                                     timeout=poll)
                if ev is not None:
                    try:
                        await handler()
                    except Exception as exc:  # noqa: BLE001 — leg must survive
                        _secret_log(f"{action_id} failed: {exc}")
        else:
            await asyncio.sleep(poll)
    return "stopped"


def surface_runner(backend: str, ui: str):
    """The runner for a backend's interactive ``ui`` surface (``terminal``)
    — an async ``runner(cfg, *, pointer, extra_system="",
    resume_harness_session_id="", first_message="", on_ready=None,
    label=...)``, one signature for every runner: ``pointer`` is
    the ``.terminal`` pointer path the runner serves its TTY at (inside the
    run folder — ``gen.serve_terminal`` writes it and deletes it when the TTY
    ends), ``resume_harness_session_id`` is the minted
    session the TTY should run on (``""``: none), ``first_message`` its
    opening turn (ignored by a TUI that takes none), and
    ``on_ready(harness_session_id)`` fires once the TTY is registered with
    the minted id it actually runs on (``""`` when it runs on none: a fresh
    conversation, including a harness whose TTY names its own —
    ``BackendSpec.harness_session_ids``); a runner that listed its
    store before a fresh TTY started passes those ids as ``before=``, so the
    live tail follows only the session that appears after them. ``cfg``
    is the parsed config: ``cfg["commands"]`` is the effective commands level
    (deny | ask | allow) the runner maps onto its harness's own TTY flags.
    ``assistant-ui`` has no dedicated surface runner (the engine drives the
    turn itself). Raises for a surface the backend doesn't offer
    (parse_agent_config already rejects that selection)."""
    from .harnesses import HARNESS
    target = HARNESS[backend].terminal
    if ui != "terminal" or not target:
        raise ValueError(f"backend {backend!r} has no {ui!r} surface")
    from .terminal_runner import run_terminal
    return run_terminal


def new_tty_session(before, now) -> str:
    """The session a TTY that names its own wrote (``BackendSpec.
    harness_session_ids``, or a fresh opencode/goose TTY): the ONE id in ``now`` —
    the store's session ids once the TTY has run — that ``before`` — its ids
    when the leg started — lacks. ``""`` when none appeared, and when
    several did (a subagent's own conversation, a second one started in the
    TTY): which of them the TTY ended on is unknowable, so none is taken. An
    id the store already held is never the leg's, whatever its file times
    say."""
    new = frozenset(now) - frozenset(before)
    return next(iter(new)) if len(new) == 1 else ""


def reset_run_state() -> None:
    """Forget the run's cell capabilities pinned at the first turn (a host
    that runs several generators in one process must not hand one run's to
    the next)."""
    _RUN_CELL_CAPS.clear()
