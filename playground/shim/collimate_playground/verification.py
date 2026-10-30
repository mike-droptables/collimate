"""Confined compiler seam and supervisor-backed synchronous verifier leases.

The bridge must suspend through JSPI: its supervisor enforces deadlines and
terminates isolated workers independently of Python and authored JavaScript.
Canonical SDK compilation identities and API validation remain unchanged.
"""
from __future__ import annotations

import base64
import json
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

MAX_FRAME = 64 * 1024 * 1024
MAX_INPUT = 32 * 1024 * 1024
BUSY = 'Verifier busy, try again (deadline exceeded)'
FILE_SECONDS = 5.0
FLUSH_SECONDS = 30.0


# Observation belongs to the outer refused attempt, including local checks and
# canonical cached returns. Nested seam calls share its context and never emit
# another record. JS only observes startup before Python is available.
_observation_depth = ContextVar('verifier_observation_depth', default=0)
_refusal_category = ContextVar('verifier_refusal_category', default=None)


def _refusal_reason(error):
    # These are fixed categories: never log authored/worker diagnostics, inputs,
    # source, paths, or a caller-supplied operation name.
    if error in (BUSY, 'verifier budget exceeded'):
        return 'deadline exceeded; verifier busy, try again'
    if error == 'Verifier cancelled':
        return 'verification cancelled'
    if error in ('invalid verifier input', 'verifier input must be JSON data',
                 'verifier input exceeds byte limit', 'Verifier input is not valid JSON',
                 'Verifier input exceeds the worker frame limit'):
        return 'invalid verifier input'
    if error.startswith('Verifier '):
        return 'verifier protocol refused'
    return 'content refused'


def _record(op, reason, started):
    record = '[collimate-verifier-refusal] ' + json.dumps({
        'op': op, 'reason': reason,
        'elapsed_ms': max(0, round((time.monotonic() - started) * 1000)),
    })
    try:
        from js import console
    except ImportError:
        print(record)
    else:
        console.warn(record)


def _expired_verdict():
    # The host's budget_exceeded() body in the build (build/patches.py): every
    # host branch that finds its deadline already passed, a scheduler skipping
    # dispatch included, returns this instead of dispatching.
    _refusal_category.set('deadline exceeded; verifier busy, try again')
    if _observation_depth.get() == 0:
        _record('verify', 'deadline exceeded; verifier busy, try again', time.monotonic())
    return {'valid': False, 'error': BUSY}


def _observe(op):
    def decorate(call):
        @wraps(call)
        def observed(*args, **kwargs):
            started = time.monotonic()
            depth = _observation_depth.get()
            token = _observation_depth.set(depth + 1)
            category_token = _refusal_category.set(None) if depth == 0 else None
            reason = None
            try:
                result = call(*args, **kwargs)
                if isinstance(result, dict) and result.get('valid') is False:
                    reason = _refusal_category.get() or 'content refused'
                return result
            except Exception as exc:
                reason = _refusal_reason(str(exc))
                _refusal_category.set(reason)
                raise
            finally:
                _observation_depth.reset(token)
                if category_token is not None:
                    _refusal_category.reset(category_token)
                if depth == 0 and reason is not None:
                    _record(op, reason, started)
        return observed
    return decorate


def _host_verifier(verify_source):
    # Authored errors are unrestricted text. Only internal expiry branches mark
    # deadline provenance; returned dictionary text is never a deadline signal.
    return _observe('verify')(verify_source)


def browser_invoke(op, payload_json, remaining_ms, cancelled=None):
    """Obtain a JSON result from the independently supervised Promise bridge."""
    from js import collimateVerifierInvoke
    from pyodide.ffi import create_proxy, run_sync

    proxy = create_proxy(cancelled) if cancelled is not None else None
    try:
        return run_sync(collimateVerifierInvoke(op, payload_json, remaining_ms, proxy))
    finally:
        if proxy is not None:
            proxy.destroy()


class BridgeFailure(RuntimeError):
    pass


def _check(deadline, cancelled=None):
    if cancelled is not None and cancelled.is_set():
        raise BridgeFailure('Verifier cancelled')
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise BridgeFailure(BUSY)
    if time.monotonic() >= deadline:
        raise BridgeFailure(BUSY)


