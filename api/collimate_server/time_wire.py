"""One timestamp shape on the wire: UTC ISO 8601 with an offset.

The channel's records keep time in three conventions:

* SQLite's ``CURRENT_TIMESTAMP``: ``'YYYY-MM-DD HH:MM:SS'``, UTC, no zone
  (``cells``, ``runs``, ``run_events``, ``cell_moves`` ...).
* The layout's ``Cell.created_at`` / ``updated_at``: a naive ``datetime``
  in the server's local time (``datetime.now()``).
* ISO strings that carry their offset (``removed_cells.removed_at``,
  ``runs.ended_at``).

:func:`iso` turns any of them into ``'2026-10-06T14:05:00+00:00'``: UTC,
whole seconds, so wire strings of one kind sort as text.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

# SQLite's CURRENT_TIMESTAMP, optionally with fractional seconds.
_SQLITE_UTC = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$")


def iso(value: str | datetime | None) -> str | None:
    """Return ``value`` as UTC ISO 8601 with an offset, or None for None.

    A ``datetime`` without tzinfo is the layout's local time; an aware one
    is converted. A string without an offset must be SQLite's
    ``'YYYY-MM-DD HH:MM:SS'`` (UTC); a string with an offset is converted.
    A naive ``T``-separated string is refused (ValueError): it is either a
    layout dump (local) or a bug, and guessing would shift it by hours.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.astimezone()
    elif isinstance(value, str):
        text = value.strip()
        if _SQLITE_UTC.match(text):
            moment = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                raise ValueError(f"ambiguous timestamp without an offset: {value!r}")
    else:
        raise TypeError(f"not a timestamp: {value!r}")
    return _wire(moment)


def now_iso() -> str:
    """The current moment in the wire form."""
    return _wire(datetime.now(timezone.utc))


def _wire(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
