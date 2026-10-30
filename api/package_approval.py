"""Package admission into the Capabilities stack, shared by Add to (a
reviewed package from any cell) and imports (``package_import``)."""

from __future__ import annotations
from dataclasses import dataclass, field
import hashlib
import json
import time
import yaml
from fastapi import HTTPException
from collimate.definitions import discover_definitions
from collimate._host.package_hash import content_hash


class PackageConflict(ValueError):
    """A held definition id or handler type; imports report these as skips."""


@dataclass
class CellSnapshot:
    cell_id: str
    tip: str
    refs: dict
    languages: dict
    binary_paths: set[str] = field(default_factory=set)

    def bytes(self):
        from cas import read_blob

        result = {}
        for path, (kind, value) in self.refs.items():
            if kind == "text":
                result[path] = value.encode("utf-8")
            else:
                body = read_blob(value)
                if body is None or hashlib.sha256(body).hexdigest() != value:
                    raise ValueError("Source object missing or damaged: " + path)
                result[path] = body
        return result


def snapshot_cell(channel, cell_id: str) -> CellSnapshot:
    """Caller holds the source-aware cell/state scope; no mutable file aliases."""
    from cell_placement import find_cell
    from database import get_db_connection
    from state import channel_db_name
    from versions import find_tip

    cell = find_cell(channel, cell_id)[1]
    if cell is None:
        raise HTTPException(404, "Cell not found")
    conn = get_db_connection(channel_db_name(channel))
    try:
        tip = find_tip(conn, cell_id)
        from persistence import snapshot_files
        accepted=snapshot_files(conn,cell_id)
        refs={f['path']:('blob',f['body_hash']) for f in accepted}
        binary_paths={f['path'] for f in accepted if f['encoding']=='binary'}
        return CellSnapshot(
            cell_id, tip, refs, {f['path']: f['language'] for f in accepted}, binary_paths
        )
    finally:
        conn.close()


def package_snapshot(snapshot: CellSnapshot, definition_path: str):
    discovery = discover_definitions(snapshot.refs)
    package = next(
        (p for p in discovery.packages if p.definition_path == definition_path), None
    )
    if package is None:
        raise ValueError("Select one definition inside an unambiguous package folder")
    prefix = package.prefix + "/"
    selected = CellSnapshot(
        snapshot.cell_id,
        snapshot.tip,
        {p: r for p, r in snapshot.refs.items() if p.startswith(prefix)},
        snapshot.languages,
        snapshot.binary_paths,
    )
    return package, {p[len(prefix) :]: b for p, b in selected.bytes().items()}


def admit_package(channel, definition_path: str, files: dict[str, bytes]) -> dict:
    """Validate one package's exact relative bytes; no cell or availability mutation.

    The verifier is always the app's built-in handler of the definition's
    type. Imports supply their final bytes and then check ids and types
    against the whole proposed Capabilities stack (``check_target``).

    A renderer is compiled, and the result's ``bundle`` is ``(source hash,
    bytes)``: keyed by the source hash of exactly these package-relative
    bytes, the identity the source route computes for the cell that holds
    them. The caller that publishes the package stores it as the cell's
    server bundle (``renderer_bundles.publish_server_bundle``); a package
    that is not published stores nothing. Other kinds carry ``None``.
    """
    from builtin_packages import verifier_for
    from collimate.handlers import validate_handler
    from verification import verify_source, verifier_input
    from collimate._host.renderer_bundle import bundle_renderer
    from collimate.lint import lint_renderer_manifest_data, Level
    from cell_placement import capabilities_stack_cells
    from runners import generator_index, validate_definition
    from collimate.fs import relative_parts

    for path in files:
        if "/".join(relative_parts(path)) != path or "_runs" in path.split("/"):
            raise ValueError("Invalid package path: " + path)
    name = definition_path.rsplit("/", 1)[-1]
    kind = name.rsplit(".", 1)[-1]
    if kind not in ("handler", "renderer", "runner", "generator") or name not in files:
        raise ValueError("Invalid definition")
    for path in files:
        if path != name and path.rsplit(".", 1)[-1] in (
            "handler",
            "renderer",
            "runner",
            "generator",
        ):
            if not (kind == "handler" and path.startswith("examples/")):
                raise ValueError("Package contains a second definition")
    manifest = yaml.safe_load(files[name])
    if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str):
        raise ValueError("Definition needs an id")
    if kind == "handler":
        manifest = validate_handler(manifest)
    verifier = verifier_for(definition_path)
    if verifier is None:
        raise ValueError("No accepted verifier for ." + kind)
    value = verifier_input(
        definition_path,
        {definition_path.rpartition("/")[0] + "/" + p: b for p, b in files.items()},
        [definition_path.rpartition("/")[0] + "/" + p for p in files],
    )
    verdict = verify_source(verifier, value, deadline=time.monotonic() + 30)
    if not verdict["valid"]:
        raise ValueError(verdict.get("error", "Definition refused"))
    bundle = None
    if kind == "renderer":
        errors = [
            i.message
            for i in lint_renderer_manifest_data(manifest, files_present=set(files))
            if i.level == Level.ERROR
        ]
        if errors:
            raise ValueError("; ".join(errors))
        compiled = bundle_renderer(
            files, entrypoint=manifest["entrypoint"], deadline=time.monotonic() + 5
        )
        bundle = (compiled.source_hash, compiled.code.encode("utf-8"))
    if kind == "runner":
        index = generator_index(capabilities_stack_cells(channel))
        target = index.get(manifest.get("generator"))
        if target is None:
            raise ValueError("Add " + str(manifest.get("generator")) + " first")
        import base64

        rows = [
            {
                "name": p[len("files/") :],
                "content": base64.b64encode(b).decode("ascii"),
                "encoding": "base64",
            }
            for p, b in files.items()
            if p.startswith("files/")
        ]
        validate_definition(target, manifest, rows)
    return {"kind": kind, "id": manifest["id"], "manifest": manifest, "bundle": bundle}