@_observe('bridge')
def _call(invoke, op, payload, deadline, cancelled=None, *, check_after=True):
    _check(deadline, cancelled)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BridgeFailure('Verifier input is not valid JSON') from exc
    if len(encoded.encode('utf-8')) > MAX_FRAME:
        raise BridgeFailure('Verifier input exceeds the worker frame limit')
    try:
        result = invoke(op, encoded, max(0, (deadline - time.monotonic()) * 1000),
                        cancelled.is_set if cancelled is not None else None)
    except Exception as exc:
        message = str(exc)[:4096]
        # Pyodide's JsException renders a JavaScript Error with this prefix.
        if message == 'Error: ' + BUSY:
            message = BUSY
        if not message.startswith(('Verifier ', 'verifier budget exceeded')):
            message = 'Verifier worker protocol failed: ' + message
        raise BridgeFailure(message[:4096]) from exc
    if check_after:
        _check(deadline, cancelled)
    if not isinstance(result, str) or len(result.encode('utf-8')) > MAX_FRAME:
        raise BridgeFailure('Verifier output exceeds the worker frame limit')
    try:
        return json.loads(result)
    except (TypeError, ValueError) as exc:
        raise BridgeFailure('Verifier worker protocol failed') from exc


def native_compiler(invoke):
    """Replace only renderer_bundle._native; outer SDK checks/hashes still run."""
    @_observe('compile')
    def compile_map(files, entrypoint, policy, deadline, own_process_group, *, verifier=False):
        from collimate._esbuild_service import MAX_BYTES, MAX_ITEMS
        from collimate._host.renderer_bundle import RendererCompileError, validate_path

        end = time.monotonic() + FILE_SECONDS
        if deadline is not None:
            if not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
                raise RendererCompileError('Compiler deadline must be finite')
            end = min(end, deadline)
        try:
            if (len(files) > MAX_ITEMS or any(not isinstance(raw, bytes) for raw in files.values())
                    or sum(len(raw) for raw in files.values()) > MAX_BYTES):
                raise RendererCompileError('Renderer source bytes exceed compiler input limits or have invalid types')
            snapshot = dict(files)
            for path in snapshot:
                validate_path(path).encode('utf-8')
            if entrypoint not in snapshot:
                raise RendererCompileError('Renderer entrypoint is absent from the package')
            result = _call(invoke, 'compile', {
                'files': [[path, base64.b64encode(raw).decode('ascii')] for path, raw in snapshot.items()],
                'entrypoint': entrypoint, 'policy': policy, 'verifier': verifier,
            }, end)
            if (not isinstance(result, dict) or set(result) != {'code', 'entrypoint_js'}
                    or any(not isinstance(result[key], str) for key in result)):
                raise RendererCompileError('Native compiler returned invalid output')
            if any(len(value.encode('utf-8')) > MAX_BYTES for value in result.values()):
                raise RendererCompileError('Native compiler output exceeds byte limit')
            return result['code'], result['entrypoint_js']
        except (BridgeFailure, ValueError, TypeError, KeyError, UnicodeError) as exc:
            if isinstance(exc, RendererCompileError):
                raise
            raise RendererCompileError(str(exc)[:4096]) from exc
    return compile_map


