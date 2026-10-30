"""Per-channel key/value config stored in the channel DB's ``ui_state``
table.

Owns the channel's ``renderer_availability`` and ``renderer_preferences``
rows, shared by discovery filtering (renderers), the file-manager "management"
view's availability endpoints and the /api/renderer_preferences routes. It also
exports the generic ``ui_state`` JSON helpers and the shared merge lock that the
other channel-scoped KV owners (channel_settings, routes/pick_sessions,
cell_catalog) use.

Renderer availability and preferences are owned here. Cell panel sessions share
the storage helpers; their endpoint owns their contents.
"""

import json
import threading

from database import get_db_connection

RENDERER_AVAILABILITY_KEY = "renderer_availability"
RENDERER_PREFERENCES_KEY = "renderer_preferences"

# The ``ui_state`` key of routes/pick_sessions.py's ``{cellId: session}`` map.
PICK_SESSIONS_KEY = "pick_sessions"

# Serializes read-modify-write merges so two concurrent per-id PUTs can't
# clobber each other (the DB write itself is atomic, the read+merge isn't).
# Imports hold this before their channel transaction; nested helpers reenter it.
_merge_lock = threading.RLock()


def _read_json(db_name: str, key: str, default, *, conn=None):
    owned = conn is None
    conn = get_db_connection(db_name) if owned else conn
    try:
        row = conn.execute(
            "SELECT value FROM ui_state WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default
    finally:
        if owned:
            conn.close()


def _write_json(db_name: str, key: str, value, *, conn=None) -> None:
    owned = conn is None
    conn = get_db_connection(db_name) if owned else conn
    try:
        conn.execute(
            "INSERT INTO ui_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        if owned:
            conn.commit()
    finally:
        if owned:
            conn.close()


# ---------------------------------------------------------------------------
# Generic ``ui_state`` access for the OTHER channel-scoped KV owners
#
# The renderer keys above are this module's own; other modules (channel_settings,
# routes/pick_sessions) store their own rows in the same table and need the
# same reader/writer + the same lock, or two read-modify-write paths hold
# different locks and a concurrent merge is silently lost. These are the public
# door onto ``_read_json`` / ``_write_json`` / ``_merge_lock`` — reuse them
# instead of re-implementing the pattern per module.
# ---------------------------------------------------------------------------

def read_ui_state_json(db_name: str, key: str, default=None, *, conn=None):
    """JSON-decoded value of one ``ui_state`` row, or ``default`` when the row
    is absent or unparseable. Does NOT take the merge lock — take it around the
    whole read-modify-write when you intend to write back."""
    return _read_json(db_name, key, default, conn=conn)


def write_ui_state_json(db_name: str, key: str, value, *, conn=None) -> None:
    """Upsert one ``ui_state`` row with the JSON encoding of ``value``. Take
    :func:`ui_state_merge_lock` around read+write pairs."""
    _write_json(db_name, key, value, conn=conn)


def ui_state_merge_lock():
    """The process-wide lock serializing read-modify-write merges over the
    channel ``ui_state`` table. Shared deliberately (see ``channel_settings``'s
    ``_merge_lock`` alias): every merging writer must take THIS lock. Held only
    for the duration of a merge, and nothing under it acquires another lock, so
    sharing it cannot deadlock."""
    return _merge_lock


# ---------------------------------------------------------------------------
# Reads (whole dict)
# ---------------------------------------------------------------------------

def read_renderer_availability(db_name: str, *, conn=None) -> dict:
    data = _read_json(db_name, RENDERER_AVAILABILITY_KEY, {}, conn=conn)
    return data if isinstance(data, dict) else {}


def read_renderer_preferences(db_name: str, *, conn=None) -> dict:
    """Extension → renderer-name override map for the channel. Absent = {}.
    Shared source for the ``/api/renderer_preferences`` endpoints and the
    settings doc's composed ``renderer_preferences`` section. A stored entry
    that would move a surface off its built-in renderer is ignored."""
    from builtin_packages import preference_locked
    data = _read_json(db_name, RENDERER_PREFERENCES_KEY, {}, conn=conn)
    return {ext: rid for ext, rid in data.items() if not preference_locked(ext, rid)} if isinstance(data, dict) else {}


def check_renderer_preferences(prefs: dict) -> None:
    """Refuse (ValueError) a preference that moves a surface extension off
    its built-in renderer (``builtin_packages.preference_locked``)."""
    from builtin_packages import preference_locked
    locked = sorted(ext for ext, rid in prefs.items() if preference_locked(ext, rid))
    if locked:
        raise ValueError(f"{', '.join(locked)} always open in the app's built-in renderer")


def write_renderer_preferences(db_name: str, prefs: dict) -> None:
    """Replace the channel's renderer-preferences map wholesale. Takes the merge
    lock so a direct ``PUT /api/renderer_preferences`` is serialized against a
    concurrent settings import that routes the same composed row. Raises
    ValueError for a locked surface preference."""
    prefs = prefs if isinstance(prefs, dict) else {}
    check_renderer_preferences(prefs)
    with _merge_lock:
        _write_json(db_name, RENDERER_PREFERENCES_KEY, prefs)


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

def renderer_enabled(availability: dict, rid: str) -> bool:
    """Whether the user has enabled an approved renderer."""
    return availability.get(rid) is not False


def set_renderer_availability(db_name: str, rid: str, enabled: bool) -> dict:
    """Set ``rid``'s enabled flag. Returns the full updated dict."""
    with _merge_lock:
        data = read_renderer_availability(db_name)
        data[rid] = bool(enabled)
        _write_json(db_name, RENDERER_AVAILABILITY_KEY, data)
        return data