def target_snapshot(channel):
    """The Capabilities stack and its capability cells (the built-in cards
    never hold a channel package) with their revision digest."""
    from cell_placement import capabilities_stack, capabilities_stack_cells

    stack = capabilities_stack(channel)
    snapshots = [snapshot_cell(channel, str(c.id)) for c in capabilities_stack_cells(channel)]
    encoded = json.dumps(
        [(s.cell_id, s.tip, sorted(s.refs.items())) for s in snapshots], sort_keys=True
    ).encode()
    return stack, snapshots, hashlib.sha256(encoded).hexdigest()


def _holders(snapshots, kind, identifier):
    """``(snapshot, package)`` of each capability package of ``kind`` with
    id ``identifier``, and every package's parsed manifest alongside. A
    definition that does not parse claims no id or type (the Capabilities
    problems list reports it)."""
    same, others = [], []
    for snapshot in snapshots:
        for package in discover_definitions(snapshot.refs).packages:
            _, files = package_snapshot(snapshot, package.definition_path)
            try:
                manifest = yaml.safe_load(files[package.definition_path.rsplit("/", 1)[-1]])
            except yaml.YAMLError:
                continue
            if not isinstance(manifest, dict):
                continue
            if package.kind == kind and manifest.get("id") == identifier:
                same.append((snapshot, package, manifest))
            else:
                others.append((snapshot, package, manifest))
    return same, others


ID_ELSEWHERE = "Definition id already exists elsewhere in Capabilities; rename first"


def check_target(channel, stack, snapshots, admitted, target_cell_id, *, probe=False):
    """The package an Add to target replaces, if any. A package that takes a
    built-in id, type or surface, or another capability package's id or
    handler type, is held (PackageConflict); a built-in card is never a
    target. ``probe`` is a preview that names no target yet: a package one
    cell already holds is then no refusal (the preview marks that cell
    ``replaces``), and Add to still refuses it as a New cell."""
    from builtin_packages import BUILTIN_REFUSAL, held_reason

    kind, identifier = admitted["kind"], admitted["id"]
    target = next((c for c in stack.cells if str(c.id) == target_cell_id), None)
    if target_cell_id is not None and target is None:
        raise ValueError("Target cell is not on Capabilities")
    if target is not None and target.builtin:
        raise ValueError(BUILTIN_REFUSAL)
    held = held_reason(kind, admitted["manifest"])
    if held is not None:
        raise PackageConflict(held)
    same, others = _holders(snapshots, kind, identifier)
    replacement = None
    if len(same) > 1 or (same and same[0][0].cell_id != target_cell_id
                         and not (probe and target_cell_id is None)):
        raise PackageConflict(ID_ELSEWHERE)
    if same and target_cell_id is not None:
        snapshot, package, manifest = same[0]
        replacement = {
            "cell_id": snapshot.cell_id,
            "folder": package.prefix,
            "version_id": snapshot.tip,
            "embed": manifest.get("embed"),
        }
    if kind == "handler":
        own = {t["ext"] for t in admitted["manifest"]["types"]}
        for _, package, manifest in others:
            if package.kind != "handler":
                continue
            types = manifest.get("types")
            overlap = {t["ext"].casefold() for t in (types if isinstance(types, list) else [])
                       if isinstance(t, dict) and isinstance(t.get("ext"), str)} & own
            if overlap:
                raise PackageConflict("Handler type clash: " + ", ".join(sorted(overlap)))
    return replacement


