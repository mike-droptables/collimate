"""Live inbox WebSocket routes backed by sessions.chat.

The run-owned session service allocates the run's one inbox session: its
``.chat`` pointer, or a transport-only path in its run folder. The returned
session token allows a renderer to attach over WebSocket. Registrations
belong to a run; re-registering never changes a sibling run. The inbox's
commands are drained through the generic chat_poll IPC op.

A broadcast inbox (transport-only, for approval cards in the status pane's
drilldowns) fans out to attached sockets and replays a bounded backlog. A
single-attach inbox retains the current turn's text, tools and state for a
reconnecting renderer. Both preserve pending requests and config options.
Stream events come from the generator; the host stores and relays their
declared fields without choosing a generator's file layout.

Permission answers, config changes and interrupts go to the run's inbox, as
do the other commands. Sessions survive renderer detach and close when the
owning run ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)
from websocket_errors import reject_websocket
from sessions import chat as session_service

router = APIRouter()

# Cap the per-session outbox so a generator that streams into a detached
# renderer can't grow memory without bound. The canonical reply always lands
# in the thread's transcript file, so dropping the oldest buffered deltas
# is cosmetic.

# A defensive cap on how many attachments one message may carry — the bytes
# already went through the 50MB blob cap; this just bounds the ref list.
_MAX_ATTACHMENTS = 32


def _permission_answer_command(payload: dict) -> Optional[dict]:
    """The chat command for an approval card's answer frame, or ``None`` when
    the frame is in neither of the wire's two shapes: ``{id, option_id}`` (a
    non-empty option id, no ``cancelled``) or ``{id, cancelled: true}`` (no
    ``option_id``)."""
    rid = payload.get("id")
    if not isinstance(rid, str) or not rid:
        return None
    option_id = payload.get("option_id")
    if "cancelled" not in payload and isinstance(option_id, str) and option_id:
        return {"kind": "permission_answer", "id": rid, "option_id": option_id}
    if payload.get("cancelled") is True and "option_id" not in payload:
        return {"kind": "permission_answer", "id": rid, "cancelled": True}
    return None


def _clean_attachments(raw) -> list:
    """Normalize inbound attachment refs to ``[{path, name, media_type}]``.

    The renderer sends cell-relative references (the bytes are staged as cell
    drafts separately). Drop anything that isn't a dict with a non-empty string
    ``path``, coerce fields to str, and cap the count — a hostile frame can't
    smuggle non-string data or an unbounded list into the turn command.
    """
    if not isinstance(raw, list):
        return []
    out: list = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path", "") or "")
        if not path:
            continue
        out.append({
            "path": path,
            "name": str(a.get("name", "") or path.rsplit("/", 1)[-1]),
            "media_type": str(a.get("media_type", "") or ""),
        })
        if len(out) >= _MAX_ATTACHMENTS:
            break
    return out


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------

def _to_frame(turn_id: str, event: dict) -> dict:
    """Translate a generator stream event to a renderer wire frame. Turn-scoped
    frames carry the turn's bound ``thread_id`` when the generator provides it
    (``thinking_delta`` / ``busy`` ride the generic passthrough)."""
    etype = event.get("type", "")
    if etype == "text_delta":
        frame = {"t": "delta", "turn_id": turn_id, "text": event.get("text", "")}
    elif etype == "turn_start":
        frame = {"t": "turn_start", "turn_id": turn_id}
    elif etype == "turn_end":
        frame = {"t": "turn_end", "turn_id": turn_id}
    elif etype == "tool":
        return {"t": "tool", "turn_id": turn_id,
                **{k: v for k, v in event.items() if k != "type"}}
    elif etype == "error":
        frame = {"t": "error", "turn_id": turn_id, "message": event.get("message", "")}
    else:
        # Pass anything else through under its own type name.
        return {"t": etype or "event", "turn_id": turn_id,
                **{k: v for k, v in event.items() if k != "type"}}
    tid = event.get("thread_id")
    if tid:
        frame["thread_id"] = tid
    return frame


@router.websocket("/api/chats/{chat_id}")
async def chat_ws(
    websocket: WebSocket,
    chat_id: str,
    token: str = Query(default=""),
):
    session = session_service.get_chat_session(chat_id)
    if session is None or session.closed:
        await reject_websocket(websocket, 4404, "no such session")
        return
    if not token or not secrets.compare_digest(token, session.token):
        await reject_websocket(websocket, 4401, "bad token")
        return
    if session.attached and not session.broadcast:
        # A single-attach inbox keeps the 4409 rule; a broadcast inbox takes
        # N sockets so two open drilldowns don't ping-pong the session.
        await reject_websocket(websocket, 4409, "session already attached")
        return

    async def _pump_to_ws(queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            if item is None:  # session closed
                return
            try:
                await websocket.send_text(json.dumps(
                    _to_frame(item.get("turn_id", ""), item.get("event", {}))
                ))
            except Exception:
                return

    async def _pump_from_ws() -> None:
        while True:
            try:
                msg = await websocket.receive()
            except WebSocketDisconnect:
                return
            except Exception:
                return
            if msg.get("type") == "websocket.disconnect":
                return
            text = msg.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            t = payload.get("t")
            if t == "user_msg":
                # thread_id rides along for the generator's turn scoping.
                # attachments are cell-relative file refs {path,name,media_type}
                # the user attached; the bytes are already staged as cell
                # drafts and get flushed into the workspace on chat_poll.
                cmd = {"kind": "user_msg", "content": payload.get("content", ""),
                       "thread_id": payload.get("thread_id", ""),
                       "attachments": _clean_attachments(payload.get("attachments"))}
            elif t == "permission_answer":
                # An approval card's answer (gen.ask) rides the inbox WS
                # (never the action queue) and reaches the waiting gen.ask
                # through serve_chat's command dispatch, in the wire's shape.
                # A frame in any other shape is a renderer bug: it is dropped
                # (the card stays open) rather than read as a refusal.
                cmd = _permission_answer_command(payload)
                if cmd is None:
                    logger.warning("dropped a malformed permission_answer frame "
                                   "on %s: %r", session.surface, payload)
                    continue
            elif t == "config_set":
                # A declared session configuration change follows the same
                # answer route as a permission answer.
                cmd = {"kind": "config_set",
                       "config_id": str(payload.get("config_id", "") or ""),
                       "value": payload.get("value")}
            elif t == "interrupt":
                # Stop the in-flight turn. serve_chat
                # cancels the turn the frame names by ``turn_id`` (or by its
                # ``thread_id``) and ignores one that names neither.
                cmd = {"kind": "interrupt",
                       "turn_id": str(payload.get("turn_id", "") or ""),
                       "thread_id": str(payload.get("thread_id", "") or "")}
            else:
                await websocket.send_json({
                    "t": "refused", "op": t,
                    "thread_id": payload.get("thread_id", ""),
                    "message": "Unsupported chat command.",
                })
                continue
            # Answers route to the run's inbox; other commands arrive only on
            # the inbox itself.
            if cmd["kind"] in ("permission_answer", "config_set", "interrupt"):
                if session.answer_route != "inbox":
                    continue
                target = session_service.get_inbox(session.run_id)
                if target is None:
                    continue
            else:
                if not session.inbox:
                    logger.debug("dropped a %s frame on non-inbox session %s",
                                 cmd["kind"], session.surface)
                    continue
                target = session
            target.commands.put_nowait(cmd)

    # Claim a single-attach session BEFORE the first await, so a second
    # upgrade racing this one sees it attached and gets 4409. The finally
    # below releases the claim on every exit, an accept() that raises
    # included.
    if not session.broadcast:
        session.attached = True
    my_queue: Optional[asyncio.Queue] = None
    try:
        await websocket.accept()
        if session.closed:
            # Unregistered while the upgrade was accepted: its close sentinel
            # already went out, so a pump started now would wait forever.
            await websocket.close(code=4404, reason="no such session")
            return

        if session.broadcast:
            # Each broadcast subscriber gets its own live queue; the bounded
            # backlog (accumulated even while detached) replays first so a
            # card emitted with no tab open still lands. The snapshot and the
            # subscriber-append happen back-to-back with NO await between
            # them: a push landing during the (awaited) replay sends then sits
            # in my_queue and flows after the replay — neither lost (it missed
            # the snapshot) nor duplicated (it postdates it).
            my_queue = asyncio.Queue(maxsize=session_service.OUTBOX_MAX)
            replay = list(session.backlog)
            session.subscribers.append(my_queue)
            session.attached = True
        else:
            # Drop any leftover buffered events from a prior attach; the
            # current turn's partial replays from the in_progress_* slots
            # below. The session is already claimed, so a push landing during
            # the (awaited) history send sits in the outbox and flows after
            # it: the snapshot below is taken with no await since this drain.
            my_queue = session.outbox
            _drain(session.outbox)
            replay = []

        # Replay the current turn's partial so a reload re-materializes it
        # before going live — whenever a turn is IN FLIGHT, even with no text
        # yet (a tool-phase turn), otherwise the renderer shows an idle
        # composer while the generator is mid-turn. ``pending_requests``
        # rides the same frame: an unanswered permission card must survive a
        # reload (the generator is blocked on it).
        if (session.in_progress_turn_id or session.pending_requests
                or session.config_options):
            history = json.dumps({
                "t": "history",
                "turn_id": session.in_progress_turn_id,
                "thread_id": session.in_progress_thread_id,
                "text": session.in_progress_text,
                "thinking": session.in_progress_thinking,
                "tools": session.in_progress_tools,
                "pending_requests": session.pending_requests,
                "config_options": session.config_options,
            })
            try:
                await websocket.send_text(history)
            except Exception:
                return
        for item in replay:
            try:
                await websocket.send_text(json.dumps(
                    _to_frame(item.get("turn_id", ""), item.get("event", {}))
                ))
            except Exception:
                return

        to_task = asyncio.create_task(_pump_to_ws(my_queue))
        from_task = asyncio.create_task(_pump_from_ws())
        _done, pending = await asyncio.wait(
            {to_task, from_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        if session.broadcast:
            if my_queue is not None:
                try:
                    session.subscribers.remove(my_queue)
                except ValueError:
                    pass
            session.attached = bool(session.subscribers)
        else:
            session.attached = False
            _drain(session.outbox)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass


def _drain(q: asyncio.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass
