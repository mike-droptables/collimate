"""Immutable run-flush snapshots and bounded verdicts, outside commit locks."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import os
import time

from collimate.definitions import discover_definitions


@dataclass
class Snapshot:
    tip: str
    tip_files: dict[str, str]
    base: dict[str, str]
    writes: dict[str, str]
    deleted: set[str]
    policy_refused: dict = field(default_factory=dict)
    surface_identity: tuple = ()
    revisions: tuple = ()

    @property
    def identity(self):
        # Unselected text bodies cannot affect any verdict or publication.
        # Accepted revisions and selected hashes detect relevant write races.
        return self.tip,self.base,self.revisions,self.surface_identity,self.writes,self.policy_refused

    def candidate(self, refused=()):
        """Accepted current files with this operation's permitted changes."""
        files = dict(self.base)
        files.update((p, h) for p, h in self.writes.items() if p not in refused)
        for path in self.deleted:
            if path not in refused:
                files.pop(path, None)
        return files

def snapshot(manager, conn, writes: dict[str, str], deleted: set[str]) -> Snapshot:
    """Caller holds the cell lock; capture accepted refs and operation hashes."""
    from cell_placement import find_cell
    from state import get_channel_by_id
    from versions import find_tip, tip_file_map
    from served_surfaces import write_refusals, identity
    if manager.sealed:
        raise RuntimeError('the run has ended; its writes are refused')
    cell = find_cell(get_channel_by_id(manager.channel), manager.cell_id)[1]
    if cell is None:
        raise RuntimeError('the run cell is no longer on the canvas')
    tip = find_tip(conn, manager.cell_id)
    tip_files = tip_file_map(conn, tip)
    from persistence import snapshot_files
    accepted = snapshot_files(conn,manager.cell_id)
    base = {f['path']:f['body_hash'] for f in accepted}
    snap = Snapshot(tip,tip_files,base,dict(writes),deleted)
    snap.revisions = tuple((f['path'],f['file_revision']) for f in accepted)
    snap.surface_identity = identity(manager.cell_id)
    snap.policy_refused = write_refusals(manager, writes.keys() | deleted)
    return snap


def refusal(path, error):
    return {'path': path, 'reason': 'handler_invalid', 'error': str(error)[:4096]}


def _input(path, files, previous):
    """Read selected admitted CAS objects; large bodies stay opaque."""
    from cas import blob_path, read_blob
    from verification import TEXT_LIMIT, verification_content, verifier_input
    prefix = path.rpartition('/')[0]
    selected, retained = {}, 0

    def content(digest):
        nonlocal retained
        size = os.stat(blob_path(digest)).st_size
        if size > TEXT_LIMIT:
            return {'size': size, 'hash': digest}
        body = read_blob(digest)
        if body is None or hashlib.sha256(body).hexdigest() != digest:
            raise ValueError('Verification source object is missing or damaged')
        retained += len(body)
        if retained > 32 * 1024**2:
            raise ValueError('Verification snapshot exceeds 32 MiB')
        return body

    for name, digest in files.items():
        if name == path or (name.startswith(prefix + '/') if prefix else '/' not in name):
            selected[name] = content(digest)
    value = verifier_input(path, selected, files)
    if path in previous:
        old = verification_content(content(previous[path]))
        value['previous_text' if isinstance(old, str) else 'previous_bytes'] = old
    return value


class RunVerifier:
    """One lazy worker lease and one deadline for preparation and both attempts."""
    def __init__(self, manager):
        from verifier_pool import FLUSH_SECONDS
        self.manager = manager
        self.deadline = time.monotonic() + FLUSH_SECONDS
        self.stack = ExitStack()
        self.lease = None
        self.catalog_key = None
        self.handlers = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def current_key(self):
        from handlers import handler_catalog_snapshot
        from state import get_channel_by_id
        return handler_catalog_snapshot(get_channel_by_id(self.manager.channel))[0]

    def prepare(self):
        from handlers import catalog
        from state import get_channel_by_id
        # Record the key before lookup: a concurrent catalog edit forces the
        # caller's normal snapshot retry even if lookup returns the newer copy.
        self.catalog_key = self.current_key()
        self.handlers = catalog(get_channel_by_id(self.manager.channel))

    def _source(self, path):
        assert self.handlers is not None
        record = self.handlers.for_path(path)
        return record.accepted_files.get('verify.ts') if record else None

    def _targets(self, snap, files):
        touched = snap.writes.keys() | snap.deleted
        targets = set(snap.writes) & files.keys()
        for package in discover_definitions(files).packages:
            if any(path.startswith(package.prefix + '/') for path in touched):
                targets.add(package.definition_path)
        return {path: source for path in sorted(targets) if (source := self._source(path)) is not None}

    def check(self, snap):
        from verification import verify_source
        from verifier_pool import get_verifier_pool, VerifierFailure, budget_exceeded
        refused = dict(snap.policy_refused)
        # Monotone removal: accepted definitions cannot depend on bytes from
        # another write that the host just refused. Each repeat removes a write.
        while True:
            files = snap.candidate(refused)
            targets = self._targets(snap, files)
            removed = False
            for path, source in targets.items():
                if path in refused:
                    continue
                try:
                    if time.monotonic() >= self.deadline:
                        result = budget_exceeded()
                    else:
                        value = _input(path, files, snap.base)
                        if self.lease is None:
                            self.lease = self.stack.enter_context(get_verifier_pool().lease(self.deadline))
                        result = verify_source(source, value, lease=self.lease, deadline=self.deadline)
                except (ValueError, OSError, VerifierFailure) as exc:
                    result = {'valid': False, 'error': str(exc)}
                if not result['valid']:
                    refused[path] = refusal(path, result.get('error', 'invalid file'))
                    removed |= path in snap.writes
            if not removed:
                return refused

    def raced(self, snap):
        """No stale verified writes after the one permitted outside-lock retry."""
        # Catalog membership comes from the last attempt; caller also passes
        # current catalog metadata identity. Fail closed for every written path
        # if that authority changed again, because its new types are unknown.
        if self.current_key() != self.catalog_key:
            targets = snap.writes
        else:
            targets = self._targets(snap, snap.candidate())
        return {path: refusal(path, 'cell changed during verification; retry') for path in targets}
