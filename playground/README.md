# Collimate browser demo (`playground/`)

Everything about the browser demo lives in this folder. The demo is the
real Collimate app — the FastAPI backend and the React frontend from this
repo — running entirely inside a visitor's browser tab: the Python side on
[pyodide](https://pyodide.org), the HTTP traffic routed through a service
worker, WebSockets bridged in-page. There is no server. What a visitor
*cannot* do is real work: the generators are scripted stand-ins that play
back a shell session, a chat and a file-editing agent with realistic
pacing, so the app's runs, live cells, versions and stop/finish lifecycle
can be seen without credentials, Docker or a machine of one's own. One channel, nothing saved: a reload starts over.

The rule that keeps this maintainable (AGENTS.md rule 6): **the main code
never knows the demo exists.** `api/`, `frontend/`, the SDK and `docs/`
carry no playground flags, branches or mentions. The demo is a *build* that
copies the main code, applies a handful of anchored patches, and adds a
shim. If the demo needs the app to behave differently, the answer is a
backend behaviour in the shim (a 400/501 JSON detail) or, as a last resort,
one more anchored patch in `build/patches.py` — never an edit to the main
tree.

## Layout

```
playground/
  build.sh              the ONE build entry point (thin bash → build/)
  serve.sh              serve dist/ locally with npx serve
  build/                the build, in stdlib python:
    __main__.py           stage → patches → vendor → assemble
    stage.py              copy api/ + frontend/ to .stage/ (never touching them)
    patches.py            anchored, fail-loud string edits to the STAGED copies
    vendor.py             demo generators, the repo's content/renderers and builtins (laid out as
                          the wheel's collimate_content/ + collimate_builtins/), the shim, demo
                          content → the staged api tree (the one archive pyodide unpacks)
    assemble.py           vite build, wheels (SDK + vendored), app tarball,
                          index.html injection
    inject_landing.py     landing copy + the "Try the browser demo" hero button
  shim/collimate_playground/   the python package installed into pyodide
    threads.py            asyncio.to_thread / run_in_executor / anyio run_sync inline
    modules.py            permissive stubs for unix-only + native-only modules
    bridge.py             ASGI bridge: streaming HTTP + WebSocket, binary-safe
    demo_runner.py        ProcessManager.start replacement (FakeProcess + timelines)
    terminal.py           the demo's own terminal sessions + websocket route
    boot.py               import main, lifespan, seed the channel, guards
  web/                  boot.js, sw.js, sw-gate.js, landing-hero.html
  demo/                 the demo content: channel.yaml, channel/ (the seeded cells),
                        generators/ (the three scripted timelines)
  tests/e2e/            Playwright specs against dist/ (see tests/e2e/README.md)
  tests/demo/           pytest: the timelines against a fake `demo`; the runner
                        drafts and the demo runner through the real app
  tests/check_isolation.sh   the grep guard (below)
  package.json, playwright.config.ts   the e2e harness; serve.sh is its webServer
  docs/history.md       where the old playground came from
  .stage/, .wheels/, dist/   build staging, the wheel download cache and the
                             output (gitignored)
```

`dist/` after a build:

```
dist/index.html, why.html, favicon.svg, fonts/   the landing (+ demo button)
dist/sw.js                                        the service worker (scope '/')
dist/playground/                                  the React app
dist/playground/app.tar.gz                        the staged api tree
dist/playground/pyodide/boot.js                   the pyodide bootstrap
dist/playground/pyodide/collimate_playground_sdk-*.whl  private SDK source payload
dist/playground/pyodide/*-py3-none-any.whl        websockets
```

## Architecture

### Request path: service worker as the HTTP server

`sw.js` is registered at the site root with scope `/` and intercepts every
same-origin `/api/*` fetch. It forwards the request (method, path, query,
headers plus the `Host` a worker's `Headers` never carry, body as an
`ArrayBuffer`) over a `MessageChannel` to the page that
hosts pyodide and answers with a **streaming** `Response`: the page posts
`{type:'start', status, headers}`, then `{type:'chunk', body}` per body
chunk (transferred, never copied or stringified), then `{type:'end'}`. The
SW feeds those into a `ReadableStream`, so server-sent events — the
channel state stream and the per-run event stream — flow exactly as from a
real server. When the consumer goes away (an `EventSource.close()`, a
navigation) the stream's `cancel()` posts `{type:'cancel'}` back and the
ASGI app receives `http.disconnect`, so streaming generators exit.

On the page, `boot.js` turns each forwarded request into a call of
`collimate_playground.boot.http(...)`, which runs the real `main.app`
(wrapped in the guard) with an ordinary ASGI `(scope, receive, send)` and
writes the response into a *sink* object; bytes cross the JS boundary as
`Uint8Array` in both directions (`bridge.py`).

Two queues cover the boot window. `sw-gate.js` — inlined as the FIRST
script in `index.html` — holds `/api/*` fetches and XHRs until the SW
controls the page; `boot.js` holds forwarded requests until pyodide is
ready. A page the active worker does not control (a hard reload bypasses
the SW) asks it to `claim` and waits for the resulting `controllerchange`.
The SW tracks readiness *per page* (`page-ready`, re-sent whenever the
controller changes): a request's headers timeout only starts once the page
that answers it has reported ready, so a request queued during a slow boot
— first visit or reload — is never turned into a synthetic 504. Requests
from a renderer sandbox iframe, which the Clients API cannot tie to its
parent tab, go to a visible top-level page known to host a runtime (a
landing tab is never picked). Null-body statuses (204/205/304) become
body-less `Response`s, and a cancelled or timed-out request closes its
message port on both sides.

### WebSockets: a page-level bridge

A service worker cannot see WebSockets, and the live renderers (`.chat`,
`.terminal` — both `embed: native`) open one each via the SDK's
`openSessionSocket`. `sw-gate.js` therefore replaces `window.WebSocket`
with a constructor that returns a `BridgedWebSocket` for same-origin
`ws(s)://…/api/*` URLs and the browser's own `WebSocket` for everything
else. The bridged socket implements the WebSocket surface the renderers use
— `readyState` (+ constants), `binaryType`, `send` (string / ArrayBuffer /
view / Blob), `close`, `on*` handlers and `addEventListener`, `url`,
`protocol`, `bufferedAmount` — and hands its frames to
`collimate_playground.boot.ws_connect(...)`, which drives the app with a
`websocket` scope: `websocket.connect`, then the client's frames as
`websocket.receive` (`text` or `bytes`), and translates
`websocket.accept` / `websocket.send` / `websocket.close` into `open`,
`message` and `close` events. A socket opened before pyodide is ready stays
`CONNECTING` until the transport arrives (`window.__COLLIMATE_WS_GATE__`).

The chat surface uses the app's real `/api/chats/{chat_id}`
route; the terminal is served by the shim's own
`/api/demo/terminals/{terminal_id}` (the app's terminal route spawns a docker
PTY on attach), speaking the same wire protocol: binary frames both ways,
`{"t":"resize","cols","rows"}` control JSON, the token on the query string,
4404/4401/4409 close codes. `terminal.py` imports the protocol pieces the
app's route defines (its accept-then-close rejection, the replay ring's cap)
from `api/routes/terminal.py` and adds only the scripted session itself.
A `close(code, reason)` from the page is reported back on `onclose` with
that code and reason, as a browser does for a close the server acknowledged.

