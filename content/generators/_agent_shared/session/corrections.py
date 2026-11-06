# SPDX-License-Identifier: MIT
"""Host-refusal corrections: the turns that ask the model to fix a write the
host refused (a typed file its accepted verifier rejected, a path the run
may not write).

The live mirror reports each refused write (``gen.live_files``'
``on_refused``) and an explicit flush raises ``gen.PartialRefusal``, which
names every refused path; no other ``PermissionError`` is a refusal a
correction can fix. A write a newer save refused (a ``gen.FileConflict``)
gets its own turn: it says who saved which revision, shows what changed,
and offers the two ways on — keep their version, or write again with
``replace_revision`` naming that revision; it reads the newer version from
the host, so on the event loop its prompt is built on a worker thread and
the turn follows when it is ready. Each refusal queues one
``@correction`` turn, at most :data:`MAX_CORRECTIONS` in a row per path;
the same refusal reported twice (by the mirror and by a flush, over the same
bytes) queues one. A completed turn with no correction pending clears the
count. Wired for the annotation and chat surfaces; never for a terminal,
whose TTY a correction turn would race."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
from collections import deque

import gen

from _agent_shared.logging import safe_log as _secret_log

from .consts import Sources

MAX_CORRECTIONS = 2

# How much of a conflict's diff, and of the newer version itself, the
# correction turn carries.
CONFLICT_DIFF_CHARS = 6000
CONFLICT_TEXT_CHARS = 4000


def _digest(path: str) -> str:
    """The sha256 of the workspace file at ``path`` ("" when there is no
    path or file): the same refusal over the same bytes is one refusal."""
    if not path:
        return ""
    try:
        if not gen.has_file(path):
            return ""
        data = gen.read_file(path)
    except Exception:  # noqa: BLE001 — an unreadable file reads as absent
        return ""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return hashlib.sha256(raw).hexdigest()


def correction_prompt(path: str, error: object, *, oneshot: bool) -> str:
    """The correction turn's prompt for a refused write of ``path`` (``""``:
    a write of the last turn the refusal does not name)."""
    what = f"your write to {path}" if path else "a write of your last turn"
    text = (f"The host refused {what}: {error}\nFix the cause and write it "
            "again — for a typed file follow its accepted handler skill "
            "(handler_skills, check with validate_file); a path you may not "
            "write stays refused. The workspace keeps your attempted bytes.")
    if oneshot:
        text += " The run retries its final push when this correction ends."
    return text


def _text(data) -> str | None:
    if data is None:
        return None
    if isinstance(data, str):
        return data
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None


def conflict_prompt(conflict: gen.FileConflict, *, oneshot: bool) -> str:
    """The correction turn's prompt for a write of ``conflict.path`` that a
    newer save refused: who saved which revision, what changed from the
    model's version to theirs, and the two ways on."""
    path, revision = conflict.path, conflict.revision
    lines = [(f"Your write of {path} was blocked: {conflict.writer} saved a newer version "
              f"(revision {revision}) after you last read it. Nothing was overwritten: their "
              "version stands, and your bytes stay in your workspace and beside it as a "
              "candidate for review.")]
    try:
        theirs = _text(conflict.read()) if conflict.body_hash is not None else None
        readable = conflict.body_hash is None or theirs is not None
    except Exception:  # noqa: BLE001 — an unreadable version is described, not shown
        theirs, readable = None, False
    yours = None
    try:
        if gen.has_file(path):
            yours = _text(gen.read_file(path))
    except Exception:  # noqa: BLE001
        yours = None
    if conflict.body_hash is None:
        lines.append(f"Their revision {revision} deleted {path}.")
    elif not readable:
        lines.append(f"Their revision {revision} is not text this turn can show; inspect "
                     "it in the cell before you decide.")
    else:
        diff = "".join(difflib.unified_diff(
            (yours or "").splitlines(keepends=True), theirs.splitlines(keepends=True),
            fromfile=f"{path} (yours)", tofile=f"{path} (revision {revision})"))
        if len(diff) > CONFLICT_DIFF_CHARS:
            diff = diff[:CONFLICT_DIFF_CHARS] + "\n… (diff truncated)\n"
        lines.append("What changed, from your version to theirs:\n```diff\n" + (diff or "(no textual difference)\n") + "```")
        if len(theirs) <= CONFLICT_TEXT_CHARS:
            lines.append(f"Their revision {revision} in full:\n```\n{theirs}\n```")
    shown = conflict.body_hash is None or (theirs is not None and len(theirs) <= CONFLICT_TEXT_CHARS)
    read = ("" if shown else
            f" This turn does not show all of it: read it with read_newer_version "
            f"(path {path}, revision {revision}) before you decide.")
    lines.append(
        f"Decide, having read their version.{read} To accept it, call accept_newer_version "
        f"(path {path}, revision {revision}), or write exactly their content: their version "
        "becomes yours and your blocked attempts close. To keep your change over theirs on "
        f"purpose, call write_file or edit_file on {path} with replace_revision={revision}: "
        f"that replaces their revision {revision} deliberately and History shows it did. "
        "Any other write of it without replace_revision is blocked again.")
    text = "\n\n".join(lines)
    if oneshot:
        text += " The run retries its final push when this correction ends."
    return text


