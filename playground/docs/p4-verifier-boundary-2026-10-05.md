# Browser verifier decision brief

Historical source assessment and measured failure, 2026-10-05. The confined verifier implementation has now passed three first-review lenses and its sole collected correction review, and is applied. Its [runtime contract and resource differences](verifier-runtime.md) describe the final mechanism. Actual worker controls pass on Node20, and browser checks prove admission, negative verdicts, independently enforced timeout, fault refusal, failed-worker termination and successful replacement. The canonical renderer build and current-contract test corrections are also applied: full units103 pass, build passes and all26 mandatory E2E cases pass with zero retries. Exact-commit fresh/private delivery gates remain required before phase acceptance.

The earlier blanket approval hold was an executor over-escalation: owner-decisions sections 1 and 7.1 require a decision for changes to enumerated contracts, not an internal execution mechanism that preserves them. A confined repair is already authorized. This correction is not an owner answer to the earlier question. The following initial diagnosis and feasibility requirements remain historical evidence, superseded by the measured implementation where stated above.

A bounded browser diagnostic reached the running ASGI app, replayed channel creation in disposable memory with traceback logging, and demonstrated `create_channel → seed_channel → generator package admission → ValueError: Native compiler resources unavailable or invalid: ValueError`. The original trace exposed only HTTP 500; it did not establish the cause. The diagnostic does not demonstrate successful compilation or verifier execution.

## Why the required demo reaches this boundary

`playground/build/vendor.py:64` copies the entire SDK renderer tree. That tree includes verifier-backed handler definitions, each with its `derived/verify.ts`, and both also exist in the current staged tree. `handlers.catalog` discovers handler definitions, checks the derived hash, and records their accepted bytes.

The required demo timelines write typed files and call `Demo.commit`, and the mandatory E2E cases require those runs to complete. This is not an optional import-only capability.

There is an earlier applicable path: startup refreshes library bundles from the copied trees. Channel seeding imports existing library bundles through `prepare_packages` and `admit_package`; the latter always executes the accepted verifier, including for shipped handlers. Its `allow_reserved_handler` argument permits reserved type claims, not verification bypass. The in-memory fallback for absent library bundles has different admission behavior, but it is not a substitute for the normal successful refresh path.

## Existing synchronous contracts

- Advisory validation: `ValidationService._validate` calls `verification.validate_file`, which snapshots current file/package/previous bytes and calls `verify_source`. Preserve debounce, revisions, catalog invalidation, stale-result rejection, bounded retention, and shutdown behavior.
- Explicit validation: `api/routes/handlers.py:25` calls the same synchronous `validate_file`. The generator IPC validation path also routes into synchronous host validation.
- Package admission: `package_approval.admit_package` calls `verify_source` synchronously and raises on a negative verdict. Renderer admission additionally uses synchronous native `bundle_renderer`. Channel/library imports and Add to share this boundary.
- Run publication: `run_verification.ValidationFlush.check` acquires a pool lease before calling `verify_source`; it repeats validation after removing refused writes and participates in the existing outside-lock snapshot/retry protocol. `Demo.commit` synchronously calls `_emit_partial` before its next await, and raises on refused paths. Preserving this protocol includes partial acceptance, previous content, catalog changes, cancellation, and publication order.
- Compilation and execution: `verify_source` compiles/cache-identifies exact accepted source plus prelude, bounds file time, and evaluates through a lease. A cache hit on compiled code does not bypass execution. The SDK engine exposes a bounded virtual-file `bundle` capability whose implementation also invokes the compiler.

## Resource and trust boundary

The native arrangement uses a separate QuickJS process, fresh contexts, heap/stack limits, bounded JSON frames and inputs, an overall flush deadline, per-file deadlines, and a parent that can kill the worker process group. SDK `verification.py` explicitly requires an external watchdog: callbacks, hostile getters, execution, and serialization must remain covered.

The existing browser runtime loads Pyodide in the page (`playground/web/boot.js:274`). Its shim runs synchronous host functions inline. Its current portable compiler exports asynchronous renderer compilation, not a complete verifier execution service. Therefore replacing the pool with inline JavaScript evaluation, or merely returning a Promise from the synchronous Python interface, does not preserve this contract. A timer on the same blocked event loop cannot enforce the current hard deadline.

## Execution default and decision boundary

Proceed with a confined browser verifier implementation and its necessary runtime adaptation under the existing P4 direct-fix authorization. A candidate must establish a separately supervised execution boundary, an isolated bounded engine, equivalent compiler/builtin behavior, and a way for existing synchronous Python callers to obtain results without blocking the supervising event loop. The source assessment does not establish which browser/Pyodide bridge can satisfy that last requirement. It is a feasibility gate before selecting an implementation. Any required asynchronous staging changes must preserve snapshots and commit ordering and remain in the auxiliary build overlays/shim.

Keep accepted source/hash identity, verifier input/verdict shapes, read/write authority, approval and catalog/type semantics, invalid-run-write refusal, user-write flagging, cancellation and resource guarantees unchanged. If a concrete implementation needs an enumerated interface change, report that choice before building on it. Difficulty and implementation size alone do not create a new permission requirement. P4 and the affected CI gate remain incomplete until actual proof; the default does not redefine E2E outcomes or conceal unavailable verification.

Allowing all files, removing handlers, pre-caching example verdicts, skipping typed flows, or hiding negative results would not satisfy the stated preservation requirement. A reduced demo would be a separate product decision.

No defensible numeric completion estimate follows from these source reads. The repair spans runtime/bridge feasibility, compiler and engine integration, lifecycle/resource enforcement, and actual browser parity tests; it is materially larger than replacing the validation scheduler. Begin with a bounded feasibility investigation before committing to an implementation estimate.

