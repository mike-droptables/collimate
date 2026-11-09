"""The user catalog: definitions published from a channel, for any channel
to add, and the saved suggestions a new channel can start from.

Both live in the app database (``.collimate/app.db``, global migration 13),
beside the content installed with the app, which they never change.

**Entries.** Publish to catalog (:func:`plan_publish`, :func:`publish`)
writes a Capabilities cell into the catalog: its accepted files without its
runner packages (a runner is a channel's own setup; a saved suggestion
carries runner drafts) and without ``_runs/``, the provenance record of each
package (kind, id, version and the content hash as admission copies it, the
shape ``provenance`` records), the libraries its ``cell.yaml`` names under
``shared:``, and where it was published from. An entry is keyed by that
origin cell: publishing the same cell again republishes the entry (its
``revision`` goes up), and so does publishing a cell whose packages came
from the entry (an upstream ``catalog`` record naming it). A cell that came
from the catalog and is republished therefore updates the entry every other
channel follows.

**Adding an entry** (:func:`add_entry`) admits it onto a channel's
Capabilities as every door admits a cell, each package recording
``provenance.Upstream("catalog", <entry id>)``, so the channel follows the
entry: a republish shows as an update there (``definition_updates``). Each
library the entry names resolves on the channel as at a run (the channel's
Capabilities, else the app's); a name that resolves nowhere brings the
catalog entry holding that library along (its own names resolve the same
way), and a name no entry supplies refuses the add with the reason.

**Saved suggestions** (:func:`save_suggestion`) list catalog cells (entries,
or cells of the app's suggestions by their folder) and runner drafts taken
from a channel's runners. Saving resolves the libraries the listed entries
name the way an add does (a catalog library entry they need is listed too,
and reported); each draft's generator must be one a listed cell declares.
The New channel page shows them beside the app's suggestions
(:func:`offered_suggestions`) and creates a channel from one
(:func:`seed_saved`): app cells record the app as their upstream, entries
the catalog.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from collimate_server.models import Cell, Channel

# What publishing leaves out of a cell: what runs write.
_RUNS = "_runs/"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    """One published definition: its id, name and icon, where it was
    published from, the libraries its ``cell.yaml`` names, each package's
    record, its revision and when, and its files (cell path to bytes, with
    their languages and which are binary)."""
    id: str
    name: str
    icon: str | None
    origin_channel: str
    origin_cell: str
    libraries: list[str]
    packages: list[dict]
    revision: int
    app_version: str
    published_at: str
    bodies: dict[str, bytes] = field(default_factory=dict)
    languages: dict[str, str] = field(default_factory=dict)
    binary_paths: set[str] = field(default_factory=set)

    @property
    def section(self) -> str:
        """``generator``, ``renderer`` or ``library``: what the catalog lists
        it under (a cell with a generator is a generator cell, one with a
        renderer or handler a renderer cell)."""
        kinds = {p["kind"] for p in self.packages}
        if "generator" in kinds:
            return "generator"
        if kinds & {"renderer", "handler"}:
            return "renderer"
        return "library"

    def library_ids(self) -> set[str]:
        """The ids of the libraries the entry holds."""
        return {p["id"] for p in self.packages if p["kind"] == "library"}

    def cell(self, store) -> Cell:
        """The entry as a new cell, under a fresh id: its bodies are inline
        in app.db, so a binary one is written into ``store``, the body store
        of the channel it goes to (None: named by hash only, for a cell
        that is inspected and never committed)."""
        from collimate_server.package_approval import vfiles
        return Cell(id=uuid.uuid4(), name=self.name, icon=self.icon,
                    files=vfiles(self.bodies, store=store, languages=self.languages, binary_paths=self.binary_paths))

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "section": self.section, "libraries": self.libraries,
                "packages": [{"kind": p["kind"], "id": p["id"], "version": p["version"]} for p in self.packages],
                "revision": self.revision, "published_at": self.published_at, "app_version": self.app_version}


_ENTRY_FIELDS = ("id", "name", "icon", "origin_channel", "origin_cell", "libraries", "packages", "revision",
                 "app_version", "published_at")


def _conn():
    from collimate_server.database import get_global_db_connection
    return get_global_db_connection()


def _entry(row, *, files: bool, conn=None) -> Entry:
    entry = Entry(row["id"], row["name"], row["icon"], row["origin_channel"], row["origin_cell"],
                  json.loads(row["libraries"]), json.loads(row["packages"]), row["revision"], row["app_version"],
                  row["published_at"])
    if files and conn is not None:
        for path, body, binary, language in conn.execute(
                "SELECT path, body, binary, language FROM catalog_files WHERE entry_id=? ORDER BY path", (entry.id,)):
            entry.bodies[path] = bytes(body)
            entry.languages[path] = language
            if binary:
                entry.binary_paths.add(path)
    return entry


def entries(*, files: bool = False) -> list[Entry]:
    """Every entry, by name then id."""
    conn = _conn()
    try:
        rows = conn.execute(f"SELECT {', '.join(_ENTRY_FIELDS)} FROM catalog_entries ORDER BY name, id").fetchall()
        return [_entry(row, files=files, conn=conn) for row in rows]
    finally:
        conn.close()


def entry(entry_id: str, *, files: bool = True) -> Entry | None:
    conn = _conn()
    try:
        row = conn.execute(f"SELECT {', '.join(_ENTRY_FIELDS)} FROM catalog_entries WHERE id=?",
                           (entry_id,)).fetchone()
        return None if row is None else _entry(row, files=files, conn=conn)
    finally:
        conn.close()


def require_entry(entry_id: str) -> Entry:
    found = entry(entry_id)
    if found is None:
        raise HTTPException(404, "No such catalog entry")
    return found


def _write_entry(conn, item: Entry) -> None:
    conn.execute("DELETE FROM catalog_files WHERE entry_id=?", (item.id,))
    conn.execute(
        f"INSERT OR REPLACE INTO catalog_entries({', '.join(_ENTRY_FIELDS)}) VALUES ({','.join('?' * len(_ENTRY_FIELDS))})",
        (item.id, item.name, item.icon, item.origin_channel, item.origin_cell, json.dumps(item.libraries),
         json.dumps(item.packages), item.revision, item.app_version, item.published_at))
    conn.executemany("INSERT INTO catalog_files(entry_id, path, body, binary, language) VALUES (?,?,?,?,?)",
                     [(item.id, path, body, int(path in item.binary_paths), item.languages.get(path, "text"))
                      for path, body in sorted(item.bodies.items())])


def remove_entry(entry_id: str) -> None:
    """Take an entry out of the catalog; 404 for an unknown one, 409 while a
    saved suggestion lists it. Channels holding cells added from it keep
    them (they follow nothing from then on)."""
    require_entry(entry_id)
    listing = [s["name"] for s in suggestions() if {"source": "catalog", "id": entry_id} in s["items"]]
    if listing:
        raise HTTPException(409, f"The saved suggestion ‘{listing[0]}’ lists this entry; take it out there first")
    conn = _conn()
    try:
        conn.execute("DELETE FROM catalog_files WHERE entry_id=?", (entry_id,))
        conn.execute("DELETE FROM catalog_entries WHERE id=?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Publish to catalog
# ---------------------------------------------------------------------------

def _publishable(bodies: dict[str, bytes]) -> dict[str, bytes]:
    """A cell's files as the catalog keeps them: no runner package, nothing
    under ``_runs/``."""
    from collimate.definitions import discover_definitions
    prefixes = [p.prefix + "/" for p in discover_definitions(bodies).packages if p.kind == "runner"]
    return {path: body for path, body in bodies.items()
            if not path.startswith(_RUNS) and not any(path.startswith(prefix) for prefix in prefixes)}


def package_records(bodies: dict[str, bytes]) -> list[dict]:
    """Each package of ``bodies`` as an entry records it: kind, id, folder,
    version and the content hash as admission copies it."""
    from collimate_server.package_import import package_identities, package_rows
    hashes = {(kind, ident): digest for kind, ident, digest in package_identities(bodies)}
    return [{"kind": row["kind"], "id": row["id"], "folder": row["folder"], "version": row["version"],
             "content_hash": hashes.get((row["kind"], row["id"]))}
            for row in package_rows(bodies)]


def _republished(channel: Channel, cell: Cell, listed: list[Entry]) -> Entry | None:
    """The entry a publish of ``cell`` writes over: the one published from
    it, else the one its packages came from."""
    from collimate_server.provenance import cell_records
    from collimate_server.state import channel_db_name
    origin = (channel_db_name(channel), str(cell.id))
    found = next((e for e in listed if (e.origin_channel, e.origin_cell) == origin), None)
    if found is not None:
        return found
    upstreams = {r["upstream_id"] for r in cell_records(channel, str(cell.id)) if r["upstream_kind"] == "catalog"}
    by_id = {e.id: e for e in listed}
    return by_id[next(iter(upstreams))] if len(upstreams) == 1 and next(iter(upstreams)) in by_id else None


def library_rows(names: list[str], *, holds: set[str], listed: list[Entry], channel_cells=()) -> list[dict]:
    """Where each library ``names`` lists resolves for a channel that adds
    the definition: the definition itself (``holds``), the app, a catalog
    entry (``entry`` its name), else nowhere (``cell`` names this channel's
    Capabilities cell holding it, to publish too)."""
    import collimate_server.libraries as libraries
    shipped = libraries.bundled_ids()
    held_here = libraries.packages(list(channel_cells))
    rows = []
    for name in names:
        carrier = next((e for e in listed if name in e.library_ids()), None)
        if name in holds:
            rows.append({"id": name, "resolves": "self", "entry": None, "cell": None})
        elif name in shipped:
            rows.append({"id": name, "resolves": "app", "entry": None, "cell": None})
        elif carrier is not None:
            rows.append({"id": name, "resolves": "catalog", "entry": carrier.name, "cell": None})
        else:
            holders = held_here.get(name, [])
            rows.append({"id": name, "resolves": None, "entry": None,
                         "cell": holders[0][0].name if holders else None})
    return rows


@dataclass
class PublishPlan:
    cell: Cell
    bodies: dict[str, bytes]
    languages: dict[str, str]
    binary_paths: set[str]
    libraries: list[str]
    packages: list[dict]
    republishes: Entry | None
    review: dict


def plan_publish(channel: Channel, cell_id: str) -> PublishPlan:
    """What Publish to catalog writes for the Capabilities cell ``cell_id``:
    ``{name, entry, packages, libraries, unchanged, token}`` (``entry`` the
    entry it republishes, ``unchanged`` when that entry already holds these
    files). 400 for a cell with no package to publish."""
    import collimate_server.libraries as libraries
    from collimate_server.cell_copy import capability_cell, freeze, token_of
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.package_import import package_rows
    from collimate_server.state import state_lock

    cell = capability_cell(channel, cell_id)
    (frozen,) = freeze(channel, [cell])
    bodies = _publishable(frozen.bodies)
    packages = package_records(bodies)
    if not packages:
        raise HTTPException(400, "This cell holds no definition to publish")
    try:
        names = libraries.shared_of_bodies(bodies)
    except ValueError as exc:
        raise HTTPException(400, f"‘{cell.name}’: {exc}") from exc
    listed = entries(files=True)
    target = _republished(channel, cell, listed)
    with state_lock:
        here = [c.model_copy(deep=True) for c in capabilities_stack_cells(channel)]
    others = [e for e in listed if target is None or e.id != target.id]
    holds = libraries.ids_in_bodies(bodies)
    review = {
        "name": cell.name,
        "entry": None if target is None else target.public(),
        "unchanged": target is not None and target.bodies == bodies,
        "packages": package_rows(bodies),
        "libraries": library_rows(names, holds=holds, listed=others, channel_cells=here),
        "token": token_of([frozen]) + ("" if target is None else f":{target.revision}"),
    }
    return PublishPlan(cell, bodies, {p: frozen.languages.get(p, "text") for p in bodies},
                       {p for p in frozen.binary_paths if p in bodies}, names, packages, target, review)


def publish(channel: Channel, cell_id: str, token: str) -> dict:
    """Publish as :func:`plan_publish` reviewed it (409 when the cell or the
    entry changed since, or when the entry already holds these files):
    answers the entry."""
    from collimate_server.app_version import app_version
    from collimate_server.state import channel_db_name
    from collimate_server.time_wire import now_iso

    plan = plan_publish(channel, cell_id)
    if plan.review["token"] != token:
        raise HTTPException(409, "The cell or its catalog entry changed; review the publish again")
    if plan.review["unchanged"]:
        raise HTTPException(409, "The catalog already holds this cell as it is")
    previous = plan.republishes
    item = Entry(
        id=previous.id if previous else str(uuid.uuid4()), name=plan.cell.name, icon=plan.cell.icon,
        origin_channel=previous.origin_channel if previous else channel_db_name(channel),
        origin_cell=previous.origin_cell if previous else str(plan.cell.id),
        libraries=plan.libraries, packages=plan.packages, revision=(previous.revision + 1) if previous else 1,
        app_version=app_version(), published_at=now_iso(),
        bodies=plan.bodies, languages=plan.languages, binary_paths=plan.binary_paths)
    conn = _conn()
    try:
        current = conn.execute("SELECT revision FROM catalog_entries WHERE id=?", (item.id,)).fetchone()
        if (current[0] if current else None) != (previous.revision if previous else None):
            raise HTTPException(409, "The catalog entry changed; review the publish again")
        _write_entry(conn, item)
        conn.commit()
    finally:
        conn.close()
    return item.public()


# ---------------------------------------------------------------------------
# Adding entries
# ---------------------------------------------------------------------------

def with_libraries(chosen: list[Entry], available: set[str], listed: list[Entry]) -> tuple[list[Entry], list[str]]:
    """``chosen`` plus each catalog entry holding a library they name that
    neither ``available`` (the library ids the destination holds) nor the
    app nor ``chosen`` resolves, its own names resolved the same way; and
    the names nothing resolves."""
    import collimate_server.libraries as libraries
    shipped = libraries.bundled_ids()
    result = list(chosen)
    unresolved: list[str] = []
    queue = [name for e in chosen for name in e.libraries]
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name in available or name in shipped or any(name in e.library_ids() for e in result):
            continue
        carrier = next((e for e in listed if name in e.library_ids()), None)
        if carrier is None:
            unresolved.append(name)
            continue
        result.append(carrier)
        queue.extend(carrier.libraries)
    return result, unresolved


def _unresolved(owner: str, names: list[str]) -> str:
    quoted = ", ".join(f"‘{name}’" for name in names)
    return (f"{owner} names {quoted} under shared: in its cell.yaml, which neither this channel's "
            f"Capabilities, the app nor any catalog entry holds; publish that library to the catalog first")


def catalog_listing(channel: Channel) -> dict:
    """The catalog as a channel's Add from catalog lists it: ``{entries:
    [entry + held]}``, ``held`` exactly when :func:`add_entry` answers 409
    'Already in this channel'."""
    from collimate_server.package_import import capture_sources, declared_packages
    from collimate_server.routes.capabilities_stack import _admitted_identities, _held
    declared = declared_packages(capture_sources(channel)[0])
    admitted = _admitted_identities(channel)
    return {"entries": [item.public() | {"held": _held(item.bodies, declared, admitted)}
                        for item in entries(files=True)]}


def add_entry(channel: Channel, entry_id: str) -> dict:
    """Add the entry onto the end of ``channel``'s Capabilities, with the
    library entries it needs (:func:`with_libraries`). 404 for an unknown
    entry, 409 'Already in this channel' when the channel holds it, 400
    when a library resolves nowhere. Answers ``{cell_id, cell_ids,
    package_skips}``."""
    import collimate_server.libraries as libraries
    from collimate.definitions import discover_definitions
    from collimate_server.cell_lineage import announce_lineage
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.package_import import apply_packages, capture_sources, declared_packages, prepare_packages
    from collimate_server.provenance import Upstream
    from collimate_server.routes.capabilities_stack import _admitted_identities, _held
    from collimate_server.state import channel_db_name, persist_state, state_lock

    item = require_entry(entry_id)
    sources = capture_sources(channel)
    if _held(item.bodies, declared_packages(sources[0]), _admitted_identities(channel)):
        raise HTTPException(409, "Already in this channel")
    with state_lock:
        available = libraries.ids_on([c.model_copy(deep=True) for c in capabilities_stack_cells(channel)])
    chosen, unresolved = with_libraries([item], available, entries(files=True))
    if unresolved:
        raise ValueError(_unresolved(f"‘{item.name}’", unresolved))
    from collimate_server.cas import channel_store
    cells = [e.cell(channel_store(channel.id)) for e in chosen]
    upstreams = {str(cell.id): Upstream("catalog", e.id) for cell, e in zip(cells, chosen)}
    prepared = prepare_packages(channel, cells, upstream=upstreams, source_state=sources)
    prepared.cells = [c for c in prepared.cells if discover_definitions([f.name for f in c.files]).packages]
    if not any(str(c.id) == str(cells[0].id) for c in prepared.cells):
        reasons = "; ".join(dict.fromkeys(skip["reason"] for skip in prepared.skipped)) or "it holds no package"
        raise HTTPException(409, f"Nothing to add: {reasons}")
    report = apply_packages(channel, prepared)
    persist_state()
    ids = [str(c.id) for c in prepared.cells]
    announce_lineage(channel_db_name(channel), ids)
    return {"cell_id": ids[0], "cell_ids": ids, "package_skips": report["skipped"]}


# ---------------------------------------------------------------------------
# Saved suggestions
# ---------------------------------------------------------------------------

class AppItem(BaseModel):
    """A cell of the app's suggestions, by its section and folder (as Add
    from catalog lists it) and its name."""
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern="^app$")
    section: str = Field(pattern="^(generator|renderer|library)$")
    folder: str = Field(min_length=1)
    name: str = Field(min_length=1)


class CatalogItem(BaseModel):
    """A catalog entry, by id."""
    model_config = ConfigDict(extra="forbid")
    source: str = Field(pattern="^catalog$")
    id: str = Field(min_length=1)


class SuggestionBody(BaseModel):
    """A saved suggestion as the editor saves it: its name and description,
    its cells, and the runners of ``channel_id`` it offers as drafts."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=400)
    items: list[AppItem | CatalogItem] = Field(min_length=1, max_length=64)
    channel_id: str | None = None
    runner_ids: list[str] = Field(default_factory=list, max_length=64)


