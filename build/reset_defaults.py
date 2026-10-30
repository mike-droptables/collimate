"""Developer tool: bring every channel's capability cells that came from a
content tree onto that tree's current copy.

    PYTHONPATH=api uv run python build/reset_defaults.py --data-dir <dir> [--source <folder>]

``bash build/build_desktop.sh --reset`` runs it against the installed app's
data dir once the app is reinstalled. It is a developer tool and gives no
product guarantees: the app never runs it, and a channel's cells otherwise
change only on a user's action.

The content is the bundled tree installed with the app
(``genesis.bundled_dir("content")``), or the folder ``--source`` names (one
holding ``generators/`` and ``renderers/``). Its cells are read from both
asset trees (``collimate._host.packaging.asset_cells.read_asset_cells``).

A channel's Capabilities cell is matched by its provenance (``provenance``,
written by admission): the content cell one of whose packages (kind and
manifest id) a record of the cell names. A cell without provenance is never
matched, and neither is one whose records name packages of two content
cells. For a matched cell:

* **Current**: every package of the content cell, as admission copies it,
  is recorded on the cell with the same content hash. Nothing changes.
* **Replaced**: otherwise the content cell's files (without its own runner
  packages, as channel creation drops them) and the cell's own runner
  packages pass admission (``package_import.prepare_packages``, the path a
  catalog add takes) against the channel's Capabilities without the cell.
  The admitted files become a new version of the cell, linked onto its
  chain like an in-app edit: it keeps its id, name, slot and history, takes
  the content cell's icon, and the records of the content's packages are the
  admission's (source ``bundled`` or ``folder``); its own runners keep
  theirs. Each admitted renderer's bundle is
  stored, and a renderer new to the channel is made available.
* **Held** or **refused**: an admission that holds a package (another cell
  declares its id or types) or refuses one leaves the cell as it was.

Every other cell, and everything else in the channels, stays as it was.

It refuses while a server runs against the data dir: the server holds an
flock on ``<data dir>/.collimate/server.lock`` for its lifetime
(``main.SERVER_LOCK_PATH``), and the reset takes the same lock until it is
done. A second run finds every matched cell current and changes nothing.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

VERSION_LABEL = "Reset to the current build"
# The version row's generator_name — the audit trail's answer to "who wrote
# this version" (the runner editor stamps 'runner-editor' the same way).
GENERATOR_NAME = "reset"
TREE_KINDS = ("generator", "renderer")
_TREE_FOLDERS = {"generator": "generators", "renderer": "renderers"}


class ResetError(RuntimeError):
    """A condition the caller has to resolve (a live server, a content tree
    that cannot be read, a saved state that does not validate)."""


def lock_data_dir(data_dir: str) -> int:
    """Try-lock ``<data_dir>/.collimate/server.lock`` — the lock a running
    server holds (main.SERVER_LOCK_PATH) — and return its fd, held until
    :func:`unlock_data_dir`. Raises :class:`ResetError` when a server (or
    another reset) holds it or the lock file cannot be opened."""
    from data_dir_lock import LockHeld, LockOpenError, acquire
    path = os.path.join(data_dir, ".collimate", "server.lock")
    try:
        return acquire(path)
    except LockHeld as exc:
        holder = exc.holder
        raise ResetError(
            f"a Collimate server (pid {holder}) is running against {data_dir}\n"
            "       (it holds .collimate/server.lock). Quit the app, then run the "
            "reset again."
        ) from None
    except LockOpenError as failure:
        exc = failure.error
        raise ResetError(
            f"could not open {path}, the data dir's server lock ({exc.strerror}; "
            "it must be a plain file). Remove whatever is there and run the reset again."
        ) from None


def unlock_data_dir(fd: int) -> None:
    from data_dir_lock import release
    release(fd)


def read_content(root: Path) -> list:
    """The content tree's cells, generators first, as ``(tree kind,
    CellSpec)``. Raises :class:`ResetError` for a tree that cannot be read."""
    from collimate._host.packaging.asset_cells import read_asset_cells
    cells = []
    for kind in TREE_KINDS:
        tree = root / _TREE_FOLDERS[kind]
        try:
            cells.extend((kind, spec) for spec in read_asset_cells(tree, kind))
        except (OSError, ValueError) as exc:
            raise ResetError(f"the {kind} cells cannot be read from {tree} ({exc})") from None
    return cells


def _accepted_vfiles(conn, cell_id):
    from persistence import snapshot_files
    from versions import build_virtual_files
    return build_virtual_files(snapshot_files(conn, cell_id))


def _commit_reset(conn, run_id: str, cell, files, icon: str | None) -> bool:
    """Replace accepted files and retain their exact prior typed history.

    The caller owns the cell lock and transaction. Display is cleared only
    when accepted files are replaced, including an encoding/language change.
    """
    from cell_display import write_display
    from persistence import _entry, restore_snapshot, snapshot_files
    from versions import find_tip, insert_version, synthesize_genesis_row, tip_file_map

    cell_id = str(cell.id)
    current_files = snapshot_files(conn, cell_id)
    tip = find_tip(conn, cell_id)
    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_num), 0) + 1 FROM cells WHERE run_id=?", (run_id,),
    ).fetchone()[0]
    synthesize_genesis_row(conn, tip, run_id, seq, current_files)
    entries = []
    for file in files:
        path, digest, language, encoding = _entry(file)
        entries.append({'path': path, 'body_hash': digest, 'encoding': encoding, 'language': language})
    desired = {f['path']: f['body_hash'] for f in entries}
    from versions import version_file_entries
    history_entries = version_file_entries(conn, tip)
    def projection(values):
        return {f['path']: (f['body_hash'], f['encoding'], f['language']) for f in values}
    history_changed = projection(history_entries) != projection(entries)
    if history_changed:
        insert_version(conn, tip_id=tip, parent_files=tip_file_map(conn, tip), owner_id=cell_id,
                       run_id=run_id, files=desired, file_entries=entries,
                       version_name=VERSION_LABEL, source_type="manual_edit", generator_name=GENERATOR_NAME)
    current_changed = projection(current_files) != projection(entries)
    restore_snapshot(conn, cell_id, entries, writer_id='host:reset')
    if current_changed:
        write_display(conn, cell_id, None)
    icon_changed = bool(icon and cell.icon != icon)
    if current_changed or history_changed:
        from versions import build_virtual_files
        cell.files = build_virtual_files(entries)
    if icon:
        cell.icon = icon
    if history_changed or current_changed or icon_changed:
        cell.updated_at = datetime.now()
    return history_changed or current_changed or icon_changed


def _content_files(spec) -> list:
    """The content cell's files as a fresh channel takes them: its own
    runner packages stay out (channel creation drops them too)."""
    import genesis
    import runners
    return runners.without_runners(genesis.package_cell_vfiles(spec))


def _match(records: list[dict], index: dict) -> int | None | str:
    """The index of the one content cell the records name a package of,
    None when they name none, ``"ambiguous"`` when they name two."""
    found = {index[(r["package_kind"], r["package_id"])] for r in records
             if (r["package_kind"], r["package_id"]) in index}
    if not found:
        return None
    return found.pop() if len(found) == 1 else "ambiguous"


def _replace(ch, cell, spec, source) -> str:
    """Admit the content cell ``spec`` with ``cell``'s own runner packages
    and commit the admitted files as a new version of ``cell``; returns the
    outcome (``replaced``, ``current``, or ``held: …`` / ``refused: …``)."""
    import genesis
    import provenance
    import runners
    from cell_commits import cell_commit_locks
    from cell_metadata import advance_snapshot_generations
    from cell_placement import capabilities_stack
    from channel_config import (
        RENDERER_AVAILABILITY_KEY,
        read_renderer_availability,
        write_ui_state_json,
    )
    from collimate.definitions import discover_definitions
    from database import get_db_connection
    from models import AppState, Cell
    from package_import import capture_sources, prepare_packages
    from renderer_bundles import publish_server_bundle
    from state import _persist_lock, channel_db_name

    channel_uuid = channel_db_name(ch)
    content_only = {f.name for f in runners.without_runners(cell.files)}
    own_runners = [f for f in cell.files if f.name not in content_only]
    own_folders = [package.prefix for package in discover_definitions([f.name for f in own_runners]).packages]
    incoming = Cell(id=cell.id, name=cell.name, icon=cell.icon, files=_content_files(spec) + own_runners)
    candidate, signature = capture_sources(ch)
    stack = capabilities_stack(candidate)
    stack.cells = [c for c in stack.cells if str(c.id) != str(cell.id)]
    try:
        prepared = prepare_packages(ch, [incoming], source=source, source_state=(candidate, signature))
    except ValueError as exc:
        return f"refused: {exc}"
    if prepared.skipped:
        return "held: " + "; ".join(dict.fromkeys(skip["reason"] for skip in prepared.skipped))
    if not prepared.cells:
        return "refused: the content cell holds no package"
    (admitted,) = prepared.cells
    with cell_commit_locks([str(cell.id)]):
        conn = get_db_connection(channel_uuid)
        try:
            with _persist_lock:
                snapshot = genesis.snapshot_app_state(AppState(channels=[ch], active_channel_id=ch.id))
                advance_snapshot_generations(snapshot, str(ch.id), [str(cell.id)])
            conn.execute("BEGIN IMMEDIATE")
            run_id = str(uuid.uuid4())
            conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, ?, 'completed')", (run_id, VERSION_LABEL))
            changed = _commit_reset(conn, run_id, cell, admitted.files, spec.icon or None)
            # The cell's own runners keep whatever record they had: the
            # content's packages are the ones this admission brings.
            conn.execute(f"DELETE FROM cell_provenance WHERE cell_id=? AND folder NOT IN "
                         f"({','.join('?' * len(own_folders))})", (str(cell.id), *own_folders))
            provenance.write(conn, source, [r for r in prepared.provenance if r["folder"] not in own_folders])
            available = read_renderer_availability(channel_uuid, conn=conn)
            for renderer_id in prepared.renderer_ids:
                available.setdefault(renderer_id, True)
            write_ui_state_json(channel_uuid, RENDERER_AVAILABILITY_KEY, available, conn=conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    for source_hash, body in prepared.bundles:
        publish_server_bundle(source_hash, body)
    return "replaced" if changed else "current"


def _reset_channel(ch, content: list, source) -> dict:
    """Reset ``ch`` to ``content`` (:func:`read_content`) from ``source``.
    Returns ``{matched: [(cell name, outcome)], unrecorded: n, other: n}``:
    each matched cell's outcome (``current``, ``replaced``, ``ambiguous``,
    ``held: …`` or ``refused: …``), and how many Capabilities cells have no
    provenance or none naming a package of the content."""
    import genesis
    from cell_placement import capabilities_stack_cells
    from database import get_db_connection
    from models import Cell
    from package_import import cell_bytes, package_identities
    from provenance import read
    from state import channel_db_name

    genesis.ensure_capabilities_stack(ch)
    cells = capabilities_stack_cells(ch)
    conn = get_db_connection(channel_db_name(ch))
    try:
        records = read(conn, [str(cell.id) for cell in cells])
    finally:
        conn.close()
    index: dict[tuple[str, str], int] = {}
    wanted: list[set] = []
    for at, (_kind, spec) in enumerate(content):
        identities = package_identities(cell_bytes(Cell(name=spec.name, files=_content_files(spec))))
        wanted.append(identities)
        for kind, ident, _hash in identities:
            index.setdefault((kind, ident), at)
    report: dict = {"matched": [], "unrecorded": 0, "other": 0}
    for cell in cells:
        rows = records.get(str(cell.id))
        if not rows:
            report["unrecorded"] += 1
            continue
        at = _match(rows, index)
        if at is None:
            report["other"] += 1
            continue
        if at == "ambiguous":
            report["matched"].append((cell.name, "ambiguous: its packages come from two content cells"))
            continue
        assert isinstance(at, int)
        have = {(r["package_kind"], r["package_id"], r["content_hash"]) for r in rows}
        if wanted[at] <= have:
            report["matched"].append((cell.name, "current"))
            continue
        report["matched"].append((cell.name, _replace(ch, cell, content[at][1], source)))
    return report


def _format_report(channel_name: str, report: dict) -> str:
    """One line per channel: the matched cells and their outcomes, then what
    was left as is."""
    matched = ", ".join(f"{name!r} {outcome}" for name, outcome in report["matched"]) or "none"
    parts = [f"matched by provenance: {matched}"]
    if report["unrecorded"]:
        parts.append(f"{report['unrecorded']} without provenance left as-is")
    if report["other"]:
        parts.append(f"{report['other']} from other sources left as-is")
    return f"      channel {channel_name!r}: Capabilities: " + "; ".join(parts)


def reset_defaults(data_dir: str, *, source_dir: str | None = None, out=print) -> dict:
    """Reset every channel under ``data_dir`` (see the module docstring) to
    the bundled content, or to the folder ``source_dir``. Returns
    ``{channel name: report}``; ``{}`` when there was nothing to do. Raises
    :class:`ResetError` for a live server, content that cannot be read or a
    saved state that does not validate.

    Chdirs into ``data_dir``: every persistent path in the app is
    CWD-relative (main.py's data-dir contract)."""
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    source_dir = os.path.abspath(os.path.expanduser(source_dir)) if source_dir else None
    if not os.path.isfile(os.path.join(data_dir, ".collimate", "app.db")):
        out(f"      no channel data under {data_dir} — nothing to reset")
        return {}
    lock_fd = lock_data_dir(data_dir)
    try:
        return _reset_locked(data_dir, source_dir, out)
    finally:
        from verifier_pool import close_verifier_pool
        close_verifier_pool()
        unlock_data_dir(lock_fd)


def _reset_locked(data_dir: str, source_dir: str | None, out) -> dict:
    """:func:`reset_defaults` under the data dir's server lock."""
    os.chdir(data_dir)

    import genesis
    from provenance import Source

    if source_dir is None:
        root = Path(genesis.bundled_dir("content"))
        source = Source("bundled", str(root))
    else:
        root = Path(source_dir)
        source = Source("folder", str(root))
    content = read_content(root)
    try:
        from builtin_packages import build
        build()
    except (OSError, ValueError) as exc:
        raise ResetError(f"the built-in packages cannot be read from {genesis.BUILTIN_DIR} ({exc})") from None

    saved = genesis.load_global_ui_state()
    try:
        app_state = genesis.parse_saved_app_state(saved) if saved is not None else None
    except ValueError as exc:
        raise ResetError(
            f"the saved app state in {data_dir}/.collimate/app.db does not validate "
            f"({exc.__class__.__name__}); repair that row, then run the reset again."
        ) from exc
    if app_state is None or not app_state.channels:
        out("      no channels — nothing to reset")
        return {}
    genesis.reconcile_restored_cell_files(app_state)

    reports: dict = {}
    for ch in app_state.channels:
        reports[ch.name] = _reset_channel(ch, content, source)
        # Persist after every channel, not once at the end: a failure in a
        # later channel then leaves no channel with a database ahead of its
        # persisted layout (and a re-run converges either way).
        from state import _persist_lock
        with _persist_lock:
            genesis.save_global_ui_state(genesis.snapshot_app_state(app_state))
        out(_format_report(ch.name, reports[ch.name]))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reset_defaults",
        description=(
            "Developer tool (no product guarantees): give every channel's "
            "Capabilities cell whose provenance names a package of the content "
            "the content's current copy, admitted as a catalog add is, as a new "
            "version on the cell. Cells without provenance, and everything else "
            "in the channels, stay. The server must be stopped."
        ),
    )
    parser.add_argument(
        "--data-dir", metavar="PATH", default=None,
        help="the data dir holding .collimate/ (default: $COLLIMATE_DATA, else the current directory)",
    )
    parser.add_argument(
        "--source", metavar="FOLDER", default=None,
        help="a content folder holding generators/ and renderers/ (default: the bundled content)",
    )
    args = parser.parse_args(argv)
    data_dir = args.data_dir or os.environ.get("COLLIMATE_DATA") or os.getcwd()
    try:
        reset_defaults(data_dir, source_dir=args.source)
    except ResetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
