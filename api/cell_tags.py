"""Cell tags: the one owner of the tag rules and the channel's vocabulary.

A tag is a free-form plain-text chip on any cell (``Cell.tags``, insertion
order), on Capabilities as on the board; a move never changes it. ``normalize_tag`` strips the text, turns
every run of whitespace (``str.isspace``) into one space, and refuses an
empty result, a remaining control character (Unicode category ``Cc``) or more
than ``TAG_MAX_LENGTH`` characters. A cell's list keeps the first spelling of
each tag compared by ``str.casefold`` and holds at most ``TAGS_PER_CELL_MAX``.
The frontend mirrors these rules in ``frontend/src/lib/tags.ts``, its two
limits generated from these by ``frontend/scripts/sync_app_constants.py``.

Writers: ``cell_metadata.publish_cell_metadata`` (the full list, PATCH
``…/meta``), ``cell_catalog.set_removed_cell_tags`` (a shelf card's full
list, PUT ``…/cells/removed/{id}/tags``, on the removed record),
``routes/stacks.remove_cell`` (a remove zone's add/remove action
applied to the removed record only), ``channel_settings`` (remove-zone tags),
Duplicate (copies), Merge (``union_tags``) and Split revival (the record's
tags).
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

TAG_MAX_LENGTH = 48
TAGS_PER_CELL_MAX = 32

_WHITESPACE = re.compile(r"\s+")


def normalize_tag(value: object) -> str | None:
    """The canonical spelling of one tag, or None when it is refused."""
    if not isinstance(value, str):
        return None
    tag = _WHITESPACE.sub(" ", value.strip())
    if not tag or len(tag) > TAG_MAX_LENGTH:
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in tag):
        return None
    return tag


def _deduped(values) -> list[str]:
    """Each value normalized, the first spelling of each casefold kept in
    order. Raises ValueError on a non-list, a non-string or a refused tag."""
    if not isinstance(values, list):
        raise ValueError("tags must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("tags must be a list of strings")
        tag = normalize_tag(value)
        if tag is None:
            raise ValueError(f"invalid tag {value!r}: tags are 1-{TAG_MAX_LENGTH} "
                             "characters with no control characters")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            out.append(tag)
    return out


def _capped(tags: list[str]) -> list[str]:
    if len(tags) > TAGS_PER_CELL_MAX:
        raise ValueError(f"a cell holds at most {TAGS_PER_CELL_MAX} tags")
    return tags


def normalize_tags(values) -> list[str]:
    """A cell's full tag list: normalized, casefold-unique keeping the first
    spelling and the order, at most ``TAGS_PER_CELL_MAX``. Raises ValueError
    on anything else."""
    return _capped(_deduped(values))


def apply_tag_action(tags, add, remove) -> list[str]:
    """``tags`` with ``add`` appended and then ``remove`` taken out (casefold),
    normalized and deduplicated. Raises ValueError as ``normalize_tags``."""
    merged = _deduped([*tags, *add])
    dropped = {tag.casefold() for tag in _deduped(list(remove))}
    return _capped([tag for tag in merged if tag.casefold() not in dropped])


def union_tags(lists: Iterable[Iterable[str]]) -> list[str]:
    """Merge's output tags: the casefold-deduplicated union of the inputs' in
    their order, refused tags skipped, the first ``TAGS_PER_CELL_MAX`` kept."""
    out: list[str] = []
    seen: set[str] = set()
    for tags in lists:
        for value in tags:
            tag = normalize_tag(value)
            if tag is not None and tag.casefold() not in seen:
                seen.add(tag.casefold())
                out.append(tag)
    return out[:TAGS_PER_CELL_MAX]


def channel_tags(channel: str) -> list[dict]:
    """The channel's tag vocabulary: ``[{tag, live, removed}]`` over every
    layout cell (``live``) and the ``removed_cells`` records of cells off the
    canvas (``removed``), each count the number of cells carrying the tag. One
    entry per casefold, spelled as first met (live cells first); sorted by
    casefold."""
    import removed_cells
    from cell_placement import iter_stacks
    from channel_db import open_channel_db
    from state import require_channel, state_lock

    with state_lock:
        ch = require_channel(channel)
        live_cells = [cell for stack in iter_stacks(ch) for cell in stack.cells]
        live_ids = {str(cell.id) for cell in live_cells}
        live_lists = [list(cell.tags) for cell in live_cells]
    conn = open_channel_db(channel)
    try:
        removed_lists = [record.tags for record in removed_cells.list_removed(conn)
                         if record.cell_id not in live_ids]
    finally:
        conn.close()
    entries: dict[str, dict] = {}
    for kind, lists in (("live", live_lists), ("removed", removed_lists)):
        for tags in lists:
            for tag in union_tags([tags]):
                entry = entries.setdefault(tag.casefold(), {"tag": tag, "live": 0, "removed": 0})
                entry[kind] += 1
    return [entries[key] for key in sorted(entries)]