### Threadless: one event loop

pyodide has one thread. `threads.py` makes `asyncio.to_thread`,
`loop.run_in_executor` and `anyio.to_thread.run_sync` (what Starlette uses
for sync routes) run their function inline and return a finished awaitable.
Every sqlite and filesystem call the app pushes "off the loop" therefore
completes before the next `await`, in program order. Verifier calls additionally
suspend through JSPI; unrelated callbacks can run during that wait, so validation
steps use an explicit in-flight guard and shutdown drain. `modules.py` stubs, in `sys.modules`, only the
modules pyodide cannot import (`fcntl`, `pty`, `termios`, `docker`,
`keyring`, `uvicorn`): a *permissive* stub
whose every attribute is a class that can be raised, caught, subclassed,
instantiated and iterated, so a lazy import inside a route the demo never
exercises cannot crash it.

### Demo runner: `ProcessManager.start` is the seam

The app runs a generator by spawning a subprocess in `ProcessManager.start`
and then wiring the run's stdout/stderr drains, microbatch log writer,
`_wait_and_cleanup` and timeout watchdog around `self.process`.
`demo_runner.py` replaces exactly that method. Its replacement repeats the
native method's post-spawn wiring around a `FakeProcess`, and each repeated
line is also an anchor (`NATIVE_WIRING`): `install()` refuses to run unless
the native `start` still contains every one of them, and
`tests/demo/test_shim.py` checks the same against the repo's `manager.py`,
so a change to the native wiring fails CI instead of drifting. The process
is the same duck as a subprocess: `pid`, `returncode`, `stdout`/`stderr`
StreamReaders, `wait()`, `send_signal`/`terminate`/`kill` — driving an
asyncio task that runs `play(demo)` from the generator's `timeline.py`.
Everything downstream is the app's own code, unchanged:
phases, log flushing, stop and force-stop (SIGTERM/SIGKILL reach the fake
process through `os.kill`/`os.killpg`/`os.getpgid` wrappers that route fake
pids and pass real ones through), status finalization, the run-end sweep
of the run's folder, chat-session teardown, the persisted run log. A
timeline's `demo.commit()` writes the cell through the host's
`_emit_partial` — the same run-folder rules a real generator meets — and
`demo.reveal_file()` goes through the host's `reveal_file` op, so the UI
updates over SSE exactly as for a real generator.
`tests/demo/test_demo_runner.py` runs a synthetic timeline through the real
app this way (two runs on one cell included).

