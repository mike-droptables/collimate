"""Where each capability package of a cell came from.

Admission writes one record per package it admits into a cell, whatever
the source, when the package is published (``import_plan.ImportPlan.publish``
in the channel transaction, ``package_approval.add_to`` with its commit).
A record holds the source (:class:`Source`), the package's kind, manifest id
and manifest ``version``, the content hash of the package's files as the cell
holds them (``collimate._host.package_hash.content_hash``) and the SDK version
the package was admitted under. It is keyed by the cell and the package's
folder in it; a later admission into the same folder writes its own record
over the earlier one.

A record is never used to update anything. It is shown in a file's Info
(``GET …/cells/{cell_id}/provenance``, with the running SDK's version, so a
mismatch reads there and nothing else happens), travels in a zip export as
:data:`EXPORT_FILE` beside the cell's files, and keys the catalog's "already
in this channel" check (``routes/capabilities_stack._held``). An import
never inherits one: the importing admission records itself (``zip`` for a
zip), and an import drops the exported file. A cell without a record (an
existing channel's, or a cell written by anything but admission) has no
provenance.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# Where an admitted package came from: a suggested source (``bundled``,
# ``folder``, ``github``), a zip, another channel's Capabilities, or another
# cell of the channel (Add to).
SOURCE_KINDS = ("bundled", "folder", "github", "zip", "channel", "cell")
# The record a zip export writes into each exported cell that has one, beside
# its files; an import drops it.
EXPORT_FILE = ".collimate-provenance.json"

_FIELDS = ("folder", "source_kind", "source_id", "package_kind", "package_id", "package_version",
           "content_hash", "sdk_version", "admitted_at")


@dataclass(frozen=True)
class Source:
    """The source an admission records: its kind (one of
    :data:`SOURCE_KINDS`) and its id (the folder path, the release URL,
    ``"zip"``, the source channel's id or the source cell's id)."""
    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"Unknown provenance source kind {self.kind!r}")


ZIP = Source("zip", "zip")


def package_record(cell_id: str, folder: str, kind: str, manifest: dict, files: dict[str, bytes]) -> dict:
    """The record of one admitted package, without its source: the package
    folder ``folder`` of the cell ``cell_id``, its definition ``kind`` and
    ``manifest``, and ``files``, its folder-relative bytes exactly as the
    cell holds them."""
    from app_version import sdk_version
    from collimate._host.package_hash import content_hash
    from time_wire import now_iso
    version = manifest.get("version")
    return {
        "cell_id": cell_id, "folder": folder, "package_kind": kind, "package_id": manifest["id"],
        "package_version": None if version is None or isinstance(version, (dict, list)) else str(version),
        "content_hash": content_hash(files), "sdk_version": sdk_version(), "admitted_at": now_iso(),
    }


def write(conn, source: Source, records: list[dict]) -> None:
    """Store ``records`` (:func:`package_record`) as admitted from
    ``source``; the caller owns the transaction."""
    conn.executemany(
        "INSERT OR REPLACE INTO cell_provenance(cell_id, folder, source_kind, source_id, package_kind, "
        "package_id, package_version, content_hash, sdk_version, admitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r["cell_id"], r["folder"], source.kind, source.id, r["package_kind"], r["package_id"],
          r["package_version"], r["content_hash"], r["sdk_version"], r["admitted_at"]) for r in records])


def read(conn, cell_ids) -> dict[str, list[dict]]:
    """``{cell id: [record]}`` for the cells of ``cell_ids`` that have
    records, each cell's records ordered by folder."""
    ids = list(dict.fromkeys(str(cell_id) for cell_id in cell_ids))
    found: dict[str, list[dict]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            f"SELECT cell_id, {', '.join(_FIELDS)} FROM cell_provenance "
            f"WHERE cell_id IN ({','.join('?' * len(chunk))}) ORDER BY cell_id, folder", chunk)
        for row in rows:
            found.setdefault(row[0], []).append(dict(zip(_FIELDS, tuple(row)[1:])))
    return found


def cell_records(channel, cell_id: str) -> list[dict]:
    """The records of one cell of ``channel`` (none for a cell without)."""
    from database import get_db_connection
    from state import channel_db_name
    conn = get_db_connection(channel_db_name(channel))
    try:
        return read(conn, [cell_id]).get(str(cell_id), [])
    finally:
        conn.close()


def export_body(records: list[dict]) -> bytes:
    """:data:`EXPORT_FILE`'s bytes for a cell's records."""
    return (json.dumps({"provenance": records}, indent=2, sort_keys=True) + "\n").encode("utf-8")
