# SPDX-License-Identifier: MIT
"""The Annotation surface: the run reads the user's messages in its
annotation context and answers in their threads.

An interactive run subscribes to its context (``gen.annotations``) and,
whenever it is idle and messages wait, submits one ``@annotations`` turn.
That turn pulls the batch at its start (:func:`begin_batch`) — so a queued
correction ahead of it never holds a stale batch — and renders each message
with what it points at: the context items the user picked in a file and
the cell files its body links (text inlined within a budget, media attached,
the rest named). Interrupt-mode messages lead the batch, then the messages
a cut turn left open, then the queued ones.

A newer interrupt — one above those the last pull answered — cancels the
running turn (``gen.session.interrupt_turn``), and its batch comes back first in the
next turn with a note that it was cut short; a stopped or failed turn keeps
its batch open the same way (a failure also posts a note in the thread). A
completed turn that posted nothing through the ``annotate`` tool has the
final answer it recorded posted under the batch's last thread. A pull that
fails leaves the messages unread, and the watcher tries again after a short
backoff. Files changed outside the run's turns become a note in the next
batch, never a turn of their own. After 15 idle minutes the heavy half of
the session (ACP adapter, container, bridge) is parked; the next batch
re-provisions it.

A ONE-SHOT never subscribes: its single batch is the host-seeded prompt
message (found with ``gen.annotations.list()``) plus whatever one pull finds
at its opening turn, and its reply goes under the prompt message."""
from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

import gen

from _agent_shared import annotation_tools
from _agent_shared.logging import safe_log as _secret_log

from .consts import EMPTY_REPLY, Sources
from .selected import read_file_content

# The watcher's rhythm when no notice wakes it (the idle-park clock).
POLL_S = 1.0
# After a failed pull the next batch waits this long, doubling per failure
# up to the cap.
PULL_RETRY_S = 1.0
PULL_RETRY_MAX_S = 30.0
# The idle clock parks the heavy half of an untouched session (ACP adapter,
# container, MCP bridge) while the subscription stays live; the next batch
# re-provisions through the same lazy session/load path every turn uses.
IDLE_PARK_S = 15 * 60
# What a batch inlines of the cell files its messages link.
REF_TEXT_FILE_BYTES = 32 * 1024
REF_TEXT_TOTAL_BYTES = 128 * 1024
MAX_REF_MEDIA = 8
# The most of one context item's text a batch carries.
CONTEXT_TEXT_CAP = 8 * 1024
# The most outside-edited paths one note names.
CHANGED_PATHS_MAX = 50
# The most of a final answer the run posts for a turn (the host takes 64 KiB).
POST_CAP_BYTES = 60 * 1024
# The placeholder user message of a batch turn; the turn pulls its real text.
BATCH_MARK = Sources.ANNOTATIONS


@dataclass
class Batch:
    """A rendered batch: the turn's prompt ``text`` and the engine ``media``
    inputs its linked files attach (``[{"data", "media_type", "name",
    "path"}]``)."""
    text: str
    media: list = field(default_factory=list)


@dataclass
class Flight:
    """The batch of the turn in flight: its ``messages``, the ``annotate``
    posts before it (``mark``), whether an interrupt cut it
    (``interrupted``) and the final answer the turn recorded (``answer``,
    ``None`` until the turn's engine replied)."""
    messages: list
    mark: int
    interrupted: bool = False
    answer: "str | None" = None


@dataclass
class RefBudget:
    """What is left of one batch's inline budget: text bytes, attached media,
    and the paths already shown."""
    text_left: int
    media_left: int
    seen: set = field(default_factory=set)


