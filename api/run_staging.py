"""Generator package discovery, frozen workspace hydration and runner staging."""

from cell_placement import capabilities_stack_cells
import logging
import os
import shutil
import stat
import tempfile

import yaml

from cas import blob_path, publication_lock, require_blob
from database import get_db_connection
from versions import find_tip

logger = logging.getLogger(__name__)

from versions import version_file_rows


def resolve_generator_cell(gid: str, channel: str, *, cell_id: str | None = None):
    """The Capabilities cell (``cell_id``, when given) and package prefix of
    the generator ``gid`` in ``channel`` (its immutable UUID): only a visible
    package whose YAML declares the requested id. ``channel`` is required, so
    run-context code never resolves another channel's generators."""
    from runners import definition_files
    from state import get_channel_by_id
    ch = get_channel_by_id(channel)
    matches = []
    for cell in capabilities_stack_cells(ch) if ch is not None else []:
        if cell_id is not None and str(cell.id) != cell_id:
            continue
        for package, file in definition_files(cell, "generator"):
            try:
                manifest = yaml.safe_load(file.content)
            except yaml.YAMLError:
                continue
            if isinstance(manifest, dict) and manifest.get("id") == gid:
                matches.append((cell, package.prefix + "/"))
    if len(matches) > 1:
        from runners import RunnerError
        raise RunnerError(409, f"Duplicate generator id {gid!r}")
    return matches[0] if matches else None


def generator_cell_id(gid: str, channel: str) -> str | None:
    """Source cell selected by the same package resolver used for staging."""
    resolved = resolve_generator_cell(gid, channel)
    return str(resolved[0].id) if resolved else None