### Boot and seeding (`boot.py`)

1. `threads.install()` + `modules.install()` — before `import main`.
2. `import main` from the unpacked api tree; trust the page's hostname in
   the app's origin guard (a tokenless server answers only loopback names,
   and this one's name is the page's); install the demo runner; add the
   demo terminal route.
3. Run the app's own lifespan startup (bootstrap, the built-in renderers
   compiled and stored as server bundles, state restore, WAL checkpoint
   task).
4. Seed the single channel *through the app's own routes*, in-process:
   `POST /api/channels?name=…` creates the channel from the `demo`
   suggestion of the app's bundled default source — the staged
   `collimate_content/`, which the app finds on `sys.path` as it finds the
   installed wheel's, and whose `suggested/demo.json` the build wrote (the
   demo generator cell and the SDK's renderer cells) — with the runner
   drafts from `channel.yaml`, then
   `POST /api/blob/stream` per welcome file and `POST …/import` per welcome
   cell, `PATCH /api/channels/{channel}/cells/{cell_id}/meta` for the
   pinned file. Creating the channel admits every suggested package through
   the verifier and compiles every renderer, storing its server bundle, so
   the source route serves each one from the first request; each file is
   admitted under its own deadline; tabs booting at once on
   one machine can overrun it, and the app refuses the creation with
   "Verifier busy, try again". That refusal alone is retried, after pauses
   of 1, 2, 4 and 8 s (`CHANNEL_BUSY_PAUSES`); boot fails only once they are
   spent, or at once on any other refusal.
5. Wrap the app in the guard: channel create / delete / rename → 400
   `{"detail": "The demo runs a single channel"}`; every mutating
   `/api/accounts` → 501 `{"detail": "Not available in the browser
   demo"}` (`GET /api/accounts` → `[]`). The generator port proxy at
   `/proxy/*` is outside `/api/` and never reaches the runtime.

Storage is pyodide's in-memory MEMFS (`/mnt/data` is the app's CWD): a
reload is a fresh demo. Nothing is persisted, by design.

### Verifier runtime

The demo requires WebAssembly Promise Integration (JSPI). Boot checks the
capability and gives an explicit error when it is unavailable; there is no
validation bypass. The startup page and Browser demo card disclose the 64 MiB
heap and 512 KiB stack per evaluation, the tested engine and this startup
requirement. Capacity and deadline refusals say "Verifier busy, try again";
structured console records give every refusal reason and elapsed time, and
the E2E helper prints them even when boot fails. Chromium is the tested browser. Other browsers need JSPI
and remain unverified by this suite.

Accepted handler bytes run in isolated QuickJS WebAssembly workers. A page
supervisor enforces deadlines and cancellation while Pyodide suspends through
JSPI. The SDK's compiler identities, verifier inputs, normalization and host
validation paths remain canonical. Engine dependencies are pinned in
`verifier-runtime/package-lock.json` and served from the same origin. See
[verifier runtime](docs/verifier-runtime.md) for the execution boundary,
resource limits and the difference from native process limits.

### The build

`build.sh` → `build/__main__.py`:

