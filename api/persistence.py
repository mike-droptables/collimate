"""Transactional file revisions, conditional writes and durable conflict receipts.

SQL owns current bytes. Memory is a derived cache updated only after commit.
HTTP, IPC and host/lifecycle writers share this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import NotRequired, TypedDict

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MutationResult(TypedDict):
    operation_id: str
    outcome: str
    files: list[dict[str, object]]
    conflicts: list[dict[str, object]]
    id: NotRequired[str]
    cell_id: NotRequired[str]
    refused: NotRequired[list[dict[str, object]]]


class FileMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str
    writer_id: str = Field(min_length=1, max_length=256)
    edit_seq: int = Field(ge=1)
    base_revision: int = Field(ge=0)
    kind: str
    content_text: str | None = None
    blob_hash: str | None = None
    language: str | None = None
    encoding: str | None = None

    @model_validator(mode='after')
    def validate_body(self):
        from cas import is_cas_hash
        from collimate.fs import relative_parts
        from reserved_paths import is_reserved_path
        try:
            if '/'.join(relative_parts(self.path)) != self.path or is_reserved_path(self.path):
                raise ValueError('Invalid or reserved file path')
        except (ValueError, PermissionError) as exc:
            raise ValueError('Invalid or reserved file path') from exc
        if self.encoding not in (None, 'utf-8', 'binary'):
            raise ValueError('encoding must be utf-8 or binary')
        if self.kind not in ('put', 'delete'):
            raise ValueError('kind must be put or delete')
        has_text = 'content_text' in self.model_fields_set
        has_hash = 'blob_hash' in self.model_fields_set
        if self.kind == 'put':
            if has_text == has_hash or (has_text and self.content_text is None):
                raise ValueError('put requires exactly one body')
            if has_hash and not is_cas_hash(self.blob_hash):
                raise ValueError('Invalid CAS hash')
        elif has_text or has_hash:
            raise ValueError('delete supplies no body')
        return self


class FileOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=1, max_length=256)
    mutations: list[FileMutation] = Field(min_length=1)
    resolves: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_paths(self):
        paths = [m.path for m in self.mutations]
        if len(paths) != len(set(paths)):
            raise ValueError('Duplicate mutation paths')
        return self


def descriptor(row):
    return {'path': row['path'], 'file_revision': row['revision'],
            'body_hash': row['body_hash'], 'deleted': bool(row['deleted']),
            'writer_id': row['writer_id'], 'edit_seq': row['edit_seq'],
            'language': row['language'], 'encoding': row['encoding']}


def snapshot_files(conn, cell_id, include_deleted=False):
    rows = conn.execute('SELECT r.* FROM working_files w JOIN file_revisions r '
                        'ON r.cell_id=w.cell_id AND r.path=w.path AND r.revision=w.revision '
                        'WHERE w.cell_id=? ORDER BY w.path', (str(cell_id),)).fetchall()
    return [descriptor(r) for r in rows if include_deleted or not r['deleted']]


def _entry(entry):
    from cas import store_bytes, retain_blob, is_cas_hash
    from collimate.fs import relative_parts
    from models import VirtualFile
    from reserved_paths import is_reserved_path
    if isinstance(entry, VirtualFile):
        path = entry.name
    elif isinstance(entry, dict) and 'path' in entry and 'body_hash' in entry:
        path = entry['path']
    else:
        raise ValueError('Snapshot entries require VirtualFile or path/body_hash descriptors')
    try:
        if '/'.join(relative_parts(path)) != path or is_reserved_path(path):
            raise ValueError('Invalid or reserved snapshot path')
    except (TypeError, ValueError, PermissionError) as exc:
        raise ValueError('Invalid or reserved snapshot path') from exc
    if isinstance(entry, VirtualFile):
        path, digest = entry.name, entry.blob_hash
        encoding = 'binary' if digest else 'utf-8'
        if digest is None:
            digest = store_bytes(entry.content.encode('utf-8'))
        language = entry.language
    else:
        digest = entry['body_hash']
        if not is_cas_hash(digest):
            raise ValueError('Snapshot descriptor requires an admitted body hash')
        encoding = entry.get('encoding')
        if encoding not in ('utf-8', 'binary') or not isinstance(entry.get('language'),str):
            raise ValueError('Snapshot descriptors require explicit encoding and language')
        language = entry['language']
    retain_blob(digest)
    return path, digest, language, encoding


def initialize_files(conn, cell_id, entries):
    """Initialize a newly created logical cell inside its caller's transaction."""
    cid = str(cell_id)
    existing = {f['path']: f for f in snapshot_files(conn, cid, include_deleted=True)}
    for entry in entries:
        path, digest, language, encoding = _entry(entry)
        if path in existing:
            if (existing[path]['body_hash'] != digest or existing[path]['deleted']
                    or existing[path]['encoding'] != encoding or existing[path]['language'] != language):
                raise ValueError('Cannot initialize over existing accepted file: ' + path)
            continue
        revision = _next_revision(conn, cid, path)
        sequence = _writer_sequence(conn, cid, path, 'host:init') + 1
        conn.execute('INSERT INTO file_revisions(cell_id,path,revision,base_revision,body_hash,deleted,'
                     'language,encoding,writer_id,edit_seq,operation_id) VALUES (?,?,?,0,?,0,?,?,?,?,?)',
                     (cid, path, revision, digest, language, encoding, 'host:init', sequence, 'init:' + cid))
        _record_writer_sequence(conn, cid, path, 'host:init', sequence)
        conn.execute('INSERT INTO working_files(cell_id,path,revision) VALUES (?,?,?)', (cid, path, revision))


