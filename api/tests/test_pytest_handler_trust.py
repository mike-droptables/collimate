"""A handler build is in use only when its ``derived:`` hash resolves to a
stored acceptance (Review and accept). Reading the catalog never accepts a
build, so a cell whose own bytes declare their build (moved in, edited,
imported) is not trusted by that alone.

Synthetic fixtures only: a 'memo' handler on '.memo'.
"""
from __future__ import annotations

import yaml

from conftest import add_capabilities_cell, install_handler
from test_pytest_handlers import client as client, write_handler_files

DERIVED = {"verify.ts": b"export default () => ({valid:true});",
           "skills/memo/SKILL.md": b"# Write a memo"}


def memo_files(derived=DERIVED, *, declare=True) -> dict[str, bytes]:
    """The package files of a 'memo' handler, ``derived:`` declared to its
    own ``derived/`` folder when ``declare``."""
    from collimate.handlers import derived_hash
    manifest = {"id": "memo", "name": "Memo", "description": "Synthetic memos", "guidance": "A title line",
                "types": [{"ext": ".memo"}], "examples": []}
    if declare:
        manifest["derived"] = derived_hash(derived)
    return {"memo.handler": yaml.safe_dump(manifest, sort_keys=False).encode(),
            **{"derived/" + path: body for path, body in derived.items()}}


def memo_cell(files):
    from collimate_server.models import Cell, VirtualFile
    return Cell(name="Memo", files=[VirtualFile(name="memo/" + path, content=body.decode())
                                    for path, body in files.items()])


def stored_rows(identity: str) -> int:
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM handler_derived_files WHERE derived_hash=?",
                            (identity,)).fetchone()[0]
    finally:
        conn.close()


def test_a_self_declared_build_moved_onto_capabilities_is_not_in_use(client):
    """The cell's own ``derived:`` line names its own folder, but nothing
    accepted it: on the board it is no handler, moved onto Capabilities it
    is listed unaccepted, and no read stores it."""
    from collimate_server.cell_placement import capabilities_stack
    from collimate_server.database import get_db_connection
    from collimate_server.handlers import catalog
    from collimate_server.models import Stack
    from collimate_server.persistence import initialize_files
    from collimate_server.state import find_active_channel
    channel = find_active_channel()
    files = memo_files()
    identity = yaml.safe_load(files["memo.handler"])["derived"]
    cell = memo_cell(files)
    with get_db_connection(str(channel.id)) as conn:
        initialize_files(conn, str(cell.id), cell.files)
    board = Stack(name="Board", cells=[cell])
    channel.main_stacks.append(board)
    assert catalog(channel).for_path("x.memo") is None

    board.cells.remove(cell)
    capabilities_stack(channel).cells.append(cell)
    record = catalog(channel).for_path("x.memo")
    assert record is not None and record.cell_id == str(cell.id)
    assert record.accepted_files == {}
    assert record.public()["accepted"] is False and record.public()["accepted_derived"] is None
    assert catalog(channel).skills(".memo") == {}
    assert stored_rows(identity) == 0


def test_an_edit_that_declares_its_own_new_build_is_not_in_use(client):
    """An accepted handler whose verifier and ``derived:`` line are rewritten
    in place keeps nothing in use: the new hash resolves nowhere."""
    from collimate.handlers import derived_hash
    from collimate_server.handlers import catalog
    from collimate_server.state import find_active_channel
    channel = find_active_channel()
    cell, manifest, derived = install_handler(channel, identity="memo", ext=".memo")
    assert catalog(channel).for_path("x.memo").public()["accepted"] is True

    rewritten = dict(derived, **{"verify.ts": b"export default () => ({valid:false,error:'mine'});"})
    identity = derived_hash(rewritten)
    write_handler_files(channel, cell, {
        "memo/derived/verify.ts": rewritten["verify.ts"].decode(),
        "memo/choice.handler": yaml.safe_dump({**manifest, "derived": identity})})
    record = catalog(channel).for_path("x.memo")
    assert record.accepted_files == {}
    assert record.public()["accepted"] is False
    assert stored_rows(identity) == 0


def test_review_and_accept_puts_the_build_in_use(client):
    from collimate_server.handler_build import accept_build, inspect_build
    from collimate_server.handlers import catalog
    from collimate_server.state import find_active_channel
    channel = find_active_channel()
    files = memo_files(declare=False)
    cell = memo_cell(files)
    add_capabilities_cell(channel, cell)
    assert catalog(channel).for_path("x.memo").public()["accepted"] is False

    result, _ = inspect_build(channel, str(cell.id), "memo")
    assert result["compile"]["valid"], result
    assert accept_build(channel, str(cell.id), "memo", result["content_hash"])["accepted"]
    record = catalog(channel).for_path("x.memo")
    assert record.public()["accepted"] is True
    assert record.accepted_files == DERIVED
    assert stored_rows(result["derived_hash"]) == len(DERIVED)


def test_a_build_accepted_for_this_handler_is_in_use(client):
    """A row of the global table accepted for this handler's id and hash puts
    its build in use."""
    from collimate_server.handlers import catalog, save_derived_skill
    from collimate_server.state import find_active_channel
    channel = find_active_channel()
    files = memo_files()
    identity = yaml.safe_load(files["memo.handler"])["derived"]
    save_derived_skill("memo", identity, DERIVED)
    add_capabilities_cell(channel, memo_cell(files))
    record = catalog(channel).for_path("x.memo")
    assert record.public()["accepted"] is True and record.accepted_files == DERIVED


def test_a_build_accepted_for_another_handler_is_not_in_use(client):
    """An acceptance belongs to the handler it was given to: a handler whose
    ``derived:`` line names the very hash another handler's build was
    accepted under (a moved or hand-edited line) puts nothing in use."""
    from collimate_server.handlers import accepted_derived, catalog, save_derived_skill
    from collimate_server.state import find_active_channel
    channel = find_active_channel()
    files = memo_files()
    identity = yaml.safe_load(files["memo.handler"])["derived"]
    save_derived_skill("other", identity, DERIVED)
    assert accepted_derived("other", identity) == DERIVED and accepted_derived("memo", identity) == {}
    add_capabilities_cell(channel, memo_cell(files))
    record = catalog(channel).for_path("x.memo")
    assert record.public()["accepted"] is False and record.accepted_files == {}