1. **stage** — copy `api/` (minus `.venv`, `.collimate`, `tests`, `static`,
   …) and `frontend/` (minus `node_modules`, `dist`) into `.stage/`, exact
   copies that the next two steps edit.
2. **patches** — `build/patches.py`: exact-text replacements on the staged
   copies. Each names its file, its anchor and why; an anchor found zero or
   two times raises. Today: the app's one budget-exceeded verdict
   constructor (`api/verifier_pool.py`) returns the shim's observed
   "Verifier busy, try again" refusal; the settings view and the Commands
   pane's Settings group offer no Key manager; the channel menu loses New /
   Rename / Delete. `tests/demo/test_patch_anchors.py` dry-runs every patch
   against the main tree, so a moved anchor fails the demo tests too.
3. **vendor** — into the staged api, laid out as the installed `collimate`
   wheel lays out its bundled trees beside the app's modules (pyodide puts
   the api tree on `sys.path`, so the app's bundled default finds them):
   `collimate_content/generators/` ← the demo generators,
   `collimate_content/renderers/` ← the repo's real `content/renderers`,
   `collimate_content/suggested/demo.json` ← the suggestion the demo
   channel is created from (the demo generator cells and the SDK Standard
   suggestion's renderer cells), `collimate_builtins/` ← `builtins/`;
   `collimate_playground/` ← the shim, `_demo/` ← `channel.yaml` +
   `channel/`. Beside the staged frontend, `.stage/sdk/` ← the SDK's
   portable compiler (`scripts/renderer_bundle_core.mjs` with its `.d.mts`,
   `src/collimate/renderer_bundle_contract.json`), at the paths the
   frontend's renderer worker and build check import by relative path. No
   renderer bundle is prebuilt: the app compiles in the browser, through the
   shim's compiler, at startup and at admission.
4. **assemble** — install the staged frontend's deps (reusing
   `frontend/node_modules` through a symlink when present, else `npm ci`);
   `vite build` in the staged frontend; bundle the verifier runtime with the
   SDK's compiler core and contract;
   create the private SDK source wheel with `build/sdk_payload.py`;
   download the `websockets` wheel
   at the version, URL and sha256 the workspace `uv.lock` pins (cached in
   `.wheels/`); tar the staged api tree; copy `web/` into place, writing
   into `sw.js` the module mirror hosts the SDK's
   `module_mirror_contract.json` lists as upstreams; inject
   into the built `index.html` the inline gate (as the first script in
   `<head>`), a preconnect to the pyodide CDN, preload hints for
   `app.tar.gz` and the wheels, and `boot.js`; copy `landing/` and inject
   `web/landing-hero.html` after the hero-actions opening tag.

The private `collimate-playground-sdk` payload copies canonical SDK source, runtime metadata and assets directly. It excludes host native verifier and compiler resources, rejects host binaries and source symlinks, and writes a complete wheel RECORD. It does not build, retag or modify the SDK’s native wheel. The existing staged shim installs the supervised WASM compiler/verifier bridge before host application imports.

pyodide 314.0.7 (Python 3.14) comes from jsdelivr at page load; `fastapi`,
`starlette`, `pydantic`, `httpx`, `pyyaml` are pyodide-native packages,
`sqlite3` is in its standard library, and the two wheels (`websockets`, the SDK) are same-origin files installed by
URL with `deps=False` — micropip never consults a package index.

## Build, serve, test

Prerequisites: `node` + `npm` and `python3` for the build, `uv` for the
demo unit tests. The SDK source is the in-repo `sdk/` (`COLLIMATE_SDK_DIR=…`
points the build at another tree). The build compiles nothing natively: it
copies the SDK's portable compiler resources, and the demo compiles in the
browser. `tests/demo` runs the app on this machine, where admission compiles
with the SDK's native compiler: prepare the SDK and the API environment with
`bash build/prepare_sdk_checkout.sh`, as the main CI build does; this installs
the SDK's pinned compiler tools and native resources.

```bash
bash playground/build.sh          # → playground/dist/
bash playground/serve.sh          # http://127.0.0.1:4321/  (demo at /playground/)
cd playground && npm ci && npx playwright install chromium && npx playwright test   # e2e against dist/
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring .venv/bin/python -m pytest playground/tests/demo -q
bash playground/tests/check_isolation.sh         # no demo tokens outside playground/
```

