"""Bounded exec-worker leases with parent-enforced file and flush deadlines."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os

from process_signals import signal_process_group
from pathlib import Path
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

MAX_FRAME = 64 * 1024 * 1024
FILE_SECONDS = 5.0
FLUSH_SECONDS = 30.0


class VerifierFailure(RuntimeError):
    pass


BUDGET_EXCEEDED = 'verifier budget exceeded'


def budget_exceeded() -> dict:
    """The verdict for a file whose verification deadline passed before its
    verifier ran: a retryable refusal, never a content verdict."""
    return {'valid': False, 'error': BUDGET_EXCEEDED}


class _Worker:
    def __init__(self, root: Path, script: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='worker-', dir=root))
        self._stop_lock = threading.Lock()
        self.dead = False
        self.calls = 0
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-I', str(script)], cwd=self.directory,
                env={'PATH': '', 'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '0'},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, start_new_session=True, bufsize=0,
            )
        except BaseException:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise

    def stop(self):
        with self._stop_lock:
            if self.dead:
                return
            self.dead = True
            # Kill the group even if the worker exited first: its compiler may remain.
            signal_process_group(self.process.pid, signal.SIGKILL, known_group=True)
            self.process.wait(timeout=2)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            shutil.rmtree(self.directory, ignore_errors=True)

    def call(self, code: str, value: dict, deadline: float, cancelled: threading.Event | None):
        try:
            payload = json.dumps({'code': code, 'input': value, 'deadline': deadline},
                                 ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError) as exc:
            raise VerifierFailure('Verifier input is not valid JSON') from exc
        if len(payload) > MAX_FRAME:
            raise VerifierFailure('Verifier input exceeds the worker frame limit')
        outgoing = memoryview(struct.pack('!I', len(payload)) + payload)
        response, sent, size = bytearray(), 0, None
        self.calls += 1
        try:
            assert self.process.stdin is not None
            assert self.process.stdout is not None
            assert self.process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    os.set_blocking(stream.fileno(), False)
                selector.register(self.process.stdin, selectors.EVENT_WRITE, 'send')
                selector.register(self.process.stdout, selectors.EVENT_READ, 'result')
                selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
                while True:
                    if cancelled is not None and cancelled.is_set():
                        raise VerifierFailure('Verifier cancelled')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VerifierFailure('Verifier timed out')
                    for key, _events in selector.select(min(remaining, .05)):
                        if key.data == 'send':
                            try:
                                sent += os.write(key.fd, outgoing[sent:sent + 65536])
                            except BlockingIOError:
                                continue
                            if sent == len(outgoing):
                                selector.unregister(key.fileobj)
                            continue
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if key.data == 'stderr':
                            # Drained without retaining private worker paths or unlimited output.
                            if not chunk:
                                selector.unregister(key.fileobj)
                            continue
                        if not chunk:
                            raise VerifierFailure('Verifier worker exited before returning a verdict')
                        response.extend(chunk)
                        if size is None and len(response) >= 4:
                            size = struct.unpack('!I', response[:4])[0]
                            if size > MAX_FRAME:
                                raise VerifierFailure('Verifier output exceeds the worker frame limit')
                        if size is not None and len(response) >= size + 4:
                            if len(response) != size + 4:
                                raise VerifierFailure('Verifier returned trailing protocol bytes')
                            result = json.loads(response[4:])
                            if (not isinstance(result, dict) or not isinstance(result.get('valid'), bool)
                                    or (not result['valid'] and not isinstance(result.get('error'), str))):
                                raise VerifierFailure('Verifier returned an invalid verdict')
                            return {'valid': True} if result['valid'] else {'valid': False, 'error': result['error'][:4096]}
        except (OSError, ValueError) as exc:
            raise VerifierFailure('Verifier worker protocol failed') from exc


class _Lease:
    def __init__(self, pool, deadline, worker, cancelled):
        self.pool, self.deadline, self.worker, self.cancelled = pool, deadline, worker, cancelled

    def verify(self, code: str, value: dict, *, deadline: float | None = None) -> dict:
        end = min(self.deadline, deadline if deadline is not None else float('inf'))
        if time.monotonic() >= end:
            return budget_exceeded()
        try:
            if self.worker is None:
                self.worker = self.pool._acquire(end, self.cancelled)
            return self.worker.call(code, value, min(end, time.monotonic() + self.pool.file_seconds), self.cancelled)
        except VerifierFailure as exc:
            if self.worker is not None:
                self.pool._discard(self.worker)
                self.worker = None
            return {'valid': False, 'error': str(exc)}


class VerifierPool:
    def __init__(self, *, root: Path | None = None, cap: int = 2, file_seconds=FILE_SECONDS, _worker_script=None):
        if cap < 1 or file_seconds <= 0:
            raise ValueError('Invalid verifier pool budget')
        self.root = (root or Path('.collimate/verifier-workers')).resolve()
        self.cap, self.file_seconds = cap, min(float(file_seconds), FILE_SECONDS)
        self.script = Path(_worker_script or Path(__file__).with_name('_verifier_worker.py')).resolve()
        self._condition = threading.Condition()
        self._all, self._idle = set(), []
        self._closed = False

    @property
    def worker_count(self):
        with self._condition:
            return len(self._all)

    def _acquire(self, deadline, cancelled):
        with self._condition:
            while True:
                if self._closed:
                    raise VerifierFailure('Verifier pool is shutting down')
                if cancelled is not None and cancelled.is_set():
                    raise VerifierFailure('Verifier cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VerifierFailure(BUDGET_EXCEEDED)
                if self._idle:
                    return self._idle.pop()
                if len(self._all) < self.cap:
                    worker = _Worker(self.root, self.script)
                    self._all.add(worker)
                    return worker
                self._condition.wait(min(remaining, .05))

    def _discard(self, worker):
        worker.stop()
        with self._condition:
            self._all.discard(worker)
            if worker in self._idle:
                self._idle.remove(worker)
            self._condition.notify_all()

    @contextmanager
    def lease(self, deadline: float | None = None, *, cancelled: threading.Event | None = None):
        end = min(deadline if deadline is not None else float('inf'), time.monotonic() + FLUSH_SECONDS)
        lease = _Lease(self, end, self._acquire(end, cancelled), cancelled)
        try:
            yield lease
        finally:
            worker = lease.worker
            if worker is not None:
                # Eight 5s jobs remain below the worker's cumulative 60s CPU backstop.
                with self._condition:
                    discard = worker.calls >= 8 or self._closed or worker.dead
                    if not discard:
                        self._idle.append(worker)
                        self._condition.notify_all()
                if discard:
                    self._discard(worker)

    def close(self):
        with self._condition:
            self._closed = True
            workers = list(self._all)
            self._all.clear()
            self._idle.clear()
            self._condition.notify_all()
        for worker in workers:
            worker.stop()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


_pool_lock = threading.Lock()
_pool = None


def get_verifier_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = VerifierPool()
        return _pool


def close_verifier_pool():
    global _pool
    with _pool_lock:
        old, _pool = _pool, None
    if old is not None:
        old.close()
