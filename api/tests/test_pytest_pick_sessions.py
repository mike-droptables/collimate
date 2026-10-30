"""Coverage for routes/pick_sessions.py — the per-cell panel session store.

The sessions live in the channel DB's ``ui_state`` KV under a single
``pick_sessions`` row (``{cellId: session}``). Three properties the UI leans on
and this file pins down:

  * a session survives a round trip verbatim (the backend does not own the
    session schema — the frontend does),
  * a session for a cell that has left the canvas is pruned on read AND the
    pruned map is written back,
  * the store is invisible to the channel settings export — this is per-user
    scratch state, never channel content.

Everything is synthetic: cells are ``models.Cell`` objects appended to the
active channel's stacks, sessions are opaque dicts. No generator or renderer
name from any shipped catalog appears here.
"""
from __future__ import annotations

import uuid

def _channel_id() -> str:
    from state import find_active_channel
    return str(find_active_channel().id)


def _session(**over) -> dict:
    """A pick-session-shaped payload (``PickSession`` in the frontend: the
    panel's kind, its search query and the open timestamp). The backend stores it
    verbatim, so the exact keys only need to be stable within this file."""
    base = {"kind": "runners", "query": "syn", "at": 1234}
    base.update(over)
    return base


def _add_cells(n: int = 1) -> list[str]:
    """Append a synthetic stack of ``n`` cells to the active channel and return
    their ids as strings."""
    from state import state
    from models import Stack, Cell

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    cells = [Cell(name=f"synthetic-{i}") for i in range(n)]
    ch.main_stacks.append(Stack(name="synthetic-stack", cells=cells))
    return [str(c.id) for c in cells]


def _stored_row() -> dict | None:
    """The raw ``pick_sessions`` ui_state value, read straight from the channel
    DB — proves what was PERSISTED, not merely what the response said."""
    from channel_config import PICK_SESSIONS_KEY, read_ui_state_json

    return read_ui_state_json(_channel_id(), PICK_SESSIONS_KEY, None)


def _url(cell_id: str | None = None) -> str:
    base = f"/api/channels/{_channel_id()}/pick_sessions"
    return base if cell_id is None else f"{base}/{cell_id}"


# ---------------------------------------------------------------------------
# PUT / GET round trip
# ---------------------------------------------------------------------------

def test_put_then_get_round_trips_the_session(client):
    (cell_id,) = _add_cells(1)
    # Extra keys ride along untouched: the backend does not own the schema,
    # so a frontend-side addition needs no backend release.
    payload = _session(query="hello", extra={"depth": 3, "flag": True})

    r = client.put(_url(cell_id), json=payload)
    assert r.status_code == 200, r.text

    r = client.get(_url())
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body.keys()) == [cell_id]
    # Verbatim: nested structures and non-string values survive the JSON round
    # trip untouched (the frontend owns this schema).
    assert body[cell_id] == payload


def test_empty_channel_returns_empty_map(client):
    r = client.get(_url())
    assert r.status_code == 200, r.text
    assert r.json() == {}


def test_sessions_are_independent_per_cell(client):
    a, b = _add_cells(2)
    client.put(_url(a), json=_session(query="a"))
    client.put(_url(b), json=_session(query="b"))

    body = client.get(_url()).json()
    assert set(body) == {a, b}
    assert body[a]["query"] == "a"
    assert body[b]["query"] == "b"


def test_put_upserts_over_an_existing_session(client):
    (cell_id,) = _add_cells(1)
    client.put(_url(cell_id), json=_session(query="first", at=1, extra="old"))

    r = client.put(_url(cell_id), json=_session(query="second", at=2))
    assert r.status_code == 200, r.text

    body = client.get(_url()).json()
    assert len(body) == 1
    # Wholesale replace, not a merge — the newer session is the whole truth.
    assert body[cell_id] == {"kind": "runners", "query": "second", "at": 2}


# ---------------------------------------------------------------------------
# DELETE — idempotent
# ---------------------------------------------------------------------------

def test_delete_removes_the_session_and_is_idempotent(client):
    (cell_id,) = _add_cells(1)
    client.put(_url(cell_id), json=_session())
    assert cell_id in client.get(_url()).json()

    r = client.delete(_url(cell_id))
    assert r.status_code == 200, r.text
    assert client.get(_url()).json() == {}

    # Second delete: same 200, no 404. Closing an already-closed panel must
    # not need special-casing on the client.
    r = client.delete(_url(cell_id))
    assert r.status_code == 200, r.text

    # And a delete for a cell that never had a session at all.
    r = client.delete(_url(str(uuid.uuid4())))
    assert r.status_code == 200, r.text


def test_delete_leaves_other_sessions_alone(client):
    a, b = _add_cells(2)
    client.put(_url(a), json=_session(query="a"))
    client.put(_url(b), json=_session(query="b"))

    client.delete(_url(a))
    body = client.get(_url()).json()
    assert set(body) == {b}


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------

def test_non_object_body_is_rejected(client):
    (cell_id,) = _add_cells(1)
    for bad in ([1, 2, 3], "a string", 42, True, None):
        r = client.put(_url(cell_id), json=bad)
        assert r.status_code == 400, f"{bad!r} → {r.status_code} {r.text}"
    # Nothing was stored by any of the rejects.
    assert client.get(_url()).json() == {}