def _next_revision(conn, cell_id, path):
    return conn.execute('INSERT INTO file_path_sequences(cell_id,path,last_allocated_revision) VALUES (?,?,1) '
                        'ON CONFLICT(cell_id,path) DO UPDATE SET last_allocated_revision=last_allocated_revision+1 '
                        'RETURNING last_allocated_revision', (cell_id,path)).fetchone()[0]


def _writer_sequence(conn, cell_id, path, writer_id):
    row = conn.execute('SELECT last_edit_seq FROM file_writer_sequences WHERE cell_id=? AND path=? AND writer_id=?',
                       (cell_id,path,writer_id)).fetchone()
    return row[0] if row else 0


def _record_writer_sequence(conn, cell_id, path, writer_id, edit_seq):
    conn.execute('INSERT INTO file_writer_sequences(cell_id,path,writer_id,last_edit_seq) VALUES (?,?,?,?) '
                 'ON CONFLICT(cell_id,path,writer_id) DO UPDATE SET last_edit_seq=MAX(last_edit_seq,excluded.last_edit_seq)',
                 (cell_id,path,writer_id,edit_seq))


def current_file(conn, cell_id, path):
    return conn.execute('SELECT r.* FROM working_files w JOIN file_revisions r '
                        'ON r.cell_id=w.cell_id AND r.path=w.path AND r.revision=w.revision '
                        'WHERE w.cell_id=? AND w.path=?', (str(cell_id), path)).fetchone()


def _stage(operation):
    """No cell/state lock spans body hashing or filesystem admission."""
    from cas import store_bytes, retain_blob, blob_is_utf8, blob_path, CorruptBlobError, TEXT_IMPORT_MAX_BYTES
    staged = {}
    for mutation in operation.mutations:
        if mutation.kind == 'delete':
            staged[mutation.path] = (None, None)
        elif 'content_text' in mutation.model_fields_set:
            staged[mutation.path] = (store_bytes(mutation.content_text.encode('utf-8')), 'utf-8')
        else:
            # A body by hash (binary, or UTF-8 text an import streamed into the
            # CAS) is verified and checked in bounded reads, never held whole.
            try:
                retain_blob(mutation.blob_hash)
            except (FileNotFoundError, CorruptBlobError) as exc:
                raise HTTPException(409, dict(code='body_unavailable',path=mutation.path,
                    body_hash=mutation.blob_hash,
                    reason='missing' if isinstance(exc,FileNotFoundError) else 'corrupt')) from exc
            if mutation.encoding not in (None,'utf-8','binary'):
                raise HTTPException(400,'Invalid body encoding')
            if mutation.encoding == 'utf-8':
                # The layout cache holds every text body decoded: the import
                # sniff's size rule bounds what a by-hash body may claim.
                if os.path.getsize(blob_path(mutation.blob_hash)) > TEXT_IMPORT_MAX_BYTES:
                    raise HTTPException(400, f'A text body by hash is at most {TEXT_IMPORT_MAX_BYTES} bytes; '
                                             'send it as binary')
                if not blob_is_utf8(mutation.blob_hash):
                    raise HTTPException(400, 'Body is not UTF-8')
            staged[mutation.path] = (mutation.blob_hash,mutation.encoding or 'binary')
    return staged


