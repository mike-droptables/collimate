# SPDX-License-Identifier: MIT
"""Run end: **live interaction files are tools; the cell keeps artifacts.**

A run's surface files — every file in its run folder: the chat pointer and
its transcripts, a terminal's pointer — are the session's *working tools*:
they exist so a person and a model can talk while the run is live. The
moment the run ends they stop being live: a pointer names a dead socket and
a transcript describes a conversation the harness no longer holds. (An
annotation run has none: its messages are the host's.)

So at run end each of them is kept in the run's **LOG** and removed from the
cell in ONE flush — ``gen.delete_ui_file(name, log=True)``, the SDK's
log-then-delete: the file's last content lands in the run node's Logs under
the same path, and the cell loses it. Only this run's own folder is touched,
never another run's and never the run card (the host's; it goes when the
host finishes the run, and the host sweeps whatever a killed run left).
What stays is what the agent *produced*: every file it wrote for the user.

A Stop is exactly this — ``entry.run_session``'s ``finally`` runs on every
path. The flush is explicit because a hard Stop never reaches ``gen.main``'s
auto-flush (it unwinds through the ``Cancelled`` branch), so nothing else
would commit either the log artifacts or the cell deletions.
"""
from __future__ import annotations
from _agent_shared.logging import safe_log as _secret_log

import gen


async def finish(sess) -> None:
    """Log-and-clear this run's surface files.

    Called from ``entry.run_session``'s ``finally`` — after ``serve_chat`` has
    returned and after ``AgentSession.close()``. Never raises: it runs while
    an exception (a hard Stop's ``Cancelled``) is unwinding, and swallowing
    that exception would turn a user Stop into a generator failure."""
    try:
        names = gen.list_ui_files()
    except Exception as exc:  # noqa: BLE001
        _log(f"run end: could not list the run's surface files ({exc})")
        names = []
    logged: list[str] = []
    for name in names:
        try:
            gen.delete_ui_file(name, log=True)
        except Exception as exc:  # noqa: BLE001
            _log(f"run end: could not clear {name} ({exc}) — left for the host")
            continue
        logged.append(name)
    if not logged:
        return

    # ONE flush commits the log artifacts and the cell deletions together.
    try:
        await gen.commit()
    except Exception as exc:  # noqa: BLE001 — teardown race with the host
        _log(f"run end: flushing the session log failed ({exc})")
        return
    surface = sess.surface or "session"
    _log(f"{surface} ended — {len(logged)} live session file(s) moved to "
         "the run log and cleared from the cell: " + ", ".join(logged))


def _log(message: str) -> None:
    try:
        _secret_log(message)
    except Exception:  # noqa: BLE001 — bridge-free tests / teardown
        pass
