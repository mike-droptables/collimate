"""Coalesced advisory user validation; never a save or approval gate."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import threading
import time

from models import Channel

logger = logging.getLogger(__name__)
DEBOUNCE_SECONDS = .25
CELL_SECONDS = 30.0
MAX_RETAINED_CELLS = 128


def _memory_stamp(cell):
    return (str(cell.updated_at), tuple((f.name,f.file_revision,f.body_hash,id(f.content))
                                      for f in cell.files))


def _catalog_stamp(channel):
    """The Capabilities cells' in-memory identity: a change there can change
    any cell's validation."""
    from cell_placement import capabilities_stack_cells
    return tuple((str(c.id), _memory_stamp(c)) for c in capabilities_stack_cells(channel))


@dataclass
class Snapshot:
    channel: Channel
    cell_id: str
    revision: str
    version: str | None
    paths: tuple[str, ...]
    # Keep text identities alive while work is running. No copied bodies.
    references: tuple


def _snapshot(channel_name, cell_id):
    from cell_placement import find_cell
    from cell_write_scope import cell_write_scope
    from channel_db import open_channel_db
    from state import require_channel, channel_db_name
    from versions import find_tip
    channel = require_channel(channel_name)
    db = channel_db_name(channel)
    with cell_write_scope(db, [cell_id], hold_state=True):
        cell = find_cell(channel, cell_id)[1]
        if cell is None:
            raise ValueError('Cell is no longer on the canvas')
        conn = open_channel_db(db)
        try:
            conn.execute('BEGIN')
            tip = find_tip(conn, cell_id)
            version = tip if conn.execute("SELECT 1 FROM cells WHERE id=?", (tip,)).fetchone() else None
            from persistence import snapshot_files
            accepted=snapshot_files(conn,cell_id)
            revision=tuple((f['path'],f['file_revision']) for f in accepted)
            paths={f['path'] for f in accepted}
        finally:
            conn.close()
        stamp = (str(channel.id), tip, revision, _memory_stamp(cell), _catalog_stamp(channel))
        digest = hashlib.sha256(json.dumps(stamp, separators=(',', ':')).encode()).hexdigest()
        return Snapshot(channel, cell_id, digest, version, tuple(sorted(paths)), tuple(f.content for f in cell.files))


class ValidationService:
    def __init__(self):
        self.condition = threading.Condition()
        self.stopping = False
        self.scan_due = None
        self.pending = OrderedDict()
        self.seen = {}
        self.results = OrderedDict()
        self.thread = threading.Thread(target=self._loop, name='user-validation', daemon=True)
        self.thread.start()

    def wake(self, channel=None, cell_id=None):
        with self.condition:
            if self.stopping:
                return
            due = time.monotonic() + DEBOUNCE_SECONDS
            if channel is None:
                # Signals are cheap inside state/commit locks. Scan only after
                # releasing them, once per burst, never serialize file bodies.
                self.scan_due = self.scan_due or due
            else:
                from database import canonical_channel_id
                key = (canonical_channel_id(channel), str(cell_id))
                self.pending[key] = due
                self.results.pop(key, None)
            self.condition.notify()

    def _scan(self):
        from cell_placement import iter_stacks
        from state import state, state_lock, channel_db_name
        with state_lock:
            current = {}
            for channel in state.channels:
                catalog = _catalog_stamp(channel)
                for stack in iter_stacks(channel):
                    for cell in stack.cells:
                        if cell.builtin:
                            continue  # the app's own packages; nothing of the user's to check
                        key = (channel_db_name(channel), str(cell.id))
                        current[key] = (_memory_stamp(cell), catalog)
        with self.condition:
            for key, stamp in current.items():
                if self.seen.get(key) != stamp:
                    self.pending[key] = time.monotonic()
                    self.results.pop(key, None)
            self.seen = current
            for key in list(self.results):
                if key not in current:
                    self.results.pop(key, None)
            for key in list(self.pending):
                if key not in current:
                    self.pending.pop(key, None)

    def read(self, channel, cell_id):
        from state import channel_db_name
        snapshot = _snapshot(channel, str(cell_id))
        key = (channel_db_name(snapshot.channel), str(cell_id))
        with self.condition:
            result = self.results.get(key)
            if result is not None and result['source_revision'] == snapshot.revision:
                self.results.move_to_end(key)
                return {**result, 'verdicts': {p: dict(v) for p, v in result['verdicts'].items()}}
            # Do not restart the debounce on each UI poll.
            self.pending.setdefault(key, time.monotonic() + DEBOUNCE_SECONDS)
            self.results.pop(key, None)
            self.condition.notify()
        return self._response(snapshot, {}, pending=True)

    @staticmethod
    def _response(snapshot, verdicts, *, pending=False):
        return {'cell_id': snapshot.cell_id, 'source_version_id': snapshot.version,
                'source_revision': snapshot.revision, 'pending': pending, 'verdicts': verdicts}

    def _validate(self, key):
        from handlers import catalog
        from verification import validate_file
        from verifier_pool import budget_exceeded
        snapshot = _snapshot(*key)
        accepted = catalog(snapshot.channel)
        paths = [p for p in snapshot.paths if (r := accepted.for_path(p)) is not None and 'verify.ts' in r.accepted_files]
        verdicts = {}
        deadline = time.monotonic() + CELL_SECONDS
        for path in paths:
            with self.condition:
                if self.stopping:
                    return
            if _snapshot(*key).revision != snapshot.revision:
                self.wake(*key)
                return
            if time.monotonic() >= deadline:
                verdicts[path] = budget_exceeded()
            else:
                verdicts[path] = validate_file(snapshot.channel, snapshot.cell_id, path, deadline=deadline)
        if _snapshot(*key).revision != snapshot.revision:
            self.wake(*key)
            return
        with self.condition:
            if not self.stopping:
                self.results[key] = self._response(snapshot, verdicts)
                self.results.move_to_end(key)
                while len(self.results) > MAX_RETAINED_CELLS:
                    self.results.popitem(last=False)

    def _loop(self):
        while True:
            with self.condition:
                if self.stopping:
                    return
                now = time.monotonic()
                scan = self.scan_due is not None and self.scan_due <= now
                key = next((k for k, due in self.pending.items() if due <= now), None)
                if scan:
                    self.scan_due = None
                elif key is not None:
                    self.pending.pop(key)
                else:
                    dates = list(self.pending.values()) + ([self.scan_due] if self.scan_due is not None else [])
                    self.condition.wait(max(0, min(dates) - now) if dates else None)
                    continue
            try:
                if scan:
                    self._scan()
                else:
                    self._validate(key)
            except Exception:
                # Advisory work cannot roll back or fail a successful save.
                # A future read/save retries, with no stale result retained.
                logger.exception('Background user validation failed for %s', key)

    def close(self):
        with self.condition:
            self.stopping = True
            self.pending.clear()
            self.condition.notify()
        # Current native/QuickJS work has a bounded per-file deadline.
        self.thread.join()
        self.results.clear()
        self.seen.clear()


_service = None


def start_user_validation():
    global _service
    close_user_validation()
    _service = ValidationService()


def close_user_validation():
    global _service
    service, _service = _service, None
    if service is not None:
        service.close()


def notify_user_validation(channel=None, cell_id=None):
    if _service is not None:
        _service.wake(channel, cell_id)


def read_user_validation(channel, cell_id):
    if _service is None:
        raise RuntimeError('User validation service is not running')
    return _service.read(channel, cell_id)
