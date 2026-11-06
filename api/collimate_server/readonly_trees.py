"""Trees the host makes read-only, and removing any tree that may hold one.

A run that does not hold its cell's repository lease gets a read-only
``.git`` per repository (``git_harvest.materialize``): every directory and
regular file of it without write bits, so git's reads work and its writes
fail. Such a tree cannot be removed by a plain ``shutil.rmtree`` (an entry
cannot be unlinked while its directory is 0555), nor by an ``onexc`` handler
that makes only the failing path writable (its parent is the one that must
be). :func:`remove_tree` first walks the whole tree making every directory
writable, then removes it; every place that removes a run workspace or a
materialized ``.git`` goes through it.

Nothing here follows a symlink: directories are reached through their
parent's fd and changed through their own (``os.fwalk``), and a regular
file is opened ``O_NOFOLLOW`` from its directory's fd and changed through
that fd, so an entry swapped for a link after the listing is refused. Each
function takes ``dir_fd``, the directory ``top`` (or ``target``) is named
in: a caller that reached that directory through no link (``fs.open_parent``
from a run's workspace) is never turned to another one by a link a live run
puts on the path above it.
"""
from __future__ import annotations

import os
import shutil
import stat

from collimate import fs

_WRITE_BITS = 0o222
_OWNER_DIR = stat.S_IRWXU


def make_readonly(top: str, *, dir_fd: int | None = None) -> None:
    """Strip the write bits from ``top`` and every directory and regular
    file under it (directories 0555, files 0444 from the usual 0755/0644);
    links and anything else are left as they are. Raises ``OSError`` when
    ``top`` cannot be walked."""
    for _dirpath, _dirnames, filenames, fd in _walk(top, dir_fd=dir_fd):
        for name in filenames:
            _change_file(fd, name, lambda mode: mode & ~_WRITE_BITS)
        _change_fd(fd, lambda mode: mode & ~_WRITE_BITS)


def make_writable(top: str, *, dir_fd: int | None = None) -> None:
    """Give ``top`` and every directory under it its owner's read, write and
    search bits, so each entry can be unlinked or moved out (a file's own
    mode never matters to that). Raises ``OSError`` when ``top`` cannot be
    walked."""
    for _dirpath, _dirnames, _filenames, fd in _walk(top, opening=True, dir_fd=dir_fd):
        _change_fd(fd, lambda mode: mode | _OWNER_DIR)


def remove_tree(top: str, *, ignore_errors: bool = False, dir_fd: int | None = None) -> None:
    """Remove ``top`` whatever its directories' modes: a link or a file is
    unlinked; a directory is made writable throughout (:func:`make_writable`)
    and then removed with ``shutil.rmtree``. Nothing missing is an error
    only without ``ignore_errors``."""
    try:
        found = os.stat(top, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        if ignore_errors:
            return
        raise
    if not stat.S_ISDIR(found.st_mode):
        try:
            os.unlink(top, dir_fd=dir_fd)
        except OSError:
            if not ignore_errors:
                raise
        return
    try:
        make_writable(top, dir_fd=dir_fd)
    except OSError:
        if not ignore_errors:
            raise
    shutil.rmtree(top, ignore_errors=ignore_errors, dir_fd=dir_fd)


def replace_entries(target: str, fresh: str, stale: str, *, dir_fd: int | None = None) -> None:
    """Put the entries of the directory ``fresh`` into the read-only
    directory ``target`` (named in ``dir_fd``) in place of its own, keeping
    ``target`` itself (its inode): a container's read-only bind of
    ``target`` keeps showing it, where a rename of a new directory over it
    would leave the bind on the old one. ``target`` is opened once, never
    through a link, and every move into or out of it goes through that fd.
    A file of ``fresh`` replaces its namesake with one rename; a directory
    takes the place of its namesake moved into ``stale`` (a new, empty
    directory on the same filesystem) a rename earlier; whatever ``fresh``
    lacks moves to ``stale`` too. A git process reading meanwhile can find
    one directory missing for that one rename, or a mix of the two trees'
    files; it reads again. Then ``target`` is made read-only and ``stale``
    removed (``fresh`` is the caller's to remove). Raises ``OSError``;
    ``target`` is left read-only either way."""
    target_fd = os.open(target, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        _change_fd(target_fd, lambda mode: mode | _OWNER_DIR)
        try:
            incoming = set(os.listdir(fresh))
            for name in sorted(set(os.listdir(target_fd)) - incoming):
                _move_out(target_fd, name, stale)
            for name in sorted(incoming):
                source = os.path.join(fresh, name)
                there = _found(name, target_fd)
                if there is not None and (_is_dir(source) or stat.S_ISDIR(there.st_mode)):
                    _move_out(target_fd, name, stale)
                os.rename(source, name, dst_dir_fd=target_fd)
        finally:
            make_readonly(".", dir_fd=target_fd)
    finally:
        os.close(target_fd)
        remove_tree(stale, ignore_errors=True)


def _is_dir(path: str) -> bool:
    return stat.S_ISDIR(os.lstat(path).st_mode)


def _found(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _move_out(target_fd: int, name: str, stale: str) -> None:
    """Move ``name`` out of the directory ``target_fd`` into ``stale``; a
    directory moving to another parent must itself be writable (its ``..``
    changes)."""
    found = os.stat(name, dir_fd=target_fd, follow_symlinks=False)
    if stat.S_ISDIR(found.st_mode):
        os.chmod(name, found.st_mode | _OWNER_DIR, dir_fd=target_fd, follow_symlinks=False)
    os.rename(name, os.path.join(stale, name), src_dir_fd=target_fd)


def _walk(top: str, *, opening: bool = False, dir_fd: int | None = None):
    """``os.fwalk`` over ``top`` (named in ``dir_fd``), top down, following
    no link. With ``opening``, a subdirectory its owner cannot list or enter
    gets those bits first (changed by name without following a link), so
    the walk can open it."""
    found = os.stat(top, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISDIR(found.st_mode):
        raise NotADirectoryError(top)
    if opening and found.st_mode & (stat.S_IRUSR | stat.S_IXUSR) != stat.S_IRUSR | stat.S_IXUSR:
        if dir_fd is None:
            os.chmod(top, found.st_mode | _OWNER_DIR)
        else:
            os.chmod(top, found.st_mode | _OWNER_DIR, dir_fd=dir_fd, follow_symlinks=False)
    for dirpath, dirnames, filenames, fd in os.fwalk(top, follow_symlinks=False, dir_fd=dir_fd):
        if opening:
            for name in dirnames:
                try:
                    mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode) and mode & (stat.S_IRUSR | stat.S_IXUSR) != stat.S_IRUSR | stat.S_IXUSR:
                        os.chmod(name, mode | _OWNER_DIR, dir_fd=fd, follow_symlinks=False)
                except (OSError, NotImplementedError, ValueError):
                    continue
        yield dirpath, dirnames, filenames, fd


def _change_fd(fd: int, change) -> None:
    mode = stat.S_IMODE(os.fstat(fd).st_mode)
    wanted = change(mode)
    if wanted != mode:
        os.fchmod(fd, wanted)


def _change_file(dir_fd: int, name: str, change) -> None:
    """Change the mode of the regular file ``name`` in ``dir_fd`` through an
    ``O_NOFOLLOW`` fd; a link, anything not regular, or an entry gone or
    unopenable is left alone."""
    try:
        found = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISREG(found.st_mode):
        return
    try:
        fd = os.open(name, os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return
    try:
        if os.path.samestat(found, os.fstat(fd)):
            _change_fd(fd, change)
    finally:
        os.close(fd)
