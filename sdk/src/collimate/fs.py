# SPDX-License-Identifier: MIT
"""Symlink-safe file access under a root directory — public.

A containerized harness is root in the bind-mounted workspace, so it can plant
(or race in) a symlink or FIFO that host-side code would otherwise follow: a
plain ``open(join(root, p))`` then reads or overwrites any host file the user
can reach, and a FIFO blocks the reader forever. Every open here walks the
path one component at a time from a directory fd with ``O_NOFOLLOW`` and
refuses anything that isn't a regular file, so the check and the open are the
same syscall — nothing can be swapped in between (a separate realpath check
can always be raced).

The SDK's own workspace I/O (``gen.update_file``, ``gen.read_file``, the
live-files mirror) goes through these helpers, and generator code that touches
a directory a container can write uses them too.

Where the platform lacks ``dir_fd`` support (:data:`FD_SAFE` is False —
Windows), every helper falls back to refusing a path whose resolved location
escapes the root (for :func:`unlink`, the resolved location of the entry's
directory); callers that need the race-free walk check ``FD_SAFE``.
:func:`walk_regular` walks a whole tree under the same rules.
"""
from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable, Iterator

__all__ = [
    "WORKSPACE_SKIP_DIRS",
    "FD_SAFE",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "O_NONBLOCK",
    "append_bytes",
    "open_file",
    "open_parent",
    "relative_parts",
    "unlink",
    "walk_regular",
    "write_file",
]

WORKSPACE_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})


#: Whether this platform supports the fd-relative, no-follow walk.
FD_SAFE: bool = (
    hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)
#: ``os.O_NOFOLLOW`` / ``os.O_NONBLOCK`` / ``os.O_DIRECTORY``, or 0 where the
#: platform has none.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)
O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)


def relative_parts(path: str) -> list[str]:
    """``path`` as root-relative components. An absolute or ``..`` path is
    refused with :class:`PermissionError` (a per-component walk would
    otherwise climb out)."""
    raw = str(path).replace("\\", "/")
    norm = os.path.normpath(raw).replace("\\", "/") if raw else ""
    if not norm or norm == "." or raw.startswith("/") or os.path.isabs(norm):
        raise PermissionError(f"not a workspace-relative path: {path!r}")
    if norm == ".." or norm.startswith("../"):
        raise PermissionError(f"path escapes the workspace: {path!r}")
    return norm.split("/")


def _check_parts(parts: list) -> None:
    """Refuse (:class:`PermissionError`) any component that is not one plain
    directory-entry name: an empty, ``.`` or ``..`` component climbs or
    stays put, and one holding a separator is a path of its own (an absolute
    one ignores ``dir_fd`` entirely)."""
    if not parts:
        raise PermissionError("no path components")
    for comp in parts:
        if (not isinstance(comp, str) or comp in ("", ".", "..") or "\0" in comp
                or "/" in comp or os.sep in comp
                or (os.altsep is not None and os.altsep in comp)):
            raise PermissionError(f"not a plain path component: {comp!r}")


