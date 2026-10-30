"""Run-owned chat sessions, independent of websocket routes."""
from __future__ import annotations
import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from collections import deque
from typing import Optional

OUTBOX_MAX = 2048

@dataclass
class ChatSession:
    chat_id: str
    run_id: str
    cell_id: str
    token: str
    # The path this session serves (the run's ``.chat`` pointer, or the
    # transport-only inbox path in its run folder). Registering the same path
    # again evicts only its predecessor.
    surface: str
    # The run's inbox: the queue chat_poll drains, and the target of its
    # answer frames. Every registration is the run's one inbox.
    inbox: bool = False
    # Broadcast sessions are the run's transport-only inbox, which several
    # viewers (status-pane drilldown tabs) may attach for approval cards.
    broadcast: bool = False
    answer_route: str = ""
    # Inbound renderer commands, drained by chat_poll.
    commands: asyncio.Queue = field(default_factory=asyncio.Queue)
    outbox: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=OUTBOX_MAX))
    # Broadcast sessions: one live queue per attached socket (fan-out), plus a
    # bounded backlog accumulated REGARDLESS of attachment — a card emitted
    # while no viewer is attached replays to the next attach (single-attach
    # sessions replay from the in_progress_* slots + the durable transcript
    # instead).
    subscribers: list = field(default_factory=list)
    backlog: deque = field(default_factory=lambda: deque(maxlen=OUTBOX_MAX))
    # Pending agent-side requests awaiting a HUMAN answer (permission cards —
    # ask). Part of the reconnect replay: a request
    # raised while the renderer was detached must not be lost while the
    # generator blocks on it. Keyed by request id; answered/expired entries
    # are removed by their resolution event.
    pending_requests: list = field(default_factory=list)
    # The session's declared configuration options. Wholesale-replaced
    # by each ``config_option`` stream event and replayed in the history
    # frame, so a (re)attaching renderer shows the switcher without waiting
    # for the next turn's re-emit.
    config_options: list = field(default_factory=list)
    # Accumulated assistant text/thinking of the in-flight turn, replayed to a
    # (re)attaching renderer so a reload mid-turn re-materializes. Single slot
    # — concurrent turns would need a per-turn dict.
    in_progress_text: str = ""
    in_progress_thinking: str = ""
    in_progress_turn_id: str = ""
    in_progress_thread_id: str = ""
    in_progress_tools: list = field(default_factory=list)
    attached: bool = False
    closed: bool = False

_sessions: dict[str, ChatSession] = {}

class InboxExists(Exception):
    """A run registered a second inbox at another path."""

def register_surface_session(run_id: str, cell_id: str, surface: str, *,
                             broadcast: bool, answer_route: str, inbox: bool = False) -> dict:
    """Allocate a live session for a path of a run.

    ONE session per ``(run_id, surface)`` — registering a path again
    evicts only its own predecessor (a broadcast inbox the same run
    re-registers keeps its live session). ``inbox`` marks the run's inbox:
    a run has at most one, so a second inbox at another path raises
    :class:`InboxExists`. The ``broadcast`` flag permits multiple
    attached sockets; the rest keep the single-attach 4409 rule.
    ``answer_route='inbox'`` sends answers and interrupts to the run's inbox;
    an empty route declines them. Returns
    ``{chat_id, ws_path, token}``.
    """
    if (not isinstance(broadcast, bool) or not isinstance(inbox, bool)
            or not isinstance(answer_route, str) or answer_route not in ("", "inbox")):
        raise ValueError("invalid_surface_declaration")
    if not isinstance(surface, str) or not surface:
        raise ValueError("a surface needs its pointer path")
    mine = [(sid, s) for sid, s in list(_sessions.items())
            if s.run_id == run_id and not s.closed]
    if inbox:
        other = next((s for _sid, s in mine if s.inbox and s.surface != surface), None)
        if other is not None:
            raise InboxExists(f"run {run_id} already has an inbox at {other.surface}")
    for sid, s in mine:
        if s.surface != surface:
            continue
        if broadcast and s.broadcast and s.inbox == inbox and s.answer_route == answer_route:
            # The SAME run re-registering a broadcast inbox keeps its live
            # session: minting a fresh id/token would drop every attached
            # viewer, losing what an attached viewer holds.
            return {
                "chat_id": sid,
                "ws_path": f"/api/chats/{sid}",
                "token": s.token,
            }
        unregister_chat_session(sid)
    chat_id = f"chat_{uuid.uuid4().hex[:16]}"
    token = secrets.token_urlsafe(24)
    _sessions[chat_id] = ChatSession(
        chat_id=chat_id,
        run_id=run_id,
        cell_id=cell_id,
        token=token,
        surface=surface,
        inbox=inbox,
        broadcast=broadcast,
        answer_route=answer_route,
    )
    return {
        "chat_id": chat_id,
        "ws_path": f"/api/chats/{chat_id}",
        "token": token,
    }

def get_inbox(run_id: str) -> Optional[ChatSession]:
    """The run's inbox session, or None when it registered none."""
    for s in _sessions.values():
        if s.run_id == run_id and s.inbox and not s.closed:
            return s
    return None

def push_event(run_id: str, turn_id: str, event: dict) -> None:
    """Route a generator-emitted stream event to the run's inbox (never
    another run's, even on the same cell). Tracks the in-flight turn for
    reconnect replay and the pending permission requests, and buffers per
    the inbox's own policy. Called from the ``stream_event`` IPC op; a run
    with no inbox drops the event.
    """
    inbox = get_inbox(run_id)
    if inbox is not None:
        _push_event_to(inbox, turn_id, event)

