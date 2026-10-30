"""Fresh, authority-free verifier contexts. Call only inside a watched worker.

The caller MUST enforce the absolute deadline outside this process. QuickJS's
binding timer disables Python callbacks, so it cannot cover the bundle builtin.
No untrusted engine object is converted with Object.json().
"""
from __future__ import annotations

import json
import math
import time
from typing import TypedDict, NotRequired


class Verdict(TypedDict):
    valid: bool
    error: NotRequired[str]


_MAX_INPUT = 32 * 1024 * 1024
_MAX_FILES = 10_000
_MAX_ERROR = 4096

# Capture trusted intrinsics before any authored code runs. The Python callable
# is removed from globals; only the bounded virtual-map bundle capability remains.
VERIFICATION_BOUNDARY = r'''
(() => {
 const parse=JSON.parse, stringify=JSON.stringify, array=Array.isArray;
 const create=Object.create, host=globalThis.__collimate_host_bundle;
 const slice=Function.prototype.call.bind(String.prototype.slice);
 delete globalThis.__collimate_host_bundle;
 globalThis.__collimate_bundle=(files, entrypoint)=>{
   const response=parse(host(stringify(files), stringify(entrypoint)));
   if (!response.ok) throw new Error(response.error);
   return response.result;
 };
 return function(inputJSON) {
   try {
     const verifier=globalThis.__collimate_verifier;
     if (!verifier || typeof verifier.default !== 'function') throw new Error('verifier needs a callable default export');
     const result=verifier.default(parse(inputJSON));
     if (!result || typeof result!=='object' || array(result) || typeof result.then==='function') throw new Error('invalid synchronous Verdict');
     const valid=result.valid;
     if (typeof valid!=='boolean') throw new Error('invalid synchronous Verdict');
     const out=create(null);
     out.valid=valid;
     if (!out.valid) {
       const error=result.error;
       if (typeof error!=='string' || !error) throw new Error('invalid Verdict error');
       out.error=slice(error,0,4096);
     }
     return stringify(out);
   } catch (_) {
     const out=create(null); out.valid=false;
     // Accessors remain inside the parent-watched invocation. Never invoke
     // exception.toString or verdict.toJSON. Keep useful compiler diagnostics.
     out.error='verifier threw or returned an invalid synchronous Verdict';
     try { if (_ && typeof _.message==='string') out.error=slice(_.message,0,4096); } catch (_) {}
     return stringify(out);
   }
 };
})()
'''


def _invalid(message: str) -> Verdict:
    return {'valid': False, 'error': message[:_MAX_ERROR]}


def run_verifier(code: str, input: dict, *, deadline: float) -> Verdict:
    """Run one compiled IIFE with fresh heap/stack and a virtual native bundle.

    ``deadline`` is absolute monotonic time, also forwarded to the compiler.
    A parent watchdog must kill this worker/process group on expiry, including
    hostile getters, loops and serialization. This function is not a sandbox for
    Python callers: its sole JS authority is validated virtual compilation.
    """
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or time.monotonic() >= deadline:
        return _invalid('verifier budget exceeded')
    if not isinstance(code, str) or not isinstance(input, dict):
        return _invalid('invalid verifier input')
    try:
        input_json = json.dumps(input, ensure_ascii=True, allow_nan=False)
        if len(input_json.encode()) > _MAX_INPUT or len(code.encode()) > _MAX_INPUT:
            return _invalid('verifier input exceeds byte limit')
    except (TypeError, ValueError):
        return _invalid('verifier input must be JSON data')

    def bundle(files_json: str, entrypoint_json: str) -> str:
        try:
            if not isinstance(files_json, str) or not isinstance(entrypoint_json, str):
                raise ValueError('bundle arguments must be JSON strings')
            if len(files_json.encode()) > _MAX_INPUT or len(entrypoint_json) > 4096:
                raise ValueError('bundle arguments exceed byte limit')
            files, entrypoint = json.loads(files_json), json.loads(entrypoint_json)
            if not isinstance(files, dict) or not files or len(files) > _MAX_FILES:
                raise ValueError('bundle needs a bounded virtual file map')
            def path_ok(path: object) -> bool:
                from .renderer_bundle import validate_path
                try:
                    validate_path(path)
                except ValueError:
                    return False
                return True
            if not path_ok(entrypoint) or entrypoint not in files:
                raise ValueError('bundle entrypoint must name a virtual package file')
            if any(not path_ok(path) or not isinstance(text, str) for path, text in files.items()):
                raise ValueError('bundle accepts relative text files only; opaque content is unavailable')
            from .renderer_bundle import bundle_renderer
            result = bundle_renderer({path: text.encode() for path, text in files.items()},
                                     entrypoint=entrypoint, deadline=deadline, _own_process_group=False)
            response = {'ok': True, 'result': {'source_hash': result.source_hash, 'code': result.code,
                       'bundle_hash': result.bundle_hash, 'entrypoint_js': result.entrypoint_js}}
            encoded = json.dumps(response, ensure_ascii=True)
            if len(encoded.encode()) > _MAX_INPUT:
                raise ValueError('bundle output exceeds byte limit')
            return encoded
        except Exception as exc:
            # Native compiler exceptions contain bounded virtual diagnostics;
            # callback errors never carry traceback/environment to JavaScript.
            return json.dumps({'ok': False, 'error': str(exc)[:_MAX_ERROR]})

    try:
        from .verifier_engine import context_type
        context = context_type()()
        context.set_memory_limit(64 * 1024 * 1024)
        context.set_max_stack_size(512 * 1024)
        context.add_callable('__collimate_host_bundle', bundle)
        normalize = context.eval(VERIFICATION_BOUNDARY)
        context.eval(code)
        encoded = normalize(input_json)
        if not isinstance(encoded, str) or len(encoded) > _MAX_ERROR + 128:
            return _invalid('verifier output exceeds byte limit')
        verdict = json.loads(encoded)
        if time.monotonic() >= deadline:
            return _invalid('verifier budget exceeded')
        return verdict
    except Exception:
        return _invalid('verifier engine failed or exhausted its resource limit')