def _refuse_escape(root: str, full: str, path: str) -> None:
    """The no-``dir_fd`` fallback's check: ``full`` must resolve inside
    ``root``."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(full)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise PermissionError(f"path resolves outside the workspace: {path!r}")


def open_parent(root: str, parts: list[str], *, create: bool) -> int:
    """An fd on the directory holding ``parts[-1]`` under ``root``, reached
    without following any symlinked component. ``create`` makes missing
    directories on the way (as ``makedirs`` would). Every component must be a
    plain name (:func:`relative_parts` gives such a list); anything else is
    refused with :class:`PermissionError`. The caller closes the fd."""
    _check_parts(parts)
    fd = os.open(root, os.O_RDONLY | O_DIRECTORY)
    try:
        for comp in parts[:-1]:
            try:
                nxt = os.open(comp, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(comp, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(comp, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(root: str, path: str, *, write: bool, append: bool = False) -> int:
    """Open ``root/path`` without following a symlink at ANY component and
    return the fd (a regular file only). ``write`` truncates/creates it and
    its missing parent directories (``append`` creates without truncating and
    appends); otherwise it is opened read-only. The caller closes the fd."""
    parts = relative_parts(path)
    if write and append:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    elif write:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    else:
        flags = os.O_RDONLY
    # O_NONBLOCK: opening a FIFO must fail/return at once, never block.
    flags |= O_NOFOLLOW | O_NONBLOCK
    if FD_SAFE:
        dfd = open_parent(root, parts, create=write)
        try:
            fd = os.open(parts[-1], flags, 0o666, dir_fd=dfd)
        finally:
            os.close(dfd)
    else:
        full = os.path.join(root, *parts)
        _refuse_escape(root, full, path)
        if write:
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        fd = os.open(full, flags | getattr(os, "O_BINARY", 0), 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path!r}")
        if O_NONBLOCK:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def unlink(root: str, path: str) -> None:
    """Remove ``root/path`` (or a symlink itself — never its target) without
    following a symlinked parent directory. A missing path is a no-op."""
    parts = relative_parts(path)
    if not FD_SAFE:
        full = os.path.join(root, *parts)
        _refuse_escape(root, os.path.dirname(full), path)
        if os.path.lexists(full) and not os.path.isdir(full):
            os.remove(full)
        return
    try:
        dfd = open_parent(root, parts, create=False)
    except (FileNotFoundError, NotADirectoryError):
        return
    try:
        os.unlink(parts[-1], dir_fd=dfd)
    except FileNotFoundError:
        pass
    finally:
        os.close(dfd)


def append_bytes(root: str, path: str, data: bytes) -> None:
    """Append ``data`` to ``root/path`` through :func:`open_file` (created if
    missing) — never through a planted symlink or FIFO."""
    fd = open_file(root, path, write=True, append=True)
    with os.fdopen(fd, "ab") as fh:
        fh.write(data)


def write_file(root: str, path: str, content: "str | bytes") -> None:
    """Write ``content`` (text as utf-8, bytes verbatim) to ``root/path``
    through :func:`open_file` — never through a planted symlink."""
    if not isinstance(content, (str, bytes, bytearray)):
        raise TypeError(
            f"content must be str or bytes, got {type(content).__name__}",
        )
    fd = open_file(root, path, write=True)
    if isinstance(content, str):
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    else:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)


def walk_regular(
    top: str,
    *,
    dir_fd: "int | None" = None,
    prune: "Callable[[str, os.DirEntry], bool] | None" = None,
    max_depth: "int | None" = None,
    max_entries: "int | None" = None,
    onerror: "Callable[[OSError], None] | None" = None,
) -> "Iterator[tuple[str, int]]":
    """Yield ``(path, fd)`` for every regular file under ``top``: ``path``
    relative to ``top`` with ``/`` separators, ``fd`` open read-only on the
    file and closed by the walk when it resumes (the caller never closes it).

    Nothing is followed. ``top`` is opened ``O_NOFOLLOW`` (from ``dir_fd``
    when given), each directory through its parent's fd, and each file
    ``O_NOFOLLOW|O_NONBLOCK`` from its directory's fd and checked regular by
    ``fstat``, so a symlink, FIFO, socket or device is never entered, opened
    or yielded, nor an entry swapped for one after the listing. The walk
    holds one directory fd per level it is inside.

    ``prune(path, entry)`` sees every entry, as its path and
    :class:`os.DirEntry`, before the walk acts on it; True leaves it out (a
    directory is not entered, a file not opened). A directory more than
    ``max_depth`` levels below ``top`` is not entered, and the walk stops
    once it has seen ``max_entries`` entries. Both, and every directory or
    file that cannot be opened or listed, go to ``onerror`` as an
    :class:`OSError` whose ``filename`` is the entry's path ("" for ``top``);
    without ``onerror`` they are skipped silently.

    Without dir_fd support (:data:`FD_SAFE` False) directories are listed
    by path and a file is opened only after ``lstat`` says it is regular;
    ``dir_fd`` then cannot be honoured and raises :class:`ValueError`."""
    if dir_fd is not None and not FD_SAFE:
        raise ValueError("walk_regular: dir_fd needs dir_fd support (FD_SAFE)")

    def report(path: str, exc: OSError) -> None:
        if onerror is not None:
            exc.filename = path
            onerror(exc)

    dir_flags = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK
    file_flags = os.O_RDONLY | O_NOFOLLOW | O_NONBLOCK | getattr(os, "O_BINARY", 0)

    def listing(handle: "int | str") -> list:
        with os.scandir(handle) as it:
            return sorted(it, key=lambda entry: entry.name)

    def enter(parent: "int | str | None", name: str) -> "int | str":
        if not FD_SAFE:
            full = name if parent is None else os.path.join(parent, name)
            if not stat.S_ISDIR(os.lstat(full).st_mode):
                raise NotADirectoryError(errno.ENOTDIR, "not a directory")
            return full
        if parent is None:
            return os.open(name, dir_flags, dir_fd=dir_fd)
        return os.open(name, dir_flags, dir_fd=parent)

    def release(handle: "int | str") -> None:
        if isinstance(handle, int):
            os.close(handle)

    try:
        root = enter(None, top)
    except OSError as exc:
        report("", exc)
        return
    frames: list = []
    seen = 0
    try:
        try:
            frames.append(["", root, 0, listing(root), 0])
        except OSError as exc:
            release(root)
            report("", exc)
            return
        while frames:
            frame = frames[-1]
            rel, handle, depth, entries, index = frame
            if index >= len(entries):
                frames.pop()
                release(handle)
                continue
            frame[4] = index + 1
            entry = entries[index]
            path = f"{rel}/{entry.name}" if rel else entry.name
            seen += 1
            if max_entries is not None and seen > max_entries:
                report(path, OSError(errno.E2BIG, f"more than {max_entries} entries"))
                return
            if prune is not None and prune(path, entry):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = not is_dir and entry.is_file(follow_symlinks=False)
            except OSError as exc:
                report(path, exc)
                continue
            if is_dir:
                if max_depth is not None and depth >= max_depth:
                    report(path, OSError(errno.ELOOP, f"deeper than {max_depth} directories"))
                    continue
                try:
                    sub = enter(handle, entry.name)
                except OSError as exc:
                    report(path, exc)
                    continue
                try:
                    frames.append([path, sub, depth + 1, listing(sub), 0])
                except OSError as exc:
                    release(sub)
                    report(path, exc)
                continue
            if not is_file:
                continue
            try:
                if FD_SAFE:
                    fd = os.open(entry.name, file_flags, dir_fd=handle)
                else:
                    full = os.path.join(handle, entry.name)
                    if not stat.S_ISREG(os.lstat(full).st_mode):
                        continue
                    fd = os.open(full, file_flags)
            except OSError as exc:
                report(path, exc)
                continue
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                if O_NONBLOCK:
                    os.set_blocking(fd, True)
                yield path, fd
            finally:
                os.close(fd)
    finally:
        for frame in frames:
            release(frame[1])