def _builtin_policy(channel, cid, operation, actor):
    """A built-in card's package bytes are the app's: a user writes only
    run-folder paths, which the run-folder rule below then refuses, and a
    run or the host only the card's overlay
    (``builtin_packages.overlay_path``)."""
    from builtin_packages import BUILTIN_REFUSAL, card_for, overlay_path
    from run_folders import is_run_path
    card = card_for(channel, cid)
    if card is None:
        return
    for mutation in operation.mutations:
        allowed = is_run_path(mutation.path) if actor == 'user' else overlay_path(card, mutation.path)
        if not allowed:
            raise HTTPException(400, BUILTIN_REFUSAL)


def _policy(channel, cid, operation, actor, manager):
    from run_folders import is_run_path
    from run_registry import lock_owner
    from served_surfaces import write_refusals
    _builtin_policy(channel, cid, operation, actor)
    if actor == 'run':
        if manager is None or manager.sealed or getattr(manager, 'stop_requested', False):
            raise HTTPException(409, 'Run has ended')
        refusal = write_refusals(manager, {m.path for m in operation.mutations})
        if refusal:
            raise HTTPException(409, list(refusal.values()))
    if actor == 'user':
        for mutation in operation.mutations:
            # Run-authored UI is immutable to user file editors.
            if is_run_path(mutation.path):
                raise HTTPException(409, 'Run UI files are read-only')
            owner = lock_owner(cid, mutation.path)
            if mutation.kind == 'delete' and owner is not None:
                raise HTTPException(409, 'File is locked by ' + owner.label)


def _mutate(conn, cid, operation, staged, request_digest, *, force_conflict=False, force_superseded=False):
    receipt = conn.execute('SELECT cell_id,request_digest,result_json FROM file_operations WHERE operation_id=?',
                           (operation.operation_id,)).fetchone()
    if receipt:
        if receipt['cell_id'] != cid or receipt['request_digest'] != request_digest:
            raise HTTPException(409, 'Operation id was already used with different input')
        return json.loads(receipt['result_json'])
    currents = {m.path: current_file(conn, cid, m.path) for m in operation.mutations}
    superseded = force_superseded or any(_writer_sequence(conn,cid,m.path,m.writer_id) >= m.edit_seq
                     for m in operation.mutations)
    conflict = force_conflict or any(m.base_revision != (currents[m.path]['revision'] if currents[m.path] else 0)
                   for m in operation.mutations)
    result: MutationResult = {
        'operation_id': operation.operation_id,
        'outcome': 'superseded' if superseded else 'conflict' if conflict else 'accepted',
        'files': [],
        'conflicts': [],
    }
    if superseded:
        result['files'] = [descriptor(r) for r in currents.values() if r]
    else:
        for mutation in operation.mutations:
            current = currents[mutation.path]
            revision = _next_revision(conn,cid,mutation.path)
            digest, encoding = staged[mutation.path]
            language = mutation.language or (current['language'] if current else 'text')
            encoding = encoding or (current['encoding'] if current else 'utf-8')
            conn.execute('INSERT INTO file_revisions(cell_id,path,revision,base_revision,body_hash,deleted,language,'
                         'encoding,writer_id,edit_seq,operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (cid, mutation.path, revision, mutation.base_revision, digest, mutation.kind == 'delete',
                          language, encoding, mutation.writer_id, mutation.edit_seq, operation.operation_id))
            _record_writer_sequence(conn,cid,mutation.path,mutation.writer_id,mutation.edit_seq)
            row = conn.execute('SELECT * FROM file_revisions WHERE cell_id=? AND path=? AND revision=?',
                               (cid, mutation.path, revision)).fetchone()
            if conflict:
                conflict_id = str(uuid.uuid4())
                current_revision = current['revision'] if current else 0
                conn.execute('INSERT INTO file_conflicts(conflict_id,operation_id,cell_id,path,base_revision,current_revision,candidate_revision) '
                             'VALUES (?,?,?,?,?,?,?)', (conflict_id, operation.operation_id, cid, mutation.path,
                                                       mutation.base_revision, current_revision, revision))
                result['conflicts'].append(dict(conflict_id=conflict_id, path=mutation.path,
                                               base_revision=mutation.base_revision, current_revision=current_revision,
                                               candidate_revision=revision, candidate_hash=row['body_hash'],
                                               writer_id=row['writer_id'], edit_seq=row['edit_seq']))
                if current:
                    result['files'].append(descriptor(current))
            else:
                conn.execute('INSERT INTO working_files(cell_id,path,revision) VALUES (?,?,?) '
                             'ON CONFLICT(cell_id,path) DO UPDATE SET revision=excluded.revision', (cid, mutation.path, revision))
                result['files'].append(descriptor(row))
        if not conflict:
            for conflict_id in operation.resolves:
                conflict_row = conn.execute('SELECT path FROM file_conflicts WHERE conflict_id=? AND cell_id=?',
                                            (conflict_id, cid)).fetchone()
                if not conflict_row or conflict_row['path'] not in currents:
                    raise HTTPException(409, 'Resolution must update the conflicting path')
                changed = conn.execute('UPDATE file_conflicts SET resolution_operation_id=? WHERE conflict_id=? AND cell_id=? '
                                       'AND resolution_operation_id IS NULL', (operation.operation_id, conflict_id, cid)).rowcount
                if not changed:
                    raise HTTPException(409, 'Unknown or already resolved conflict')
    conn.execute('INSERT INTO file_operations(operation_id,cell_id,request_digest,result_json) VALUES (?,?,?,?)',
                 (operation.operation_id, cid, request_digest, json.dumps(result, sort_keys=True)))
    return result