## Measured synchronous bridge

The subsequent memory-only probe passed on the pinned Pyodide 0.29.5 and installed Chromium 153. `pyodide.ffi.run_sync` resolved JavaScript promises from a direct async entrypoint, an asyncio task, a timer callback and a real synchronous FastAPI route (HTTP 200). A separate JavaScript heartbeat advanced 33 ticks during five 60 ms suspensions. This demonstrates a responsive supervisor while synchronous Python waits; it does not yet prove verifier equivalence.

Unrelated Python callbacks did interleave during the wait. Therefore the adapter must not suspend while holding a thread-owned lock that another task can re-enter, and the advisory scheduler must preserve a single active validation with shutdown draining. Existing snapshot/revision and publication guards remain required. The first 150-second probe timed out because its borrowed Python timer callback did not remain alive; the corrected probe used a pure JavaScript promise. That failed probe is not a runtime compatibility result.

The implementation candidate uses this bridge with independently terminable workers, bounded QuickJS contexts and the canonical compiler policy and normalization code. It patches only internal runtime seams; authored inputs, accepted hashes, verifier ABI, approval, refused writes and user validation remain canonical. JSPI behavior is described by the [Pyodide maintainers](https://blog.pyodide.org/posts/jspi/); the actual local probe is the evidence for this pinned runtime. Other browser engines are not proven by this Chromium result.

## Evidence required before calling it complete

Exercise the actual seeded catalog and all mandatory demo flows in the browser; compare valid and invalid canonical verifier outcomes; demonstrate accepted-byte identity and compiler behavior; test hostile execution and getters, timeout/cancellation, teardown, stale edits/catalog changes, refusal/partial publication, and ordering. Establish that failures cannot leave a worker or publish an unverified write. Keep all changes inside the authorized auxiliary folder and leave canonical API/SDK source unchanged.

## P5 follow-up P4F46: optional visual coverage

The required default E2E suite is separate from `tests/e2e/visual/renderer.visual.spec.ts`, which runs only under the existing visual opt-in. Source inspection found that its sole `FIXTURES` key is the removed `markdown` renderer. Canonical SDK `_build_requests` discovers16 native renderers, none with that ID. Every loop iteration therefore takes the missing-fixture `continue`, leaving zero screenshot assertions. This is a source/catalog reproduction, not an executed visual-job result.

P5's tests/CI batch must replace the stale fixture, obsolete Babel loading description/stage and retired scroll members in the fake host; require a nonzero asserted fixture count and reviewed Linux baselines. Keep the visual job opt-in and keep missing coverage visible. P4 does not claim its unrun visual baselines passed. Normal required build/unit/E2E results remain independently required.


## Current candidate after A4 CI cancellation

SDK S2 `9767cf30d1290ec95d8aac41ce2563468b3b54b5` remains unchanged, pushed, with all three private jobs green and exact fresh5,582/150.58s/zero skips plus Ruff. APP A4 `a7fd29b90a6b42a289434fa49b599ac93fa335b9` is pushed, pins S2, and has all eight main private jobs green at run37299970848. Its API passed2,513/1,554.90s/six warnings/zero skips and all eight standalone scripts passed. Exact remote A4 fresh setup and all15 APP suites completed with exit0 and clean tracked clone trees; API2,513/1,171.16s/six warnings/zero skips, frontend unit2,160/24.01s, coverage2,160/30.01s, browser62/7.26s, desktop333/3.67s, Electron7/4.2s, auxiliary103/13.36s/two warnings/zero skips, plus the remaining type/lint/build/isolation/map gates.

A4 required auxiliary run37299970798 passed build/units but was cancelled at its45-minute limit during E2E. Ten reporter groups/flaky markers and no final test count do not establish an accepted suite result. A controlled same-immutable-artifact CPU0 comparison of two unchanged cases produced1failed/1passed with two workers in75.35s versus2passed with one worker in100.64s. A separate diagnostic changes only one traceback-print line and reproduces1failed/1passed80.06s: actual package admission raises `ValueError: Verifier worker protocol failed: Error: Verifier timed out`. This establishes the controlled slow/concurrent admission refusal, not the unobserved inner cause of the remote cancelled run. The existing verifier deadline and refusal remain intact.

The exact five-file readiness/config correction is applied after THREE FIRST acceptance. It enforces the intended helper timeout, reports existing boot errors/last stage, retains the180s measured cold-boot bound, uses one CI worker and maxFailures1 with the existing two CI retries, and preserves all26 original cases plus two new controls. Product limits, API/SDK code, verifier inputs/verdicts and approval/typed-write rules are unchanged. All28 canonical cases passed in598.54s (10.0min), with CI=1, the configured one worker, zero retries and zero skips, against the unchanged immutable runtime artifact through the canonical serving script. A5 is the app commit containing this checkpoint; its exact fresh/private gates and final record delivery remain required. The final A5 exact fresh/private gates, completion record R and delivery proof remain required. P4 stays68/69 accepted; P4F09 remains in_progress. Finish P4 and both main pushes, then STOP before P5.

The controlled admission timeout is an explicit portability/performance default under OW-P4-19 for later owner review. JSPI support, QuickJS64MiB heap/512KiB stack, input/output limits, independently enforced deadlines and worker ownership remain required. Serial test scheduling does not increase those limits or establish all hardware/concurrency support. The trace-only diagnostic is not a product change; no canonical API/SDK bytes or authored/accepted verifier identities changed. The remote cancelled job's inner cause remains unobserved.
