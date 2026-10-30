"""Provenance (``provenance``): one record per admitted package, written by
admission for every source, shown through ``GET …/cells/{cell_id}/provenance``,
carried by a zip export and never inherited by an import.

The gate of the content-path plan: a renderer cell of the bundled content,
exported as a packages zip and imported into another channel, gets the same
content hash, the same provenance shape (kind ``zip``) and the same renderer
listing and stored bundle as the seeded one. The renderer cell is the first
Capabilities cell holding a renderer, whichever that is.
"""
from __future__ import annotations

import json

from conftest import create_suggested_channel
from suggested_trees import build_tree, create_suggested, folder_hash, provenance, use_source

# What a record says about the package, as against about its admission.
_PACKAGE = ("folder", "package_kind", "package_id", "package_version", "content_hash", "sdk_version")


def _channel(channel_id):
    from state import require_channel
    return require_channel(channel_id)


def _capability_cells(channel_id):
    from cell_placement import capabilities_stack_cells
    return capabilities_stack_cells(_channel(channel_id))


def _user_renderers(client) -> dict:
    return {row["id"]: row for row in client.get("/api/renderers").json() if not row["builtin"]}


def _files(cell) -> dict:
    return {f.name: (f.content, f.blob_hash) for f in cell.files}


def test_a_shipped_renderer_reimported_from_a_zip_matches_the_seeded_one(client):
    from cas import read_blob
    from database import get_global_db_connection
    from provenance import EXPORT_FILE

    seeded_id = create_suggested_channel(client, "Seeded")
    seeded = next(c for c in _capability_cells(seeded_id)
                  if any(r["package_kind"] == "renderer" for r in provenance(seeded_id, c.id)))
    records = provenance(seeded_id, seeded.id)
    assert {(r["source_kind"], r["source_id"]) for r in records} == {
        ("bundled", client.get("/api/suggested").json()["source"]["path"])}
    renderer_ids = [r["package_id"] for r in records if r["package_kind"] == "renderer"]
    listed = _user_renderers(client)

    exported = client.get(f"/api/channels/{seeded_id}/export.zip", params={"scope": "packages"})
    assert exported.status_code == 200, exported.text
    expansion = client.post("/api/import/zip", content=exported.content)
    assert expansion.status_code == 200, expansion.text
    prefix = f"Seeded/Capabilities/{seeded.name}/"
    entries = [e for e in expansion.json()["entries"] if e["path"].startswith(prefix)]
    # The zip carries the cell's records beside its files.
    (carried,) = [e for e in entries if e["path"] == prefix + EXPORT_FILE]
    assert json.loads(read_blob(carried["blob_hash"]))["provenance"] == records

    response = client.post("/api/channels", params={"name": "Imported"}, json={"config": {"packages": {"cells": [
        {"name": seeded.name, "files": [{"path": e["path"][len(prefix):], "blob_hash": e["blob_hash"]}
                                        for e in entries]}]}}})
    assert response.status_code == 200, response.text
    imported_id = response.json()["id"]
    (imported,) = _capability_cells(imported_id)
    assert imported.id != seeded.id and imported.name == seeded.name
    # The same files (the carried record is not one of them) ...
    assert _files(imported) == _files(seeded) and EXPORT_FILE not in _files(imported)
    # ... the same records of the same packages, admitted from a zip ...
    again = provenance(imported_id, imported.id)
    assert [{k: r[k] for k in _PACKAGE} for r in again] == [{k: r[k] for k in _PACKAGE} for r in records]
    assert {(r["source_kind"], r["source_id"]) for r in again} == {("zip", "zip")}
    answer = client.get(f"/api/channels/{imported_id}/cells/{imported.id}/provenance").json()
    assert answer["records"] == again and answer["sdk_version"] == records[0]["sdk_version"]
    # ... and the same renderers, each with its server-compiled bundle.
    relisted = _user_renderers(client)
    conn = get_global_db_connection()
    try:
        for renderer_id in renderer_ids:
            row, before = relisted[renderer_id], listed[renderer_id]
            assert (row["cell_id"], before["cell_id"]) == (str(imported.id), str(seeded.id))
            assert {**row, "cell_id": None} == {**before, "cell_id": None}
            origin = conn.execute("SELECT origin FROM renderer_bundles WHERE source_hash=?",
                                  (row["content_hash"],)).fetchone()
            assert origin is not None and origin[0] == "server"
    finally:
        conn.close()


def test_the_route_answers_the_records_and_the_running_sdk(client, tmp_path):
    from app_version import sdk_version
    root = build_tree(tmp_path / "sdk")
    use_source(client, root)
    channel_id = create_suggested(client, "Folder").json()["id"]
    views = next(c for c in _capability_cells(channel_id) if c.name == "Views")
    answer = client.get(f"/api/channels/{channel_id}/cells/{views.id}/provenance")
    assert answer.status_code == 200, answer.text
    (record,) = answer.json()["records"]
    assert answer.json()["sdk_version"] == sdk_version() == record["sdk_version"]
    assert {k: record[k] for k in ("folder", "source_kind", "source_id", "package_kind", "package_id")} == {
        "folder": "widget", "source_kind": "folder", "source_id": str(root),
        "package_kind": "renderer", "package_id": "widget"}
    assert record["content_hash"] == folder_hash(root / "renderers" / "views" / "widget")
    # The tutorial cell was never admitted: it has no provenance.
    tour = _channel(channel_id).main_stacks[0].cells[0]
    assert client.get(f"/api/channels/{channel_id}/cells/{tour.id}/provenance").json()["records"] == []
    assert client.get(f"/api/channels/{'0' * 8}-0000-4000-8000-{'0' * 12}/cells/{tour.id}/provenance"
                      ).status_code == 404


def test_a_channel_copy_records_the_source_channel(client, tmp_path):
    use_source(client, build_tree(tmp_path / "sdk"))
    source_id = create_suggested(client, "Source", tutorial=False).json()["id"]
    response = client.post("/api/channels", params={"name": "Copy"},
                           json={"config": {"packages": {"channel_id": source_id}}})
    assert response.status_code == 200, response.text
    copy_id = response.json()["id"]
    pairs = list(zip(_capability_cells(source_id), _capability_cells(copy_id), strict=True))
    assert pairs
    for original, copy in pairs:
        before, after = provenance(source_id, original.id), provenance(copy_id, copy.id)
        assert [{k: r[k] for k in _PACKAGE} for r in after] == [{k: r[k] for k in _PACKAGE} for r in before]
        assert {(r["source_kind"], r["source_id"]) for r in after} == {("channel", source_id)}


def test_an_existing_channel_without_records_shows_none(client):
    """A cell written by anything but admission (an older channel's cells,
    a runner editor's new cell) has no provenance, and a zip export of it
    carries no record."""
    from conftest import add_capabilities_cell
    from models import Cell, VirtualFile
    from provenance import EXPORT_FILE
    from state import persist_state
    cell = Cell(name="Older", files=[VirtualFile(name="notes.md", content="kept\n")])
    add_capabilities_cell(_channel(client.channel_id), cell)
    persist_state()
    answer = client.get(f"/api/channels/{client.channel_id}/cells/{cell.id}/provenance").json()
    assert answer["records"] == []
    manifest = client.get(f"/api/channels/{client.channel_id}/export/manifest",
                          params={"scope": "cell", "cell_id": str(cell.id)}).json()
    assert [e["path"] for e in manifest["entries"]] == ["Older/notes.md"]
    assert not any(e["path"].endswith(EXPORT_FILE) for e in manifest["entries"])
