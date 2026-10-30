"""Accepted-handler validation outside commit locks, shared by user and run paths."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
import re
import threading
import time

from verifier_pool import BUDGET_EXCEEDED, FILE_SECONDS, VerifierFailure, budget_exceeded, get_verifier_pool

TEXT_LIMIT = 1_000_000
_verdicts = OrderedDict()
_verdict_lock = threading.Lock()


def _file_path(path):
    # Run-folder files are valid inputs too. Only the compiler's import graph
    # excludes _runs; verification itself must cover their declared file type.
    if (not isinstance(path, str) or not path or '\\' in path or '\x00' in path
            or re.match(r'^[A-Za-z]:', path) or any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('Invalid verification file path')
    return path


def verification_content(body):
    if isinstance(body, dict):
        if (set(body) != {'size', 'hash'} or not isinstance(body['size'], int) or body['size'] < 0
                or not isinstance(body['hash'], str) or len(body['hash']) != 64
                or any(c not in '0123456789abcdef' for c in body['hash'])):
            raise ValueError('Invalid opaque file metadata')
        return dict(body)
    if not isinstance(body, bytes):
        raise ValueError('Verification needs exact bytes or opaque file metadata')
    if len(body) <= TEXT_LIMIT:
        try:
            text = body.decode('utf-8')
            if '\x00' not in text:
                return text
        except UnicodeError:
            pass
    return {'size': len(body), 'hash': hashlib.sha256(body).hexdigest()}


def verifier_input(path: str, files: dict, cell_paths, *, root_is_package: bool = False) -> dict:
    """The verifier's value for ``path``: its package is the subtree of its
    folder. A cell-root file's package is the root files only, unless
    ``root_is_package`` (a handler example's folder, whose root is itself the
    package folder) gives it the whole snapshot."""
    _file_path(path)
    if path not in files:
        raise ValueError('File does not exist in the verification snapshot')
    prefix = path.rsplit('/', 1)[0] + '/' if '/' in path else ''
    package = {name[len(prefix):]: verification_content(body) for name, body in files.items()
               if name.startswith(prefix) and (prefix or root_is_package or '/' not in name)}
    for name in package:
        _file_path(name)
    own = verification_content(files[path])
    return {'path': path, 'text' if isinstance(own, str) else 'bytes': own,
            'package': package, 'cell_paths': sorted(set(cell_paths))}


def compiled_verifier(source: bytes, *, deadline: float) -> tuple[str, str]:
    from collimate._host.renderer_bundle import compile_verifier, verifier_source_hash
    from collimate.shared import shared_path
    from renderer_bundles import get_bundle, publish_server_bundle
    prelude = shared_path('verify', 'prelude.js').read_bytes()
    identity = verifier_source_hash(source, prelude=prelude)
    cached = get_bundle(identity)
    if cached is not None:
        return hashlib.sha256(cached).hexdigest(), cached.decode('utf-8')
    compiled = compile_verifier(source, prelude=prelude, deadline=deadline)
    publish_server_bundle(identity, compiled.code.encode('utf-8'))
    return compiled.bundle_hash, compiled.code


def verify_source(source: bytes, value: dict, *, lease=None, deadline: float | None = None):
    """Compile/cache source and evaluate under the same remaining file budget."""
    end = min(deadline if deadline is not None else float('inf'), time.monotonic() + FILE_SECONDS)
    if time.monotonic() >= end:
        return budget_exceeded()
    try:
        bundle_hash, code = compiled_verifier(source, deadline=end)
        encoded = json.dumps([bundle_hash, value], sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')
        key = hashlib.sha256(encoded).hexdigest()
        with _verdict_lock:
            if time.monotonic() >= end:
                return budget_exceeded()
            if key in _verdicts:
                _verdicts.move_to_end(key)
                return dict(_verdicts[key])
        if time.monotonic() >= end:
            return budget_exceeded()
        if lease is None:
            with get_verifier_pool().lease(end) as owned:
                result = owned.verify(code, value, deadline=end)
        else:
            result = lease.verify(code, value, deadline=end)
        # A transient pool/resource failure is retryable, not a content verdict.
        error = result.get('error', '')
        if not error.startswith(('Verifier ', BUDGET_EXCEEDED)):
            with _verdict_lock:
                _verdicts[key] = dict(result)
                while len(_verdicts) > 256:
                    _verdicts.popitem(last=False)
        return result
    except (ValueError, OSError, RuntimeError, VerifierFailure) as exc:
        return {'valid': False, 'error': str(exc)[:4096]}


def _cell_files(channel, cell_id: str, path: str, verifier: str | None):
    """Read the selected package from accepted SQL references."""
    from cas import read_blob, blob_path
    from cell_placement import find_cell
    from cell_write_scope import cell_write_scope
    from channel_db import open_channel_db
    from state import channel_db_name
    db = channel_db_name(channel)
    with cell_write_scope(db, [cell_id], hold_state=True):
        cell = find_cell(channel, cell_id)[1]
        if cell is None:
            raise ValueError('Cell is no longer on the canvas')
        references = {}
        conn = open_channel_db(db)
        try:
            conn.execute('BEGIN')
            from persistence import snapshot_files
            accepted=snapshot_files(conn,cell_id)
            references={f['path']:f['body_hash'] for f in accepted}
            previous_hash=references.get(path)
        finally:
            conn.close()
    files, retained = {}, 0
    parent = path.rpartition('/')[0]
    for name, value in references.items():
        if name not in (path, verifier) and not (name.startswith(parent + '/') if parent else '/' not in name):
            continue
        size = os.stat(blob_path(value)).st_size
        if size > TEXT_LIMIT and name != verifier:
            # Large accepted bodies give the verifier metadata without a full read.
            files[name] = {'size': size, 'hash': value}
            continue
        if size > 32 * 1024**2:
            raise ValueError('Verification snapshot exceeds 32 MiB')
        body = read_blob(value)
        if body is None or hashlib.sha256(body).hexdigest() != value:
            raise ValueError('Verification source object is missing or damaged')
        if len(body) > TEXT_LIMIT and name != verifier:
            files[name] = {'size': len(body), 'hash': hashlib.sha256(body).hexdigest()}
            continue
        retained += len(body)
        if retained > 32 * 1024**2:
            raise ValueError('Verification snapshot exceeds 32 MiB')
        files[name] = body
    previous = _previous_content(previous_hash)
    if isinstance(previous, str) and retained + len(previous.encode('utf-8')) > 32 * 1024**2:
        raise ValueError('Verification snapshot exceeds 32 MiB')
    return files, sorted(references), previous


def _previous_content(digest):
    """Read only a bounded committed target; larger/binary bodies are opaque."""
    from cas import blob_path, read_blob
    if digest is None:
        return None
    size = os.stat(blob_path(digest)).st_size
    if size > TEXT_LIMIT:
        return {'size': size, 'hash': digest}
    body = read_blob(digest)
    if body is None or hashlib.sha256(body).hexdigest() != digest:
        raise ValueError('Verification source object is missing or damaged')
    return verification_content(body)


def _workspace_previous(channel, cell_id: str, path: str):
    """Workspace contents are local; their baseline still belongs to this cell."""
    from cell_write_scope import cell_write_scope
    from channel_db import open_channel_db
    from state import channel_db_name
    from persistence import current_file
    db = channel_db_name(channel)
    with cell_write_scope(db, [cell_id], hold_state=True):
        conn = open_channel_db(db)
        try:
            accepted = current_file(conn, cell_id, path)
            digest = accepted['body_hash'] if accepted is not None and not accepted['deleted'] else None
        finally:
            conn.close()
    return _previous_content(digest)


def _cell_has_file(channel, cell_id: str, path: str) -> bool:
    """Check authoritative current membership without reading any file body."""
    from cell_placement import find_cell
    from cell_write_scope import cell_write_scope
    from channel_db import open_channel_db
    from state import channel_db_name
    db = channel_db_name(channel)
    with cell_write_scope(db, [cell_id], hold_state=True):
        cell = find_cell(channel, cell_id)[1]
        if cell is None:
            return False
        conn = open_channel_db(db)
        try:
            from persistence import current_file
            row=current_file(conn,cell_id,path)
            return row is not None and not row['deleted']
        finally:
            conn.close()


def validate_file(channel, cell_id: str, path: str, *, verifier: str | None = None,
                  workspace_files: dict | None = None, workspace_paths=None,
                  deadline: float | None = None):
    from handlers import catalog
    try:
        _file_path(path)
        record = catalog(channel).for_path(path)
        no_verifier = verifier is None and (record is None or 'verify.ts' not in record.accepted_files)
        if no_verifier:
            exists = (_cell_has_file(channel, cell_id, path) if workspace_files is None else
                      path in workspace_files or (workspace_paths is not None and path in workspace_paths))
            if not exists:
                return {'valid': False, 'error': 'File does not exist in the verification snapshot'}
            return {'valid': True}
        if workspace_files is not None:
            files = workspace_files
            paths = workspace_paths if workspace_paths is not None else files.keys()
            previous = _workspace_previous(channel, cell_id, path)
        else:
            files, paths, previous = _cell_files(channel, cell_id, path, verifier)
        if verifier is not None:
            _file_path(verifier)
            source = files.get(verifier)
            if not isinstance(source, bytes):
                return {'valid': False, 'error': 'Draft verifier text is unavailable'}
        else:
            assert record is not None
            source = record.accepted_files['verify.ts']
        value = verifier_input(path, files, paths)
        if previous is not None:
            value['previous_text' if isinstance(previous, str) else 'previous_bytes'] = previous
        return verify_source(source, value, deadline=deadline)
    except (ValueError, OSError) as exc:
        return {'valid': False, 'error': str(exc)[:4096]}