class AnnotationLeg:
    """The annotation surface's working memory for one run.

    ``prompt`` is a one-shot's prompt message (or a stand-in with id ``""``);
    ``flight`` the batch of the turn in flight (:class:`Flight`); ``open``
    the batch a cut turn left; ``cut_short`` why (for the next batch's
    note); ``resume`` whether the next batch starts at once (an interrupt
    cut it); ``changed`` the paths edited outside the run's turns;
    ``selected_pending`` whether the selected file still waits for its turn;
    ``pulled_seq`` the ``interrupt_seq`` the last pull answered (an
    interrupt at or below it is already in a batch); ``pull_failures`` /
    ``retry_at`` the failed pulls in a row and when the next batch may try
    again; ``sub`` / ``watch`` / ``wake`` the subscription, the watcher task
    and its doorbell; ``notes`` the posts of :func:`note` still in flight."""

    def __init__(self, *, oneshot: bool, prompt=None,
                 selected_pending: bool = False) -> None:
        self.oneshot = oneshot
        self.prompt = prompt
        self.flight: "Flight | None" = None
        self.open: list = []
        self.cut_short = ""
        self.resume = False
        self.changed: dict[str, float] = {}
        self.selected_pending = selected_pending
        self.pulled_seq = 0
        self.pull_failures = 0
        self.retry_at = 0.0
        self.last_activity = time.monotonic()
        self.parked = False
        self.sub = None
        self.watch: "asyncio.Task | None" = None
        self.wake = asyncio.Event()
        self.notes: set = set()


def _available() -> bool:
    try:
        return bool(gen.annotations.available)
    except Exception:  # noqa: BLE001 — no bridge: no context
        return False


# ── opening ─────────────────────────────────────────────────────────


async def open_annotations(sess) -> None:
    """Set up the run's annotation leg (entry calls it before
    ``serve_chat``). A one-shot finds its host-seeded prompt message; with
    none (no host, a host fault) a stand-in carries ``gen.prompt``."""
    prompt = None
    available = _available()
    if sess.oneshot:
        if available:
            try:
                prompt = next((m for m in await gen.annotations.list()
                               if m.kind == "prompt"), None)
            except (asyncio.CancelledError, gen.Cancelled):
                raise
            except Exception as exc:  # noqa: BLE001 — the stand-in carries it
                _secret_log(f"annotations: could not read the prompt message ({exc})")
        if prompt is None:
            prompt = SimpleNamespace(
                id="", text=(gen.prompt or "").strip(), author="user",
                author_label=None, kind="prompt", reply_to=None,
                mode="queued", created_at="", read_at=None, context=(),
                refs=())
    sess.annotations = AnnotationLeg(oneshot=sess.oneshot, prompt=prompt,
                                     selected_pending=sess.selected is not None)
    if not available:
        _secret_log("annotations: no host — the run reads no messages")


def start(sess) -> None:
    """An interactive run's subscription and watcher (``state.surface_loop``
    starts it once ``serve_chat`` has booted)."""
    leg = sess.annotations
    if leg is None or leg.oneshot or leg.watch is not None:
        return
    if _available():
        leg.sub = gen.annotations.subscribe(
            lambda notice: leg.wake.set(),
            on_interrupt=lambda notice: _on_interrupt(sess, notice))
    leg.watch = asyncio.ensure_future(watcher(sess))


def _on_interrupt(sess, notice) -> None:
    """A newer message interrupts: cut the running turn (any source); the
    batch in flight comes back first. An interrupt the last pull answered
    (its messages are in that batch) cuts nothing."""
    leg = sess.annotations
    if notice.interrupt_seq <= leg.pulled_seq:
        return
    if gen.session.interrupt_turn():
        if leg.flight is not None:
            leg.flight.interrupted = True
        _secret_log("a newer annotation interrupted the running turn")
    leg.wake.set()


