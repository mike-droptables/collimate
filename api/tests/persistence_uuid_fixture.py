"""Reusable populated pre-migration8 store for API and actual-browser gates.

Invoke with the API interpreter: ``persistence_uuid_fixture.py build DIRECTORY``
or ``inspect DIRECTORY``. Legacy filenames are explicit fixture paths only.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import NAMESPACE_URL, uuid5

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

CHANNEL_ID = str(uuid5(NAMESPACE_URL, "persistence-uuid-fixture:primary"))
OTHER_CHANNEL_ID = str(uuid5(NAMESPACE_URL, "persistence-uuid-fixture:other"))
CELL_ID = str(uuid5(NAMESPACE_URL, "persistence-uuid-fixture:cell"))
REMOVED_ID = str(uuid5(NAMESPACE_URL, "persistence-uuid-fixture:removed"))
LABEL = "Persistence probe"
LEGACY = "persistence_probe"


@contextmanager
def _cwd(directory):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


def _display(path):
    return {"version": 1, "root": {"type": "leaf", "id": "fixture-leaf",
        "tabs": [{"id": "fixture-tab", "kind": "file", "path": path}],
        "activeTabId": "fixture-tab"}, "activeLeafId": "fixture-leaf",
        "filesPane": {"collapsed": False, "side": "left", "width": None,
            "height": None, "viewMode": "tree", "panelFilters": {"runsOnly": False, "sortByRecency": False}},
        "runsPane": {"open": False, "side": "right", "width": None, "height": None},
        "pinnedTabCompact": False, "history": None}


def _encoded(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    return value


def _database(path):
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {}
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            table = row[0]
            if table == "schema_migrations" or table.startswith("sqlite_"):
                continue
            identifier = '"' + table.replace('"', '""') + '"'
            records = [{key: _encoded(value) for key, value in dict(record).items()}
                       for record in connection.execute(f"SELECT * FROM {identifier}")]
            records.sort(key=lambda record: json.dumps(record, sort_keys=True, ensure_ascii=False))
            tables[table] = records
        return tables
    finally:
        connection.close()


def inspect(directory) -> dict:
    """Record semantic rows and exact accepted/retained CAS bodies, not layout.

    Migration receipts and SQLite checkpoint mechanics are excluded. Channel
    keys remain UUIDs whether the physical file still has its old basename.
    """
    directory = Path(directory).resolve()
    with _cwd(directory):
        global_tables = _database(directory / ".collimate/app.db")
        row = next(row for row in global_tables["ui_state"] if row["key"] == "app_state")
        layout = json.loads(row["value"])
        channels = {}
        filenames = {}
        for channel in layout["channels"]:
            identity = channel["id"]
            target = directory / ".collimate/channels" / f"{identity}.db"
            legacy = "".join(c for c in channel["name"].lower().replace(" ", "_") if c.isalnum() or c in "_-")
            source = target if target.exists() else target.with_name(f"{legacy}.db")
            tables = _database(source)
            hashes = {value for rows in tables.values() for record in rows
                      for key, value in record.items() if key in ("hash", "body_hash", "blob_hash") and value}
            from cas import read_blob
            bodies = {}
            for digest in sorted(hashes):
                raw = read_blob(digest)
                if raw is None:
                    raise FileNotFoundError(f"Fixture CAS body is unavailable: {digest}")
                bodies[digest] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            channels[identity] = {"tables": tables, "bodies": bodies}
            filenames[identity] = source.name
        return {"channel_id": CHANNEL_ID, "cell_id": CELL_ID, "filename": "notes.py",
            "body": 'print("UUID persistence probe")\n', "label": LABEL,
            "legacy_databases": filenames,
            "semantic": {"global": global_tables, "channels": channels}}


def _leave_wal(directory):
    """Exit without SQLite cleanup so a committed fixture row remains in WAL."""
    script = """import os, sqlite3, sys