class _Lease:
    def __init__(self, pool, token, deadline, cancelled):
        self.pool, self.token, self.deadline, self.cancelled = pool, token, deadline, cancelled

    @_observe('evaluate')
    def verify(self, code, value, *, deadline=None):
        end = min(self.deadline, deadline if deadline is not None else self.deadline,
                  time.monotonic() + self.pool.file_seconds)
        try:
            _check(end, self.cancelled)
            if self.pool.closed:
                raise BridgeFailure('Verifier pool is shutting down')
            if not isinstance(code, str) or not isinstance(value, dict):
                _refusal_category.set('invalid verifier input')
                return {'valid': False, 'error': 'invalid verifier input'}
            try:
                input_json = json.dumps(value, ensure_ascii=True, allow_nan=False)
            except (TypeError, ValueError, UnicodeError):
                _refusal_category.set('invalid verifier input')
                return {'valid': False, 'error': 'verifier input must be JSON data'}
            if len(input_json.encode()) > MAX_INPUT or len(code.encode()) > MAX_INPUT:
                _refusal_category.set('invalid verifier input')
                return {'valid': False, 'error': 'verifier input exceeds byte limit'}
            if self.token is None:
                self.token = self.pool._acquire(end, self.cancelled)
            from collimate._host.verification import VERIFICATION_BOUNDARY
            result = _call(self.pool.invoke, 'evaluate', {
                'lease_id': self.token, 'code': code, 'input_json': input_json,
                'boundary': VERIFICATION_BOUNDARY,
            }, end, self.cancelled)
            if (not isinstance(result, dict) or type(result.get('valid')) is not bool
                    or (not result['valid'] and (not isinstance(result.get('error'), str)
                                               or not result['error']))):
                raise BridgeFailure('Verifier returned an invalid verdict')
            return {'valid': True} if result['valid'] else {'valid': False, 'error': result['error'][:4096]}
        except BridgeFailure as exc:
            _refusal_category.set(_refusal_reason(str(exc)))
            if self.token is not None:
                token, self.token = self.token, None
                self.pool._release(token, discard=True)
            return {'valid': False, 'error': str(exc)}


class VerifierPool:
    """Lease surface retained; all queue waits and worker ownership live in JS."""
    def __init__(self, *, invoke=browser_invoke, root=None, cap=2, file_seconds=FILE_SECONDS):
        if cap < 1 or file_seconds <= 0:
            raise ValueError('Invalid verifier pool budget')
        self.invoke, self.cap = invoke, cap
        self.file_seconds = min(float(file_seconds), FILE_SECONDS)
        self.closed = False

    @_observe('acquire')
    def _acquire(self, end, cancelled):
        if self.closed:
            raise BridgeFailure('Verifier pool is shutting down')
        result = _call(self.invoke, 'acquire', {'cap': self.cap}, end, cancelled, check_after=False)
        if (not isinstance(result, dict) or not isinstance(result.get('lease_id'), str)
                or not result['lease_id']):
            raise BridgeFailure('Verifier worker protocol failed')
        try:
            _check(end, cancelled)
        except BridgeFailure:
            self._release(result['lease_id'], discard=True)
            raise
        return result['lease_id']

    def _release(self, token, *, discard):
        # Cleanup has its own bounded ownership budget; no authored code runs.
        _call(self.invoke, 'release', {'lease_id': token, 'discard': discard},
              time.monotonic() + FILE_SECONDS)

    @contextmanager
    def lease(self, deadline=None, *, cancelled=None):
        from verifier_pool import VerifierFailure
        end = min(deadline if deadline is not None else float('inf'), time.monotonic() + FLUSH_SECONDS)
        try:
            token = self._acquire(end, cancelled)
        except BridgeFailure as exc:
            raise VerifierFailure(str(exc)) from exc
        lease = _Lease(self, token, end, cancelled)
        try:
            yield lease
        finally:
            if lease.token is not None:
                token, lease.token = lease.token, None
                self._release(token, discard=self.closed)

    def close(self):
        if not self.closed:
            self.closed = True
            _call(self.invoke, 'close', {}, time.monotonic() + FILE_SECONDS)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def install(invoke=browser_invoke):
    """Install before host imports capture pool getter references."""
    import collimate._host.renderer_bundle as compiler
    import verifier_pool as host

    compiler._native = native_compiler(invoke)
    pool = None

    def get_pool():
        nonlocal pool
        if pool is None:
            pool = VerifierPool(invoke=invoke)
        return pool

    def close_pool():
        nonlocal pool
        current, pool = pool, None
        if current is not None:
            current.close()

    host.VerifierPool = VerifierPool
    host.get_verifier_pool = get_pool
    host.close_verifier_pool = close_pool
    # Install before main (and its routes) capture the host verifier function.
    import verification as validation
    validation.get_verifier_pool = get_pool
    validation.verify_source = _host_verifier(validation.verify_source)
    validation.validate_file = _host_verifier(validation.validate_file)