class SavedSelection(BaseModel):
    """The New channel page's choice of a saved suggestion: its id and the
    complete runner drafts ``{runner, files}`` it keeps."""
    model_config = ConfigDict(extra="forbid")
    id: str
    runners: list[dict] = Field(default_factory=list)


def _app_cell(item: dict, store) -> Cell:
    """An app item as a new cell (a generator cell without its runners)."""
    import collimate_server.runners as runners
    import collimate_server.suggested as suggested
    from collimate._host.packaging import CellRef
    from collimate._host.packaging.suggested import read_suggested_cell
    from collimate_server.channel_sources import specs_to_cells
    try:
        spec = read_suggested_cell(suggested.content_root(), CellRef(item["name"], item["folder"]), item["section"])
    except (OSError, ValueError) as exc:
        raise ValueError(f"The app no longer ships ‘{item['name']}’ ({exc})") from exc
    (cell,) = specs_to_cells([spec], store)
    if item["section"] == "generator":
        cell.files = runners.without_runners(cell.files)
    return cell


def _resolve_items(items: list[dict], listed: list[Entry]) -> tuple[list[dict], list[str]]:
    """``items`` with the catalog library entries their entries need
    appended, and the names of those added. Raises ``ValueError`` for an
    unknown entry or a library that resolves nowhere."""
    by_id = {e.id: e for e in listed}
    chosen = []
    for item in items:
        if item["source"] == "catalog":
            if item["id"] not in by_id:
                raise ValueError("A listed catalog entry is no longer in the catalog")
            chosen.append(by_id[item["id"]])
    app_libraries = {item["folder"].rsplit("/", 1)[-1] for item in items if item["source"] == "app"
                     and item["section"] == "library"}
    full, unresolved = with_libraries(chosen, app_libraries, listed)
    if unresolved:
        raise ValueError(_unresolved("A listed entry", unresolved))
    added = [e for e in full if e not in chosen]
    return items + [{"source": "catalog", "id": e.id} for e in added], [e.name for e in added]