The e2e suite (`tests/e2e/README.md`) boots the demo per spec and runs every
demo runner to completion from the picker, so a full run takes a couple of
minutes. `@playwright/test` is pinned exactly in `package.json`; the visual
job's container image in `.github/workflows/playground.yml` carries the
same version.

`build.sh` honours:

| variable | meaning |
| --- | --- |
| `COLLIMATE_SDK_DIR` | the SDK source tree (default: the in-repo `sdk/`) |
| `PLAYGROUND_DEMO_DIR` | the demo content dir (default: `playground/demo`) — point it at a scratch copy to develop against your own timelines |
| `PLAYGROUND_FRESH_NPM` | `1` forces `npm ci` in the staged frontend instead of reusing `frontend/node_modules` |

`serve.sh` honours `PORT` (default 4321). It runs `npx serve` with clean
URLs disabled: serve's default 301 from `sandbox.html` to `/sandbox` drops
the query string the renderer sandbox is parameterised with.

While developing, the browser console carries `[collimate]` boot messages
and `[collimate-timing]` per-step durations; `window.__COLLIMATE_PLAYGROUND__`
exposes `ready`, `pyodide`, `shim` (the boot module) and `channel`. After
boot a "Browser demo" badge (`#collimate-playground-badge`) sits in the
corner; it opens a one-paragraph card (`#collimate-playground-card`, a
dialog that Escape, its button or a backdrop click dismiss, returning focus
to the badge).

### CI (`.github/workflows/playground.yml`)

`build` (the site + `check_isolation.sh`, uploaded as an artifact),
`demo-unit` (`tests/demo` in the api environment), `e2e` (Playwright
against the artifact, Chromium cached on `package-lock.json`, report
uploaded on failure), `visual` (on the `visual` PR label or a dispatch
input; `update_visual_baselines` re-renders the baselines into an artifact)
and `deploy` (GitHub Pages, only on `main` of the public mirror).

Every run's network is the page origin, `cdn.jsdelivr.net` (pyodide and the
renderers' pinned CSS) and `esm.sh` (the renderers' pinned modules). Nothing
else is contacted; every Python package the demo installs is served from
the page origin.

## Writing a demo generator (the timeline contract)

A demo generator is an ordinary SDK generator — a `definition.generator` that
passes `collimate-lint` and the app's runner validator (`api/runners.py`),
a main config `.set_yaml`, optional configs — whose `entrypoint` is a
`timeline.py` exporting `async def play(demo)`. It lives under
`playground/demo/generators/<cell>/<generator>/`, and `channel.yaml` names a
runner draft for it so it shows in the runner picker. A generator with live
surfaces declares their file types in the manifest's `ui_files` (the chat:
`.chat .json`; the terminal: `.terminal`).

Several runs can share a cell. Each run owns one folder there,
`_runs/<label>/` (`demo.folder`), minted by the host: the run card and the
run's UI files — its pointers and the chat transcript — live
in it, locked to the run, and the host sweeps the folder into the run's Logs
when the run ends. Work files (edits, reports) go anywhere else in
the cell and stay. Nothing is pinned by a run: `demo.reveal_file(path)`
opens a file's tab for the visitor instead.

```yaml
# playground/demo/channel.yaml
name: Demo                     # the one channel's display name
cells:                         # created at boot from channel/<folder>/**
  - folder: welcome
    name: Welcome
    pinned: README.md          # optional: the file to open first
runners:                       # RunnerDraft objects, verbatim into POST /api/channels
  - runner: {id: my-demo, generator: my-demo, name: My demo, description: …,
             tags: [], prompt: '', settings: {settings.set_yaml: {…every field…}},
             accounts: {}, folders: [], permissions: {}, packages: {}}
    files: []
```

`play(demo)` runs on the app's event loop as the run. The `demo` object
(`shim/collimate_playground/demo_runner.py`, class `Demo`):

