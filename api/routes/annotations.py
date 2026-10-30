"""Annotation routes: a cell's message contexts (its own, and one per live
run on it), the writes users make to them, a run's file touches and the
transport-only inbox a run serves its approval cards on.

The store is ``annotations``; every write runs in one ``BEGIN IMMEDIATE``
transaction while the channel is marked busy, refuses with
``{error, detail}`` (``annotations.AnnotationRefused``) and, once committed,
rings one ``annotations_changed`` doorbell per touched context. A request
the models refuse (an unknown field, a bound) answers the same way, as
``invalid`` (``_AnnotationRoute``).
"""
from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

import annotations
from annotations import AnnotationRefused, Bell
from channel_db import open_channel_db


def _invalid(exc: RequestValidationError) -> AnnotationRefused:
    """The store's ``invalid`` refusal for a request the models refused,
    its sentence naming the first field at fault."""
    errors = exc.errors()
    if not errors:
        return AnnotationRefused("invalid")
    first = errors[0]
    where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg") or "is not valid").rstrip(".")
    return AnnotationRefused("invalid", f"{where}: {message}." if where else f"{message}.")


class _AnnotationRoute(APIRoute):
    """Every refusal of these routes is ``{error, detail}``: a request the
    models refuse answers as the store's ``invalid``, not FastAPI's
    ``{detail: [...]}``."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def answer(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return _refused(_invalid(exc))

        return answer


router = APIRouter(route_class=_AnnotationRoute)


class ContextItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    renderer_id: str = Field(min_length=1, max_length=annotations.RENDERER_ID_MAX)
    label: str = Field(max_length=annotations.ITEM_LABEL_MAX)
    text: str = Field(max_length=annotations.ITEM_TEXT_MAX)
    anchor: dict | None = None


class PostAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=256)
    context_id: str = Field(min_length=1, max_length=64)
    parent_id: str | None = None
    body: str
    context_items: list[ContextItemIn] = Field(default_factory=list, max_length=annotations.CONTEXT_ITEMS_MAX)
    mode: Literal["queued", "interrupt"]
    held: bool = False


class PatchAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    body: str | None = None
    context_items: list[ContextItemIn] | None = Field(None, max_length=annotations.CONTEXT_ITEMS_MAX)
    mode: Literal["queued", "interrupt"] | None = None


class SendAnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)


class OrderAnnotationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(max_length=annotations.CONTEXT_MAX_MESSAGES)
    held_from: int | None = Field(None, ge=0)


class AnnotationBarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: bool


class InterruptAnnotationsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1, max_length=annotations.CONTEXT_MAX_MESSAGES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _on_canvas(channel: str, cell_id: str) -> str:
    """The channel's UUID; 404 unless the cell is on its canvas (409 while
    the channel is being deleted)."""
    from cell_placement import find_cell
    from state import require_channel, state_lock
    ch = require_channel(channel)
    with state_lock:
        if find_cell(ch, cell_id)[1] is None:
            raise HTTPException(404, "The cell is not on the canvas")
    return str(ch.id)


def _open_runs(ch: str, cell_id: str) -> set[str]:
    """The runs on the cell whose context takes writes: registered and not
    ending, or accepted and still spawning without a Stop."""
    from database import canonical_channel_id
    from routes.runs.create import starting_runs
    from run_registry import active_managers
    open_ids = {run_id for run_id, m in list(active_managers.items())
                if canonical_channel_id(m.channel or "") == ch and m.cell_id == cell_id
                and not (m.sealed or m.stop_requested or m.final_status)}
    open_ids.update(run_id for run_id, s in starting_runs()
                    if canonical_channel_id(s.channel or "") == ch and s.cell_id == cell_id
                    and not s.stop_requested)
    return open_ids


def _listed_runs(ch: str, cell_id: str) -> list[str]:
    """The runs on the cell that are live, starting or stopping, in start
    order: the contexts the cell's listing carries."""
    from database import canonical_channel_id
    from routes.runs.queries import live_and_starting
    return [run_id for run_id, m in live_and_starting()
            if canonical_channel_id(m.channel or "") == ch and m.cell_id == cell_id]


def _ring(ch: str, bells: list[Bell]) -> None:
    from collimate._host.channel_events import AnnotationsChanged
    from events import broadcast
    for bell in bells:
        broadcast(ch, AnnotationsChanged(bell.cell_id, bell.context_id, bell.rev).to_dict())


async def _read(ch: str, fn):
    def work():
        conn = open_channel_db(ch)
        try:
            conn.execute("BEGIN")
            return fn(conn)
        finally:
            conn.rollback()
            conn.close()
    return await asyncio.to_thread(work)


def _refused(exc: AnnotationRefused) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.message})


async def _write(ch: str, fn):
    """Run ``fn(conn) -> (result, bells)`` in one write transaction while
    the channel is marked busy; ring the bells after commit."""
    from run_registry import channel_busy

    def work():
        conn = open_channel_db(ch)
        try:
            conn.execute("BEGIN IMMEDIATE")
            out = fn(conn)
            conn.commit()
            return out
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    with channel_busy(ch):
        try:
            result, bells = await asyncio.to_thread(work)
        except AnnotationRefused as exc:
            return _refused(exc)
    _ring(ch, bells)
    return result


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/cells/{cell_id}/annotations")
async def list_cell_annotations(channel: str, cell_id: str):
    """The cell's annotation contexts: its own (``id: 'cell'``, empty until
    its first write), then each live, starting or stopping run's on it, in
    start order."""
    ch = _on_canvas(channel, cell_id)
    runs = _listed_runs(ch, cell_id)
    return {"contexts": await _read(ch, lambda c: annotations.list_contexts(c, cell_id, runs))}