def test_oversize_body_is_rejected(client):
    from routes.pick_sessions import MAX_SESSION_BYTES

    (cell_id,) = _add_cells(1)
    # A query well over the per-session cap.
    r = client.put(_url(cell_id), json=_session(query="x" * (MAX_SESSION_BYTES + 1024)))
    assert r.status_code == 400, r.text
    assert "too large" in r.json()["detail"]
    assert client.get(_url()).json() == {}


def test_body_just_under_the_cap_is_accepted(client):
    from routes.pick_sessions import MAX_SESSION_BYTES

    (cell_id,) = _add_cells(1)
    # Leave room for the rest of the JSON envelope.
    payload = _session(query="y" * (MAX_SESSION_BYTES - 64))
    r = client.put(_url(cell_id), json=payload)
    assert r.status_code == 200, r.text
    assert client.get(_url()).json()[cell_id]["query"] == payload["query"]


# ---------------------------------------------------------------------------
# Read-time prune
# ---------------------------------------------------------------------------

def test_get_prunes_sessions_for_unknown_cells_and_persists(client):
    (live_cell,) = _add_cells(1)
    ghost_cell = str(uuid.uuid4())  # never on the canvas

    client.put(_url(live_cell), json=_session(query="live"))
    client.put(_url(ghost_cell), json=_session(query="ghost"))
    # Both were stored — the PUT does not check cell existence.
    assert set(_stored_row()) == {live_cell, ghost_cell}

    body = client.get(_url()).json()
    assert set(body) == {live_cell}
    assert body[live_cell]["query"] == "live"
    # The prune was written back, not just filtered on the way out.
    assert set(_stored_row()) == {live_cell}


def test_get_prunes_a_cell_removed_after_its_session_was_saved(client):
    from state import state

    (cell_id,) = _add_cells(1)
    client.put(_url(cell_id), json=_session())
    assert set(client.get(_url()).json()) == {cell_id}

    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    ch.main_stacks = [
        s for s in ch.main_stacks
        if not any(str(c.id) == cell_id for c in s.cells)
    ]

    assert client.get(_url()).json() == {}
    assert _stored_row() == {}


def test_unknown_channel_without_db_refuses_without_creating_a_ghost(client):
    """A late save to a deleted UUID cannot recreate or address its database."""
    import os

    ghost_channel = str(uuid.uuid4())
    cell_id = str(uuid.uuid4())
    base = f"/api/channels/{ghost_channel}/pick_sessions"

    assert client.put(f"{base}/{cell_id}", json=_session(query="dropped")).status_code == 404
    assert client.get(base).status_code == 404
    assert client.delete(f"{base}/{cell_id}").status_code == 404

    assert not os.path.exists(f".collimate/channels/{ghost_channel}.db")


def test_registered_inactive_channel_sessions_are_isolated(client):
    """An inactive canvas retains its own panel sessions without leaking them
    into the active channel's hydration map."""
    import database
    from models import Channel, Cell, Stack
    from state import state

    cell = Cell(name="Inactive cell")
    inactive = Channel(name="Inactive", main_stacks=[Stack(cells=[cell])])
    state.channels.append(inactive)
    channel_id = str(inactive.id)
    database.get_db_connection(channel_id).close()
    cell_id = str(cell.id)
    base = f"/api/channels/{channel_id}/pick_sessions"
    response = client.put(f"{base}/{cell_id}", json=_session(query="kept"))
    assert response.status_code == 200, response.text
    response = client.get(base)
    assert response.status_code == 200, response.text
    assert response.json() == {cell_id: _session(query="kept")}
    assert client.get(_url()).json() == {}


def test_malformed_stored_row_degrades_to_empty(client):
    """A hand-edited / half-written row must not break channel hydration."""
    from channel_config import PICK_SESSIONS_KEY, write_ui_state_json

    write_ui_state_json(_channel_id(), PICK_SESSIONS_KEY, ["not", "a", "map"])
    assert client.get(_url()).json() == {}

    write_ui_state_json(_channel_id(), PICK_SESSIONS_KEY, {"cell-x": "not a session"})
    assert client.get(_url()).json() == {}


# ---------------------------------------------------------------------------
# Never exported
# ---------------------------------------------------------------------------

def test_settings_export_does_not_contain_pick_sessions(client):
    """Pick sessions are per-user scratch, not channel content: no settings
    section, and nothing leaks into the composed doc even
    when sessions exist in the same ui_state table."""
    import channel_settings

    (cell_id,) = _add_cells(1)
    client.put(_url(cell_id), json=_session(query="secret draft"))

    assert "pick_sessions" not in channel_settings.SECTIONS
    assert "pick_sessions" not in channel_settings.DOC_SECTIONS
    assert "pick_sessions" not in channel_settings.COMPOSED_SECTIONS
    assert "pick_sessions" not in channel_settings.default_settings()

    doc = channel_settings.load_channel_settings(_channel_id())
    assert "pick_sessions" not in doc

    r = client.get(f"/api/channels/{_channel_id()}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pick_sessions" not in body
    assert "secret draft" not in r.text
