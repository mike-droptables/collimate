"""Isolated-server timing for accepted mutations and the P5 sync comparison.

Installed only by persistence_browser.server when PERSISTENCE_MEASURE_SERVICE=1.
The production connection and CAS policies never read these controls.
"""

from contextvars import ContextVar
from functools import wraps
import json
import os
import threading
import time


def install(control):
    cas_sync = os.environ.get('PERSISTENCE_MEASURE_CAS_SYNC', 'on')
    sqlite_sync = os.environ.get('PERSISTENCE_MEASURE_SQLITE', 'FULL')
    if cas_sync not in {'on', 'off'} or sqlite_sync not in {'NORMAL', 'FULL'}:
        raise ValueError('P5 comparison requires CAS on/off and SQLite NORMAL/FULL')

    # Patch connections before importing the app or mutation service. Every
    # benchmark connection still runs the production opener and migrations.
    import database

    if sqlite_sync == 'NORMAL':
        class NormalDurableConnection(database.DurableConnection):
            def execute(self, sql, parameters=(), /):
                # The opener's FULL policy must be changed before its first
                # migration transaction, including the fresh global store.
                if sql.strip().rstrip(';').casefold() == 'pragma synchronous = full':
                    sql = 'PRAGMA synchronous = NORMAL;'
                return super().execute(sql, parameters)

        database.DurableConnection = NormalDurableConnection

    rows = []
    observed = {'channel': {}, 'global': {}}
    metadata = {'kind': 'server_configuration', 'casSync': cas_sync,
                'sqliteRequested': sqlite_sync, 'sqliteObserved': observed,
                'connectionOpenCounts': {'channel': 0, 'global': 0},
                'maxServiceConcurrency': 0}
    rows.append(metadata)
    observation_lock = threading.Lock()

    def configure_connection(connection, store):
        with observation_lock:
            metadata['connectionOpenCounts'][store] += 1
            observe = not observed[store]
        if not observe:
            return connection
        actual = {
            'synchronous': connection.execute('PRAGMA synchronous').fetchone()[0],
            'journalMode': connection.execute('PRAGMA journal_mode').fetchone()[0],
            'busyTimeoutMs': connection.execute('PRAGMA busy_timeout').fetchone()[0],
        }
        key = json.dumps(actual, sort_keys=True)
        with observation_lock:
            observed[store][key] = observed[store].get(key, 0) + 1
        return connection

    original_connect = database._connect

    @wraps(original_connect)
    def channel_connect(*args, **kwargs):
        return configure_connection(original_connect(*args, **kwargs), 'channel')

    database._connect = channel_connect
    original_global = database.get_global_db_connection

    @wraps(original_global)
    def global_connect(*args, **kwargs):
        return configure_connection(original_global(*args, **kwargs), 'global')

    database.get_global_db_connection = global_connect

    import cas
    import cell_commits
    import persistence

    active = ContextVar('persistence_measurement', default=None)
    verifying = ContextVar('persistence_cas_verifying', default=False)
    cas_directory_syncing = ContextVar('persistence_cas_directory_syncing', default=False)
    service_lock = threading.Lock()
    in_service = 0

    def add_time(name, milliseconds):
        row = active.get()
        if row is not None:
            row[name] += milliseconds

    def timed(name, function):
        @wraps(function)
        def call(*args, **kwargs):
            started = time.perf_counter_ns()
            try:
                return function(*args, **kwargs)
            finally:
                add_time(name, (time.perf_counter_ns() - started) / 1_000_000)
        return call

    class TimedHash:
        def __init__(self, digest):
            self._hash = digest

        def _call(self, method, *args):
            started = time.perf_counter_ns()
            try:
                return getattr(self._hash, method)(*args)
            finally:
                add_time('verifyHashMs' if verifying.get() else 'hashMs',
                         (time.perf_counter_ns() - started) / 1_000_000)

        def update(self, data):
            return self._call('update', data)

        def hexdigest(self):
            return self._call('hexdigest')

        def digest(self):
            return self._call('digest')

        def copy(self):
            return TimedHash(self._hash.copy())

        def __getattr__(self, name):
            return getattr(self._hash, name)

    original_hashlib = cas.hashlib

    class HashlibProxy:
        def sha256(self, *args, **kwargs):
            started = time.perf_counter_ns()
            try:
                return TimedHash(original_hashlib.sha256(*args, **kwargs))
            finally:
                add_time('verifyHashMs' if verifying.get() else 'hashMs',
                         (time.perf_counter_ns() - started) / 1_000_000)

        def __getattr__(self, name):
            return getattr(original_hashlib, name)

    cas.hashlib = HashlibProxy()
    persistence.hashlib = cas.hashlib
    original_verify = cas._verify_path

    @wraps(original_verify)
    def verify_path(*args, **kwargs):
        token = verifying.set(True)
        try:
            return timed('verifyMs', original_verify)(*args, **kwargs)
        finally:
            verifying.reset(token)

    cas._verify_path = verify_path

    original_os = cas.os

    class OsProxy:
        def replace(self, source, target):
            return timed('renameMs', original_os.replace)(source, target)

        def __getattr__(self, name):
            return getattr(original_os, name)

    cas.os = OsProxy()

    class TimedLock:
        def __init__(self, lock):
            self.lock = lock
            self.local = threading.local()

        def acquire(self, *args, **kwargs):
            started = time.perf_counter_ns()
            acquired = self.lock.acquire(*args, **kwargs)
            if acquired:
                depth = getattr(self.local, 'depth', 0)
                self.local.depth = depth + 1
                if depth == 0:
                    add_time('publicationLockWaitMs', (time.perf_counter_ns() - started) / 1_000_000)
                    self.local.entered = time.perf_counter_ns()
                    self.local.row = active.get()
            return acquired

        def release(self):
            depth = self.local.depth - 1
            self.local.depth = depth
            if depth == 0:
                row = self.local.row
                if row is not None:
                    row['publicationLockHoldMs'] += (time.perf_counter_ns() - self.local.entered) / 1_000_000
            self.lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_exc):
            self.release()

        def __getattr__(self, name):
            return getattr(self.lock, name)

    cas.publication_lock = TimedLock(cas.publication_lock)

    original_store = cas._store_blob

    @wraps(original_store)
    def store_blob(blob_hash, write_fn, **options):
        row = active.get()
        if row is not None:
            path = cas.blob_path(blob_hash)
            exists = original_os.path.isfile(path)
            row['dedupe'] = row['dedupe'] or exists
            row['newObject'] = row['newObject'] or not exists
            row['newShard'] = row['newShard'] or not original_os.path.isdir(original_os.path.dirname(path))
        return original_store(blob_hash, write_fn, **options)

    cas._store_blob = store_blob

    original_fsync_directory = cas.fsync_directory

    @wraps(original_fsync_directory)
    def fsync_directory(path):
        started = time.perf_counter_ns()
        failed = False
        try:
            return original_fsync_directory(path)
        except BaseException:
            failed = True
            raise
        finally:
            row = active.get()
            if row is not None and not cas_directory_syncing.get():
                row['dbDirectorySyncCalls'].append({
                    'path': str(path), 'ms': (time.perf_counter_ns() - started) / 1_000_000,
                    'failed': failed})

    cas.fsync_directory = fsync_directory

    def sync_wrapper(name, function):
        @wraps(function)
        def call(path):
            started = time.perf_counter_ns()
            failed = False
            token = cas_directory_syncing.set(True) if name == 'directorySyncCalls' else None
            try:
                if cas_sync == 'on':
                    return function(path)
            except BaseException:
                failed = True
                raise
            finally:
                row = active.get()
                if row is not None:
                    row[name].append({'path': str(path), 'ms': (time.perf_counter_ns() - started) / 1_000_000,
                                      'skipped': cas_sync == 'off', 'failed': failed})
                if token is not None:
                    cas_directory_syncing.reset(token)
        return call

    # A owns these production CAS-only seams. Never wrap os.fsync globally:
    # SQLite native sync and database-directory confirmation stay untouched.
    cas.sync_cas_file = sync_wrapper('fileSyncCalls', cas.sync_cas_file)
    cas.sync_cas_directory = sync_wrapper('directorySyncCalls', cas.sync_cas_directory)
    database.confirm_sql_durability = timed('dbConfirmationMs', database.confirm_sql_durability)

    stage = persistence._stage

    @wraps(stage)
    def stage_with_fault(operation):
        cfg = json.loads(control.read_text()) if control.exists() else {}
        if any(item.path == cfg.get('stage_delay_path') for item in operation.mutations):
            time.sleep(cfg.get('stage_delay_ms', 0) / 1000)
        return stage(operation)

    persistence._stage = timed('stageMs', stage_with_fault)
    persistence._commit = timed('sqlCommitCallMs', persistence._commit)
    original_cell_acquire = cell_commits.CellLock.acquire
    original_cell_release = cell_commits.CellLock.release

    @wraps(original_cell_acquire)
    def cell_acquire(self):
        started = time.perf_counter_ns()
        result = original_cell_acquire(self)
        add_time('cellLockWaitMs', (time.perf_counter_ns() - started) / 1_000_000)
        if self._depth == 1:
            self._measure_entered = time.perf_counter_ns()
            self._measure_row = active.get()
        return result

    @wraps(original_cell_release)
    def cell_release(self):
        if self._depth == 1:
            row = self._measure_row
            if row is not None:
                row['cellLockHoldMs'] += (time.perf_counter_ns() - self._measure_entered) / 1_000_000
        return original_cell_release(self)

    cell_commits.CellLock.acquire = cell_acquire
    cell_commits.CellLock.release = cell_release
    mutate = persistence.mutate_files

    @wraps(mutate)
    def measured(channel, cell_id, operation, **kwargs):
        nonlocal in_service
        request = operation if isinstance(operation, dict) else operation.model_dump()
        row = {'operationId': request['operation_id'], 'cellId': str(cell_id),
               'stageMs': 0.0, 'sqlCommitCallMs': 0.0, 'dbConfirmationMs': 0.0,
               'cellLockWaitMs': 0.0, 'cellLockHoldMs': 0.0,
               'hashMs': 0.0, 'verifyHashMs': 0.0,
               'verifyMs': 0.0, 'renameMs': 0.0, 'publicationLockWaitMs': 0.0,
               'publicationLockHoldMs': 0.0, 'fileSyncCalls': [], 'directorySyncCalls': [],
               'dbDirectorySyncCalls': [],
               'newObject': False, 'newShard': False, 'dedupe': False}
        with service_lock:
            row['serviceQueueDepthAtEntry'] = in_service
            in_service += 1
            metadata['maxServiceConcurrency'] = max(metadata['maxServiceConcurrency'], in_service)
        token = active.set(row)
        started = time.perf_counter_ns()
        try:
            result = mutate(channel, cell_id, operation, **kwargs)
            row['outcome'] = result['outcome']
            row['receipt'] = result
            return result
        except BaseException as exc:
            row['errorType'] = type(exc).__name__
            raise
        finally:
            row['serviceMs'] = (time.perf_counter_ns() - started) / 1_000_000
            active.reset(token)
            with service_lock:
                in_service -= 1
                rows.append(row)

    persistence.mutate_files = measured
    return rows