def _push_event_to(session: ChatSession, turn_id: str, event: dict) -> None:
    if session.closed:
        return
    etype = event.get("type")
    # The session tracks WHICH turn is in flight, so the history frame
    # tells a (re)attaching renderer that a turn is running and which
    # ``turn_id`` its Stop names, and accumulates the turn's
    # text/thinking/tools so the replay re-materializes the partial bubble.
    if etype == "turn_start":
        session.in_progress_text = ""
        session.in_progress_thinking = ""
        session.in_progress_tools = []
        session.in_progress_turn_id = turn_id
        session.in_progress_thread_id = event.get("thread_id", "") or ""
    elif etype == "turn_end":
        session.in_progress_text = ""
        session.in_progress_thinking = ""
        session.in_progress_tools = []
        session.in_progress_turn_id = ""
        session.in_progress_thread_id = ""
    elif etype == "text_delta":
        session.in_progress_text += event.get("text", "") or ""
    elif etype == "thinking_delta":
        session.in_progress_thinking += event.get("text", "") or ""
    elif etype == "tool":
        _upsert_tool(session, event)
    # Pending generator requests join the replay state: a request raised
    # while no renderer was attached must still reach the next attach —
    # the generator is blocked on the answer. Resolution events clear them.
    if etype == "permission_request" and event.get("id"):
        session.pending_requests = [
            r for r in session.pending_requests if r.get("id") != event["id"]
        ] + [{k: v for k, v in event.items() if k != "type"}]
    elif etype == "permission_resolved" and event.get("id"):
        session.pending_requests = [
            r for r in session.pending_requests if r.get("id") != event["id"]
        ]
    elif etype == "config_option":
        # The session's config options — wire semantics are wholesale
        # replace (ConfigOptionUpdate carries the FULL set).
        opts = (event.get("update") or {}).get("configOptions")
        if isinstance(opts, list):
            session.config_options = [o for o in opts if isinstance(o, dict)]
    elif etype == "current_mode":
        # A mode change reported by the generator — patch the mode option's
        # currentValue so the replayed switcher agrees with the session.
        mode_id = str(event.get("modeId") or "")
        if mode_id:
            session.config_options = [
                {**o, "currentValue": mode_id}
                if o.get("category") == "mode" else o
                for o in session.config_options
            ]
    item = {"turn_id": turn_id, "event": event}
    if session.broadcast:
        # Buffer regardless of attachment (bounded), then fan out live.
        session.backlog.append(item)
        for q in list(session.subscribers):
            _queue_put(q, item)
    elif session.attached:
        # Single-attach sessions only buffer while someone is listening —
        # otherwise the live deltas are stale on reattach (the partial is
        # replayed from ``in_progress_text``, completed turns from the
        # transcript).
        _outbox_put(session, item)

_TOOL_EXTRA_KEYS = ("kind", "locations", "content")


def _upsert_tool(session: ChatSession, event: dict) -> None:
    tid = event.get("id", "") or ""
    status = event.get("status")
    name = event.get("name", "") or ""
    inp = event.get("input", "") or ""
    res = event.get("result", "") or ""
    if tid:
        for entry in session.in_progress_tools:
            if entry.get("id") == tid:
                if status:
                    entry["status"] = status
                if name:  # never blank an existing name with an empty one
                    entry["name"] = name
                if inp:
                    entry["input"] = inp
                if res:
                    entry["result"] = res
                for k in _TOOL_EXTRA_KEYS:
                    if event.get(k):
                        entry[k] = event[k]
                return
    if len(session.in_progress_tools) >= 200:
        return  # cap appends; id-matched patches above always apply
    entry = {"id": tid, "name": name, "status": status or "running"}
    if inp:
        entry["input"] = inp
    if res:
        entry["result"] = res
    for k in _TOOL_EXTRA_KEYS:
        if event.get(k):
            entry[k] = event[k]
    session.in_progress_tools.append(entry)

def _outbox_put(session: ChatSession, item) -> None:
    _queue_put(session.outbox, item)

def _queue_put(q: asyncio.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()  # drop oldest
            q.put_nowait(item)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass

def pop_command(run_id: str) -> Optional[dict]:
    """Non-blocking pop of the next inbound renderer command on the run's
    inbox (``{kind, content?}``). Called from the ``chat_poll`` IPC op."""
    session = get_inbox(run_id)
    if session is None:
        return None
    try:
        return session.commands.get_nowait()
    except asyncio.QueueEmpty:
        return None

def unregister_chat_session(chat_id: str) -> None:
    session = _sessions.pop(chat_id, None)
    if session is None:
        return
    session.closed = True
    from served_surfaces import release_chat
    release_chat(chat_id)
    # Wake every blocked pump so the sockets close promptly.
    _outbox_put(session, None)
    for q in list(session.subscribers):
        _queue_put(q, None)

def unregister_sessions_for_run(run_id: str) -> None:
    """Drop every session of a run. Called when the run ends (and eagerly by
    a force stop) — the cell's other runs keep theirs."""
    for sid, s in list(_sessions.items()):
        if s.run_id == run_id:
            unregister_chat_session(sid)


def get_chat_session(session_id: str) -> Optional[ChatSession]:
    """Look up the registered session for a websocket attach."""
    return _sessions.get(session_id)
