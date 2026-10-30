"""User-reviewed derived builds; current source is inert until exact-hash Accept.

A user handler (in a Capabilities cell) is built in a work stack
(``capability_spawn``): a run there writes its ``derived/`` folder, the user
adds the package back with Replace, and Accept here writes ``derived:`` into
its ``.handler``. A built-in handler (on the Definitions card) only
regenerates its skills: a regenerate work stack writes them, the user offers
them to the card's overlay ``<id>/derived/skills/**`` (``routes.builtins``),
Inspect runs every example through the build's fixed verifier, and Accept
records the skills hash in ``<id>/additions.yaml``. The work stacks' briefs
are the build's ``handler`` skills (:func:`brief_body`), never a channel's.
"""

from __future__ import annotations
import time
import uuid
from typing import Literal
import yaml
from fastapi import HTTPException

BriefName = Literal["build", "regenerate", "create"]


def handler_snapshot(channel, cell_id, folder):
    """``(snapshot, definition_path, files, builtin)`` of the handler package
    ``folder`` in the Capabilities cell ``cell_id``: its files are
    package-relative bytes. On the Definitions card they are the overlay
    files of the built-in handler ``folder`` (``builtin`` is its
    :class:`builtin_packages.BuiltinHandler`, else None)."""
    from builtin_packages import builtin_handlers, card_spec
    from cell_write_scope import cell_write_scope
    from cell_placement import find_cell, is_capabilities_stack
    from state import channel_db_name
    from package_approval import CellSnapshot, snapshot_cell, package_snapshot
    from collimate.definitions import discover_definitions

    with cell_write_scope(channel_db_name(channel), [cell_id], hold_state=True):
        stack, cell = find_cell(channel, cell_id)
        if cell is None or stack is None or not is_capabilities_stack(stack):
            raise ValueError("Handlers are reviewed from their Capabilities cell")
        card = card_spec(channel, cell)
        snap = snapshot_cell(channel, cell_id)
    if card is not None:
        handler = builtin_handlers().get(folder)
        if handler is None or handler.cell != card.spec_id:
            raise ValueError("Handler folder does not exist")
        prefix = folder + "/"
        selected = CellSnapshot(snap.cell_id, snap.tip,
                                {p: r for p, r in snap.refs.items() if p.startswith(prefix)},
                                snap.languages, snap.binary_paths)
        files = {p[len(prefix):]: b for p, b in selected.bytes().items()}
        return snap, handler.definition_path, files, handler
    package = next(
        (
            p
            for p in discover_definitions(snap.refs).packages
            if p.kind == "handler" and p.prefix == folder
        ),
        None,
    )
    if package is None:
        raise ValueError("Handler folder does not exist")
    _, files = package_snapshot(snap, package.definition_path)
    return snap, package.definition_path, files, None


def _brief_skill(name: BriefName) -> dict[str, bytes]:
    """The ``handler`` built-in's skill ``name``: its files relative to the
    skill folder."""
    from builtin_packages import builtin_handlers
    prefix = f"derived/skills/{name}/"
    return {path[len(prefix):]: body for path, body in builtin_handlers()["handler"].files.items()
            if path.startswith(prefix)}


def brief_body(name: BriefName) -> bytes:
    """The body of the build's ``handler`` skill ``name`` (``build`` for a
    user handler's Build, ``regenerate`` for a built-in's Regenerate skills,
    ``create`` for Create): its ``SKILL.md`` with the skill's own front
    matter stripped, for a work cell's ``brief.md`` to follow the host's
    YAML block."""
    text = _brief_skill(name)["SKILL.md"].decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"The {name} skill has no front matter")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError(f"The {name} skill's front matter is not closed")
    return text[end + len("\n---\n"):].lstrip("\n").encode("utf-8")


def brief_references(name: BriefName) -> dict[str, bytes]:
    """The ``references/`` files of the build's ``handler`` skill ``name``,
    keyed relative to that folder."""
    prefix = "references/"
    return {path[len(prefix):]: body for path, body in _brief_skill(name).items()
            if path.startswith(prefix)}


def _check_examples(manifest, source, files, lease, deadline, result):
    """Run every example through ``source``; fills ``result['examples']``."""
    from collimate.handlers import handler_for
    from verification import verify_source, verifier_input
    for example in manifest.get("examples", []):
        prefix = "examples/" + example["folder"].strip("/") + "/"
        selected = {p[len(prefix):]: b for p, b in files.items() if p.startswith(prefix)}
        verdicts = [
            {"path": path, **verify_source(source, verifier_input(path, selected, selected, root_is_package=True),
                                           lease=lease, deadline=deadline)}
            for path in sorted(selected) if handler_for([manifest], path) is not None
        ]
        result["examples"].append({
            "folder": example["folder"],
            "expect": example["expect"],
            "verdicts": verdicts,
            "error": None if verdicts else "No matching example files",
        })


