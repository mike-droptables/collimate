"""O03: the lineage record (cell_moves) and its indexed reads."""
import pytest
import uuid


@pytest.fixture
def conn(fresh_cwd):
    import database
    database._initialised_dbs.clear()
    c = database.get_db_connection(str(uuid.uuid4()))
    c.execute("INSERT INTO cells (id) VALUES ('v-a1')")
    c.execute("INSERT INTO cells (id) VALUES ('v-b1')")
    c.commit()
    yield c
    c.close()


def test_record_move_round_trips_with_wire_time_O03(conn):
    from cell_moves import CellMove, moves_in_channel, record_move
    first = record_move(conn, op="duplicate", op_id="sys-1", from_cell="a", from_version="v-a1", to_cell="a2")
    second = record_move(conn, op="run_cell", op_id="gen-1", from_cell="a", from_version="v-a1",
                         to_cell="a3", to_version=None)
    conn.commit()
    assert second > first
    rows = moves_in_channel(conn)
    assert [m.id for m in rows] == [first, second]
    move = rows[0]
    assert isinstance(move, CellMove)
    assert (move.op, move.op_id, move.from_cell, move.from_version, move.to_cell, move.to_version, move.ord) == (
        "duplicate", "sys-1", "a", "v-a1", "a2", None, 0)
    assert move.created_at.endswith("+00:00") and "T" in move.created_at


def test_merge_inputs_keep_their_order_and_split_outputs_share_an_op_O03(conn):
    from cell_moves import moves_for, record_move
    for i, (cell, version) in enumerate((("a", "v-a1"), ("b", "v-b1"))):
        record_move(conn, op="merge", op_id="m-1", from_cell=cell, from_version=version, to_cell="ab", ord=i)
    for out in ("x", "y"):
        record_move(conn, op="split", op_id="s-1", from_cell="ab", from_version=None, to_cell=out)
    record_move(conn, op="duplicate", op_id="d-1", from_cell="q", from_version=None, to_cell="q2")
    merged = moves_for(conn, ["ab"])
    assert [(m.op, m.from_cell, m.to_cell, m.ord) for m in merged] == [
        ("merge", "a", "ab", 0), ("merge", "b", "ab", 1), ("split", "ab", "x", 0), ("split", "ab", "y", 0)]
    assert {m.op_id for m in merged if m.op == "split"} == {"s-1"}
    # Both ends match; a move touching two asked cells comes back once.
    assert [m.to_cell for m in moves_for(conn, ["a", "ab"])] == ["ab", "ab", "x", "y"]
    assert moves_for(conn, []) == []
    assert [m.to_cell for m in moves_for(conn, ["q2"])] == ["q2"]


def test_unknown_op_is_refused_O03(conn):
    from cell_moves import record_move
    with pytest.raises(ValueError):
        record_move(conn, op="fork", op_id="r", from_cell="a", from_version=None, to_cell="b")


def test_moves_for_reads_through_the_indexes_O03(conn):
    from cell_moves import moves_for
    plans = []
    original = conn.execute

    class Spy:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT") and "cell_moves" in sql:
                plans.append(" ".join(str(tuple(r)) for r in original("EXPLAIN QUERY PLAN " + sql, params)))
            return original(sql, params)

    moves_for(Spy(), ["a", "b"])  # type: ignore[arg-type]
    assert len(plans) == 2
    assert "idx_cell_moves_from" in plans[0] and "idx_cell_moves_to" in plans[1]


def test_many_cell_ids_are_read_in_chunks_O03(conn):
    from cell_moves import moves_for, record_move
    ids = [f"c{i}" for i in range(1200)]
    for i, cid in enumerate(ids):
        record_move(conn, op="duplicate", op_id=f"d{i}", from_cell=cid, from_version=None, to_cell=f"{cid}-copy")
    assert len(moves_for(conn, ids)) == 1200