connection=sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute("INSERT INTO ui_state(key,value) VALUES('uuid_fixture_wal','committed before migration')")
connection.commit()
os._exit(0)
"""
    database = Path(directory) / ".collimate/channels" / f"{LEGACY}.db"
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    if not Path(str(database) + "-wal").exists():
        raise AssertionError("Populated UUID fixture must retain its committed source WAL")


def build(directory) -> dict:
    """Build global7 + legacy filenames with real seeded renderer capabilities."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".collimate").exists():
        raise FileExistsError("UUID fixture destination already contains an app store")
    with _cwd(directory):
        import state
        from cas import store_bytes
        from cell_display import write_display
        from cell_metadata import publish_cell_metadata
        from channel_settings import default_settings, save_channel_settings
        from database import get_db_connection, get_global_db_connection
        from genesis import bootstrap, ensure_capabilities_stack, save_global_ui_state, snapshot_app_state
        from seed_cells import seed_asset_channel
        from models import AppState, Cell, Channel, Stack, VirtualFile
        from persistence import mutate_files, publish_files
        from cell_catalog import remember_removed_cells
        previous_state = state.state
        try:
            bootstrap()
            body = 'print("UUID persistence probe")\n'
            cell = Cell(id=CELL_ID, name="Probe before rename", files=[
                VirtualFile(name="notes.py", content=body),
                VirtualFile(name="sibling.txt", content="durable sibling\n"),
                VirtualFile(name="empty.txt", content=""),
                VirtualFile(name="conflict.txt", content="base\n"),
                *[VirtualFile(name=path, language="binary", content="", blob_hash=store_bytes(raw))
                  for path, raw in [("raw.bin", b"\x00\xff\x80binary"), ("empty.bin", b""),
                                    ("utf8.bin", b"valid UTF-8 remains binary\n")]]])
            removed = Cell(id=REMOVED_ID, name="Removed fixture", files=[
                VirtualFile(name="shelf.txt", content="typed shelf history\n"),
                VirtualFile(name="shelf.bin", language="binary", content="", blob_hash=store_bytes(b"shelf\xff"))])
            channel = Channel(id=CHANNEL_ID, name=LABEL, main_stacks=[Stack(name="Persistence probe", cells=[cell, removed])])
            other = Channel(id=OTHER_CHANNEL_ID, name="Other migration probe")
            current = AppState(channels=[channel, other], active_channel_id=channel.id)
            state.state = current
            seed_asset_channel(CHANNEL_ID, channel)
            # The other channel holds no shipped cells: only its Capabilities
            # stack of built-in cards, and its database.
            ensure_capabilities_stack(other)
            get_db_connection(OTHER_CHANNEL_ID).close()
            cell = channel.main_stacks[0].cells[0]
            removed = channel.main_stacks[0].cells[1]
            connection = get_db_connection(CHANNEL_ID)
            try:
                write_display(connection, CELL_ID, _display("notes.py"))
                write_display(connection, REMOVED_ID, _display("shelf.txt"))
                for number, text, base, writer in [(1, "current\n", 1, "fixture-accepted"),
                                                   (2, "candidate\n", 1, "fixture-conflict")]:
                    mutate_files(CHANNEL_ID, CELL_ID, {"operation_id": str(uuid5(NAMESPACE_URL, f"uuid-fixture:{number}")),
                        "mutations": [{"kind": "put", "path": "conflict.txt", "content_text": text,
                            "base_revision": base, "writer_id": writer, "edit_seq": number,
                            "language": "plaintext"}], "resolves": []}, actor="host", conn=connection)
                connection.execute("INSERT INTO file_path_sequences VALUES (?, 'highwater-only.txt', 900)", (CELL_ID,))
                connection.execute("INSERT INTO file_writer_sequences VALUES (?, 'highwater-only.txt', 'retained-writer', 700)", (CELL_ID,))
                artifact = store_bytes(b"run configuration\n")
                connection.execute("INSERT OR IGNORE INTO blobs(hash,mime_type,size) VALUES (?, 'text/yaml', ?)", (artifact, len(b"run configuration\n")))
                run_id = str(uuid5(NAMESPACE_URL, "uuid-fixture:run"))
                connection.execute("INSERT INTO runs(id,prompt,status,kind,target_cell_id) VALUES (?, 'fixture run', 'completed', 'runner', ?)", (run_id, CELL_ID))
                connection.execute("INSERT INTO run_artifacts(run_id,category,filepath,blob_hash) VALUES (?, 'config', 'run.yaml', ?)", (run_id, artifact))
                connection.commit()
            finally:
                connection.close()
            publish_files(CHANNEL_ID, CELL_ID, {}, persist=False)
            remember_removed_cells(CHANNEL_ID, channel.main_stacks[0], [removed])
            channel.main_stacks[0].cells.remove(removed)
            settings = default_settings()
            settings["editing"]["spellcheck"] = True
            save_channel_settings(CHANNEL_ID, settings)
            save_global_ui_state(snapshot_app_state(current))
            publish_cell_metadata(CHANNEL_ID, CELL_ID, {"name": "Persistence probe"}, source_type="user", actor={"kind": "user"})
            # The startup's built-in bundles, so the migrated start adds no row.
            from builtin_packages import publish_bundles
            publish_bundles()
            global_connection = get_global_db_connection()
            try:
                global_connection.execute("DELETE FROM schema_migrations WHERE version=8")
                global_connection.commit()
                global_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                global_connection.close()
            for identity, basename in [(CHANNEL_ID, LEGACY), (OTHER_CHANNEL_ID, "other_migration_probe")]:
                source = directory / ".collimate/channels" / f"{identity}.db"
                connection = sqlite3.connect(source)
                try:
                    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    assert result[0] == 0
                finally:
                    connection.close()
                for suffix in ("-wal", "-shm"):
                    Path(str(source) + suffix).unlink(missing_ok=True)
                source.rename(source.with_name(f"{basename}.db"))
            _leave_wal(directory)
        finally:
            state.state = previous_state
    return inspect(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("build", "inspect"))
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()
    receipt = build(arguments.directory) if arguments.operation == "build" else inspect(arguments.directory)
    print(json.dumps(receipt, sort_keys=True))