def _commit(conn):
    conn.commit()


def mutate_files(channel, cell_id, operation, *, actor='user', manager=None, conn=None, request_digest=None):
    operation = operation if isinstance(operation, FileOperation) else FileOperation.model_validate(operation)
    cid = str(cell_id)
    canonical = operation.model_dump(exclude_unset=True)
    request_digest = request_digest or hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    # A replay depends only on its durable receipt, never on re-reading CAS
    # or repeating current run policy.
    from channel_db import open_channel_db
    receipt_conn = conn if conn is not None else open_channel_db(channel)
    try:
        replay = receipt_conn.execute("SELECT cell_id,request_digest,result_json FROM file_operations WHERE operation_id=?",
                                      (operation.operation_id,)).fetchone()
        if replay:
            if replay["cell_id"] != cid or replay["request_digest"] != request_digest:
                raise HTTPException(409, "Operation id was already used with different input")
            result = json.loads(replay["result_json"])
            from database import confirm_sql_durability
            confirm_sql_durability(receipt_conn)
            if conn is None:
                _publish_safely(channel, cid, result)
            return result
    finally:
        if conn is None:
            receipt_conn.close()
    staged = _stage(operation)
    from cell_commits import cell_commit_lock
    from channel_db import open_channel_db
    from cell_placement import find_cell
    from state import require_channel, state_lock
    from cas import publication_lock, retain_blob
    owned = conn is None
    with cell_commit_lock(cid):
        identity = require_channel(channel)
        if actor != 'host':
            with state_lock:
                if find_cell(identity, cid)[1] is None:
                    raise HTTPException(404, 'Cell is no longer on the canvas')
        if owned:
            conn = open_channel_db(channel)
        try:
            if owned:
                conn.execute('BEGIN IMMEDIATE')
            # Storage age remains protected until the durable references commit.
            with publication_lock:
                for digest, _encoding in staged.values():
                    if digest:
                        retain_blob(digest)
                # Check replay before policy transforms: old outcomes are immutable.
                replay = conn.execute('SELECT cell_id,request_digest,result_json FROM file_operations WHERE operation_id=?',
                                      (operation.operation_id,)).fetchone()
                if replay:
                    if replay['cell_id'] != cid or replay['request_digest'] != request_digest:
                        raise HTTPException(409, 'Operation id was already used with different input')
                    result = json.loads(replay['result_json'])
                else:
                    _policy(channel, cid, operation, actor, manager)
                    result = _mutate(conn, cid, operation, staged, request_digest)
                if owned:
                    _commit(conn)
        except BaseException:
            if owned:
                conn.rollback()
            raise
        finally:
            if owned:
                conn.close()
        if owned:
            _publish_safely(channel, cid, result)
    return result


def _publish_safely(channel, cell_id, receipt):
    """A derived cache or event failure cannot revoke an acknowledged SQL write."""
    try:
        publish_files(channel, cell_id, receipt)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Committed files need cache reconstruction: %s/%s',channel,cell_id)
        from state import persist_state
        persist_state()  # Metadata resync reads durable refs even if bodies failed.