| member | meaning |
| --- | --- |
| `demo.prompt: str` | the runner's saved prompt |
| `demo.params: dict` | the runner's main-config values (`.set_yaml` field defaults ← saved `values`) |
| `demo.inputs: list[dict]` | the run's cell as `[{name, files: {path: text}}]` (text files only; run folders left out) |
| `demo.label: str`, `demo.folder: str` | the host-minted run label and the run's folder `_runs/<label>` |
| `demo.run_path(name) -> str` | the cell path of `name` inside the run folder |
| `demo.workspace: str` | the run workspace; files staged here are what the cell receives |
| `demo.cancelled: bool` | true once Stop was pressed |
| `demo.log(line)` | a stdout line in the run log |
| `demo.phase(name, message='')` | a lifecycle phase (`api/process_manager/phases.py`: `spinning_up`, `running`, `awaiting_input`, `finishing`) |
| `demo.write(path, content: str \| bytes)` | stage a work file (relative cell path; `.collrun/` is reserved; a `_runs/` path raises `PermissionError`) |
| `demo.update_ui_file(name, content: str \| bytes) -> str` | stage the UI file `<folder>/<name>` (folder-relative, subfolders allowed) and return its cell path; its suffix must be in the manifest's `ui_files` and the run card is the host's, else `ValueError` |
| `demo.read(path) -> str` | read a workspace file |
| `await demo.commit(cell_name=None)` | persist staged files as a new cell version; the UI updates live. `cell_name=None` leaves the cell's name as it is; a given name is accepted only for a cell this run created. The version is labelled with the accepted name, else the last name this run gave, else the generator id. A path the host refuses (an undeclared UI type, another run's folder) raises `PermissionError` after the rest landed |
| `await demo.reveal_file(path)` | open `path`'s tab for the visitor (a run's surface takes over its run card's tab); a path still staged is committed first |
| `await demo.sleep(seconds)` | a pause that raises `asyncio.CancelledError` when the run is stopped |
| `demo.finish(success=True, message='')` | the run's terminal verdict, applied when `play` returns (a success message is logged; a failure message is the run's failure reason) |
| `demo.chat_session(label='Chat') -> session` | register the run's chat surface (its inbox) and stage its pointer (`chat/conversation.chat`); `await demo.reveal_file(session.pointer_path)` opens it. `await session.start_turn()`, `.delta(text)`, `.thinking(text)`, `.tool(**fields)`, `.event(dict)`, `.end_turn()`, `await session.wait_user_message() -> str \| None` (polls `chat_live.pop_command` for the run; None once the session is gone). The timeline owns the transcript: `demo.update_ui_file(session.transcript, …)` (`chat/main.json`, `collimate.transcript` shapes) |
| `demo.terminal(label='Terminal', name='terminal/session.terminal') -> term` | a scripted terminal on the shim's websocket route, its `.terminal` pointer at the folder-relative `name`; `await term.write(str \| bytes)`, `term.on_input(callback(bytes))`, `term.on_resize(callback(cols, rows))`, `term.pointer_path`, `term.cols`/`term.rows` |

Rules: no network calls, no randomness that changes structure (timing
jitter is fine), realistic pacing, honour Stop (every `await demo.sleep`
is a cancellation point), and end with `demo.finish(...)`. Exceptions fail
the run with a traceback in its log — the same outcome a crashing generator
has natively. Unit-check timelines under `playground/tests/demo/` with a
fake `demo` object; run one for real with `PLAYGROUND_DEMO_DIR` pointing at
a scratch copy of `demo/`.

## The isolation rule

`playground/` is the only place the browser demo exists. The vocabulary
that identifies it — `playground`, `pyodide`, `micropip`, `emscripten`, its
guest engine `quickjs.wasm` and `verifier-worker.js`, `JSPI`, an
`isWasm`/`IS_WASM` flag, `sw-gate`, `boot.js`, … — must not appear in the
main tree. The bare word `wasm` is not one of them: the app itself ships
wasm (the browser compiler, the pdf renderer's pdfium).
`tests/check_isolation.sh` greps every tracked and untracked (not ignored)
file outside `playground/` for those tokens and fails on anything but the
four allowed integration points (the README layout row, AGENTS.md rule 6,
`.github/workflows/playground.yml`, the one `run:` line in `test.yml`) and
an explicit allowlist of unrelated legitimate uses, each an exact
file + line pattern (the emscripten-targeted wheels and markers in `uv.lock`,
emscripten glue in the module snapshot). `tests/demo/test_isolation_guard.py`
plants every token in a main file and sees the guard fail. CI runs it in the
playground workflow and in the main test workflow's cheapest job, so a
main-app change cannot reintroduce demo handling.

When a main-code change moves one of the patch anchors, `build.sh` fails
with the file and the anchor: update `build/patches.py`, never the app.