def inspect_build(channel, cell_id, folder):
    from package_approval import content_hash
    from collimate.handlers import validate_handler, derived_hash
    from verification import compiled_verifier
    from verifier_pool import get_verifier_pool

    snap, definition_path, files, builtin = handler_snapshot(channel, cell_id, folder)
    if builtin is not None:
        return _inspect_builtin(builtin, snap, files)
    manifest = validate_handler(yaml.safe_load(files[definition_path.rsplit("/", 1)[-1]]))
    derived = {
        p[len("derived/") :]: b for p, b in files.items() if p.startswith("derived/")
    }
    result = {
        "builtin": False,
        "content_hash": content_hash(files),
        "derived_hash": derived_hash(derived) if derived else None,
        "compile": {"valid": False, "error": "derived/verify.ts is missing"},
        "examples": [],
    }
    source = derived.get("verify.ts")
    if source is None:
        return result, (snap, definition_path, files, manifest, derived)
    deadline = time.monotonic() + 30
    try:
        from collimate.handlers import validate_added_skills
        from handlers import catalog
        current = next((record for record in catalog(channel).records
                        if record.cell_id == cell_id and record.prefix == folder), None)
        validate_added_skills(files, current.accepted_files if current is not None else {})
        _, code = compiled_verifier(
            source, deadline=min(deadline, time.monotonic() + 5)
        )
        # Fixed trusted probe checks export shape without calling the authored
        # verifier on an invented input. Authored module evaluation is isolated.
        probe = (
            code
            + '\n;{const candidate=globalThis.__collimate_verifier;globalThis.__collimate_verifier={default:()=>({valid:typeof candidate.default==="function",error:"verifier needs a callable default export"})};}'
        )
        with get_verifier_pool().lease(deadline) as lease:
            result["compile"] = lease.verify(
                probe, {}, deadline=min(deadline, time.monotonic() + 5)
            )
            if result["compile"]["valid"]:
                _check_examples(manifest, source, files, lease, deadline, result)
    except (ValueError, OSError, RuntimeError) as exc:
        result["compile"] = {"valid": False, "error": str(exc)[:4096]}
    return result, (snap, definition_path, files, manifest, derived)


def _inspect_builtin(handler, snap, files):
    """A built-in's regenerated skills: no channel verifier to compile;
    every example (the build's and the added ones) runs through the build's
    fixed ``verify.ts``. ``content_hash`` covers the overlay files,
    ``derived_hash`` is the regenerated skills' channel-owned hash
    (``builtin_packages.owned_hash``)."""
    from builtin_packages import ADDITIONS, owned_hash, parse_additions
    from package_approval import content_hash
    from verifier_pool import get_verifier_pool

    additions = parse_additions(files.get(ADDITIONS))
    regenerated = {p[len("derived/"):]: b for p, b in files.items() if p.startswith("derived/skills/")}
    manifest: dict = dict(handler.manifest, examples=[*handler.manifest["examples"], *additions.examples])
    derived = {"verify.ts": handler.verify_ts, **regenerated}
    result = {
        "builtin": True,
        "content_hash": content_hash(files),
        "derived_hash": owned_hash(handler.id, regenerated) if regenerated else None,
        "compile": {"valid": True, "error": None},
        "examples": [],
    }
    deadline = time.monotonic() + 30
    try:
        with get_verifier_pool().lease(deadline) as lease:
            _check_examples(manifest, handler.verify_ts, {**handler.files, **files}, lease, deadline, result)
    except (ValueError, OSError, RuntimeError) as exc:
        result["compile"] = {"valid": False, "error": str(exc)[:4096]}
    return result, (snap, handler.definition_path, files, manifest, derived)


