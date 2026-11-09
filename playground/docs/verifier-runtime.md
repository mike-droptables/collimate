# Verifier execution in the browser

The runtime substitutes two internal mechanisms: the SDK compiler's `_native`
transport and the API's verifier pool. The canonical outer compiler functions
still validate inputs and compute hashes. Host admission, user validation, run
write refusal, snapshot rechecks and publication remain the app's own code.
The startup page and the Browser demo card disclose the 64 MiB heap,
512 KiB stack, tested Chromium engine and required JSPI capability. Browsers
without JSPI refuse startup. A deadline or capacity refusal says
"Verifier busy, try again (deadline exceeded)"; its `Verifier ` prefix keeps
it retryable by the canonical host rather than cached as invalid content.
Negative content verdicts keep their authored reason. Every refused verifier
attempt, including local checks, host pre-dispatch
expiry and cached negative verdicts, emits one structured console record with
only a fixed internal `op`, a fixed refusal-category `reason` and `elapsed_ms`.
Only the known expiry branches in staged host/scheduler copies call a private
helper that marks internal deadline provenance. Authored error strings are
unrestricted, preserved byte-for-byte, and classified as content even when
they match deadline wording. Returned dictionaries are never interpreted as
expiry signals.
The confined outer host adapter observes the whole attempt; nested seam calls
share its context and produce no duplicate record. Categories distinguish
deadline, cancellation, input, content, protocol and startup. Authored reasons
and untrusted worker diagnostics remain in their returned verdicts and UI;
they never enter these records. The JS supervisor observes only startup,
before Python is available. The E2E helper prints these
records from before navigation, including boot failures. No request payload,
source code or credential is included. Deadline values remain unchanged.

Neither successful verdicts nor accepted handler identities are synthesized.

## Synchronous calls and independent deadlines

Pyodide provides `pyodide.ffi.run_sync` through WebAssembly Promise
Integration (JSPI). A synchronous Python caller can wait for a JavaScript
Promise while the page remains responsive. It was tested on 0.29.5 from an
asynchronous entrypoint, an asyncio task, a timer callback and a real
synchronous FastAPI route, and rechecked on the pinned 314.0.7 (Chromium 156)
from a synchronous caller under `runPythonAsync`. This follows Pyodide's documented
[stack-switching API](https://blog.pyodide.org/posts/jspi/).

The supervisor prewarms two verifier workers and an independent compiler
worker under one 30-second startup budget. The separate compiler avoids
deadlock when run verification acquires a verifier lease before compiling.
Lease waits retain the caller's deadline. Evaluation and compilation remain
bounded by the smaller of the remaining caller budget and five seconds.
The page terminates workers on expiry or cancellation, including when guest
code loops in a getter or while invoking the compiler. Shutdown terminates all
owned workers; late replies cannot complete a different request.

Each evaluation creates a fresh QuickJS context with a 64 MiB heap and
512 KiB stack. The only guest host capability is bounded virtual-file
compilation. No page, network, filesystem or Python object is exposed to the
guest. The SDK's `_BOUNDARY` string is supplied unchanged: captured intrinsics
normalize the verdict inside the supervised engine, and only the resulting
bounded JSON string crosses back. QuickJS Asyncify permits the synchronous
guest compiler capability to await esbuild without giving guest code a
Promise-based verdict contract; see the engine's
[documented Asyncify support](https://github.com/justjake/quickjs-emscripten).

The exact esbuild version and compiler policy come from the SDK. Its exported
portable compiler module and contract are copied byte-for-byte. The
`esbuild-wasm` pin in `verifier-runtime/package.json` and the version
`web/verifier-worker.js` accepts at `init` move with that contract, in the
same change as the re-vendor; a build against a newer SDK contract refuses
every verifier at startup. Verifier
compilation permits only `collimate/verify`; renderer compilation preserves
the canonical virtual-file resolver. Compiler callbacks return both the
bundle and the transformed original entrypoint for static export checks.

## Interleaving and resource differences

JSPI suspension allows unrelated Python callbacks to run. The host's run
publication and package-preview paths release cell/state locks before
verification, then recheck snapshots before publication. Channel seeding works
on an unpublished channel. The scheduler additionally prevents a second
validation step from entering while the first is suspended, and shutdown
drains active work before clearing results. Native thread locks are not used
to wait for browser worker capacity.

Browser workers do not implement POSIX `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`
or `RLIMIT_NOFILE`. In particular, total browser/compiler memory is not bounded
by the native worker's 2 GiB address-space limit. Enforced browser bounds are
the QuickJS heap/stack limits, input/output limits, bounded worker count and
independent elapsed-time deadlines. Guest code has no filesystem or descriptor
authority. This is an explicit runtime resource difference, not a claim of
identical OS sandboxing.

JSPI support is checked before app startup. Chromium is tested; absence of
JSPI produces a boot error rather than skipping verification. JSPI is not
available in every browser: consult the platform's
[compatibility documentation](https://developer.mozilla.org/en-US/docs/WebAssembly/Reference/JavaScript_interface/promising_static)
before assuming portability.

## Verification evidence

The P4F09 browser regression boots through actual package admission, saves an
invalid typed user file and observes its negative advisory verdict, exercises
valid/invalid and nonterminating verifiers through the canonical host path,
checks page responsiveness, and proves in-flight scheduler shutdown drains.
Supervisor tests cover the lease cap, independent compilation, timeout worker
replacement, cancellation, shutdown and mismatched reply rejection. Worker
tests compare canonical compiler output and verifier behavior directly.

Run the artifact-only regression through the existing E2E harness:

```sh
npm --prefix playground ci
npm --prefix playground run test:e2e -- tests/e2e/verifier-runtime.spec.ts
```

The supervisor lifecycle controls also run with the existing demo pytest gate.
For compiler/native parity controls, first install the normal API/SDK and
frontend development dependencies, then run:

```sh
npm --prefix playground/verifier-runtime ci
node --test playground/tests/runtime/verifier-worker.test.mjs
```

That separate worker suite uses the SDK source and native compiler as its
reference. The browser E2E uses only the built artifact and E2E dependencies.
