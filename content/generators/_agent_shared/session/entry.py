# SPDX-License-Identifier: MIT
"""One agent run on the surface its config picks.

The Agent cell's generator runs one surface per run, its ``surface``
setting: ``annotation`` reads the user's messages in the run's annotation
context and answers in their threads (interactive, or a one-shot whose
prompt is its first message), ``chat`` is a conversation, ``terminal``
(labelled CLI) the harness's own TTY. The run's surface files live in its
_runs/<label>/ folder (an annotation run has none); work output stays in
shared cell paths. A run ends when the user stops it, a terminal's program
exits, or a one-shot ends itself. With ``selected_file_first`` the file
selected when the run started is part of its first turn (``selected``).
Credentials come from the bound accounts. Commands follow the runner's
grant on every backend.
"""
from __future__ import annotations

import asyncio
import json

import gen

from _agent_shared.logging import safe_log as _secret_log
from _agent_shared.secret_hygiene import write_log as _write_log
from _agent_shared.run_files import AGENT_LIVE_SKIP_GLOBS
from _agent_shared import (
    TRACE_FILE,
    TOOL_INPUT_CAP,
    TOOL_RESULT_CAP,
    tool_summary,
    explain_turn_failure,
    TurnRefused,
    run_agent,
)

from . import annotation, run_end
from .consts import (
    EMPTY_REPLY,
    RUN_END_TASK_WAIT_S,
    SessionPaths,
    Sources,
    annotation_directive,
    identity_directive,
)
from .media import _workspace_media_inputs, _read_turn_attachments
from .selected import read_selected
from .state import AgentSession

# The cell label a run sends, by its surface, with the backend
# ("annotation · pydantic-ai"). A terminal run's runner names the cell when
# its TTY opens.
_CELL_LABEL = {"annotation": "annotation", "chat": "chat"}

# The turns an annotation run runs: its batches, a one-shot's opening and the
# host-refusal corrections. A message sent to its inbox directly is not one.
_ANNOTATION_SOURCES = (Sources.ANNOTATIONS, Sources.OPENING, Sources.CORRECTION)


def _log_surface(cfg: dict) -> None:
    """Log the backend, model, workspace tools and command policy."""
    _secret_log(
        f"backend={cfg['backend']} model={cfg['model']} "
        f"tools={'F' if cfg['enable_file_tools'] else ''} "
        f"commands={cfg['commands']}"
    )


# ──────────────────────────────────────────────────────────────────────
# the session — engine runs inline per turn
# ──────────────────────────────────────────────────────────────────────


# Soft cap on the per-turn tool-activity accumulation — the transcript's own
# cleaner caps the persisted record at 50; this only stops a runaway heartbeat
# producer from growing the in-memory list without bound.
_MAX_TOOL_EVENTS = 200


# The tool-event payload keys that survive into the accumulated activity list
# (and therefore the transcript + reconnect replay). ``kind`` / ``locations``
# / ``content`` are the ACP normalizer's typed extras — ToolKind, file
# locations, and structured diff/terminal blocks — widened here with the
# normalizer so a reload doesn't strip them from the display record.
_TOOL_EVENT_KEYS = ("input", "result", "kind", "locations", "content")


def _upsert_tool(acc: list, ev: dict) -> None:
    """Fold one engine tool event into the turn's accumulated activity list.

    Events pair by ``id`` — a completion (``done``/``error``) patches its
    running entry's status/result in place; an event with no id (a coarse
    heartbeat) just appends. Non-empty fields win; an existing name is never
    blanked by a nameless completion."""
    tid = ev.get("id") or ""
    if tid:
        for t in acc:
            if t.get("id") == tid:
                if ev.get("status"):
                    t["status"] = ev["status"]
                if ev.get("name") and not t.get("name"):
                    t["name"] = ev["name"]
                for k in _TOOL_EVENT_KEYS:
                    if ev.get(k):
                        t[k] = ev[k]
                return
    if len(acc) >= _MAX_TOOL_EVENTS:
        return
    entry: dict = {
        "id": tid,
        "name": ev.get("name") or "",
        "status": ev.get("status") or "running",
    }
    for k in _TOOL_EVENT_KEYS:
        if ev.get(k):
            entry[k] = ev[k]
    acc.append(entry)


