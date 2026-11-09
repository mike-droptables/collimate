# SPDX-License-Identifier: MIT
"""Anchored private writes for native harness credentials and configuration."""
import os

def write_native_file(root: str, relative: str, body: bytes | None) -> None:
    """Replace a native config file under the retained home without following links.

    The directory fd stays open across validation, temporary write and rename.
    Missing hints remove only a validated regular leaf in that same directory.
    """
    from collimate import fs
    import stat
    import uuid

    parts = fs.relative_parts(relative)
    if not fs.FD_SAFE:
        # Match the SDK fs fallback on platforms without directory-fd operations.
        target = os.path.join(root, *parts)
        if os.path.islink(target) or os.path.realpath(target) != os.path.abspath(target):
            raise PermissionError("Harness native config contains a linked path")
        if body is None:
            fs.unlink(root, relative)
            return
        temporary = relative + ".native-" + uuid.uuid4().hex
        fd = fs.open_file(root, temporary, write=True)
        try:
            os.chmod(os.path.join(root, temporary), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            os.replace(os.path.join(root, temporary), target)
        finally:
            fs.unlink(root, temporary)
        return
    try:
        anchor_parts = fs.relative_parts(os.path.abspath(root).lstrip(os.sep))
        parent = fs.open_parent(os.path.abspath(os.sep), anchor_parts + parts,
                                create=body is not None)
    except FileNotFoundError:
        if body is None:
            return
        raise
    temporary = ".native-" + uuid.uuid4().hex
    try:
        os.fchmod(parent, 0o700)
        try:
            leaf = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            leaf = None
        if leaf is not None and not stat.S_ISREG(leaf.st_mode):
            raise PermissionError("Harness native config leaf is not a regular file")
        if body is None:
            if leaf is not None:
                os.unlink(parts[-1], dir_fd=parent)
            return
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
        os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)

