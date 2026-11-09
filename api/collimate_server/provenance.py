"""Where each capability package of a cell came from: its upstream link.

Admission writes one record per package it admits into a cell, whatever
the door, when the package is published (``import_plan.ImportPlan.publish``
in the channel transaction, ``package_approval.add_to`` and
``definition_updates.replace`` with their commits). A record holds the
package's **upstream** (:class:`Upstream`: a kind and an id), its kind,
manifest id and manifest ``version``, the content hash of the package's
files as admission copied them (``collimate._host.package_hash.content_hash``
over ``package_import.admitted_files``) and the collimate version it was
admitted under (``app_version``). It is keyed by the cell and the package's
folder in it; a later admission into the same folder writes its own record
over the earlier one.

The upstream kinds:

- ``app``: the content installed with the app (id ``''``): a seed from a
  suggestion, Add from catalog of an app cell.
- ``catalog``: an entry of the user catalog (``user_catalog``; id the
  entry's id).
- ``channel``: another channel's Capabilities (id the channel's uuid): a
  new channel copied from it, Copy to channel.
- ``cell``: a cell of this channel (id the cell's uuid): Add to.
- ``zip``: a file (id ``''``): an import has no upstream.

The package's upstream copy is found by its kind and manifest id inside
the upstream (the app's content cells, the entry, that channel's
Capabilities). ``app``, ``catalog`` and ``channel`` are the upstreams a
cell follows (:data:`FOLLOWED`): when the upstream's copy of a cell's
packages, hashed the same way, differs from what the records hold, the cell
has an update (``definition_updates``). ``cell`` is not followed (Add to
with Replace is that package's own update path) and ``zip`` has nothing to
follow. Every record carries its hash (channel migration 30 made the column
required, 33 gave the table this shape); a cell with no record (moved onto
Capabilities, or in a channel older than migration 30) follows nothing.

Records are also shown in a file's Info (``GET …/cells/{cell_id}/provenance``,
with the running app's version), travel in a zip export as
:data:`EXPORT_FILE` beside the cell's files (with the export's notes on what
it left behind), and key the catalog's "already in this channel" check
(``routes/capabilities_stack._held``). An import never inherits one: the
importing admission records itself (``zip``), and an import drops the
exported file.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

UPSTREAM_KINDS = ("app", "catalog", "channel", "cell", "zip")
# The upstreams a cell follows for updates.
FOLLOWED = ("app", "catalog", "channel")
# The record a zip export writes into each exported cell that has one, beside
# its files; an import drops it.
EXPORT_FILE = ".collimate-provenance.json"

_FIELDS = ("folder", "upstream_kind", "upstream_id", "package_kind", "package_id", "package_version",
           "content_hash", "app_version", "admitted_at")


@dataclass(frozen=True)
class Upstream:
    """Where admitted packages came from: a kind of :data:`UPSTREAM_KINDS`
    and its id (``''`` for ``app`` and ``zip``, else the entry's, channel's
    or cell's id)."""
    kind: str
    id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in UPSTREAM_KINDS:
            raise ValueError(f"Unknown upstream kind {self.kind!r}")
        if (self.kind in ("app", "zip")) != (self.id == ""):
            raise ValueError(f"Upstream {self.kind!r} takes {'no' if self.kind in ('app', 'zip') else 'an'} id")


APP = Upstream("app")
ZIP = Upstream("zip")


def upstream_of(upstream: Upstream | Mapping[str, Upstream], cell_id: str) -> Upstream:
    """The upstream of the arriving cell ``cell_id``: ``upstream`` itself,
    or its entry for that cell."""
    return upstream if isinstance(upstream, Upstream) else upstream[cell_id]


def package_record(cell_id: str, folder: str, kind: str, manifest: dict, files: dict[str, bytes],
                   upstream: Upstream) -> dict:
    """The record of one admitted package from ``upstream``: the package
    folder ``folder`` of the cell ``cell_id``, its definition ``kind`` and
    ``manifest``, and ``files``, its folder-relative bytes exactly as the
    cell holds them."""
    from collimate_server.app_version import app_version
    from collimate._host.package_hash import content_hash
    from collimate_server.time_wire import now_iso
    version = manifest.get("version")
    return {
        "cell_id": cell_id, "folder": folder, "upstream_kind": upstream.kind, "upstream_id": upstream.id,
        "package_kind": kind, "package_id": manifest["id"],
        "package_version": None if version is None or isinstance(version, (dict, list)) else str(version),
        "content_hash": content_hash(files), "app_version": app_version(), "admitted_at": now_iso(),
    }


def write(conn, records: list[dict]) -> None:
    """Store ``records`` (:func:`package_record`); the caller owns the
    transaction."""
    conn.executemany(
        f"INSERT OR REPLACE INTO cell_provenance(cell_id, {', '.join(_FIELDS)}) "
        f"VALUES ({','.join('?' * (len(_FIELDS) + 1))})",
        [(r["cell_id"], *(r[field] for field in _FIELDS)) for r in records])


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


def channel_records(channel, cell_ids) -> dict[str, list[dict]]:
    """:func:`read` over ``channel``'s database."""
    from collimate_server.database import get_db_connection
    from collimate_server.state import channel_db_name
    conn = get_db_connection(channel_db_name(channel))
    try:
        return read(conn, cell_ids)
    finally:
        conn.close()


def cell_records(channel, cell_id: str) -> list[dict]:
    """The records of one cell of ``channel`` (none for a cell without)."""
    return channel_records(channel, [cell_id]).get(str(cell_id), [])


def export_body(records: list[dict], notes: Sequence[str] = ()) -> bytes:
    """:data:`EXPORT_FILE`'s bytes for a cell's records, with ``notes`` when
    the export left something of the cell behind (a repository's ``.git``
    or its stash)."""
    body: dict = {"provenance": records}
    if notes:
        body["notes"] = list(notes)
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")
