"""Tests for the input-type generator rework:
  - the catalog emits the fields group, group_icon and daemon
    and no longer emits category/icon/type
  - genesis stubs are always source_type 'genesis'
  - the ``duplicate_stack`` endpoint copies a whole stack one slot right
"""
from __future__ import annotations

import uuid
import pytest

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:generator-rework:channel"))


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = CHANNEL
        yield http



def _prepend_cell(client, stack_id: str, name: str, files: list[dict]) -> dict:
    """Insert a cell, accept its files, then move it onto the stack."""
    ch = client.post(f"/api/stacks/{CHANNEL}/{stack_id}/insert_blank_cell", json={"name": name}).json()
    idx = next(i for i, s in enumerate(ch["main_stacks"]) if s["id"] == stack_id)
    carrier = ch["main_stacks"][idx + 1]
    cell = carrier["cells"][0]
    if files:
        response = client.post(f"/api/channels/{CHANNEL}/cells/{cell['id']}/files/mutate", json={
            'operation_id': str(uuid.uuid4()), 'mutations': [dict(
                path=file['name'], kind='put', content_text=file['content'],
                writer_id='test-user', edit_seq=1, base_revision=0) for file in files]})
        assert response.status_code == 200, response.text
        assert response.json()['outcome'] == 'accepted'
    r = client.post(f"/api/stacks/{CHANNEL}/{carrier['id']}/move_cell_to/{idx}/stack",
                    params={"cell_id": cell["id"], "tgt_cell_index": 0})
    assert r.status_code == 200, r.text
    return r.json()


def test_catalog_emits_new_fields(client, test_generator):
    """group is the source cell's name; the capability flags default false;
    the removed fields (input, type/input_kind/category/icon) are all gone."""
    gens = client.get("/api/generators").json()
    g = next(x for x in gens if x["name"] == test_generator)
    assert "input" not in g
    assert g["group"] == "TestGenerators"
    assert g["group_icon"] is None
    assert g["daemon"] is False
    assert "type" not in g
    assert "input_kind" not in g
    assert "inputs_cells_min" not in g
    assert "inputs_cells_max" not in g
    assert "category" not in g
    assert "icon" not in g


def test_genesis_stub_is_always_source_type_genesis(client):
    """_ensure_genesis_row always writes source_type 'genesis' beside the
    run's other version rows."""
    from database import get_db_connection
    from process_manager.cells import CellsMixin

    class _FakeMgr(CellsMixin):
        # Subclasses the mixin (not a bare duck-type): _ensure_genesis_row
        # reaches sibling helpers (_provenance_for_write) through self.
        channel = CHANNEL
        run_id = "r-stub"
        generator_name = "demo-gen"

    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES ('r-stub', 'x', 'completed')")
        # A generator cell on the same run, which the stub leaves as it was.
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num, "
            "version_name, source_type) "
            "VALUES ('cap-1', 'r-stub', 'demo-gen', 1, 'Capture', 'generator')"
        )
        conn.commit()
    finally:
        conn.close()

    conn = get_db_connection(CHANNEL)
    try:
        CellsMixin._ensure_genesis_row(_FakeMgr(), conn, "genesis-stub")
        conn.commit()
        rows = dict(conn.execute(
            "SELECT id, source_type FROM cells WHERE run_id='r-stub'").fetchall())
    finally:
        conn.close()
    assert rows == {"genesis-stub": "genesis", "cap-1": "generator"}


def test_duplicate_stack_copies_all_cells(client):
    """duplicate_stack copies every cell of a stack into a fresh stack one
    slot to the right, leaving the original untouched."""
    ch = client.post(f"/api/stacks/{CHANNEL}/create").json()
    stack_id = ch["main_stacks"][-1]["id"]
    # Seed the stack with two named cells (the create seeds one "New Cell").
    _prepend_cell(client, stack_id, "Alpha", [])
    before = _prepend_cell(client, stack_id, "Beta", [])
    src = next(s for s in before["main_stacks"] if s["id"] == stack_id)
    src_idx = before["main_stacks"].index(src)
    src_names = [c["name"] for c in src["cells"]]

    after = client.post(f"/api/stacks/{CHANNEL}/{stack_id}/duplicate_stack").json()
    # A new stack sits immediately to the right; copied cells carry the
    # "(Copy)" suffix (same convention as duplicate_cell_in_stack).
    dup = after["main_stacks"][src_idx + 1]
    assert dup["id"] != stack_id
    assert [c["name"] for c in dup["cells"]] == [f"{n} (Copy)" for n in src_names]
    # Fresh cell ids — the copy is independent of the original.
    src_ids = {c["id"] for c in src["cells"]}
    assert all(c["id"] not in src_ids for c in dup["cells"])
    # Original stack is unchanged.
    still = next(s for s in after["main_stacks"] if s["id"] == stack_id)
    assert [c["name"] for c in still["cells"]] == src_names
