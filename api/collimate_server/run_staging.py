"""Generator package discovery, frozen workspace hydration and runner staging."""

from collimate_server.cell_placement import capabilities_stack_cells
import hashlib
import logging
import os
import secrets
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass

import yaml

from collimate import fs

from collimate_server.cas import channel_store
from collimate_server.database import get_db_connection
from collimate_server.versions import find_tip

logger = logging.getLogger(__name__)

from collimate_server.versions import version_file_rows


def resolve_generator_cell(gid: str, channel: str, *, cell_id: str | None = None):
    """The Capabilities cell (``cell_id``, when given) and package prefix of
    the generator ``gid`` in ``channel`` (its immutable UUID): only a visible
    package whose YAML declares the requested id. ``channel`` is required, so
    run-context code never resolves another channel's generators."""
    from collimate_server.runners import definition_files
    from collimate_server.state import get_channel_by_id
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
        from collimate_server.runners import RunnerError
        raise RunnerError(409, f"Duplicate generator id {gid!r}")
    return matches[0] if matches else None


def generator_cell_id(gid: str, channel: str) -> str | None:
    """Source cell selected by the same package resolver used for staging."""
    resolved = resolve_generator_cell(gid, channel)
    return str(resolved[0].id) if resolved else None


def _copy_required_blob(store, digest: str, dest: str, name: str) -> None:
    """Copy verified bytes from ``store`` with their source mode;
    missing/corrupt input aborts."""
    try:
        with store.publication_lock:
            raw = store.require_blob(digest)
            source_stat = os.stat(store.blob_path(digest))
    except OSError as exc:
        raise OSError(f"Required staging body unavailable for {name}: {exc}") from exc
    with open(dest, "wb") as handle:
        handle.write(raw)
    os.chmod(dest, stat.S_IMODE(source_stat.st_mode))
    os.utime(dest, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


def _write_source_file(store, dest: str, file) -> None:
    """Materialize one required frozen generator source file or raise."""
    if file.blob_hash:
        _copy_required_blob(store, file.blob_hash, dest, file.name)
    else:
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(file.content)


def find_generator_dir(
    generator_name: str, channel: str, *, cell_id: str | None = None,
) -> str | None:
    """Stage a frozen package with its resolved libraries beside it."""
    snapshot = snapshot_generator_source(generator_name, channel, cell_id=cell_id)
    if snapshot is None:
        return None
    tmp = tempfile.mkdtemp(prefix=f"collimate_gen_{generator_name}_")
    try:
        from collimate_server.cas import channel_store
        return materialize_generator_source(snapshot, tmp, channel_store(channel))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def snapshot_generator_source(generator_name: str, channel: str, *, cell_id: str | None = None):
    """Freeze file field values while the caller holds the source/state locks:
    the generator's package under its folder's name, and beside it each
    library its cell names under ``shared:`` (``runners.generator_libraries``:
    from Capabilities, else the app's), under the library's id."""
    from collimate_server.runners import generator_libraries, generator_source_files
    from collimate_server.state import get_channel_by_id
    resolved = resolve_generator_cell(generator_name, channel, cell_id=cell_id)
    if resolved is None:
        return None
    source_cell, target_prefix = resolved
    gen_subdir = target_prefix.rstrip("/").rsplit("/", 1)[-1]
    # Libraries keep their ids as folder names; reserve a distinct app dir.
    if gen_subdir.startswith("_"):
        gen_subdir = "generator"
    writes = []
    for file in generator_source_files(source_cell, target_prefix):
        writes.append((gen_subdir + "/" + file.name[len(target_prefix):], file.model_copy()))
    if not writes:
        return None
    for library in generator_libraries(get_channel_by_id(channel), source_cell):
        writes.extend((file.name, file.model_copy()) for file in library.files)
    return gen_subdir, writes


def materialize_generator_source(snapshot, app_root: str, store) -> str:
    """Write a frozen source tree once, outside global state_lock; binary
    bodies come from ``store``, the generator channel's body store."""
    gen_subdir, writes = snapshot
    for rel, file in writes:
        dest = safe_dest_under(app_root, rel)
        if dest is None:
            raise ValueError(f"Invalid generator source path: {rel}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _write_source_file(store, dest, file)
    return os.path.join(app_root, gen_subdir)


#: Where the stable generator trees live, relative to the data dir (the
#: server's CWD): one read-only folder per generator version.
ENVS_DIR = os.path.join(".collimate", "envs")
#: The name a staged tree's folder takes while it is being written.
STAGING_PREFIX = ".staging-"
#: The file whose presence says a tree is whole.
COMPLETE = ".complete"
_KEY_DOMAIN = b"collimate-envtree-v1\0"


@dataclass(frozen=True)
class StableTree:
    """Where one generator version's read-only tree lives.

    ``key`` is :func:`stable_tree_key` of the source snapshot; the folder is
    ``envs/<gen_subdir>-<key[:16]>``. ``root`` holds the generator's own
    folder (``gen_dir``, a run's cwd) and each library beside it."""

    key: str
    gen_subdir: str

    @property
    def name(self) -> str:
        return f"{self.gen_subdir}-{self.key[:16]}"

    @property
    def root(self) -> str:
        return os.path.abspath(os.path.join(ENVS_DIR, self.name))

    @property
    def gen_dir(self) -> str:
        return os.path.join(self.root, self.gen_subdir)


def _tree_mode(file, store) -> int:
    """The mode ``file`` has in a stable tree: 0555 when its body is
    executable (a body keeps the mode of its stored copy, as
    :func:`_copy_required_blob` copies it), else 0444."""
    if file.blob_hash:
        executable = os.stat(store.blob_path(file.blob_hash)).st_mode & 0o111
    else:
        executable = 0
    return 0o555 if executable else 0o444


def stable_tree_key(snapshot, store) -> str:
    """The sha256 that names a source snapshot's tree: over a domain tag
    and, for each ``(rel, file)`` sorted by ``rel``, the path, the mode the
    tree gives it and its body digest (the sha256 of inline content). The
    generator's PEP 723 header and its libraries are inside the snapshot,
    so its requirements are covered; the interpreter is not (uv re-creates
    an environment whose interpreter changed, at the same path)."""
    _gen_subdir, writes = snapshot
    digest = hashlib.sha256(_KEY_DOMAIN)
    for rel, file in sorted(writes, key=lambda item: item[0]):
        body = file.blob_hash or hashlib.sha256((file.content or "").encode("utf-8")).hexdigest()
        digest.update(f"{rel}\0{_tree_mode(file, store):o}\0{body}\0".encode("utf-8"))
    return digest.hexdigest()


def stable_tree(snapshot, store) -> StableTree:
    """The stable tree a run of ``snapshot`` uses (not staged yet); ``store``
    holds the snapshot's binary bodies (the generator channel's)."""
    return StableTree(key=stable_tree_key(snapshot, store), gen_subdir=snapshot[0])


_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def tree_lock(key: str) -> threading.Lock:
    """The lock that serializes staging and deleting the tree of ``key``
    (one server per data dir, so a thread lock is enough)."""
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _seal(top: str, *, seal_top: bool = True) -> None:
    """Give every file under ``top`` its tree mode (0444, or 0555 where it
    is executable) and fsync it, then every directory 0555, deepest first,
    each fsynced. ``seal_top=False`` leaves ``top`` itself writable: macOS
    refuses to rename a directory its caller cannot write (EACCES), so the
    staging folder is sealed last, after its rename into place."""
    directories = []
    for dirpath, _dirnames, filenames, dir_fd in os.fwalk(top, follow_symlinks=False):
        for name in filenames:
            fd = os.open(name, os.O_RDONLY | fs.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                mode = stat.S_IMODE(os.fstat(fd).st_mode)
                os.fchmod(fd, 0o555 if mode & 0o111 else 0o444)
                os.fsync(fd)
            finally:
                os.close(fd)
        directories.append(dirpath)
    for directory in reversed(directories):
        fd = os.open(directory, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
        try:
            os.fsync(fd)
            if seal_top or directory != top:
                os.fchmod(fd, 0o555)
        finally:
            os.close(fd)


def _seal_root(root: str) -> None:
    """The renamed tree's own directory: 0555 and fsynced."""
    fd = os.open(root, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o555)
        os.fsync(fd)
    finally:
        os.close(fd)


def stage_stable_tree(snapshot, tree: StableTree, store) -> bool:
    """Make ``tree`` exist, whole and read-only; True when this call wrote
    it. Under the key's lock: a tree whose ``.complete`` exists is reused;
    otherwise the snapshot is written into ``envs/.staging-<key16>-<rand>``,
    sealed (:func:`_seal`), marked ``.complete`` and renamed to its name.
    A folder left at that name without ``.complete`` (a deletion cut
    short) is removed first."""
    import collimate_server.readonly_trees as readonly_trees
    envs = os.path.abspath(ENVS_DIR)
    with tree_lock(tree.key):
        if os.path.isfile(os.path.join(tree.root, COMPLETE)):
            return False
        os.makedirs(envs, exist_ok=True)
        if os.path.lexists(tree.root):
            readonly_trees.remove_tree(tree.root)
        staging = os.path.join(envs, f"{STAGING_PREFIX}{tree.key[:16]}-{secrets.token_hex(4)}")
        os.mkdir(staging, 0o755)
        try:
            materialize_generator_source(snapshot, staging, store)
            with open(os.path.join(staging, COMPLETE), "w", encoding="utf-8") as marker:
                marker.write(tree.key + "\n")
            _seal(staging, seal_top=False)
            try:
                os.rename(staging, tree.root)
            except OSError:
                if not os.path.isfile(os.path.join(tree.root, COMPLETE)):
                    raise
                readonly_trees.remove_tree(staging, ignore_errors=True)
                return False
            _seal_root(tree.root)
        except BaseException:
            readonly_trees.remove_tree(staging, ignore_errors=True)
            raise
        fd = os.open(envs, os.O_RDONLY | fs.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True


def remove_staging_trees() -> int:
    """Remove every ``envs/.staging-*`` a stopped server left (startup,
    before any run); returns how many."""
    import collimate_server.readonly_trees as readonly_trees
    try:
        names = os.listdir(ENVS_DIR)
    except FileNotFoundError:
        return 0
    removed = 0
    for name in names:
        if name.startswith(STAGING_PREFIX):
            readonly_trees.remove_tree(os.path.join(ENVS_DIR, name), ignore_errors=True)
            removed += 1
    return removed


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
) -> list[str]:
    """Copy the version ``tip_id`` of ``cell_id`` (its current tip when
    omitted) from the CAS into ``dest_dir``, at the workspace root, and
    return the cell paths it wrote.

    ``tip_id`` is the tip the run-start flush resolved for this run — trusted
    rather than re-walked, so a concurrent commit landing between the flush
    and the hydrate cannot split the run across two versions of its cell.

    ``.collrun/*`` is filtered out — that namespace is reserved for the SDK —
    and so is ``_runs/`` with ``strip_runs`` (a copy into another run's
    workspace never carries run folders)."""
    from collimate_server.reserved_paths import is_reserved_path
    from collimate_server.run_folders import is_run_path

    conn = get_db_connection(channel)
    try:
        if tip_id is None:
            tip_id = find_tip(conn, cell_id)
        rows = version_file_rows(conn, tip_id)
    finally:
        conn.close()

    copied: list[str] = []
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
        _copy_required_blob(channel_store(channel), row['blob_hash'], dest, row['filepath'])
        copied.append(row["filepath"])
    logger.info("cell %s: %d required files copied from %d snapshot entries",
                cell_id[:8], len(copied), len(rows))
    return copied


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
        _copy_required_blob(channel_store(channel), blob, dest, row["filepath"])
        hashes.setdefault(category, {})[relative] = blob
    return hashes