@router.get("/api/channels/{channel}/cells/{cell_id}/annotations/{context_id}")
async def get_annotation_context(channel: str, cell_id: str, context_id: str):
    """One annotation context of the cell in any state, sealed included."""
    ch = _on_canvas(channel, cell_id)
    context = await _read(ch, lambda c: annotations.get_context(c, cell_id, context_id))
    if context is None:
        raise HTTPException(404, "No such annotation context")
    return context


@router.post("/api/channels/{channel}/cells/{cell_id}/annotations")
async def post_annotation(channel: str, cell_id: str, body: PostAnnotationRequest):
    """A user message (top-level, or a reply to a top-level message) in the
    cell's context or a live run's. The same ``operation_id`` replays the
    stored answer."""
    ch = _on_canvas(channel, cell_id)
    request = body.model_dump()
    digest = annotations.request_digest({"cell_id": cell_id, **request})
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.post(
        c, cell_id, context_id=body.context_id, body=body.body, context_items=request["context_items"],
        mode=body.mode, held=body.held, parent_id=body.parent_id, operation_id=body.operation_id,
        digest=digest, live=live))


@router.patch("/api/channels/{channel}/cells/{cell_id}/annotations/{annotation_id}")
async def patch_annotation(channel: str, cell_id: str, annotation_id: str, body: PatchAnnotationRequest):
    """Edit an unread message's body, context items or mode."""
    ch = _on_canvas(channel, cell_id)
    request = body.model_dump()
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.patch(
        c, cell_id, annotation_id, expected_revision=body.expected_revision, body=body.body,
        context_items=request["context_items"], mode=body.mode, live=live))


@router.delete("/api/channels/{channel}/cells/{cell_id}/annotations/{annotation_id}")
async def delete_annotation(channel: str, cell_id: str, annotation_id: str,
                            expected_revision: int = Query(ge=1)):
    """Delete an unread message (a root takes its unread replies)."""
    ch = _on_canvas(channel, cell_id)
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: ({"ok": True}, annotations.delete(
        c, cell_id, annotation_id, expected_revision=expected_revision, live=live)))


@router.post("/api/channels/{channel}/cells/{cell_id}/annotations/{annotation_id}/send")
async def send_annotation(channel: str, cell_id: str, annotation_id: str, body: SendAnnotationRequest):
    """Move a message of the cell's context, with its replies, into a live
    run's context: last queued, or first when it is an interrupt."""
    ch = _on_canvas(channel, cell_id)
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.send(c, cell_id, annotation_id, run_id=body.run_id, live=live))


@router.put("/api/channels/{channel}/cells/{cell_id}/annotations/{context_id}/order")
async def order_annotations(channel: str, cell_id: str, context_id: str, body: OrderAnnotationsRequest):
    """Reorder the context's unread top-level messages (``ids`` names them
    all); those from ``held_from`` on go below the queue bar."""
    ch = _on_canvas(channel, cell_id)
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.reorder(
        c, cell_id, context_id, ids=body.ids, held_from=body.held_from, live=live))


@router.put("/api/channels/{channel}/cells/{cell_id}/annotations/{context_id}/bar")
async def set_annotation_bar(channel: str, cell_id: str, context_id: str, body: AnnotationBarRequest):
    """Turn the context's queue bar on or off (off releases every held
    message)."""
    ch = _on_canvas(channel, cell_id)
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.set_bar(c, cell_id, context_id, on=body.on, live=live))


@router.post("/api/channels/{channel}/cells/{cell_id}/annotations/{context_id}/interrupt")
async def interrupt_annotations(channel: str, cell_id: str, context_id: str, body: InterruptAnnotationsRequest):
    """Make unread top-level messages interrupts, moved to the head; the
    context's interrupt counter steps once."""
    ch = _on_canvas(channel, cell_id)
    live = _open_runs(ch, cell_id)
    return await _write(ch, lambda c: annotations.interrupt(c, cell_id, context_id, ids=body.ids, live=live))


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@router.get("/api/channels/{channel}/runs/{run_id}/file_touches")
async def run_file_touches(channel: str, run_id: str):
    """Every file the run's own writes created, edited or deleted (folders
    included), oldest first: ``{touches: [{at, path, op, folder,
    version_id}]}``."""
    from state import require_channel
    ch = str(require_channel(channel).id)

    def touches(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            return None
        out = []
        for row in conn.execute("SELECT payload, created_at FROM run_events "
                                "WHERE run_id = ? AND event_type = 'commit' ORDER BY id", (run_id,)):
            payload = json.loads(row["payload"])
            for change in payload.get("changes", []):
                out.append({"at": row["created_at"], "path": change["path"], "op": change["op"],
                            "folder": bool(change["folder"]), "version_id": payload["cell_id"]})
        return out

    found = await _read(ch, touches)
    if found is None:
        raise HTTPException(404, "Run not found")
    return {"touches": found}


@router.get("/api/channels/{channel}/runs/{run_id}/inbox")
async def run_inbox(channel: str, run_id: str):
    """The coordinates of a live run's broadcast inbox, which several
    viewers attach to answer its approval cards; 404 for any other run
    (a single-attach chat inbox included)."""
    from database import canonical_channel_id
    from run_registry import active_managers
    from sessions.chat import get_inbox
    from state import require_channel
    ch = str(require_channel(channel).id)
    manager = active_managers.get(run_id)
    if manager is None or canonical_channel_id(manager.channel or "") != ch:
        raise HTTPException(404, "No live run")
    inbox = get_inbox(run_id)
    if inbox is None or not inbox.broadcast:
        raise HTTPException(404, "The run has no inbox to attach")
    return {"chat_id": inbox.chat_id, "ws_path": f"/api/chats/{inbox.chat_id}", "token": inbox.token}
