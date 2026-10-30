# SPDX-License-Identifier: MIT
"""Bidirectional file reconciliation for native.live_files.

Native owns the active run and public file operations. These endpoints resolve
that owner lazily at use, so importing this module creates no run state or
native import cycle. One Mirror owns one reconciliation/held-refusal state.
"""
from __future__ import annotations

import asyncio
import fnmatch
import posixpath

from collimate import fs
from collimate._host.bridge import PartialRefusal as _PartialRefusal
from collimate.pointers import is_run_path, run_label, run_card_path


def _runtime():
    from collimate import native
    return native


def mirror_skip(path: str, exclude: tuple, skip_globs: tuple = ()) -> bool:
    log_paths = getattr(_runtime()._bridge, "log_paths", None) or ()
    return (
        (not path)
        or path == ".collrun" or path.startswith(".collrun/")
        or (_runtime()._bridge is not None and _runtime()._bridge.is_private_path(path))
        or path in log_paths
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in skip_globs)
        or (bool(exclude) and path.endswith(exclude))
    )


class FileEndpoint:
    """One side of a mirror. ``hashes`` returns ``{path: sha256}``; ``read``
    returns one file's content (``str``/``bytes``); ``write``/``delete``
    mutate; ``commit`` flushes any batched writes (only the cell needs it)."""

    label = "endpoint"

    async def hashes(self) -> dict:
        raise NotImplementedError

    async def read(self, path):
        raise NotImplementedError

    async def write(self, path, content) -> None:
        raise NotImplementedError

    async def delete(self, path) -> None:
        raise NotImplementedError

    async def observed(self, path, digest) -> None:
        """Record a revision only after this content (or deletion) was observed."""
        return None

    async def commit(self) -> None:
        return None


class LocalWorkspace(FileEndpoint):
    """This run's own workspace dir (the one a Docker generator bind-mounts
    into its container). Reads/writes go straight to disk — never staged for
    the cell, since the cell endpoint owns that direction."""

    label = "workspace"

    def __init__(self, skip_dirs):
        self._skip_dirs = set(skip_dirs or ())

    async def hashes(self) -> dict:
        # A full walk + sha256 of every file: off the event loop, so chat
        # deltas, polls and Stop keep flowing while it runs.
        return await asyncio.to_thread(_runtime().snapshot, skip_dirs=self._skip_dirs)

    async def read(self, path):
        return _runtime().read_file(path)

    async def write(self, path, content) -> None:
        # Never through a symlink: a container harness (root in this bind
        # mount) could swap a synced file for a link to a host path, and a
        # following cell→workspace pull would then write the cell's content
        # onto that host file. fs.write_file refuses (ELOOP) instead.
        fs.write_file(_runtime().workspace_path(), path, content)

    async def delete(self, path) -> None:
        fs.unlink(_runtime().workspace_path(), path)


class CellStore(FileEndpoint):
    """The cell's accepted revisions, written back via
    tracking existing workspace files + a single ``commit()`` flush per tick."""

    label = "cell"

    def __init__(self):
        self._cache: dict = {}
        self._descriptors: dict = {}

    async def hashes(self) -> dict:
        # A transient host/DB hash-read failure must PROPAGATE
        # (so Mirror._tick skips this pass) rather than return {} — an empty
        # result that looks like "the cell has no files" would make the engine
        # treat every synced file as deleted and remove it. See Mirror._tick.
        self._cache.clear()
        self._descriptors = await asyncio.to_thread(_runtime().require_bridge().read_cell_descriptors)
        return {path: item["body_hash"] for path, item in self._descriptors.items()
                if not item.get("deleted")}

    async def read(self, path):
        if path in self._cache:
            return self._cache[path]
        files = await asyncio.to_thread(
            _runtime().require_bridge().read_cell_files, paths=[path],
            revisions={path: self._descriptors[path]["file_revision"]})
        if path not in files:
            raise FileNotFoundError(path)
        self._cache.update(files)
        return files[path]

    async def observed(self, path, digest) -> None:
        descriptor = self._descriptors.get(path)
        if descriptor and descriptor["body_hash"] == digest:
            await asyncio.to_thread(_runtime().require_bridge()._accept_clean_file_revisions,
                                    [{"path": path, **descriptor}])

    async def write(self, path, content) -> None:
        # The mirror pushes a run-folder path only for this run's declared
        # UI files (see Mirror._rule), and those go through the UI path.
        if is_run_path(path):
            bridge = _runtime().require_bridge()
            reason = bridge.ui_refusal(path)
            if reason:
                raise ValueError(reason)
            bridge.track_write(path, ui=True)
        else:
            # Local endpoint already holds the output; marking it must never
            # overwrite a concurrent edit or change bytes/mtime.
            _runtime().track_file(path)

    async def delete(self, path) -> None:
        # NO has_file() guard: has_file checks the WORKSPACE, but in the
        # workspace→cell delete path the file is already gone from the workspace
        # (that's why it's being deleted), so the guard would always be False and
        # the deletion would never be staged — the cell would keep the file and
        # resurrect it next tick. delete_file/fs_unlink no-ops harmlessly when
        # the workspace file is absent and records tracked_deletions, which is
        # exactly what the next update() flush needs to drop it from the cell.
        if is_run_path(path):
            _runtime().require_bridge().fs_unlink_ui(path)
        else:
            _runtime().delete_file(path)

    async def commit(self) -> None:
        await _runtime().commit()