def _tools_from_trace(acc: list, trace: list) -> None:
    """Fold a turn's trace ``tool_use``/``tool_result`` events into the
    display accumulation — for backends that record tools only in the trace
    (the end-parsed CLI turns), so the transcript shows their
    tool activity too. FIFO-by-name pairing: a result attaches to the
    EARLIEST call of the same tool that is still waiting for one, else to the
    earliest waiting call of any name, else it becomes an entry of its own —
    so calls a model issued in parallel keep their own results. Everything is
    ``done`` (the turn is over) and bounded like the live events."""
    for ev in trace or []:
        et = ev.get("type")
        if et == "tool_use":
            if len(acc) >= _MAX_TOOL_EVENTS:
                return
            entry: dict = {
                "id": "", "name": str(ev.get("tool") or ""), "status": "done",
            }
            inp = tool_summary(ev.get("input"), TOOL_INPUT_CAP)
            if inp:
                entry["input"] = inp
            acc.append(entry)
        elif et == "tool_result":
            out = tool_summary(ev.get("output"), TOOL_RESULT_CAP)
            if not out:
                continue
            name = str(ev.get("tool") or "")
            waiting = [t for t in acc if "result" not in t]
            target = next((t for t in waiting if name and t.get("name") == name), None)
            if target is None and waiting:
                target = waiting[0]
            if target is not None:
                target["result"] = out
            elif len(acc) < _MAX_TOOL_EVENTS:
                acc.append({"id": "", "name": name, "status": "done", "result": out})