def _item_cells(items: list[dict], listed: list[Entry], store=None) -> list[tuple[Cell, object]]:
    """Each item as a new cell with the upstream it records."""
    from collimate_server.provenance import APP, Upstream
    by_id = {e.id: e for e in listed}
    cells = []
    for item in items:
        if item["source"] == "app":
            cells.append((_app_cell(item, store), APP))
        else:
            found = by_id.get(item["id"])
            if found is None:
                raise ValueError("A listed catalog entry is no longer in the catalog")
            cells.append((found.cell(store), Upstream("catalog", found.id)))
    return cells


def _drafts(channel_id: str | None, runner_ids: list[str], cells: list[Cell]) -> list[dict]:
    """The runners ``runner_ids`` of the channel as drafts ``{runner,
    files}``, each of a generator one of ``cells`` declares."""
    import collimate_server.runners as runners
    from collimate_server.package_import import declared_definition_ids
    from collimate_server.state import require_channel
    if not runner_ids:
        return []
    if channel_id is None:
        raise ValueError("Name the channel the runners come from")
    channel = require_channel(channel_id)
    generators = {ident for cell in cells for kind, ident in declared_definition_ids(cell.files) if kind == "generator"}
    drafts = []
    for runner_id in dict.fromkeys(runner_ids):
        try:
            cell, _, data = runners.find_runner(channel, runner_id)
            files = runners.runner_files(cell, runner_id)
        except runners.RunnerError as exc:
            raise ValueError(exc.reason) from exc
        if data.get("generator") not in generators:
            raise ValueError(f"The runner ‘{data.get('name', runner_id)}’ runs ‘{data.get('generator')}’, "
                             "which no listed cell holds")
        drafts.append({"runner": data, "files": files})
    return drafts


