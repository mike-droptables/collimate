"""Run-bound handler metadata and safe current-workspace verification."""
from __future__ import annotations

import hashlib
import os
import stat

from collimate import fs

TEXT_LIMIT = 1_000_000
SNAPSHOT_LIMIT = 32 * 1024 * 1024


def own_channel(manager):
    from state import get_channel_by_id
    channel = get_channel_by_id(manager.channel)
    if channel is None:
        raise ValueError('run channel no longer exists')
    return channel


def public_handlers(manager) -> list[dict]:
    from handlers import catalog
    keys = ('id', 'name', 'description', 'types')
    return [{key: record[key] for key in keys} for record in catalog(own_channel(manager)).public() if not record.get("disabled")]


def handler_skills(manager, ext_or_id: str) -> dict[str, str]:
    from handlers import catalog
    if not isinstance(ext_or_id, str):
        raise ValueError('ext_or_id must be a string')
    return catalog(own_channel(manager)).skills(ext_or_id)


def _path(manager, path: str) -> str:
    if not isinstance(path, str):
        raise ValueError('path must be a workspace-relative string')
    normalized = '/'.join(fs.relative_parts(path))
    if normalized == '.collrun' or normalized.startswith('.collrun/'):
        raise PermissionError('private run configuration is not a cell file')
    if normalized == '_runs' or normalized.startswith('_runs/'):
        own = f'_runs/{manager.label}/'
        if normalized != own[:-1] and not normalized.startswith(own):
            raise PermissionError('path belongs to another run')
    return normalized


def workspace_snapshot(manager, path: str, verifier: str | None = None):
    """Read only target package bodies; expose other current paths as names.

    Opens are fd-relative and refuse symlinks/devices/FIFOs even after a
    directory enumeration race. Large or non-UTF8 bodies become metadata.
    Explicit verifier bytes are retained; large targets retain size/hash.
    """
    path = _path(manager, path)
    verifier = _path(manager, verifier) if verifier is not None else None
    root = manager.workspace_dir
    parent = path.rpartition('/')[0]
    required = {path} | ({verifier} if verifier else set())
    paths, selected = [], set(required)
    if not fs.FD_SAFE:
        raise PermissionError('safe workspace enumeration requires descriptor-relative opens')

    def walk(directory_fd: int, prefix: str = ''):
        # Pin each directory before enumerating it. An entry renamed into a
        # symlink after stat cannot redirect a later scandir outside the root.
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            rel = prefix + name
            try:
                if rel != '_runs':  # Traverse the container to this run only.
                    _path(manager, rel)
            except PermissionError:
                continue
            mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                if name in fs.WORKSPACE_SKIP_DIRS or name == '.collrun':
                    continue
                child = os.open(name, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW,
                                dir_fd=directory_fd)
                try:
                    walk(child, rel + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(mode):
                paths.append(rel)
                if (rel.startswith(parent + '/') if parent else '/' not in rel):
                    selected.add(rel)

    root_fd = os.open(root, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW)
    try:
        walk(root_fd)
    finally:
        os.close(root_fd)
    values, retained = {}, 0
    for rel in sorted(selected):
        with os.fdopen(fs.open_file(root, rel, write=False), 'rb') as stream:
            digest, size, chunks = hashlib.sha256(), 0, []
            # Bounded body retention; hashing large files remains streaming.
            for chunk in iter(lambda: stream.read(64 * 1024), b''):
                size += len(chunk)
                digest.update(chunk)
                if size <= TEXT_LIMIT or rel == verifier:
                    if retained + len(chunk) > SNAPSHOT_LIMIT:
                        raise ValueError('validation workspace snapshot exceeds 32 MiB')
                    chunks.append(chunk)
                    retained += len(chunk)
            body = b''.join(chunks)
            if rel != verifier and size > TEXT_LIMIT:
                retained -= len(body)
                values[rel] = {'size': size, 'hash': digest.hexdigest()}
                continue
            try:
                text = '\x00' not in body.decode('utf-8')
            except UnicodeDecodeError:
                text = False
            if rel == verifier or text:
                values[rel] = body
            else:
                retained -= len(body)
                values[rel] = {'size': size, 'hash': digest.hexdigest()}
    return path, verifier, values, sorted(paths)


def validate_workspace(manager, path: str, verifier: str | None = None) -> dict:
    from verification import validate_file
    from handlers import catalog
    path = _path(manager, path)
    fd = fs.open_file(manager.workspace_dir, path, write=False)
    os.close(fd)
    channel = own_channel(manager)
    record = catalog(channel).for_path(path)
    if verifier is None and (record is None or 'verify.ts' not in record.accepted_files):
        return validate_file(channel, manager.cell_id, path, workspace_files={}, workspace_paths=[path])
    path, verifier, files, paths = workspace_snapshot(manager, path, verifier)
    return validate_file(channel, manager.cell_id, path, verifier=verifier,
                         workspace_files=files, workspace_paths=paths)