async def run_session(cfg: dict) -> None:
    _log_surface(cfg)

    # The run's surface files: its own surface is the file the manifest's
    # opens names (cfg["opens"]), the one the host created when it accepted
    # the run.
    paths = SessionPaths.for_run(cfg["backend"], cfg=cfg)
    st = AgentSession(cfg, paths=paths)
    # A one-shot's prompt is its whole task, its first message: none, no run.
    if st.oneshot and not (gen.prompt or "").strip():
        raise ValueError("a one-shot run needs a prompt: it is the run's first message")
    # The selected file as part of the first turn: a chat's own turn before
    # its prompt, an annotation run's first turn (a one-shot's opening, an
    # interactive run's first batch), or the TTY's first message. A media
    # file rides as the input of the turn that carries it.
    st.selected = read_selected(cfg)
    if st.selected is not None and st.selected.media:
        st.selected_source = {
            "chat": Sources.SELECTED_FILE,
            "annotation": Sources.OPENING if st.oneshot else Sources.ANNOTATIONS,
        }.get(st.surface)
    trace_acc = st.trace

    async def on_turn(turn: gen.ChatTurn) -> None:
        # The turn boundary runs here, whatever the body did: a body that
        # raised (a flush, the trace write) still settles an annotation batch
        # or a one-shot pass — as a failed turn — rather than leaving it
        # waiting on a boundary that never comes. The run's own teardown
        # (Stop) skips it: the run end handles what is left. ``reached``
        # holds the turn's own (stopped, failed) once its reply is recorded.
        reached: dict = {}
        try:
            outcome = await _on_turn_body(turn, reached)
        except (asyncio.CancelledError, gen.Cancelled):
            raise
        except gen.PartialRefusal as exc:
            # A flush the host partly refused (the rest landed): the refused
            # paths get their corrections. A turn refused before its reply
            # did no work, so it ends failed and its batch stays open.
            st.corrections.refused_exc(exc)
            stopped, failed = reached.get("outcome") or (
                bool(turn.cancelled), f"the host refused a write before the turn ran: {exc}")
            await st.on_turn_end(turn, stopped=stopped, failed=failed)
            return
        except Exception as exc:
            _secret_log(f"turn failed outside the engine: {exc}")
            await st.on_turn_end(turn, stopped=bool(turn.cancelled),
                                 failed=f"turn failed: {exc}")
            raise
        if outcome is not None:
            stopped, failed = outcome
            await st.on_turn_end(turn, stopped=stopped, failed=failed)

    # The cell label this run last set — re-sent only when it changes, so a
    # rename the user makes mid-session survives.
    auto_name = [""]

    async def _on_turn_body(turn: gen.ChatTurn, reached: dict) -> "tuple | None":
        """One turn; returns ``(stopped, failed)`` for its boundary, or None
        when it was no turn at all (a turn skipped, an empty batch). The
        outcome is in ``reached`` as soon as the reply is recorded, for a
        flush after it that raises."""
        source = getattr(turn, "source", "")
        # The service refused an earlier turn (AgentSession.end_refused), or
        # the run asked to finish: a turn still queued is not sent.
        if st.refused or st.finish_requested:
            return None
        # A terminal run's TTY is its conversation: the inbox runs no engine
        # turn.
        if st.surface == "terminal":
            return None
        if st.surface == "annotation" and source not in _ANNOTATION_SOURCES:
            _secret_log("annotation run: ignored a direct message")
            return None
        prompt = turn.user_msg
        batch_media: list = []
        if source in (Sources.ANNOTATIONS, Sources.OPENING):
            if turn.cancelled:
                # Cut before it pulled anything: an opening is a stopped
                # one-shot; a batch leaves its messages unread for the next.
                if source == Sources.OPENING:
                    return True, ""
                annotation.cancelled_before_pull(st)
                return None
            batch = await annotation.begin_batch(st, opening=source == Sources.OPENING)
            if batch is None:
                return None
            prompt, batch_media = batch.text, batch.media

        # The turn that carries the selected file takes its media input, once.
        carries_selected = (st.selected_source is not None
                            and source == st.selected_source)
        if carries_selected:
            st.selected_source = None
        # The user's message + any files they attached (shown inline / as chips
        # by the renderer; the bytes are already in the workspace).
        turn.append("user", prompt,
                    attachments=list(turn.attachments or [])
                    + (st.selected.refs if carries_selected else []))
        await gen.commit()  # surface the user bubble immediately

        # The engine holds the conversation natively — the persistent ACP
        # session, one per chat thread (session_key) — so each turn sends ONLY
        # the new user message.
        session_key = str(getattr(turn, "thread_id", "") or "main")
        attachment_inputs = _read_turn_attachments(turn)
        if carries_selected:
            attachment_inputs = attachment_inputs + st.selected.media
        attachment_inputs = attachment_inputs + batch_media
        if source == Sources.OPENING:
            # The opening turn is the whole one-shot pass, so the workspace's
            # media rides as model input the way a chat turn's attachments do
            # — the cell holds these inputs and expects the model to see
            # them.
            carried = {a.get("path") for a in attachment_inputs}
            media = [a for a in _workspace_media_inputs() if a["path"] not in carried]
            if media:
                _secret_log(f"attachments: {len(media)} workspace media file(s)")
                attachment_inputs = attachment_inputs + media
        chunks: list[str] = []
        think_chunks: list[str] = []
        tool_acc: list = []

        # Set when a non-text event (a tool call, a thought section) lands
        # between text deltas: the raw stream carries no separator across
        # that boundary, so the joined reply would glue the sentences
        # around it ("…wrote the report.The tests pass…"). The next
        # text delta then opens a fresh paragraph — in the durable record
        # AND the live stream (turn.delta), so the bubble matches.
        text_break = [False]

        def emit(ev: dict) -> None:
            # Per-type dispatch: the delta types still require text, but a tool
            # event carries none — a blanket text guard would swallow every
            # tool event before the socket (and the transcript record).
            if turn.cancelled:
                return
            et = ev.get("type")
            if et == "text_delta" and ev.get("text"):
                if text_break[0]:
                    text_break[0] = False
                    # A single trailing newline is a SOFT break in markdown
                    # (it renders as a space), so only an existing blank
                    # line counts — judged over the joined tail, not the
                    # last chunk alone (a break can split across deltas).
                    # Top up to exactly one blank line.
                    tail = "".join(chunks[-3:])
                    if chunks and not tail.endswith("\n\n"):
                        sep = "\n" if tail.endswith("\n") else "\n\n"
                        chunks.append(sep)
                        turn.delta(sep)
                chunks.append(ev["text"])
                turn.delta(ev["text"])
            elif et == "text_reset":
                # What streamed so far is no reply: the engine is re-sending
                # the turn after its transport died (the retry's reply must
                # not be glued onto the dead attempt's), or it was the
                # service's refusal (TurnRefused follows). The live bubble is
                # reconciled from the durable transcript at turn_end.
                chunks.clear()
                think_chunks.clear()
                text_break[0] = False
            elif et == "thinking_delta" and ev.get("text"):
                text_break[0] = True
                think_chunks.append(ev["text"])
                turn.thinking_delta(ev["text"])
            elif et == "tool" and ev.get("name"):
                text_break[0] = True
                extra = {k: v for k, v in ev.items()
                         if k not in ("type", "name", "status", "thread_id")}
                turn.tool(ev["name"], ev.get("status") or "running", **extra)
                _upsert_tool(tool_acc, ev)
            elif et == "tool" and ev.get("id"):
                text_break[0] = True
                # A nameless completion (some backends only carry the pairing
                # id there) — it patches the running entry, so forward it too.
                completion = {k: v for k, v in ev.items()
                              if k not in ("type", "name", "status", "thread_id")}
                turn.tool("", ev.get("status") or "done", **completion)
                _upsert_tool(tool_acc, ev)
            elif et in ("plan", "context", "session_info",
                        "available_commands", "current_mode", "config_option"):
                # The ACP normalizer's typed events: the plan feed, the
                # context meter (window occupancy + cumulative cost — NOT
                # per-turn tokens), and the session-state passthroughs.
                # Ephemeral, straight to the surface socket — the renderer
                # decides what to show.
                turn.emit(ev)

        # The runner's own system prompt, plus what the run adds: which run
        # it is among its cell's, and on the annotation surface how it works
        # there (and how a one-shot ends).
        sys_prompt = (cfg["system_prompt"] + "\n\n" + identity_directive(paths)).strip()
        if st.surface == "annotation":
            sys_prompt += "\n\n" + annotation_directive(oneshot=st.oneshot)

        reply = ""
        thinking = ""
        turn_trace: list | None = None
        result = None
        # A turn counts as stopped on EITHER signal: the chat interrupt
        # (turn.cancelled) or a whole-run graceful stop (gen.cancelled —
        # run_agent's watchdog returns a partial instead of raising, so
        # without this the truncated reply would persist as a complete,
        # unmarked assistant message). Read once so a flip mid-finalization
        # can't split the decisions below.
        stopped = False
        # The explained error of a turn whose run_agent raised ("" when it
        # completed): the turn boundary must know the turn did no work — a
        # correction would fail the same way, and a one-shot must not report
        # the error text as its answer.
        failed = ""
        try:
            result = await run_agent(
                prompt=prompt, stream=True, on_event=emit,
                should_stop=lambda: turn.cancelled,
                session_key=session_key,
                backend=cfg["backend"], auth=cfg["auth"], provider=cfg["provider"],
                model=cfg["model"],
                system_prompt=sys_prompt,
                enable_file_tools=cfg["enable_file_tools"],
                websearch=cfg["websearch"],
                cell_mcp=cfg["mcp"],
                approvals=cfg["approvals"],
                commands=cfg["commands"],
                annotation_tools=st.surface == "annotation",
                attachments=attachment_inputs,
                runtime=cfg["runtime"], host_harness=cfg["host_harness"],
            )
            # The engine's user events carry the whole folded history prompt —
            # accumulate a clean per-turn record instead: this turn's actual
            # user message + everything the model did with it.
            turn_trace = [{"type": "user", "text": prompt}] + [
                ev for ev in result.trace if ev.get("type") != "user"
            ]
            # Once interrupted, keep only what actually streamed — never resurrect
            # the full background answer for a turn the user stopped.
            stopped = turn.cancelled or gen.cancelled
            if stopped:
                reply = "".join(chunks).strip()
                thinking = "".join(think_chunks).strip()
            else:
                reply = "".join(chunks).strip() or (result.text or "").strip()
                # The trace holds the authoritative full thinking; streamed
                # chunks are the fallback.
                # One trace event per contiguous thought BLOCK (the ACP
                # client coalesces streamed chunks), joined as paragraphs —
                # each block trimmed so a chunk boundary's stray newlines
                # never become blank lines inside the record.
                blocks = [
                    (ev.get("text") or "").strip() for ev in result.trace
                    if ev.get("type") == "thinking"
                ]
                thinking = ("\n\n".join(b for b in blocks if b)
                            or "".join(think_chunks).strip())
        except gen.Cancelled:
            if gen.cancelled:      # real run teardown — let serve_chat unwind
                raise
            # A per-turn cancel that is not the run's own teardown (a
            # settings request the turn was waiting
            # on). Pair the turn instead of leaving a dangling user bubble.
            stopped = turn.cancelled
            reply = "".join(chunks).strip() or "_(turn cancelled)_"
            if not stopped:
                failed = "the turn was cancelled before it completed"
            thinking = "".join(think_chunks).strip()
            # A cancelled turn must not vanish from the trace record either —
            # keep the user message, whatever streamed, and the cancel itself.
            # The note says "interrupted" so the dedupe below reads it as THE
            # interrupt note rather than stacking a second one on a turn the
            # user also stopped.
            turn_trace = [{"type": "user", "text": prompt}]
            if chunks:
                turn_trace.append({"type": "text", "text": "".join(chunks)})
            turn_trace.append({
                "type": "system_note", "text": "turn interrupted before completion",
            })
        except gen.PartialRefusal as e:
            # A flush inside the turn the host partly refused: the refused
            # paths get their corrections, and the turn has no answer. Every
            # other PermissionError is a failed turn (below).
            stopped = turn.cancelled or gen.cancelled
            queued = False if stopped else st.corrections.refused_exc(e)
            reply = ("_(write refused; a correction turn follows)_" if queued
                     else "_(write refused)_")
            thinking = "".join(think_chunks).strip()
            turn_trace = [{"type": "user", "text": prompt}]
        except Exception as e:  # noqa: BLE001
            stopped = turn.cancelled
            # The raw error, plus the real fix where its own advice misleads
            # (a Claude Code version floor names `claude update` even when the
            # turn ran the CLI bundled in Collimate's adapter).
            internet = gen.grant().internet
            failure = explain_turn_failure(
                e, backend=cfg["backend"], runtime=cfg["runtime"],
                cli_path=(cfg.get("host_harness") or {}).get("path"),
                internet=internet)
            reply = f"_(error: {failure})_"
            thinking = "".join(think_chunks).strip()
            if not stopped:
                failed = failure
                # The run log names the real cause (not only the transcript);
                # an annotation run's batch gets a note in its thread
                # (annotation.after_turn).
                _secret_log(f"turn failed: {failure}")
                if isinstance(e, TurnRefused):
                    st.refused = failure
            # A failed turn must not vanish from the trace record — keep the
            # user message, whatever streamed, and the failure itself.
            turn_trace = [{"type": "user", "text": prompt}]
            if chunks:
                turn_trace.append({"type": "text", "text": "".join(chunks)})
            turn_trace.append({"type": "system_note", "text": f"turn failed: {failure}"})

        # Files the model returned (e.g. a generated image) — write them into
        # the cell and reference them on the assistant message so the renderer
        # shows them (images inline, others as chips). Skipped on a stopped
        # turn (a partial reply shouldn't drop half-generated files in).
        if stopped and not reply:
            reply = "_(stopped by user)_"
        # Backends with no live tool events (the end-parsed CLI turns) still
        # record tool_use/tool_result in the trace — fold those
        # in post-turn so every backend's transcript shows its tool activity,
        # not just the live emitters'. Live events win when both exist.
        if not tool_acc and result is not None and not stopped:
            _tools_from_trace(tool_acc, result.trace)
        # The turn's tool activity, id-stripped for the durable record (ids
        # only pair running/completion events on the wire). On a stopped turn
        # tool_acc already holds only what streamed before the interrupt —
        # the same keep-only-what-streamed rule the reply text follows.
        tools_out = [
            {k: v for k, v in t.items() if k != "id" and v}
            for t in tool_acc
        ]
        from _agent_shared.secret_hygiene import refresh, scrub, scrub_object
        refresh()
        content = scrub(reply or EMPTY_REPLY)
        turn.append("assistant", content,
                    thinking=scrub(thinking),
                    tools=scrub_object(tools_out), stopped=stopped)
        reached["outcome"] = (stopped, failed)
        # A completed turn: the engine's own reply is an annotation batch's
        # answer, and one ephemeral usage event follows (the renderer shows
        # it as a footer until the next reload — cost is run metadata, not
        # conversation content, so it never enters the transcript).
        if result is not None and not stopped:
            annotation.answered(st, content)
            picked = {
                k: v for k, v in (getattr(result, "usage", None) or {}).items()
                if k in ("input_tokens", "output_tokens", "total_tokens",
                         "total_cost_usd")
                and isinstance(v, (int, float))
            }
            if picked:
                turn.emit({"type": "usage", **picked})
        if turn_trace is not None:
            # run_agent's stop watchdog records the same note on the partial
            # trace it assembles — don't double it up in the trace record.
            if stopped and not any(
                ev.get("type") == "system_note" and "interrupted" in (ev.get("text") or "")
                for ev in turn_trace
            ):
                turn_trace.append({
                    "type": "system_note", "text": "turn interrupted by user",
                })
            trace_acc.extend(turn_trace)
            # Persist the whole accumulated trace every turn — it's the run's
            # raw record ("every mode records it") and a local write, so a
            # session that ends abruptly still leaves everything up to here.
            _write_log(TRACE_FILE, json.dumps(trace_acc, default=str), category="log")
        # Name the cell, so a session cell never sits in the stack as an
        # anonymous placeholder — sent only when the label changes: the host
        # takes a sent name over the current one, so re-sending it every turn
        # would undo a rename the user made mid-session. A terminal run's
        # runner named the cell when its TTY opened.
        label = (f"{_CELL_LABEL[st.surface]} · {cfg['backend']}"
                 if st.surface in _CELL_LABEL else "")
        if label and label != auto_name[0]:
            auto_name[0] = label
            await gen.commit(name=label)
        else:
            await gen.commit()

        # The turn boundary (the on_turn wrapper runs it): the corrections,
        # the annotation batch's settling and a one-shot's finish.
        return stopped, failed

    # The chat session is every run's command inbox and turn runner; only a
    # chat run shows it (``files``): its pointer and transcript — in the run
    # folder — are the chat surface. An annotation run's inbox is broadcast
    # (``broadcast``), so every viewer of the run's status pane gets its
    # approval cards; a terminal run's is transport-only. Neither writes a
    # chat file: the conversation lives in memory.
    #
    # live_files: the engine's file tools work on this run's workspace — the
    # mirror makes those writes appear in the cell's Files tab as they happen
    # and pushes the user's Files-tab edits back into the workspace mid-run
    # (the manifest's live_files_mode: manual keeps the tab editable). A
    # refused write queues its correction (annotation and chat); an outside
    # edit is noted for an annotation run's next batch.
    #
    # The run's prompt. A chat run's is an optional opening message: the
    # user's first turn, after the selected file. A one-shot's is its first
    # annotation message (the host seeded it; the opening turn presents it).
    # An interactive annotation run takes none (its schema hides the Prompt
    # field), and a terminal run's goes to its TTY's first message
    # (terminal.opening_message).
    prompt = (gen.prompt or "").strip()
    ready = asyncio.Event()

    def _on_ready() -> None:
        # The boot boundary. A config the run could not honour as chosen
        # (the selected Claude CLI, …) is said where the user is looking,
        # not only in a run-log line the header shows for a moment: a chat
        # run's transcript, an annotation run's context (a terminal has only
        # its TTY).
        for w in cfg.get("warnings") or []:
            st.say(f"Config: {w}", log=False)
        if st.surface == "chat" and st.selected is not None:
            gen.session.submit_turn(st.selected.message, source=Sources.SELECTED_FILE)
        if st.surface == "chat" and prompt:
            gen.session.submit_turn(prompt, source=Sources.COMPOSER)
        if st.oneshot:
            gen.session.submit_turn(Sources.OPENING, source=Sources.OPENING)
        ready.set()

    surface_task = asyncio.ensure_future(st.surface_loop(ready))
    config_task = asyncio.ensure_future(st.config_loop(ready))
    try:
        async with gen.live_files(
                skip_globs=AGENT_LIVE_SKIP_GLOBS,
                on_refused=(st.corrections.refused if st.surface != "terminal" else None),
                on_changed=((lambda path: annotation.changed(st, path))
                            if st.surface == "annotation" else None)) as mirror:
            st.flush_output = mirror.flush
            if st.surface == "annotation":
                # The leg is set up before the first turn can read messages.
                await annotation.open_annotations(st)
            await gen.serve_chat(
                on_turn, label="Agent",
                pointer_file=paths.chat_pointer, threads_dir=paths.chat_threads,
                files=st.surface == "chat",
                broadcast=st.surface == "annotation",
                on_ready=_on_ready,
            )
    finally:
        # The surface and config loops end inside ONE bound, so the run end
        # lands inside the host's stop grace. A Stop once a terminal run's
        # TTY is up lets the surface loop end BY ITSELF: the runner sees the
        # Stop within its poll and runs its teardown — the exporter backfill
        # against a container that still exists (session.run's close_all
        # comes right after this frame) — to the end; a cancel landing
        # inside that teardown would abandon it. Otherwise, and past the
        # bound, the loops are cancelled and awaited: a cancel that reaches
        # a runner before its teardown starts still lets the teardown run,
        # cut at the bound.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RUN_END_TASK_WAIT_S
        config_task.cancel()
        if gen.cancelled and st.tty_up:
            await asyncio.wait({surface_task}, timeout=RUN_END_TASK_WAIT_S)
        surface_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(surface_task, config_task,
                               return_exceptions=True),
                timeout=max(deadline - loop.time(), 0.1),
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        await st.close()
        # Run end: the surface files are the session's TOOLS — write them to
        # the run's LOG and clear them from the cell; what the agent produced
        # stays. A hard Stop is exactly this.
        await run_end.finish(st)
    # A run whose turn the model service refused, a one-shot whose opening
    # turn produced no answer, or a terminal that could not run, ends
    # FAILED — after the run end above has logged the session. Returning
    # normally would report success to the host and hand the run would
    # incorrectly appear successful.
    if st.refused:
        raise RuntimeError(st.refused)
    if st.oneshot_stopped:
        raise gen.Cancelled("the one-shot's turn was stopped by the user")
    if st.oneshot_error:
        raise RuntimeError(st.oneshot_error)
    if st.terminal_error:
        raise RuntimeError(st.terminal_error)