def _suggestion_row(row) -> dict:
    return {"id": row["id"], "name": row["name"], "description": row["description"],
            "items": json.loads(row["items"]), "runners": json.loads(row["runners"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def suggestions() -> list[dict]:
    """Every saved suggestion as stored, oldest first."""
    conn = _conn()
    try:
        return [_suggestion_row(row) for row in conn.execute(
            "SELECT * FROM saved_suggestions ORDER BY created_at, id")]
    finally:
        conn.close()


def _suggestion(suggestion_id: str) -> dict:
    found = next((s for s in suggestions() if s["id"] == suggestion_id), None)
    if found is None:
        raise HTTPException(404, "No such saved suggestion")
    return found


def save_suggestion(body: SuggestionBody, suggestion_id: str | None = None) -> dict:
    """Save a new suggestion, or (``suggestion_id``) replace one: answers it
    with ``added``, the names of the catalog library entries it lists
    because its entries need them. 400 for an item or runner it cannot
    hold, 404 for an unknown suggestion."""
    from collimate_server.time_wire import now_iso
    previous = None if suggestion_id is None else _suggestion(suggestion_id)
    listed = entries(files=True)
    raw = [item.model_dump() for item in body.items]
    unique = list({json.dumps(item, sort_keys=True): item for item in raw}.values())
    items, added = _resolve_items(unique, listed)
    cells = [cell for cell, _ in _item_cells(items, listed)]
    drafts = _drafts(body.channel_id, body.runner_ids, cells)
    now = now_iso()
    saved = {"id": suggestion_id or str(uuid.uuid4()), "name": body.name.strip(), "description": body.description.strip(),
             "items": items, "runners": drafts, "created_at": previous["created_at"] if previous else now,
             "updated_at": now}
    conn = _conn()
    try:
        conn.execute("INSERT OR REPLACE INTO saved_suggestions(id, name, description, items, runners, created_at, "
                     "updated_at) VALUES (?,?,?,?,?,?,?)",
                     (saved["id"], saved["name"], saved["description"], json.dumps(items), json.dumps(drafts),
                      saved["created_at"], saved["updated_at"]))
        conn.commit()
    finally:
        conn.close()
    return saved | {"added": added}


def delete_suggestion(suggestion_id: str) -> None:
    _suggestion(suggestion_id)
    conn = _conn()
    try:
        conn.execute("DELETE FROM saved_suggestions WHERE id=?", (suggestion_id,))
        conn.commit()
    finally:
        conn.close()


def offered_suggestions() -> list[dict]:
    """Every saved suggestion in the shape the New channel page reads an
    app suggestion in (``suggested._entry``: its cells by section, the
    runner generators and the drafts, each draft's quick fields its
    generator's account roles), with ``saved``, its ``items`` and a
    ``problem`` instead of the cells when one no longer reads (an entry
    gone, a cell the app stopped shipping)."""
    import collimate_server.runners as runners
    listed = entries(files=True)
    offered = []
    for saved in suggestions():
        base = {"id": saved["id"], "name": saved["name"], "description": saved["description"], "saved": True,
                "items": saved["items"], "generators": [], "renderers": [], "libraries": [], "tutorial": [],
                "runner_generators": [], "runners": [], "problem": None}
        try:
            cells = [cell for cell, _ in _item_cells(saved["items"], listed)]
        except ValueError as exc:
            offered.append(base | {"problem": str(exc)})
            continue
        index = runners.generator_index(cells)
        sections: dict[str, list] = {"generator": [], "renderer": [], "library": []}
        for item, cell in zip(saved["items"], cells):
            section = item["section"] if item["source"] == "app" else next(
                e.section for e in listed if e.id == item["id"])
            sections[section].append({"name": cell.name, "folder": item.get("folder") or item["id"]})
        drafts = []
        for draft in saved["runners"]:
            generator = index.get(draft["runner"].get("generator"))
            roles = generator["manifest"].get("account_requirements", {}) if generator else {}
            drafts.append({"draft": draft, "quick_fields": [{"account": role} for role in roles]})
        offered.append(base | {"generators": sections["generator"], "renderers": sections["renderer"],
                               "libraries": sections["library"],
                               "runner_generators": [runners.generator_payload(gi) for gi in index.values()],
                               "runners": drafts})
    return offered


def seed_saved(ch: Channel, selection: SavedSelection) -> list[dict]:
    """Seed the unregistered channel ``ch`` from a saved suggestion: its
    cells admitted onto Capabilities together with the kept drafts written
    into their generators' cells (``suggested._with_drafts``), app cells
    recording the app as their upstream and entries the catalog. Returns
    the held packages."""
    from collimate_server.channel_sources import admission_errors, admit_packages
    from collimate_server.import_plan import ImportPlan
    from collimate_server.suggested import _with_drafts

    saved = _suggestion(selection.id)
    with admission_errors():
        from collimate_server.cas import channel_store
        store = channel_store(ch.id)
        pairs = _item_cells(saved["items"], entries(files=True), store)
        incoming = [cell for cell, _ in pairs]
        _with_drafts(store, incoming, selection.runners)
        plan = ImportPlan(ch, new_channel=True)
        skipped = admit_packages(ch, plan, incoming, {str(cell.id): upstream for cell, upstream in pairs})
        plan.publish()
    return skipped