def publish_files(channel, cell_id, receipt, *, persist=True):
    """Rebuild a derived cell cache and notify only after durable publication."""
    from database import get_db_connection
    from cell_placement import find_cell
    from state import require_channel, state_lock, persist_state
    from cas import blob_path, read_blob
    from models import VirtualFile
    from models import Channel
    identity = channel if isinstance(channel, Channel) else require_channel(channel)
    conn = get_db_connection(str(identity.id), create=False)
    try:
        metadata = snapshot_files(conn, cell_id)
    finally:
        conn.close()
    with state_lock:
        cell = find_cell(identity, str(cell_id))[1]
        previous = {f.name: f for f in cell.files} if cell is not None else {}
    # Unchanged bodies preserve their decoded cache; only changed hashes read CAS.
    files = []
    for entry in metadata:
        prior = previous.get(entry['path'])
        if prior is not None and prior.body_hash == entry['body_hash'] and prior.encoding == entry['encoding']:
            prior.file_revision = entry['file_revision']
            prior.writer_id = entry['writer_id']
            prior.edit_seq = entry['edit_seq']
            prior.language = entry['language']
            files.append(prior)
            continue
        # A binary body is served from the CAS, never decoded: only its
        # presence is checked, so a large import is not read into memory.
        binary = entry['encoding'] == 'binary'
        if binary:
            if not os.path.isfile(blob_path(entry['body_hash'])):
                raise OSError('Missing accepted file body ' + entry['path'])
            content = ''
        else:
            raw = read_blob(entry['body_hash'])
            if raw is None:
                raise OSError('Missing accepted file body ' + entry['path'])
            content = raw.decode('utf-8')
        files.append(VirtualFile(name=entry['path'], content=content,
                                 blob_hash=entry['body_hash'] if binary else None,
                                 language=entry['language'], body_hash=entry['body_hash'],
                                 file_revision=entry['file_revision'], writer_id=entry['writer_id'],
                                 edit_seq=entry['edit_seq'], encoding=entry['encoding']))
    with state_lock:
        cell = find_cell(identity, str(cell_id))[1]
        if cell is not None:
            cell.files = files
            cell.updated_at = datetime.now()
    if persist:
        persist_state()
        from events import broadcast
        from collimate._host.channel_events import FileChanges
        event = FileChanges(str(cell_id),receipt['operation_id'],receipt['outcome'],
                            receipt['files'],receipt['conflicts']).to_dict()
        broadcast(str(identity.id), event)


def prune_file_revisions(conn):
    """Drop unrooted revision bodies; immutable receipts and sequences remain.

    The caller owns the SQL transaction. Current refs, every unresolved
    conflict comparison and active body leases protect their revisions.
    """
    from cas import pinned_hashes
    conn.execute('DELETE FROM file_conflicts WHERE resolution_operation_id IS NOT NULL')
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS retained_file_revisions '
                 '(cell_id TEXT,path TEXT,revision INTEGER,PRIMARY KEY(cell_id,path,revision)) WITHOUT ROWID')
    conn.execute('DELETE FROM retained_file_revisions')
    conn.execute('INSERT OR IGNORE INTO retained_file_revisions SELECT cell_id,path,revision FROM working_files')
    for column in ('candidate_revision','base_revision','current_revision'):
        conn.execute(f'INSERT OR IGNORE INTO retained_file_revisions '
                     f'SELECT cell_id,path,{column} FROM file_conflicts WHERE resolution_operation_id IS NULL')
    for digest in pinned_hashes():
        conn.execute('INSERT OR IGNORE INTO retained_file_revisions '
                     'SELECT cell_id,path,revision FROM file_revisions WHERE body_hash=?', (digest,))
    deleted = conn.execute('DELETE FROM file_revisions AS r WHERE NOT EXISTS '
                          '(SELECT 1 FROM retained_file_revisions k '
                          'WHERE k.cell_id=r.cell_id AND k.path=r.path AND k.revision=r.revision)').rowcount
    conn.execute('DROP TABLE retained_file_revisions')
    return deleted