class Mirror:
    """Bidirectional, echo-suppressed sync between two :class:`FileEndpoint`\\ s,
    run for the lifetime of an ``async with`` block.

    One reconcile pass per ``interval``: each path is compared on both sides
    against a shared ``path -> sha256`` map. Whichever side changed relative to
    the last sync is propagated to the other and re-recorded, so a value can't
    ping-pong. Simultaneous edits are proposed against the last observed cell
    revision; the host retains a conflict instead of replacing competing bytes.
    ``to_remote``/``from_remote`` gate the two directions. A ``guard`` callable
    (host present?) makes the whole thing a clean no-op in dev
    mode. A final reconcile on exit captures a write landing after the last
    tick.

    The ``remote`` (right) side is always the cell. Run folders follow
    ``own_folder`` (the local side's run folder, ``_runs/<label>``):

    * inside ``own_folder`` (minus the run card) — pulled always, pushed
      only for the UI file types the manifest declares (``ui_files``); a
      file of any other type the local side drops there is never pushed and
      is restored from the cell (removed, as the cell never has one), with a
      ``[live] restored <path> (undeclared ui type)`` run-log line;
    * the run card and every other ``_runs/`` path — the CELL WINS: never
      pushed, and whenever the local copy differs from the cell's it is
      written back (or removed) from the cell, with a ``[live] restored``
      run-log line when the local side had changed it;
    """

    def __init__(self, local, remote, *, own_folder: str,
                 to_remote=True, from_remote=True,
                 exclude=(), interval=0.5, guard=None, skip_dirs=(), skip_globs=(), on_refused=None,
                 on_changed=None):
        self._a = local
        self._b = remote
        self._own_folder = own_folder
        self._own_card = run_card_path(posixpath.basename(own_folder))
        self._a_to_b = bool(to_remote)
        self._b_to_a = bool(from_remote)
        self._exclude = tuple(exclude)
        self._skip_globs = tuple(skip_globs)
        self._skip_dirs = frozenset(skip_dirs or ())
        self._interval = max(0.1, float(interval))
        self._synced: dict = {}
        self._task = None
        self._stopped = False
        self._guard = guard
        self._failures = 0
        self._tick_lock = asyncio.Lock()
        self._pending_failure: Exception | None = None
        if on_refused is not None and not callable(on_refused):
            raise TypeError('on_refused must be a synchronous callable')
        self._on_refused = on_refused
        if on_changed is not None and not callable(on_changed):
            raise TypeError('on_changed must be a synchronous callable')
        self._on_changed = on_changed
        self._held: dict[str, str | None] = {}

    def _failed(self, exc: Exception) -> None:
        self._failures += 1
        if self._failures == 3:
            _runtime().log(f"[live] sync failed three times: {exc}")

    def _recovered(self) -> None:
        if self._failures >= 3:
            _runtime().log("[live] sync recovered")
        self._failures = 0

    async def _reconcile(self) -> None:
        try:
            await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Even a later successful background tick cannot hide a refused
            # last write from a caller waiting to finish its work.
            if self._pending_failure is None:
                self._pending_failure = exc
            self._failed(exc)
        else:
            self._recovered()

    async def flush(self) -> None:
        """Reconcile the current output and wait for its host acknowledgement.

        Background failures are reported once here, even if a later tick
        recovered. The caller can repair the output and flush again while its
        input surface remains open. A failed or empty snapshot is never an
        acknowledgement. Without a host this is a no-op.
        """
        if self._guard is not None and not self._guard():
            return
        async with self._tick_lock:
            if self._pending_failure is not None:
                failure, self._pending_failure = self._pending_failure, None
                raise failure
            try:
                await self._tick_locked(final=True)
            except Exception as exc:
                self._failed(exc)
                raise
            else:
                self._recovered()

    async def __aenter__(self) -> "Mirror":
        if self._guard is not None and not self._guard():
            return self
        try:
            a0, b0 = await self._a.hashes(), await self._b.hashes()
            for p in set(a0) & set(b0):
                if a0[p] == b0[p]:
                    self._synced[p] = a0[p]
        except Exception as exc:
            self._failed(exc)
        else:
            self._recovered()
        self._task = asyncio.ensure_future(self._loop())
        return self

    async def _loop(self) -> None:
        while not self._stopped and not _runtime().require_bridge().cancelled:
            await asyncio.sleep(self._interval)
            if self._stopped:
                break
            await self._reconcile()

    def _rule(self, path: str) -> str:
        """How ``path`` is mirrored: ``"skip"`` (neither way), ``"cell"``
        (the cell wins: never pushed, restored locally), ``"undeclared"``
        (an own-folder file of a type the manifest does not declare: the
        cell wins, too) or ``"sync"``."""
        if mirror_skip(path, self._exclude, self._skip_globs):
            return "skip"
        if self._skip_dirs and any(
                seg in self._skip_dirs for seg in path.split("/")[:-1]):
            return "skip"
        if not is_run_path(path):
            return "sync"
        if path.startswith(self._own_folder + "/") and path != self._own_card:
            return "sync" if _runtime().require_bridge().ui_type_declared(path) else "undeclared"
        return "cell"

    async def _restore(self, p: str, ah, bh, sh, rule: str) -> None:
        """Make the local copy of a cell-wins path the cell's (``bh`` its
        hash; ``None`` = absent from the cell)."""
        if bh is None:
            await self._a.delete(p)
            self._synced.pop(p, None)
        else:
            await self._a.write(p, await self._b.read(p))
            self._synced[p] = bh
        if rule == "undeclared":
            _runtime().log(f"[live] restored {p} (undeclared ui type)")
        elif ah != sh:
            what = "restored" if bh is not None else "removed"
            _runtime().log(f"[live] {what} {p} (read-only, owned by {run_label(p) or 'no run'})")
        else:
            _runtime().log(f"[live] {self._b.label}→{self._a.label}: "
                f"{'deleted' if bh is None else 'pulled'} {p}")

    async def _tick(self) -> None:
        async with self._tick_lock:
            await self._tick_locked()

    async def _tick_locked(self, *, final: bool = False) -> None:
        a_h = await self._a.hashes()
        b_h = await self._b.hashes()
        # Transient-empty guard. If we already have synced state but one side now
        # reports ZERO files, that is almost always a raced/failed snapshot, not
        # a real wipe — propagating it would mass-delete the other side. Skip the
        # whole reconcile this tick rather than destroy files. (CellStore.hashes
        # RAISES on a host read error so it can't reach here as a silent {}; this
        # guards workspace snapshot paths, which can momentarily read
        # empty. A genuine "user emptied the cell" is left for them to re-trigger
        # — never inferred from an empty read.)
        if self._synced and (not a_h or not b_h):
            if final:
                raise RuntimeError("live output snapshot is unexpectedly empty; retry after checking the workspace")
            return
        a_dirty = b_dirty = False
        errors = []
        # What each path's echo entry was before this tick pushed it to a
        # side (``None`` = none): restored when that side's commit fails.
        a_undo: dict = {}
        b_undo: dict = {}
        b_attempted: dict = {}
        pulled: set[str] = set()
        for p in set(a_h) | set(b_h) | set(self._synced) | set(self._held):
            rule = self._rule(p)
            if rule == "skip":
                continue
            try:
                ah, bh, sh = a_h.get(p), b_h.get(p), self._synced.get(p)
                released = p in self._held
                if released:
                    if ah == self._held[p]:
                        # A rejected edit belongs to its author. Neither retry
                        # nor restore it until the workspace bytes change.
                        continue
                    del self._held[p]
                if ah == bh:
                    # Matching bytes are an observation, not an inventory-only rebase.
                    await self._b.observed(p, bh)
                    self._synced.pop(p, None) if ah is None else self._synced.__setitem__(p, ah)
                    continue
                if rule in ("cell", "undeclared"):
                    # Read-only here: never pushed; the cell's copy stands.
                    if self._b_to_a:
                        a_undo.setdefault(p, sh)
                        await self._restore(p, ah, bh, sh, rule)
                        a_dirty = True
                        if bh != sh:
                            pulled.add(p)
                    continue
                if (released or ah != sh) and self._a_to_b:  # left changed → push to right
                    b_undo.setdefault(p, sh)
                    b_attempted[p] = ah
                    if ah is None:
                        await self._b.delete(p)
                        self._synced.pop(p, None)
                    else:
                        await self._b.write(p, await self._a.read(p))
                        self._synced[p] = ah
                    b_dirty = True
                elif bh != sh and self._b_to_a:        # right changed → push to left
                    a_undo.setdefault(p, sh)
                    if bh is None:
                        await self._a.delete(p)
                        await self._b.observed(p, None)
                        self._synced.pop(p, None)
                    else:
                        await self._a.write(p, await self._b.read(p))
                        self._synced[p] = bh
                    a_dirty = True
                    pulled.add(p)
                    # The inbound direction is the only path that can change
                    # a file under a running generator, so every pull leaves
                    # a run-log fingerprint — a reverted workspace file then
                    # lines up with exactly which side wrote it and when.
                    try:
                        _runtime().log(f"[live] {self._b.label}→{self._a.label}: "
                            f"{'deleted' if bh is None else 'pulled'} {p}")
                    except Exception:  # noqa: BLE001 — logging never breaks a tick
                        pass
            except Exception as exc:
                # One racing path (a file read mid-write, a transient unlink)
                # must not abort the whole reconcile or skip the commit for the
                # paths that already synced this tick.
                errors.append(exc)
        for dirty, side, undo in ((b_dirty, self._b, b_undo), (a_dirty, self._a, a_undo)):
            if dirty:
                try:
                    await self._commit(side, undo, b_attempted if side is self._b else {})
                except Exception as exc:
                    errors.append(exc)
                else:
                    if side is self._a and self._on_changed is not None:
                        for path in sorted(pulled):
                            try:
                                self._on_changed(path)
                            except Exception as exc:
                                _runtime().log(f'[live] on_changed failed: {exc}')
        if errors:
            raise errors[0]

    async def _commit(self, side: "FileEndpoint", undo: dict, attempted: dict) -> None:
        """Commit ``side``'s writes of this tick. When the commit fails, or
        is cancelled before its flush landed, this tick's pushes to that
        side may not have landed, so the echo entries it recorded for them
        are rolled back: the next tick sees each change as still unsynced
        and pushes it again (a second commit of a push that did land is
        harmless), instead of taking the other side's stale copy for the
        newer one and pulling it back over it. A :class:`PartialRefusal`
        committed the rest: only its refused members roll back their echo
        entries and are held until another workspace edit. A cancel that came once the flush was on
        its way waited for it (:func:`commit` says on the cancel whether it
        landed), so their entries stand: rolled back, a committed path the
        user then edits would be pushed again over that edit."""
        try:
            await side.commit()
        except _PartialRefusal as exc:
            self._hold_refused(exc, undo, attempted if side is self._b else {})
            raise
        except BaseException as exc:
            partial = getattr(exc, '_flush_refusal', None)
            if isinstance(partial, _PartialRefusal):
                self._hold_refused(partial, undo, attempted if side is self._b else {})
            elif not getattr(exc, "_flush_landed", False):
                for p, prev in undo.items():
                    if prev is None:
                        self._synced.pop(p, None)
                    else:
                        self._synced[p] = prev
            raise

    def _hold_refused(self, exc, undo, attempted):
        for p in exc.paths & undo.keys():
            prev = undo[p]
            if prev is None:
                self._synced.pop(p, None)
            else:
                self._synced[p] = prev
            if p in attempted:
                # A newer local edit during verification releases this hold.
                self._held[p] = attempted[p]
        for p in sorted(exc.paths & attempted.keys()):
            error = exc.errors.get(p, str(exc))
            _runtime().log(f'[live] skipped {p}: {error}')
            if self._on_refused is not None:
                try:
                    self._on_refused(p, error)
                except Exception as callback_error:
                    # Preserve the held edit and explicit PermissionError.
                    _runtime().log(f'[live] on_refused failed: {callback_error}')

    async def __aexit__(self, *exc) -> None:
        self._stopped = True
        if self._task is None:
            # The guard disabled the mirror (no host): nothing
            # ran, so there is nothing to reconcile.
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        # Final reconcile so a write between the last tick and teardown is kept.
        await self._reconcile()


