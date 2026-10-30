"""O03: the removed-cell record (removed_cells)."""
import json
from datetime import datetime, timezone

import pytest


@pytest.fixture
def conn(fresh_cwd):
    import database
    database._initialised_dbs.clear()
    from uuid import uuid4
    c = database.get_db_connection(str(uuid4()))
    yield c
    c.close()


def _cell(**overrides):
    from models import Cell, VirtualFile
    fields = {"name": "Notes", "icon": "BookOpen", "pinned": "a.md", "settings": {"depth": 2},
              "channel": "removed_channel", "created_at": datetime(2026, 10, 1, 9, 30),
              "updated_at": datetime(2026, 10, 2, 10, 0),
              "files": [VirtualFile(name="a.md", content="body")]}
    fields.update(overrides)
    return Cell(**fields)


def test_remember_get_and_forget_round_trip_O03(conn):
    import removed_cells
    cell = _cell()
    removed_cells.remember(conn, cell=cell, stack_name="Project", stack_cell_count=3,
                           removed_at=datetime(2026, 10, 3, 12, 0, 0, 999, tzinfo=timezone.utc),
                           reason="removed")
    conn.commit()
    record = removed_cells.get(conn, str(cell.id))
    assert record is not None
    assert (record.cell_id, record.name, record.icon, record.stack_name,
            record.stack_cell_count, record.pinned, record.reason) == (
        str(cell.id), "Notes", "BookOpen", "Project", 3, "a.md", "removed")
    assert not hasattr(record, "stack_type")
    assert record.removed_at == "2026-10-03T12:00:00+00:00"
    local = datetime(2026, 10, 1, 9, 30).astimezone().astimezone(timezone.utc).isoformat()
    assert record.created_at == local
    assert record.updated_at and record.updated_at.endswith("+00:00")
    assert record.display is None
    # The metadata restore rebuilds the layout cell from: the dump minus files.
    assert "files" not in record.metadata
    assert record.metadata["settings"] == {"depth": 2} and record.metadata["channel"] == "removed_channel"
    from models import Cell
    rebuilt = Cell.model_validate({**record.metadata, "files": []})
    assert rebuilt.created_at == cell.created_at and rebuilt.id == cell.id
    assert removed_cells.forget(conn, str(cell.id)) is True
    assert removed_cells.forget(conn, str(cell.id)) is False
    assert removed_cells.get(conn, str(cell.id)) is None


def test_merge_and_split_reasons_and_newest_first_listing_O03(conn):
    import removed_cells
    a, b, c = _cell(name="A"), _cell(name="B"), _cell(name="C")
    for cell, reason, at in ((a, "merged", "2026-10-03 10:00:00"), (b, "merged", "2026-10-03T11:00:00+00:00"),
                             (c, "split", "2026-10-03T12:00:00+00:00")):
        removed_cells.remember(conn, cell=cell, stack_name="Mix", stack_cell_count=2,
                               removed_at=at, reason=reason)
    assert [(r.name, r.reason) for r in removed_cells.list_removed(conn)] == [
        ("C", "split"), ("B", "merged"), ("A", "merged")]
    with pytest.raises(ValueError):
        removed_cells.remember(conn, cell=_cell(), stack_name=None, stack_cell_count=1,
                               removed_at="2026-10-03 10:00:00", reason="deleted")


def test_removing_again_replaces_the_record_O03(conn):
    import removed_cells
    cell = _cell()
    for name, count in (("First", 4), ("Second", 1)):
        cell.name = name
        removed_cells.remember(conn, cell=cell, stack_name="S", stack_cell_count=count,
                               removed_at="2026-10-03 10:00:00", reason="removed")
    rows = removed_cells.list_removed(conn)
    assert [(r.name, r.stack_cell_count) for r in rows] == [("Second", 1)]
    assert json.loads(conn.execute("SELECT metadata_json FROM removed_cells").fetchone()[0])["name"] == "Second"


def test_metadata_overrides_replace_keys_of_the_recorded_dump_only_TG03(conn):
    import removed_cells
    cell = _cell(tags=["Draft", "client"])
    removed_cells.remember(conn, cell=cell, stack_name="S", stack_cell_count=1,
                           removed_at="2026-10-03 10:00:00", reason="removed",
                           metadata_overrides={"tags": ["Draft", "later"]})
    record = removed_cells.get(conn, str(cell.id))
    assert record is not None
    assert record.tags == ["Draft", "later"] and record.metadata["tags"] == ["Draft", "later"]
    assert record.metadata["settings"] == {"depth": 2} and record.name == "Notes"
    assert cell.tags == ["Draft", "client"]
    removed_cells.remember(conn, cell=cell, stack_name="S", stack_cell_count=1,
                           removed_at="2026-10-03 11:00:00", reason="removed")
    assert removed_cells.get(conn, str(cell.id)).tags == ["Draft", "client"]