async def watcher(sess) -> None:
    """Submit one batch turn whenever the run is idle and messages wait
    (after a failed pull, once its backoff has passed) or an interrupted
    batch must resume, and park an idle session."""
    leg = sess.annotations
    while True:
        try:
            try:
                await asyncio.wait_for(leg.wake.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass
            leg.wake.clear()
            if sess.finish_requested or sess.refused or gen.cancelled:
                continue
            if gen.session.chat_busy():
                continue
            waiting = (gen.annotations.notice.unread > 0
                       and time.monotonic() >= leg.retry_at)
            if waiting or leg.resume:
                leg.resume = False
                gen.session.submit_turn(BATCH_MARK, source=Sources.ANNOTATIONS)
                continue
            if not leg.parked and time.monotonic() - leg.last_activity >= IDLE_PARK_S:
                leg.parked = True
                from _agent_shared.acp_run import close_acp_sessions
                from _agent_shared.cli_container import close_all
                await close_acp_sessions()
                await asyncio.to_thread(close_all)
        except (asyncio.CancelledError, gen.Cancelled):
            raise
        except Exception as exc:  # noqa: BLE001 — the watcher must survive
            _secret_log(f"annotation feed: {exc}")
            # A fault that repeats must not spin the loop.
            await asyncio.sleep(POLL_S)


# ── a batch ─────────────────────────────────────────────────────────


def _units(messages: list) -> list:
    """``messages`` in the host's pull units: a root with its replies that
    follow it, or a reply alone (its root was read earlier)."""
    units: list = []
    for m in messages:
        lead = units[-1][0] if units else None
        if (lead is not None and lead.reply_to is None and m.reply_to
                and m.reply_to == lead.id):
            units[-1].append(m)
        else:
            units.append([m])
    return units


async def begin_batch(sess, *, opening: bool = False) -> "Batch | None":
    """Pull the waiting messages and build the batch turn's prompt; ``None``
    when there is nothing to present (no turn runs). ``opening``: a
    one-shot's opening turn, led by its prompt message."""
    leg = sess.annotations
    pulled: list = []
    if _available():
        try:
            pulled = list(await gen.annotations.pull())
        except (asyncio.CancelledError, gen.Cancelled):
            raise
        except Exception as exc:  # noqa: BLE001 — the messages stay unread
            leg.pull_failures += 1
            wait = min(PULL_RETRY_S * 2 ** (leg.pull_failures - 1), PULL_RETRY_MAX_S)
            leg.retry_at = time.monotonic() + wait
            _secret_log(f"annotations: the pull failed ({exc}); trying again in {wait:g} s")
        else:
            leg.pull_failures = 0
            leg.retry_at = 0.0
            leg.pulled_seq = gen.annotations.pull_notice.interrupt_seq
    units = _units(pulled)
    first = [leg.prompt] if opening and leg.prompt is not None else []
    order = (first
             + [m for u in units if u[0].mode == "interrupt" for m in u]
             + leg.open
             + [m for u in units if u[0].mode != "interrupt" for m in u])
    leg.open = []
    batch: list = []
    ids: set = set()
    for m in order:
        mid = getattr(m, "id", "") or ""
        if mid and mid in ids:
            continue
        ids.add(mid)
        batch.append(m)
    if not batch:
        return None
    leg.flight = Flight(batch, annotation_tools.mark())
    annotation_tools.note_messages(batch)
    cut_short, leg.cut_short = leg.cut_short, ""
    changed = sorted(leg.changed)
    leg.changed.clear()
    more = max(len(changed) - CHANGED_PATHS_MAX, 0)
    selected = None
    if leg.selected_pending:
        leg.selected_pending = False
        selected = sess.selected
    return render_batch(batch, opening=opening, oneshot=leg.oneshot,
                        cut_short=cut_short, changed=changed[:CHANGED_PATHS_MAX],
                        more_changed=more, selected=selected,
                        added=bool(pulled) and opening)


def _attr(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _author(m) -> str:
    if getattr(m, "author", "user") == "run":
        return f"run {getattr(m, 'author_label', '') or ''}".strip()
    return "user"


def _ref_block(path: str, budget: RefBudget) -> tuple[str, list]:
    """One linked cell file as the batch carries it: inlined text, an
    attached media file, or its name with why it is not shown."""
    def named(what: str, extra: str = "") -> str:
        return f'<file path="{_attr(path)}"{extra}>{what}</file>'

    if path in budget.seen:
        return named("(shown above)"), []
    budget.seen.add(path)
    if path.startswith(("_runs/", ".collrun/")):
        return named("a run's interface file; read it with your file tools"), []
    try:
        content = read_file_content(
            path, text_cap=max(min(REF_TEXT_FILE_BYTES, budget.text_left), 1))
    except Exception as exc:  # noqa: BLE001 — an unreadable ref is named
        return named(f"could not be read ({exc})"), []
    if content.kind == "missing":
        return named("not in the workspace"), []
    if content.kind == "binary":
        return named(f"a binary file of {content.size} bytes; read it with your file tools"), []
    if content.kind == "media":
        media_attr = f' media="{_attr(content.media_type)}"'
        if content.media is None:
            return named(f"{content.size} bytes — too large to attach; read it with your "
                         "file tools", media_attr), []
        if budget.media_left <= 0:
            return named("attached files are capped; read it with your file tools",
                         media_attr), []
        budget.media_left -= 1
        return named("attached", media_attr), [content.media]
    if budget.text_left <= 0:
        return named("not inlined: this batch's inline budget is spent; read it with your "
                     "file tools"), []
    budget.text_left -= len(content.text.encode("utf-8"))
    tail = (f"\n[Truncated: the first {len(content.text.encode('utf-8'))} of {content.size} "
            "bytes. Read the file for the rest.]" if content.truncated else "")
    return f'<file path="{_attr(path)}">\n{content.text}{tail}\n</file>', []


def render_message(m, budget: RefBudget) -> tuple[str, list]:
    """One message as a ``<message>`` block, with its context items and
    linked files; returns the block and the media its files attach."""
    attrs = []
    mid = getattr(m, "id", "") or ""
    if mid:
        attrs.append(f'id="{_attr(mid)}"')
        attrs.append(f'thread="{_attr(getattr(m, "reply_to", None) or mid)}"')
    attrs.append(f'author="{_attr(_author(m))}"')
    attrs.append(f'mode="{_attr(getattr(m, "mode", "queued") or "queued")}"')
    if getattr(m, "created_at", ""):
        attrs.append(f'at="{_attr(m.created_at)}"')
    parts = [f"<message {' '.join(attrs)}>", (getattr(m, "text", "") or "").strip()]
    for item in getattr(m, "context", ()) or ():
        text = item.text or ""
        raw = text.encode("utf-8")
        if len(raw) > CONTEXT_TEXT_CAP:
            text = raw[:CONTEXT_TEXT_CAP].decode("utf-8", "ignore") + "\n[truncated]"
        parts.append(f'<context path="{_attr(item.path)}" label="{_attr(item.label)}" '
                     f'renderer="{_attr(item.renderer_id)}">\n{text}\n</context>')
    media: list = []
    for path in getattr(m, "refs", ()) or ():
        block, attached = _ref_block(path, budget)
        parts.append(block)
        media += attached
    parts.append("</message>")
    return "\n".join(p for p in parts if p), media


def render_batch(batch: list, *, opening: bool, oneshot: bool, cut_short: str,
                 changed: list, more_changed: int, selected=None,
                 added: bool = False) -> Batch:
    """The batch turn's prompt: a header, the notes that apply (a cut turn,
    outside edits, the selected file), one block per message and how to
    answer. ``added``: an opening that also pulled messages."""
    lines: list = []
    if opening:
        lines.append("@annotations — the run's opening message (one-shot: it is the whole task).")
    else:
        lines.append(f"@annotations — {len(batch)} message(s) for you.")
    if cut_short:
        lines.append(f"Your previous turn was {cut_short} before it finished. The messages it "
                     "was answering are presented again below; check any file it may have "
                     "half-written.")
    if changed:
        names = ", ".join(changed) + (f", and {more_changed} more" if more_changed else "")
        lines.append("Files changed outside your turns since your last turn (the user or "
                     f"another run): {names}. Re-read the ones you rely on.")
    if selected is not None:
        lines.append(selected.message)
    budget = RefBudget(text_left=REF_TEXT_TOTAL_BYTES, media_left=MAX_REF_MEDIA)
    media: list = []
    for m in batch:
        block, attached = render_message(m, budget)
        lines.append(block)
        media += attached
    if opening:
        prompt_id = getattr(batch[0], "id", "") if batch else ""
        how = (f'reply to it with annotate(text, reply_to="{prompt_id}")' if prompt_id
               else "reply with annotate(text)")
        footer = (f"This message is the whole task. Do it, then {how} saying what you did "
                  "and where the results are.")
        if added:
            footer += (" Messages the user added while the run was starting follow it; "
                       "answer them in their threads.")
    else:
        footer = ('Answer with the annotate tool: annotate(text, reply_to="<thread>") for each '
                  "thread you handle — a short acknowledgement first when the work will take "
                  'a while, then the outcome. Messages with mode="interrupt" come first: the '
                  "user sent them to change what you are doing.")
    lines.append(footer)
    return Batch("\n\n".join(lines), media)


# ── after a turn ────────────────────────────────────────────────────


async def _post(text: str, reply_to: "str | None") -> None:
    try:
        await gen.annotations.post(text, reply_to=reply_to or None)
    except (asyncio.CancelledError, gen.Cancelled):
        raise
    except Exception as exc:  # noqa: BLE001 — a post never fails the turn
        _secret_log(f"annotations: could not post ({exc})")


def _capped_post(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= POST_CAP_BYTES:
        return text
    return raw[:POST_CAP_BYTES].decode("utf-8", "ignore") + "\n\n[truncated]"


def answered(sess, text: str) -> None:
    """The turn in flight's engine replied: ``text`` is its final assistant
    message, the answer :func:`after_turn` posts when the turn posted
    nothing itself."""
    leg = sess.annotations
    if leg is not None and leg.flight is not None:
        leg.flight.answer = text


async def after_turn(sess, turn, *, stopped: bool, failed: str) -> None:
    """A turn ended (``state.on_turn_end``): settle the batch it carried —
    keep it open when it was cut, post the final answer the turn recorded
    and did not post itself."""
    leg = sess.annotations
    if leg is None:
        return
    leg.last_activity = time.monotonic()
    leg.parked = False
    source = getattr(turn, "source", "")
    if source not in (Sources.ANNOTATIONS, Sources.OPENING):
        return
    flight, leg.flight = leg.flight, None
    if flight is None or gen.cancelled:
        return
    batch = flight.messages
    opening = source == Sources.OPENING
    if opening:
        target = (getattr(leg.prompt, "id", "") or None) if leg.prompt is not None else None
    else:
        last = batch[-1]
        target = (getattr(last, "reply_to", None) or getattr(last, "id", "")) or None
    if stopped or failed:
        if not opening:
            leg.open = batch
            leg.cut_short = ("interrupted by a newer message" if stopped and flight.interrupted
                             else "stopped" if stopped else f"cut short: {failed}")
            leg.resume = stopped and flight.interrupted
        if failed:
            detail = failed.strip()
            if not detail.endswith((".", "!", "?")):
                detail += "."
            await _post(f"_(This turn failed: {detail}"
                        + ("" if opening else " These messages come back with your next message.")
                        + ")_", target)
        return
    if annotation_tools.posted_since(flight.mark):
        return
    text = (flight.answer or "").strip()
    if not text or text == EMPTY_REPLY:
        return
    await _post(_capped_post(text), target)


def cancelled_before_pull(sess) -> None:
    """A batch turn was cut before it pulled anything: the watcher looks
    again."""
    leg = sess.annotations
    if leg is not None:
        leg.wake.set()


# ── notes, outside edits, teardown ──────────────────────────────────


def note(sess, text: str) -> None:
    """Post ``text`` as a top-level note of the run (a config warning)."""
    leg = sess.annotations
    if not _available():
        return
    task = asyncio.get_running_loop().create_task(_post(text, None))
    if leg is not None:
        leg.notes.add(task)
        task.add_done_callback(leg.notes.discard)


def changed(sess, path: str) -> None:
    """An outside edit (the live mirror's ``on_changed``): noted for the
    next batch, never a turn of its own. The run folders are not news."""
    leg = sess.annotations
    if leg is None or path.startswith(("_runs/", ".collrun/")):
        return
    leg.changed[path] = time.monotonic()


async def close(sess) -> None:
    """Teardown: end the subscription and the watcher, and let the notes in
    flight land."""
    leg = sess.annotations
    if leg is None:
        return
    if leg.sub is not None:
        sub, leg.sub = leg.sub, None
        try:
            sub.close()
        except Exception:  # noqa: BLE001 — teardown
            pass
    if leg.watch is not None:
        watch, leg.watch = leg.watch, None
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
    if leg.notes:
        await asyncio.wait(set(leg.notes), timeout=2.0)