def read_file(channel, cell_id, path, *, revision=None):
    """``(descriptor, bytes)`` of a cell file, current or at ``revision``. On
    a built-in card a path its overlay does not hold reads the build's bytes
    (revision 0) when the card shows it (``builtin_packages.projection``)."""
    from builtin_packages import card_for, projected_entry, vendored_body
    from channel_db import open_channel_db
    from cas import read_blob, publication_lock
    card = card_for(channel, cell_id)
    conn = open_channel_db(channel)
    try:
        if card is not None and revision in (None, 0):
            current = current_file(conn, cell_id, path)
            if current is None or current['deleted']:
                entry = projected_entry(card, snapshot_files(conn, cell_id), path)
                raw = vendored_body(entry['body_hash']) if entry is not None else None
                if entry is not None and raw is not None:
                    return {k: v for k, v in entry.items() if k != 'builtin'}, raw
        # No writer transaction is acquired under this lock. It closes the
        # descriptor-to-byte gap against collection; response bytes own their
        # lifetime after this verified capture completes.
        with publication_lock:
            row = (current_file(conn, cell_id, path) if revision is None else
                   conn.execute('SELECT * FROM file_revisions WHERE cell_id=? AND path=? AND revision=?',
                                (str(cell_id), path, revision)).fetchone())
            if row is None and revision is not None:
                high = conn.execute('SELECT last_allocated_revision FROM file_path_sequences WHERE cell_id=? AND path=?',
                                    (str(cell_id),path)).fetchone()
                if high is not None and 0 < revision <= high[0]:
                    raise HTTPException(410, 'File revision is no longer retained')
            if row is None or row['deleted']:
                raise HTTPException(404, 'File revision not found')
            metadata = descriptor(row)
            raw = read_blob(metadata['body_hash'])
            if raw is None:
                raise HTTPException(409, 'Accepted file body is missing')
            return metadata, raw
    finally:
        conn.close()


def list_conflicts(channel, cell_id):
    from channel_db import open_channel_db
    conn = open_channel_db(channel)
    try:
        return [dict(r) for r in conn.execute('SELECT c.*,r.body_hash AS candidate_hash,r.writer_id,r.edit_seq FROM file_conflicts c '
            'JOIN file_revisions r ON r.cell_id=c.cell_id AND r.path=c.path AND r.revision=c.candidate_revision '
            'WHERE c.cell_id=? AND c.resolution_operation_id IS NULL ORDER BY c.rowid', (str(cell_id),))]
    finally:
        conn.close()


def restore_snapshot(conn, cell_id, entries, writer_id='host:restore'):
    entries = list(entries)
    current = {f['path']: f for f in snapshot_files(conn, cell_id, include_deleted=True)}
    prepared = {}
    for entry in entries:
        path = entry.get('path') if isinstance(entry, dict) else None
        prior = current.get(path)
        if prior and not prior['deleted'] and all(entry.get(k)==prior[k] for k in ('body_hash','encoding','language')):
            # Existing accepted refs need no new CAS admission or body decode.
            value = (path, prior['body_hash'], prior['language'], prior['encoding'])
        else:
            value = _entry(entry)
        prepared[value[0]] = value
    mutations = []
    for path in sorted(prepared.keys() | current.keys()):
        prior = current.get(path)
        value = prepared.get(path)
        if value and prior and not prior['deleted'] and (prior['body_hash'],prior['language'],prior['encoding']) == value[1:]:
            continue
        if value is None and (prior is None or prior['deleted']):
            continue
        seq = _writer_sequence(conn,str(cell_id),path,writer_id) + 1
        mutation = dict(path=path, writer_id=writer_id, edit_seq=seq,
                        base_revision=prior['file_revision'] if prior else 0, kind='put' if value else 'delete')
        if value:
            mutation.update(blob_hash=value[1], language=value[2])
        mutations.append(mutation)
    if not mutations:
        return dict(operation_id=str(uuid.uuid4()), outcome='accepted', files=list(current.values()), conflicts=[])
    operation = FileOperation(operation_id=str(uuid.uuid4()), mutations=mutations)
    staged = {p:(v[1],v[3]) for p,v in prepared.items()}
    staged.update((m.path,(None,None)) for m in operation.mutations if m.kind == 'delete')
    digest = hashlib.sha256(operation.model_dump_json().encode()).hexdigest()
    return _mutate(conn, str(cell_id), operation, staged, digest)


