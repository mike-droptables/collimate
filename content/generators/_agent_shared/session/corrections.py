# SPDX-License-Identifier: MIT
"""Host-refusal corrections: the turns that ask the model to fix a write the
host refused (a typed file its accepted verifier rejected, a path the run
may not write).

The live mirror reports each refused write (``gen.live_files``'
``on_refused``) and an explicit flush raises ``gen.PartialRefusal``, which
names every refused path; no other ``PermissionError`` is a refusal a
correction can fix. Each refusal queues one
``@correction`` turn, at most :data:`MAX_CORRECTIONS` in a row per path;
the same refusal reported twice (by the mirror and by a flush, over the same
bytes) queues one. A completed turn with no correction pending clears the
count. Wired for the annotation and chat surfaces; never for a terminal,
whose TTY a correction turn would race."""
from __future__ import annotations

import hashlib
from collections import deque

import gen

from _agent_shared.logging import safe_log as _secret_log

from .consts import Sources

MAX_CORRECTIONS = 2


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


class Corrections:
    """The run's correction ledger: ``strikes`` per path, the last refusal
    ``signatures`` per path, and the ``queue`` of paths whose correction
    turn is queued or running."""

    def __init__(self, *, oneshot: bool = False) -> None:
        self.oneshot = oneshot
        self.strikes: dict[str, int] = {}
        self.signatures: dict[str, str] = {}
        self.queue: deque[str] = deque()

    @property
    def pending(self) -> bool:
        """A correction turn is queued or running."""
        return bool(self.queue)

    def refused(self, path: str, error: object) -> bool:
        """One refused write (the live mirror's ``on_refused``): queue its
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
        gen.session.submit_turn(correction_prompt(path, error, oneshot=self.oneshot),
                        source=Sources.CORRECTION)
        return True

    def refused_exc(self, exc: "gen.PartialRefusal") -> bool:
        """A flush the host partly refused: one :meth:`refused` per path the
        refusal names, else one for the write as a whole. True when any
        correction was queued."""
        errors = dict(exc.errors)
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