def _derived_files(files: dict[str, bytes]) -> dict[str, bytes]:
    return {p[len("derived/"):]: b for p, b in files.items() if p.startswith("derived/")}


def _definition_bytes(snapshot, folder):
    """The bytes of the definition file directly in ``folder`` of a snapshot."""
    prefix = folder + "/"
    path = next(p for p in snapshot.refs
                if p.startswith(prefix) and "/" not in p[len(prefix):] and p.endswith(".handler"))
    return CellSnapshot(snapshot.cell_id, snapshot.tip, {path: snapshot.refs[path]},
                        snapshot.languages).bytes()[path]


def preview(channel, request, *, probe=True):
    """The Add to review of one package for the Capabilities stack: its
    files, content hash and summary, every target (New cell, then each
    capability cell, ``replaces`` on the one that holds this package) and
    the replacement it makes. ``probe`` (the preview route) lets a request
    with no target answer while a cell holds the package; Add to passes
    False."""
    from cell_write_scope import cell_write_scope
    from cell_placement import capabilities_stack_cells
    from collimate.handlers import derived_hash
    from state import channel_db_name
    from handlers import catalog, handler_catalog_snapshot

    ids = [request.source_cell_id] + [str(c.id) for c in capabilities_stack_cells(channel)]
    with cell_write_scope(channel_db_name(channel), ids, hold_state=True):
        source = snapshot_cell(channel, request.source_cell_id)
        stack, targets, revision = target_snapshot(channel)
    package, files = package_snapshot(source, request.definition_path)
    key = handler_catalog_snapshot(channel)[0]
    admitted = admit_package(channel, request.definition_path, files)
    replacement = check_target(
        channel, stack, targets, admitted, request.target_cell_id, probe=probe
    )
    same, _ = _holders(targets, admitted["kind"], admitted["id"])
    holders = {(snapshot.cell_id, held.prefix) for snapshot, held, _ in same}
    if admitted["kind"] == "handler":
        own_types = {t["ext"] for t in admitted["manifest"]["types"]}
        for record in catalog(channel).records:
            if (record.cell_id, record.prefix) in holders:
                continue
            overlap = own_types & {t["ext"] for t in record.manifest["types"]}
            if overlap:
                raise ValueError("Handler type clash: " + ", ".join(sorted(overlap)))
    manifest = admitted["manifest"]
    summary = {
        k: manifest.get(k)
        for k in (
            "name",
            "embed",
            "extensions",
            "default_for",
            "generator",
            "permissions",
            "accounts",
            "account_requirements",
            "env_passthrough",
            "types",
        )
        if k in manifest
    }
    summary["code_files"] = sorted(
        p for p in files if p != request.definition_path.rsplit("/", 1)[-1]
    )
    derived = _derived_files(files) if admitted["kind"] == "handler" else {}
    summary["derived_hash"] = derived_hash(derived) if derived else None
    kept = []
    if admitted["kind"] == "handler" and replacement:
        prefix = replacement["folder"] + "/"
        held = next(s for s in targets if s.cell_id == replacement["cell_id"])
        kept = sorted(p[len(prefix):] for p in held.refs if p.startswith(prefix + "skills/"))
    summary["kept_from_target"] = kept
    if admitted["kind"] == "runner":
        summary["mcp"] = {
            path: settings["mcp"]
            for path, settings in manifest.get("settings", {}).items()
            if isinstance(settings, dict) and "mcp" in settings
        }
        summary["host_process_notice"] = "docker: none and full run as host processes"
    replacing = {snapshot.cell_id for snapshot, _, _ in same}
    targets_public = [{"cell_id": None, "name": "New cell", "replaces": False}] + [
        {"cell_id": str(c.id), "name": c.name, "replaces": str(c.id) in replacing}
        for c in capabilities_stack_cells(channel)
    ]
    result = {
        "targets": targets_public,
        "content_hash": content_hash(files),
        "source_version_id": source.tip,
        "kind": admitted["kind"],
        "id": admitted["id"],
        "folder": package.prefix,
        "files": sorted(files),
        "summary": summary,
        "replacement": replacement,
        "target_revision": revision,
    }
    return result, (source, targets, files, admitted, key)