def commit_overlay(channel, cell_id, handler_id, change, label):
    """Rewrite the built-in ``handler_id``'s overlay on its card ``cell_id``
    in one version (writer ``host:builtin``): ``change(files)`` maps the
    current overlay files under ``<id>/`` (package-relative bytes) to the new
    ones, and may raise to refuse. Every written path must be an overlay
    path. Returns the version id."""
    from builtin_packages import BUILTIN_REFUSAL, card_spec, overlay_path
    from cas import publication_lock
    from cell_commits import write_source_version
    from cell_placement import find_cell
    from cell_write_scope import cell_write_scope
    from database import get_db_connection
    from package_approval import snapshot_cell, vfiles
    from persistence import publish_files
    from state import channel_db_name

    db = channel_db_name(channel)
    prefix = handler_id + "/"
    with cell_write_scope(db, [cell_id], hold_state=True), publication_lock:
        cell = find_cell(channel, cell_id)[1]
        card = card_spec(channel, cell)
        if cell is None or card is None:
            raise HTTPException(404, "Built-in card not found")
        snap = snapshot_cell(channel, cell_id)
        current = {p[len(prefix):]: b for p, b in snap.bytes().items() if p.startswith(prefix)}
        files = change(current)
        if any(not overlay_path(card, prefix + path) for path in files):
            raise ValueError(BUILTIN_REFUSAL)
        conn = get_db_connection(db)
        try:
            run_id = str(uuid.uuid4())
            conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')", (run_id, label))
            version = write_source_version(
                conn, cell, vfiles(files, prefix=prefix, languages=snap.languages,
                                   binary_paths={prefix + p for p in files if prefix + p in snap.binary_paths}),
                run_id, label=label, generator_name="builtin", replace_prefix=prefix)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    publish_files(channel, cell_id, dict(operation_id=run_id, outcome="accepted", files=[], conflicts=[]))
    return version


def accept_build(channel, cell_id, folder, expected_content_hash):
    from datetime import datetime
    from package_approval import snapshot_cell, vfiles
    from collimate.handlers import validate_handler
    from handlers import save_derived_skill
    from cell_write_scope import cell_write_scope
    from cell_placement import find_cell
    from cell_commits import write_source_version
    from state import channel_db_name, persist_state, flush_persist_state
    from database import get_db_connection
    from cas import publication_lock

    result, (snap, definition_path, files, manifest, derived) = inspect_build(
        channel, cell_id, folder
    )
    if result["content_hash"] != expected_content_hash:
        raise HTTPException(409, "Handler changed; review the build again")
    if not result["compile"]["valid"]:
        raise ValueError(result["compile"].get("error", "Verifier does not compile"))
    if result["builtin"]:
        return _accept_builtin(channel, cell_id, folder, result)
    accepted = validate_handler(dict(manifest, derived=result["derived_hash"]))
    db = channel_db_name(channel)
    with cell_write_scope(db, [cell_id], hold_state=True), publication_lock:
        current = snapshot_cell(channel, cell_id)
        if current.refs != snap.refs or current.tip != snap.tip:
            raise HTTPException(409, "Handler changed; review the build again")
        cell = find_cell(channel, cell_id)[1]
        assert cell is not None  # snapshot recheck held the same cell/state scope
        definition = yaml.safe_dump(accepted, sort_keys=False).encode()
        newfiles = [
            f for f in cell.files if f.name != definition_path
        ] + vfiles({definition_path: definition}, languages=snap.languages)
        # Retain exact executable CAS refs before manifest publication. If the
        # channel transaction fails these harmless cached bytes stay inactive.
        save_derived_skill(result["derived_hash"], derived)
        conn = get_db_connection(db)
        try:
            run_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')",
                (run_id, "Accept handler " + manifest["id"]),
            )
            version = write_source_version(
                conn, cell, newfiles, run_id, label="Accept " + manifest["id"]
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        cell.files = newfiles
        cell.updated_at = datetime.now()
    persist_state()
    flush_persist_state()
    return {
        "accepted": True,
        "derived_hash": result["derived_hash"],
        "version_id": version,
    }


def _accept_builtin(channel, cell_id, folder, result):
    """Accept a built-in's regenerated skills: store their channel-owned
    files under their skills hash and record it in ``<id>/additions.yaml``;
    the build's bytes are never touched."""
    from builtin_packages import ADDITIONS, Additions, owned_skills, parse_additions
    from handlers import save_derived_skill
    from package_approval import content_hash

    skills_hash = result["derived_hash"]
    if skills_hash is None:
        raise ValueError("No regenerated skills to accept")

    def accept(files):
        if content_hash(files) != result["content_hash"]:
            raise HTTPException(409, "Handler changed; review the build again")
        save_derived_skill(skills_hash, owned_skills(folder, {p[len("derived/"):]: b for p, b in files.items()
                                                               if p.startswith("derived/skills/")}))
        additions = parse_additions(files.get(ADDITIONS))
        return files | {ADDITIONS: Additions(additions.examples, skills_hash).dump()}

    version = commit_overlay(channel, cell_id, folder, accept, "Accept skills " + folder)
    return {"accepted": True, "derived_hash": skills_hash, "version_id": version}