class Corrections:
    """The run's correction ledger: ``strikes`` per path, the last refusal
    ``signatures`` per path, and the ``queue`` of paths whose correction
    turn is queued or running."""

    def __init__(self, *, oneshot: bool = False) -> None:
        self.oneshot = oneshot
        self.strikes: dict[str, int] = {}
        self.signatures: dict[str, str] = {}
        self.queue: deque[str] = deque()
        # Conflict prompts being built off the loop (their newer version is
        # a blocking host read).
        self.building: set[asyncio.Task] = set()

    @property
    def pending(self) -> bool:
        """A correction turn is queued or running."""
        return bool(self.queue)

    def refused(self, path: str, error: object) -> bool:
        """One refused write (the live mirror's ``on_refused``; ``error`` a
        ``gen.FileConflict`` when a newer save refused it): queue its
        correction turn. False when none was queued — the run is stopping,
        the same refusal was already reported, or the path has had its
        :data:`MAX_CORRECTIONS` in a row."""
        if gen.cancelled:
            return False
        path = str(path or "")
        signature = f"{error}:{_digest(path)}"
        if self.signatures.get(path) == signature:
            return False
        self.signatures[path] = signature
        count = self.strikes.get(path, 0)
        if count >= MAX_CORRECTIONS:
            _secret_log(f"{path or 'a write'}: stopped correcting after "
                        f"{MAX_CORRECTIONS} consecutive refusals: {error}")
            return False
        self.strikes[path] = count + 1
        self.queue.append(path)
        if not isinstance(error, gen.FileConflict):
            gen.session.submit_turn(correction_prompt(path, error, oneshot=self.oneshot),
                                    source=Sources.CORRECTION)
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            gen.session.submit_turn(conflict_prompt(error, oneshot=self.oneshot), source=Sources.CORRECTION)
            return True
        # On the loop (the mirror's on_refused, a flush in a turn): the
        # prompt reads the newer version from the host, a blocking IPC
        # round trip, so it is built on a worker thread and its turn is
        # submitted when it is ready. The path is queued already.
        task = loop.create_task(self._submit_conflict(path, error))
        self.building.add(task)
        task.add_done_callback(self.building.discard)
        return True

    async def _submit_conflict(self, path: str, conflict: gen.FileConflict) -> None:
        try:
            prompt = await asyncio.to_thread(conflict_prompt, conflict, oneshot=self.oneshot)
        except Exception:  # noqa: BLE001 — the refusal is still corrected, in its plain words
            prompt = correction_prompt(path, conflict, oneshot=self.oneshot)
        if gen.cancelled:
            # The run is stopping: no turn will take this path off the queue.
            try:
                self.queue.remove(path)
            except ValueError:
                pass
            return
        gen.session.submit_turn(prompt, source=Sources.CORRECTION)

    def refused_exc(self, exc: "gen.PartialRefusal") -> bool:
        """A flush the host partly refused: one :meth:`refused` per path the
        refusal names, else one for the write as a whole. True when any
        correction was queued."""
        errors: dict = dict(exc.errors)
        errors.update(getattr(exc, "conflicts", {}))
        for path in sorted(exc.paths):
            errors.setdefault(path, str(exc))
        if not errors:
            return self.refused("", exc)
        queued = False
        for path, error in sorted(errors.items()):
            queued = self.refused(path, error) or queued
        return queued

    def after_turn(self, source: str, *, stopped: bool, failed: str) -> None:
        """A turn ended: a correction turn takes its path off the queue; a
        completed turn with nothing pending clears the strikes."""
        if source == Sources.CORRECTION:
            if self.queue:
                self.queue.popleft()
        elif not stopped and not failed and not self.queue:
            self.strikes.clear()