class MoveFile(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_path: str
    target_path: str
    source_revision: int = Field(ge=1)
    target_revision: int = Field(ge=0)


class FileMove(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(min_length=1, max_length=200)
    source_cell_id: str
    target_cell_id: str
    files: list[MoveFile] = Field(min_length=1)
    writer_id: str = Field(min_length=1, max_length=256)
    edit_seq: int = Field(ge=1)

    @model_validator(mode='after')
    def validate_move(self):
        if self.source_cell_id == self.target_cell_id:
            raise ValueError('Use grouped mutations to rename within a cell')
        if len({f.source_path for f in self.files}) != len(self.files) or len({f.target_path for f in self.files}) != len(self.files):
            raise ValueError('Move paths must be unique')
        return self


def move_files(channel, request):
    """One transaction conditionally copies immutable refs and tombstones sources."""
    from cell_commits import cell_commit_locks
    from channel_db import open_channel_db
    from cell_placement import find_cell
    from state import require_channel, state_lock
    from cas import publication_lock, retain_blob
    request = request if isinstance(request, FileMove) else FileMove.model_validate(request)
    digest = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with cell_commit_locks([request.source_cell_id, request.target_cell_id]):
        with state_lock:
            identity = require_channel(channel)
            if any(find_cell(identity, cid)[1] is None for cid in (request.source_cell_id, request.target_cell_id)):
                raise HTTPException(404, 'Move cell is no longer on the canvas')
        conn = open_channel_db(channel)
        try:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT cell_id,request_digest,result_json FROM file_operations WHERE operation_id=?',
                                 (request.operation_id,)).fetchone()
            if prior:
                if prior['cell_id'] != request.source_cell_id or prior['request_digest'] != digest:
                    raise HTTPException(409, 'Operation id was already used with different input')
                conn.rollback()
                from database import confirm_sql_durability
                confirm_sql_durability(conn)
                return json.loads(prior['result_json'])
            sources, targets, staged = [], [], {}
            for file in request.files:
                row = conn.execute('SELECT * FROM file_revisions WHERE cell_id=? AND path=? AND revision=?',
                                   (request.source_cell_id, file.source_path, file.source_revision)).fetchone()
                if row is None or row['deleted']:
                    raise HTTPException(404, 'Move source revision not found')
                sources.append(FileMutation(path=file.source_path,writer_id=request.writer_id,edit_seq=request.edit_seq,
                                            base_revision=file.source_revision,kind='delete'))
                targets.append(FileMutation(path=file.target_path,writer_id=request.writer_id,edit_seq=request.edit_seq,
                                            base_revision=file.target_revision,kind='put',blob_hash=row['body_hash'],
                                            language=row['language'],encoding=row['encoding']))
                staged[file.target_path] = (row['body_hash'], row['encoding'])
            source_op = FileOperation(operation_id=request.operation_id + ':source', mutations=sources)
            target_op = FileOperation(operation_id=request.operation_id + ':target', mutations=targets)
            source_stage = {m.path:(None,None) for m in sources}
            _policy(channel,request.source_cell_id,source_op,'user',None)
            _policy(channel,request.target_cell_id,target_op,'user',None)
            groups = [(request.source_cell_id,source_op),(request.target_cell_id,target_op)]
            stale = any(m.base_revision != (r['revision'] if (r := current_file(conn,cid,m.path)) else 0)
                        for cid,op in groups for m in op.mutations)
            superseded = any(_writer_sequence(conn,cid,m.path,m.writer_id) >= m.edit_seq
                             for cid,op in groups for m in op.mutations)
            with publication_lock:
                for body_hash,_encoding in staged.values():
                    retain_blob(body_hash)
                results = [_mutate(conn,cid,op,body_stage,digest,force_conflict=stale,force_superseded=superseded)
                           for (cid,op),body_stage in zip(groups,(source_stage,staged))]
                receipt = dict(operation_id=request.operation_id,outcome=results[0]['outcome'],
                               files=[],source_files=results[0]['files'],target_files=results[1]['files'],
                               conflicts=[dict(c,cell_id=cid) for (cid,_),result in zip(groups,results) for c in result['conflicts']])
                conn.execute('INSERT INTO file_operations(operation_id,cell_id,request_digest,result_json) VALUES (?,?,?,?)',
                             (request.operation_id,request.source_cell_id,digest,json.dumps(receipt,sort_keys=True)))
                _commit(conn)
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        for (cid,_),result in zip(groups,results):
            _publish_safely(channel,cid,dict(result,operation_id=request.operation_id))
    return receipt