def _copy_required_blob(digest: str, dest: str, name: str) -> None:
    """Copy verified bytes with their source mode; missing/corrupt input aborts."""
    try:
        with publication_lock:
            raw = require_blob(digest)
            source_stat = os.stat(blob_path(digest))
    except OSError as exc:
        raise OSError(f"Required staging body unavailable for {name}: {exc}") from exc
    with open(dest, "wb") as handle:
        handle.write(raw)
    os.chmod(dest, stat.S_IMODE(source_stat.st_mode))
    os.utime(dest, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


def _write_source_file(dest: str, file) -> None:
    """Materialize one required frozen generator source file or raise."""
    if file.blob_hash:
        _copy_required_blob(file.blob_hash, dest, file.name)
    else:
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(file.content)


def find_generator_dir(
    generator_name: str, channel: str, *, cell_id: str | None = None,
) -> str | None:
    """Stage a frozen package with cell-top-level shared folder siblings."""
    snapshot = snapshot_generator_source(generator_name, channel, cell_id=cell_id)
    if snapshot is None:
        return None
    tmp = tempfile.mkdtemp(prefix=f"collimate_gen_{generator_name}_")
    try:
        return materialize_generator_source(snapshot, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def snapshot_generator_source(generator_name: str, channel: str, *, cell_id: str | None = None):
    """Freeze file field values while the caller holds the source/state locks."""
    from runners import generator_source_files
    resolved = resolve_generator_cell(generator_name, channel, cell_id=cell_id)
    if resolved is None:
        return None
    source_cell, target_prefix = resolved
    gen_subdir = target_prefix.rstrip("/").rsplit("/", 1)[-1]
    # Shared folders keep their cell-top-level names; reserve a distinct app dir.
    if gen_subdir.startswith("_"):
        gen_subdir = "generator"
    writes = []
    for file in generator_source_files(source_cell, target_prefix):
        rel = (gen_subdir + "/" + file.name[len(target_prefix):]
               if file.name.startswith(target_prefix) else file.name)
        writes.append((rel, file.model_copy()))

    if not writes:
        return None
    return gen_subdir, writes


def materialize_generator_source(snapshot, app_root: str) -> str:
    """Write a frozen source tree once, outside global state_lock."""
    gen_subdir, writes = snapshot
    for rel, file in writes:
        dest = safe_dest_under(app_root, rel)
        if dest is None:
            raise ValueError(f"Invalid generator source path: {rel}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _write_source_file(dest, file)
    return os.path.join(app_root, gen_subdir)


def safe_dest_under(dest_dir: str, rel: str) -> str | None:
    """Resolve ``rel`` under ``dest_dir`` and return the absolute dest path,
    or None if the result would escape the workspace.

    Defends against three things stored cell_files.filepath rows could carry:
      * Absolute paths — ``os.path.join`` ignores its left operand when the
        right one is absolute, so ``/etc/passwd`` would land at /etc/passwd.
      * Parent-traversal segments — ``a/../../etc/passwd`` resolves outside
        the workspace after normalization.
      * Backslash-encoded variants on POSIX — ``..\\..\\foo`` looks safe to
        normpath but not after replacing separators. Reject as a precaution.
    """
    return _safe_dest_under_resolved(os.path.realpath(dest_dir), rel)


def _safe_dest_under_resolved(abs_root: str, rel: str) -> str | None:
    """:func:`safe_dest_under` against an already-resolved root
    (``os.path.realpath`` of the destination dir), so a caller placing many
    files under one root resolves it once."""
    if os.path.isabs(rel) or "\\" in rel:
        return None
    abs_dest = os.path.normpath(os.path.join(abs_root, rel))
    if abs_dest != abs_root and not abs_dest.startswith(abs_root + os.sep):
        return None
    return abs_dest


def hydrate_workspace(
    channel: str,
    cell_id: str,
    dest_dir: str,
    tip_id: str | None = None,
    *,
    strip_runs: bool = False,
):
    """Copy the version ``tip_id`` of ``cell_id`` (its current tip when
    omitted) from the CAS into ``dest_dir``, at the workspace root.

    ``tip_id`` is the tip the run-start flush resolved for this run — trusted
    rather than re-walked, so a concurrent commit landing between the flush
    and the hydrate cannot split the run across two versions of its cell.

    ``.collrun/*`` is filtered out — that namespace is reserved for the SDK —
    and so is ``_runs/`` with ``strip_runs`` (a copy into another run's
    workspace never carries run folders)."""
    from reserved_paths import is_reserved_path
    from run_folders import is_run_path

    conn = get_db_connection(channel)
    try:
        if tip_id is None:
            tip_id = find_tip(conn, cell_id)
        rows = version_file_rows(conn, tip_id)
    finally:
        conn.close()

    copied = 0
    root = os.path.realpath(dest_dir)
    made_dirs: set[str] = set()
    for row in rows:
        # cell_files carries only Files — run Config/Logs live in
        # run_artifacts. The reserved-band skip is defense-in-depth:
        # hydration runs AFTER the runner's config folders are staged, so a
        # smuggled `.collrun/*` row would otherwise land ON TOP of the
        # trusted staged tree.
        if is_reserved_path(row["filepath"]):
            continue
        if strip_runs and is_run_path(row["filepath"]):
            continue
        dest = _safe_dest_under_resolved(root, row["filepath"])
        if dest is None:
            raise ValueError(f"Unsafe workspace file: {row['filepath']}")
        if os.path.islink(dest):
            raise ValueError(f"Workspace file is a symlink: {row['filepath']}")
        parent = os.path.dirname(dest)
        if parent not in made_dirs:
            os.makedirs(parent, exist_ok=True)
            made_dirs.add(parent)
        _copy_required_blob(row['blob_hash'], dest, row['filepath'])
        copied += 1
    logger.info("cell %s: %d required files copied from %d snapshot entries",
                cell_id[:8], copied, len(rows))


def stage_runner_files(
    channel: str, runner_version_id: str | None, runner_dir: str,
    folders: list[str], workspace_dir: str,
) -> dict[str, dict[str, str]]:
    """Stage a frozen runner's full supplemental file set from CAS."""
    if not runner_version_id:
        return {}
    conn = get_db_connection(channel)
    try:
        rows = version_file_rows(conn, runner_version_id)
    finally:
        conn.close()
    hashes: dict[str, dict[str, str]] = {}
    prefix = runner_dir + "files/"
    for row in rows:
        if not row["filepath"].startswith(prefix):
            continue
        name = row["filepath"][len(prefix):]
        first, _, rest = name.partition("/")
        if first in folders and rest:
            category, relative = first, rest
            root = os.path.join(workspace_dir, ".collrun", "config", "folders", first)
        else:
            category, relative = "__files__", name
            root = os.path.join(workspace_dir, ".collrun", "config", "files")
        dest = safe_dest_under(root, relative)
        if dest is None:
            raise ValueError(f"Unsafe runner file: {name}")
        blob = row["blob_hash"]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _copy_required_blob(blob, dest, row["filepath"])
        hashes.setdefault(category, {})[relative] = blob
    return hashes