def vfiles(files, *, prefix="", languages=None, binary_paths=()):
    from cas import store_bytes
    from models import VirtualFile
    from models import language_for

    result = []
    for path, body in files.items():
        name = prefix + path
        try:
            if name in binary_paths:
                raise UnicodeError()
            text = body.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError()
            result.append(
                VirtualFile(
                    name=name,
                    content=text,
                    language=(languages or {}).get(name, language_for(name)),
                )
            )
        except UnicodeError:
            result.append(
                VirtualFile(
                    name=name,
                    blob_hash=store_bytes(body),
                    language=(languages or {}).get(name, "binary"),
                )
            )
    return result


def _folder_bytes(files, folder: str) -> dict[str, bytes]:
    """The folder-relative bytes of the ``folder`` package among the cell
    files ``files``."""
    from cas import require_blob
    prefix = folder + "/"
    return {f.name[len(prefix):]: require_blob(f.blob_hash) if f.blob_hash else f.content.encode("utf-8")
            for f in files if f.name.startswith(prefix)}


ADDED = "Added"
ADDED_BUILD_WAITS = "Added. Review and accept its build from its card in Capabilities."
ADDED_NEEDS_BUILD = "Added. Build it from its card in Capabilities."


def add_to(channel, request):
    """Copy one reviewed package into a capability cell (or a new one at
    the end of the Capabilities stack) as the user's approved version.

    A handler carries its ``derived/`` folder but never the source's
    ``derived:`` line: a new cell gets none, and a replacement keeps the
    target's, so the build in use stays in use until Review and accept. A
    replacement also keeps the target's added skills (``skills/``) in place
    of the source's. The package's provenance record (source ``cell``, the
    source cell's id) commits with the copy."""
    import uuid
    from datetime import datetime
    from cell_write_scope import cell_write_scope
    from cell_commits import write_source_version, ensure_tip_row
    from database import get_db_connection
    from state import channel_db_name, persist_state, flush_persist_state
    from cas import publication_lock, store_bytes
    from versions import tip_file_map, insert_version
    from handlers import handler_catalog_snapshot
    from models import Cell
    from channel_config import ui_state_merge_lock
    from provenance import Source, package_record, write as write_provenance
    from renderer_bundles import publish_server_bundle

    result, (source, targets, files, admitted, key) = preview(channel, request, probe=False)
    if (
        result["content_hash"] != request.expected_content_hash
        or result["target_revision"] != request.expected_target_revision
    ):
        raise HTTPException(409, "Package or target changed; review Add to again")
    if result["replacement"] and not request.replace:
        raise HTTPException(409, "Confirm replacement of the existing package")
    db = channel_db_name(channel)
    ids = [source.cell_id] + [s.cell_id for s in targets]
    target = None
    with cell_write_scope(db, ids, hold_state=True):
        fresh = snapshot_cell(channel, source.cell_id)
        stack, _, revision = target_snapshot(channel)
        if (
            fresh.refs != source.refs
            or fresh.tip != source.tip
            or revision != result["target_revision"]
            or handler_catalog_snapshot(channel)[0] != key
        ):
            raise HTTPException(409, "Package or target changed; review Add to again")
        if request.source_cell_id == request.target_cell_id:
            raise HTTPException(400, "Add to needs a different target cell")
        target = (
            next((c for c in stack.cells if str(c.id) == request.target_cell_id), None)
            if request.target_cell_id
            else Cell(name=admitted["manifest"].get("name", admitted["id"]))
        )
        if target is None:
            raise HTTPException(409, "Target cell moved")
        folder = (
            result["replacement"]["folder"]
            if result["replacement"]
            else result["folder"]
        )
        if not result["replacement"] and any(
            f.name.startswith(folder + "/") for f in target.files
        ):
            raise HTTPException(
                409, "Target folder already exists; rename the package first"
            )
        copied = dict(files)
        keep = []
        if admitted["kind"] == "handler":
            name = request.definition_path.rsplit("/", 1)[-1]
            manifest = dict(yaml.safe_load(copied[name]))
            manifest.pop("derived", None)
            if result["replacement"]:
                held = next(s for s in targets if s.cell_id == result["replacement"]["cell_id"])
                current = yaml.safe_load(_definition_bytes(held, folder)) or {}
                if current.get("derived"):
                    manifest["derived"] = current["derived"]
                copied = {p: b for p, b in copied.items() if not p.startswith("skills/")}
                keep = [f for f in target.files if f.name.startswith(folder + "/skills/")]
            copied[name] = yaml.safe_dump(manifest, sort_keys=False).encode()
        with (
            cell_write_scope(db, [str(target.id)], hold_state=True),
            publication_lock,
            ui_state_merge_lock(),
        ):
            newfiles = [
                f for f in target.files if not f.name.startswith(folder + "/")
            ] + keep + vfiles(
                copied,
                prefix=folder + "/",
                binary_paths={
                    folder + "/" + p
                    for p in copied
                    if result["folder"] + "/" + p in source.binary_paths
                },
                languages={
                    folder + "/" + p: source.languages.get(
                        result["folder"] + "/" + p, "text"
                    )
                    for p in copied
                },
            )
            conn = get_db_connection(db)
            try:
                run_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')",
                    (run_id, "Add to " + admitted["id"]),
                )
                ensure_tip_row(conn, db, source.tip, run_id)
                baseline = tip_file_map(conn, source.tip)
                from persistence import snapshot_files, restore_snapshot
                prefix = result['folder'] + '/'
                approved_entries = {f['path']: f for f in snapshot_files(conn, source.cell_id)
                                    if not f['path'].startswith(prefix)}
                for path, body in files.items():
                    full_path = prefix + path
                    approved_entries[full_path] = dict(
                        path=full_path, body_hash=store_bytes(body),
                        encoding='binary' if full_path in source.binary_paths else 'utf-8',
                        language=source.languages.get(full_path, 'text'))
                typed = list(approved_entries.values())
                approved = {path: f['body_hash'] for path, f in approved_entries.items()}
                source_version = source.tip
                if approved != baseline:
                    restore_snapshot(conn, source.cell_id, typed, writer_id='host:package-approval')
                    source_version, _ = insert_version(
                        conn, tip_id=source.tip, parent_files=baseline, run_id=run_id,
                        files=approved, file_entries=typed, version_name='Approve package',
                        source_type='manual_edit', generator_name='user', owner_id=source.cell_id,
                    )
                # The user explicitly approved these package edits only.
                version = write_source_version(
                    conn,
                    target,
                    newfiles,
                    run_id,
                    label="Add " + admitted["id"],
                    source_type="add_to",
                    generator_name="user",
                    replace_prefix=folder + "/",
                )
                conn.execute(
                    "INSERT INTO package_approvals VALUES (?,?,?,?)",
                    (version, source.cell_id, source_version, result["content_hash"]),
                )
                held = _folder_bytes(newfiles, folder)
                definition = request.definition_path.rsplit("/", 1)[-1]
                write_provenance(conn, Source("cell", source.cell_id), [package_record(
                    str(target.id), folder, admitted["kind"], yaml.safe_load(held[definition]), held)])
                if admitted["kind"] == "renderer":
                    row = conn.execute(
                        "SELECT value FROM ui_state WHERE key='renderer_availability'"
                    ).fetchone()
                    availability = json.loads(row[0]) if row else {}
                    availability[admitted["id"]] = True
                    conn.execute(
                        "INSERT INTO ui_state(key,value) VALUES ('renderer_availability',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(availability),),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            target.files = newfiles
            target.updated_at = datetime.now()
            if request.target_cell_id is None:
                stack.cells.append(target)
    if admitted["bundle"] is not None:
        publish_server_bundle(*admitted["bundle"])
    persist_state()
    flush_persist_state()
    return {
        "cell_id": str(target.id),
        "version_id": version,
        "source_version_id": source_version,
        "content_hash": result["content_hash"],
        "message": ADDED
        if admitted["kind"] != "handler"
        else ADDED_BUILD_WAITS
        if _derived_files(copied)
        else ADDED_NEEDS_BUILD,
    }
