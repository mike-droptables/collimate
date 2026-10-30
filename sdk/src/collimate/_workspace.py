# SPDX-License-Identifier: MIT
"""Workspace files, config reads and staged output shared by host bridges.

The mixin uses the bridge's run identity, declared UI types and staging sets;
it owns no IPC connection or generator singleton.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from typing import Any, Optional

from collimate import fs
from collimate.config_values import parse_set_yaml, resolve_generator_values
from collimate.pointers import RUNS_PREFIX, is_run_path, norm_path, run_card_path, run_label

def _is_collrun_path(path: str) -> bool:
    """``.collrun/`` is private; generators do not read or write it
    via the public ``gen.*`` API."""
    norm = path.replace("\\", "/").lstrip("/")
    return norm == ".collrun" or norm.startswith(".collrun/")


class WorkspaceMixin:
    """Filesystem and output staging on a bridge-owned workspace."""

    def _initialize_workspace(self) -> None:
        """Establish staging ownership before any workspace access.

        Writes and deletions drain at each flush. Log classification persists
        across flushes so mirrors and snapshots exclude those private artifacts.
        """
        self.tracked_writes: set[str] = set()
        self.tracked_deletions: set[str] = set()
        self.log_paths: set[str] = set()
        self._write_encodings: dict[str, str] = {}
        self._staging_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._workspace_digests = {}

    def write_refusal(self, path: str) -> Optional[str]:
        """Why an ordinary write or delete of ``path`` is refused — the
        message of the :class:`PermissionError` ``gen.update_file`` /
        ``gen.delete_file`` raise — or ``None`` when it is allowed.

        Every ``_runs/`` path is refused: another run's folder is read-only,
        the run card is the host's, and this run's own folder holds UI files
        only, written through ``gen.update_ui_file`` / ``gen.delete_ui_file``."""
        p = norm_path(path)
        if not is_run_path(p):
            return None
        if p == run_card_path(self.label):
            return (f"{p} is this run's card: the host writes it and removes "
                    "it when the run stops")
        if p.startswith(self.folder + "/"):
            return (f"{p} is in this run's folder, which holds UI files only: "
                    "write it with gen.update_ui_file and remove it with "
                    "gen.delete_ui_file")
        owner = run_label(p)
        if owner:
            return (f"{p} is locked by {owner}: another run's folder is "
                    "read-only to this run")
        return (f"{p} is under {RUNS_PREFIX}/, where this run writes only its "
                f"own UI files ({self.folder}/, with gen.update_ui_file)")


    def _refuse_foreign(self, path: str) -> None:
        reason = self.write_refusal(path)
        if reason:
            raise PermissionError(reason)


    def ui_type_declared(self, path: str) -> bool:
        """Whether ``path``'s filename ends in one of the manifest's
        ``ui_files`` suffixes (always, for a host-free run that declares
        none)."""
        if self.ui_suffixes is None:
            return True
        return any(path.endswith(suffix) for suffix in self.ui_suffixes)


    def ui_refusal(self, path: str) -> Optional[str]:
        """Why ``path`` (cell-relative) cannot be one of this run's UI
        files, or ``None`` when it can: it must lie inside the run folder,
        must not be the run card, and its suffix must be declared in the
        manifest's ``ui_files``. The :func:`gen.update_ui_file` path raises
        :class:`ValueError` with this message."""
        p = norm_path(path)
        if not p.startswith(self.folder + "/") or ".." in p.split("/"):
            return f"{p} is not inside this run's folder {self.folder}/"
        if p == run_card_path(self.label):
            return (f"{p} is this run's card: the host writes it and removes "
                    "it when the run stops")
        if not self.ui_type_declared(p):
            declared = ", ".join(self.ui_suffixes or ()) or "none"
            return (f"{p}: its type is not a declared UI file type — add its "
                    "suffix to ui_files in the generator's custom.generator "
                    f"(declared: {declared})")
        return None


    def fs_write_ui(self, path: str, content: "str | bytes") -> None:
        """Write one of this run's UI files (``path`` cell-relative, inside
        the run folder): the private half of ``gen.update_ui_file``. Tracked like
        any write, so the next ``update_cell`` flush carries it."""
        with self._stage_lock():
            p = norm_path(path)
            reason = self.ui_refusal(p)
            if reason:
                raise ValueError(reason)
            if not isinstance(content, (bytes, str)):
                raise TypeError(
                    f"ui_file: content must be str or bytes, got {type(content).__name__}")
            fs.write_file(self.workspace, p, content)
            self._write_encodings[p] = "binary" if isinstance(content, bytes) else "utf-8"
            self.log_paths.discard(p)
            self.tracked_writes.add(p)
            self.tracked_deletions.discard(p)


    def fs_unlink_ui(self, path: str, *, log: bool = False) -> None:
        """Remove one of this run's UI files from the cell: the private half
        of ``gen.delete_ui_file``. With ``log`` the file's last content is
        kept in the run's Logs under the same path (a log artifact is never a
        cell file), in the same flush that removes it from the cell."""
        with self._stage_lock():
            p = norm_path(path)
            reason = self.ui_refusal(p)
            if reason:
                raise ValueError(reason)
            self.tracked_deletions.add(p)
            if log and os.path.isfile(os.path.join(self.workspace, p)):
                # The workspace file stays: it is the log artifact's content at
                # the flush. As a log path it is no longer cell content, so the
                # listing and the live mirror skip it.
                self.log_paths.add(p)
                self.tracked_writes.add(p)
                return
            try:
                fs.unlink(self.workspace, p)
            except OSError:
                pass
            self.tracked_writes.discard(p)
            self.log_paths.discard(p)


    def ui_file_names(self) -> list[str]:
        """This run's UI files the SDK knows about — the workspace files in
        its folder (the run card excepted) whose type is declared — as
        folder-relative names, sorted."""
        prefix = self.folder + "/"
        card = run_card_path(self.label)
        return sorted(
            p[len(prefix):] for p in self.fs_list(self.folder)
            if p.startswith(prefix) and p != card and self.ui_type_declared(p))


    def fs_read_bytes(self, path: str) -> bytes:
        if self.is_private_path(path):
            raise PermissionError(f".collrun/ is reserved: {path}")
        with os.fdopen(fs.open_file(self.workspace, path, write=False), "rb") as fh:
            return fh.read()


    def fs_read_any(self, path: str) -> "str | bytes":
        """Read a workspace file as text if utf-8 decodable, else bytes.

        Binary inputs (images, archives) come back untouched instead of
        failing to decode. The file is read once, and text is lossless: its
        line endings come back as written (``\\r\\n`` stays ``\\r\\n``), so a
        read followed by a write of the same text never rewrites the file.
        """
        raw = self.fs_read_bytes(path)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw


    def fs_write_any(self, path: str, content: "str | bytes") -> None:
        """Write a workspace file, accepting either str or bytes.

        Bytes go through binary mode (``wb``); str through text mode
        (``w``, utf-8). Tracked in ``tracked_writes`` so the next
        ``update_cell`` flush picks them up regardless of encoding.
        """
        with self._stage_lock():
            if self.is_private_path(path):
                raise PermissionError(f".collrun/ is reserved: {path}")
            if not isinstance(content, (bytes, str)):
                raise TypeError(
                    f"fs_write_any: content must be str or bytes, got {type(content).__name__}",
                )
            self._refuse_foreign(path)
            fs.write_file(self.workspace, path, content)
            self._write_encodings[path] = "binary" if isinstance(content, bytes) else "utf-8"
            self.tracked_writes.add(path)
            self.tracked_deletions.discard(path)


    def fs_exists(self, path: str) -> bool:
        """Whether ``path`` is a regular file or a directory of the
        workspace, reached the way :meth:`fs_read_bytes` reaches it: no
        symlink is followed at any component, and ``.collrun/``, absolute
        and ``..`` paths are never there. A symlink (even one pointing
        inside the workspace), FIFO or device reads as absent, so for a
        file ``gen.has_file`` and ``gen.read_file`` agree (a directory is
        there, and reading it raises)."""
        p = norm_path(path)
        if self.is_private_path(p):
            return False
        try:
            parts = fs.relative_parts(p)
            dfd = fs.open_parent(self.workspace, parts, create=False)
        except OSError:
            # Refused (PermissionError), missing, or a symlinked or
            # non-directory parent component (ELOOP / ENOTDIR).
            return False
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
        except OSError:
            return False
        finally:
            os.close(dfd)
        return stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)


    def is_private_path(self, path: str) -> bool:
        """Both the host band and this process's relocated run band are private."""
        path = norm_path(path)
        if _is_collrun_path(path):
            return True
        run_dir = self.run_dir
        rel = os.path.relpath(run_dir, self.workspace).replace("\\", "/")
        return rel != ".." and not rel.startswith("../") and (path == rel or path.startswith(rel + "/"))


    def fs_list(self, under: str = "") -> list[str]:
        """Every workspace file relative path. ``.collrun/`` (the private
        band: host-staged run config) and declared log
        artifacts are filtered out so generators never see run metadata as
        cell content — several shipped call sites feed this listing
        straight to a model.

        ``under`` (a normalized workspace-relative folder) walks only that
        subtree; the paths stay workspace-relative, and a missing folder
        lists nothing."""
        ws = self.workspace
        out: list[str] = []
        for root, dirs, files in os.walk(os.path.join(ws, under) if under else ws):
            base = os.path.relpath(root, ws)
            pre = "" if base == "." else base.replace("\\", "/") + "/"
            if not pre:
                # ``.collrun`` can only match at the top level.
                dirs[:] = [d for d in dirs if d != ".collrun"]
            dirs[:] = [d for d in dirs if not self.is_private_path(pre + d)]
            for fname in files:
                rel = pre + fname.replace("\\", "/")
                if self.is_private_path(rel) or rel in self.log_paths:
                    continue
                out.append(rel)
        return out


    def fs_unlink(self, path: str) -> None:
        with self._stage_lock():
            if self.is_private_path(path):
                raise PermissionError(f".collrun/ is reserved: {path}")
            self._refuse_foreign(path)
            try:
                fs.unlink(self.workspace, path)
            except OSError:
                pass
            self.tracked_writes.discard(path)
            if path in self.log_paths:
                # A log artifact was never cell content — deleting it must not
                # emit a phantom cell_files deletion. (run_artifacts rows are
                # run-scoped and append/replace-only; the delete is local.)
                self.log_paths.discard(path)
                return
            self.tracked_deletions.add(path)


    def config(self, name: str) -> Any:
        """Read a draft file by extension.

        ``.set_yaml`` / ``.yaml`` / ``.yml`` parse YAML and merge the
        ``fields:`` defaults with ``values:`` (returns a flat dict).
        ``.json`` parses JSON. Any other name, and a missing, unreadable or
        malformed file, reads ``{}``, so callers can use ``cfg.get(...)``
        without try/except.
        """
        lower = name.lower()
        if not lower.endswith((".set_yaml", ".yaml", ".yml", ".json")):
            return {}
        # The run dispatcher seeds the user's edited draft files into
        # ``.collrun/config/files/<name>``.
        full = os.path.join(
            self.run_dir, "config", "files", name,
        )
        if not os.path.isfile(full):
            return {}
        try:
            with open(full, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return {}
        if lower.endswith(".json"):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}
        return resolve_generator_values(parse_set_yaml(raw))


    def _config_folders_root(self) -> str:
        return os.path.join(self.run_dir, "config", "folders")


    def config_folder_path(self, name: str) -> str:
        """Absolute filesystem path to a staged config folder (whether or not
        it exists) — for handing to a subprocess / Docker mount."""
        return os.path.join(self._config_folders_root(), name)


    def config_folder(self, name: str) -> dict:
        """Return every file staged under config folder ``name`` as
        ``{relpath: str | bytes}`` (relpath is relative to the folder root;
        utf-8 → ``str`` else ``bytes``). ``{}`` when the folder is absent."""
        base = self.config_folder_path(name)
        out: dict = {}
        if not os.path.isdir(base):
            return out
        for root, _dirs, files in os.walk(base):
            for fname in files:
                abs_path = os.path.join(root, fname)
                rel = os.path.relpath(abs_path, base).replace("\\", "/")
                try:
                    with open(abs_path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                try:
                    out[rel] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    out[rel] = raw
        return out


    def config_folders(self) -> list[str]:
        """Sorted names of the config folders staged for this run (the folder
        names a runner tag provided). ``[]`` when none were staged."""
        base = self._config_folders_root()
        if not os.path.isdir(base):
            return []
        return sorted(
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))
        )


    def _stage_lock(self):
        return self._staging_lock


    def take_writes(self) -> list[str]:
        with self._stage_lock():
            writes = list(self.tracked_writes)
            self.tracked_writes.clear()
            return writes


    def restage_writes(self, writes) -> None:
        with self._stage_lock():
            self.tracked_writes.update(
                p for p in writes if p not in self.tracked_deletions or p in self.log_paths)


    def _classify_write(self, path: str, category: str, pending_removal: bool) -> None:
        self.tracked_writes.add(path)
        self.tracked_deletions.discard(path)
        if category == "log":
            if path not in self.log_paths or pending_removal:
                self.tracked_deletions.add(path)
            self.log_paths.add(path)
        else:
            self.log_paths.discard(path)


    def stage_file(self, path: str, content: str | bytes, *, category: str) -> None:
        """Write and classify one staged file, including file-to-log removal."""
        with self._stage_lock():
            pending_removal = path in self.tracked_deletions
            self.fs_write_any(path, content)
            self._classify_write(path, category, pending_removal)


    def track_write(self, path: str, *, category: str = "file", max_bytes: int | None = None, ui: bool = False) -> None:
        """Track an existing regular workspace file without rewriting its bytes.

        Safe-open validation and ownership refusals match writes. A caller's
        optional size ceiling is checked on the opened file before staging.
        """
        with self._stage_lock():
            if self.is_private_path(path):
                raise PermissionError(f".collrun/ is reserved: {path}")
            if ui:
                reason = self.ui_refusal(path)
                if reason:
                    raise ValueError(reason)
            else:
                self._refuse_foreign(path)
            fd = fs.open_file(self.workspace, path, write=False)
            try:
                size = os.fstat(fd).st_size
                if max_bytes is not None and size > max_bytes:
                    raise ValueError(f"{path} is {size} bytes, over {max_bytes}")
            finally:
                os.close(fd)
            self._write_encodings.pop(path, None)
            self._classify_write(path, category, path in self.tracked_deletions)


    def snapshot_workspace(self, *, skip_dirs: Optional[set[str]] = None) -> dict[str, str]:
        """Safe workspace digests, cached by inode, size, mtime and ctime.

        Mirror and exec snapshots share this cache. A changed or replaced
        file is rehashed; unchanged content is never read again.
        """
        with self._snapshot_lock:
            return self._snapshot_workspace(skip_dirs=skip_dirs)


    def _snapshot_workspace(
        self,
        *,
        skip_dirs: Optional[set[str]] = None,
    ) -> dict[str, str]:
        """``{relpath: sha256_hex}`` for every workspace file. Pair with a
        second snapshot to detect what changed."""
        if skip_dirs is None:
            skip_dirs = fs.WORKSPACE_SKIP_DIRS
        skip_dirs = set(skip_dirs) | {".collrun"}
        result: dict[str, str] = {}
        ws = self.workspace
        if not os.path.isdir(ws):
            return result
        # Regular files only: a symlink / FIFO / socket / device is never
        # opened (a FIFO would block forever; a planted symlink would pull a
        # host file into the restage / live mirror) — the same rule as the
        # host's manifest. fwalk pins each directory by fd and never descends
        # a symlinked one, so a dir swapped for a link mid-walk can't redirect
        # the opens either.
        if fs.FD_SAFE and hasattr(os, "fwalk"):
            walk = ((r, d, f, rfd) for r, d, f, rfd in os.fwalk(ws))
        else:
            walk = ((r, d, f, None) for r, d, f in os.walk(ws))
        for root, dirs, files, rootfd in walk:
            dirs[:] = [d for d in dirs if d not in skip_dirs
                       and not self.is_private_path(os.path.relpath(os.path.join(root, d), ws).replace("\\", "/"))]
            for fname in files:
                abs_path = os.path.join(root, fname)
                rel = os.path.relpath(abs_path, ws).replace("\\", "/")
                if self.is_private_path(rel) or rel in self.log_paths:
                    continue
                try:
                    if rootfd is not None:
                        fd = os.open(fname, os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK,
                                     dir_fd=rootfd)
                    else:
                        if not stat.S_ISREG(os.lstat(abs_path).st_mode):
                            continue
                        fd = os.open(abs_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                    with os.fdopen(fd, "rb") as fh:
                        before = os.fstat(fh.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            continue
                        key = (before.st_dev, before.st_ino, before.st_size,
                               before.st_mtime_ns, before.st_ctime_ns)
                        cached = self._workspace_digests.get(rel)
                        if cached is not None and cached[0] == key:
                            result[rel] = cached[1]
                            continue
                        digest = hashlib.file_digest(fh, "sha256").hexdigest()
                        after = os.fstat(fh.fileno())
                        after_key = (after.st_dev, after.st_ino, after.st_size,
                                     after.st_mtime_ns, after.st_ctime_ns)
                        if key == after_key:
                            self._workspace_digests[rel] = (key, digest)
                        else:
                            self._workspace_digests.pop(rel, None)
                        result[rel] = digest
                except OSError:
                    continue
        self._workspace_digests = {p: v for p, v in self._workspace_digests.items()
                                   if p in result}
        return result
