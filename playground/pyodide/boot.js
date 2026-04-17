// Collimate pyodide playground — main-thread bootstrap.
//
// Execution order matters here. The React app's axios client issues /api/*
// fetches as soon as it mounts, and those can arrive before pyodide is
// ready. To avoid lost messages and spurious 504s we:
//   1. Install the SW → page message listener BEFORE doing anything async.
//   2. Register the service worker and — if we're the first load after
//      install — wait for it to take control, then reload once so the
//      page comes back already controlled.
//   3. Boot pyodide, show progress in an overlay, and drain the queue of
//      /api/* requests that arrived while it was still loading.
//
// The stub `app.py` handles enough startup endpoints to keep the React
// app from crashing. The next slice swaps it for the real api/main.py.

const PYODIDE_VERSION = '0.28.3';
const PYODIDE_INDEX = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

const PLAYGROUND = { ready: false, pyodide: null, _asgi: null, persistence: null };
window.__COLLIMATE_PLAYGROUND__ = PLAYGROUND;

// ─── IndexedDB-backed persistence ────────────────────────────────────────
//
// Pyodide's default filesystem is MEMFS — everything is wiped on reload.
// We mount IDBFS at /mnt/collimate-data, chdir into it, and populate it
// from IndexedDB at boot so the api's `.collimate/` tree (sqlite dbs,
// CAS blobs, .genesis marker) survives refreshes. Flushes happen after
// mutating requests and on beforeunload.

function createPersistence(pyodide, root) {
    return {
        async mount() {
            pyodide.FS.mkdirTree(root);
            pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, root);
            // true = pull from IDB into MEMFS (restore on boot)
            await new Promise((resolve, reject) => {
                pyodide.FS.syncfs(true, (e) => (e ? reject(e) : resolve()));
            });
            pyodide.FS.chdir(root);
        },
        _flushQueued: false,
        _flushing: null,
        async flush() {
            // Coalesce concurrent flush requests — one in-flight at a time.
            if (this._flushing) return this._flushing;
            this._flushing = new Promise((resolve) => {
                // false = push MEMFS → IDB (persist)
                pyodide.FS.syncfs(false, (e) => {
                    if (e) console.warn('[collimate] persistence flush failed:', e);
                    this._flushing = null;
                    resolve();
                });
            });
            return this._flushing;
        },
        scheduleFlush() {
            if (this._flushQueued) return;
            this._flushQueued = true;
            // Debounce: many mutations close together → one flush.
            setTimeout(() => { this._flushQueued = false; this.flush(); }, 250);
        },
    };
}

const log = (...a) => console.log('[collimate]', ...a);
const err = (...a) => console.error('[collimate]', ...a);

// ─── Progress overlay ────────────────────────────────────────────────────

const overlay = createOverlay();

function createOverlay() {
    const install = () => {
        const el = document.createElement('div');
        el.id = 'collimate-playground-overlay';
        el.style.cssText = [
            'position:fixed', 'bottom:14px', 'right:14px',
            'padding:10px 14px',
            'background:rgba(18,18,18,0.94)',
            'color:#eee',
            'font:12px/1.45 ui-monospace,SFMono-Regular,monospace',
            'border:1px solid rgba(255,255,255,0.12)',
            'border-radius:5px',
            'z-index:99999',
            'max-width:380px',
            'box-shadow:0 4px 18px rgba(0,0,0,0.45)',
            'transition:opacity .45s',
            'pointer-events:none',
            'white-space:pre-line',
        ].join(';');
        document.body.appendChild(el);
        return el;
    };

    const state = { el: null, startedAt: performance.now(), lines: [] };

    function ensure() {
        if (state.el) return state.el;
        if (document.body) {
            state.el = install();
        } else {
            document.addEventListener('DOMContentLoaded', () => { state.el = install(); render(); }, { once: true });
        }
        return state.el;
    }

    function render() {
        if (!state.el) return;
        const elapsed = ((performance.now() - state.startedAt) / 1000).toFixed(1);
        state.el.textContent = `playground · ${elapsed}s\n` + state.lines.join('\n');
    }

    return {
        step(msg) { state.lines.push('• ' + msg); ensure(); render(); },
        detail(msg) {
            if (state.lines.length === 0) state.lines.push('• ' + msg);
            else state.lines[state.lines.length - 1] = '• ' + msg;
            ensure(); render();
        },
        done() {
            if (!state.el) return;
            state.el.style.opacity = '0';
            setTimeout(() => { state.el && state.el.remove(); state.el = null; }, 700);
        },
        error(e) {
            ensure();
            if (state.el) {
                state.el.style.color = '#f99';
                state.el.style.borderColor = 'rgba(255,90,90,0.5)';
                state.el.textContent = `playground failed\n${(e && e.stack) || e}`;
            }
        },
    };
}

// ─── SW → page message plumbing (installed immediately, before pyodide) ──

const pendingRequests = [];

async function handleSwMessage(event) {
    const data = event.data || {};
    const port = event.ports && event.ports[0];
    if (!data.request || !port) return;
    if (!PLAYGROUND.ready) {
        pendingRequests.push({ request: data.request, port });
        return;
    }
    dispatchToPyodide(data.request, port);
}

async function dispatchToPyodide(request, port) {
    try {
        const resultStr = await PLAYGROUND._asgi(JSON.stringify(request));
        port.postMessage({ response: JSON.parse(resultStr) });
        // Persist after every dispatch (debounced). Mutations aren't always
        // triggered by the initial request that prompts them — wasm
        // generators run as asyncio background tasks that write to state
        // AFTER the POST /runs response has already returned. A mutation-
        // only flush misses those writes, and the user sees a 404/empty
        // cell after reload. Flushing on GETs too is cheap (syncfs of an
        // unchanged MEMFS is a no-op) and lets subsequent polls or even
        // unrelated requests catch up trailing writes.
        if (PLAYGROUND.persistence) {
            PLAYGROUND.persistence.scheduleFlush();
            // Secondary flush a few seconds later to specifically pick up
            // in-flight wasm-generator writes that fall outside the 250ms
            // debounce window.
            if (request.method !== 'GET' && request.method !== 'HEAD') {
                setTimeout(() => PLAYGROUND.persistence && PLAYGROUND.persistence.scheduleFlush(), 3000);
            }
        }
    } catch (e) {
        err('ASGI dispatch failed:', e);
        port.postMessage({ error: String((e && e.message) || e) });
    }
}

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', handleSwMessage);
}

// ─── Boot sequence ───────────────────────────────────────────────────────

boot().catch((e) => { err('boot failed:', e); overlay.error(e); });

async function boot() {
    overlay.step('Starting playground…');

    // 1. Service worker. On a fresh install the page isn't controlled yet,
    // which means the current load's /api/* fetches bypass us. Reload once
    // the SW takes control so the next load starts correctly.
    if ('serviceWorker' in navigator) {
        overlay.step('Registering service worker…');
        const swUrl = new URL('/sw.js', window.location.origin).toString();
        const reg = await navigator.serviceWorker.register(swUrl, { scope: '/' });
        await navigator.serviceWorker.ready;

        if (!navigator.serviceWorker.controller) {
            overlay.detail('Service worker installed — reloading to activate…');
            await new Promise((resolve) => {
                navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true });
            });
            location.reload();
            return;  // never reached; reload aborts the rest
        }
        if (reg.active) reg.active.postMessage({ type: 'claim' });
    } else {
        overlay.step('Service workers not supported — /api/* will bypass pyodide');
    }

    // 2. Pyodide runtime (~10 MB; most of the time here is download).
    overlay.step('Downloading Python runtime (~10 MB, first load only)…');
    const { loadPyodide } = await import(`${PYODIDE_INDEX}pyodide.mjs`);
    const pyodide = await loadPyodide({
        indexURL: PYODIDE_INDEX,
        stdout: (s) => console.log('[py]', s),
        stderr: (s) => console.warn('[py]', s),
    });
    PLAYGROUND.pyodide = pyodide;

    // 3. Native packages pyodide ships. pydantic needs the compiled build;
    // sqlite3 is unvendored from pyodide's stdlib and must be loaded
    // explicitly (the api's database.py imports it unconditionally).
    overlay.step('Installing native packages (pydantic, sqlite3, micropip)…');
    await pyodide.loadPackage(['micropip', 'pydantic', 'sqlite3']);

    // 4. Pure-Python packages via micropip. websockets is needed by
    // routes/proxy at import time; httpx by routes/proxy + user-written
    // wasm generators that want rich HTTP clients (pyodide.http is the
    // lower-level alternative that's already built in).
    overlay.step('Installing FastAPI (+ deps)…');
    await pyodide.runPythonAsync(`
import micropip
await micropip.install(['fastapi', 'python-multipart', 'httpx', 'websockets'])
`);
    await pyodide.loadPackage(['pyyaml']);

    // 5. Install the collimate-sdk wheel we bundled next to boot.js.
    overlay.step('Installing collimate-sdk (local wheel)…');
    const sdkWheelUrl = new URL('./collimate_sdk-0.1.0-py3-none-any.whl', import.meta.url).toString();
    await pyodide.runPythonAsync(`
await micropip.install('${sdkWheelUrl}', deps=False)
`);

    // 6. Unpack the api/ source tarball into /mnt/app/.
    overlay.step('Unpacking api/ source…');
    const appTarResp = await fetch(new URL('../app.tar.gz', import.meta.url));
    if (!appTarResp.ok) throw new Error(`app.tar.gz fetch failed: ${appTarResp.status}`);
    const appTarBuf = await appTarResp.arrayBuffer();
    pyodide.FS.mkdirTree('/mnt/app');
    pyodide.unpackArchive(appTarBuf, 'tar.gz', { extractDir: '/mnt/app' });

    // 6b. Mount IndexedDB-backed storage for the api's .collimate/ tree.
    // Restoring here (before genesis runs) lets the bootstrap marker +
    // prior sqlite dbs survive a refresh. Failure is non-fatal — we fall
    // back to ephemeral MEMFS.
    //
    // If the user asked to clear IDB (flag set by the playground card),
    // do it NOW — before we mount IDBFS, so no open connection blocks
    // the deleteDatabase call. Clearing mid-boot leaves us with a fresh
    // IDB, so the subsequent mount + genesis runs as if on first boot.
    try {
        if (sessionStorage.getItem('collimate-playground-clear-idb') === '1') {
            overlay.step('Clearing IndexedDB (user-requested reset)…');
            sessionStorage.removeItem('collimate-playground-clear-idb');
            await clearAllIndexedDbDatabases();
        }
    } catch (e) {
        err('clear-idb reset failed:', e);
    }

    overlay.step('Restoring persisted state from IndexedDB…');
    try {
        const persistence = createPersistence(pyodide, '/mnt/collimate-data');
        await persistence.mount();
        PLAYGROUND.persistence = persistence;
        // Flush pending writes when the tab is closing.
        addEventListener('beforeunload', () => {
            // syncfs is async but browsers let the inflight IDB write finish.
            pyodide.FS.syncfs(false, () => {});
        });
    } catch (e) {
        err('persistence mount failed, falling back to ephemeral MEMFS:', e);
    }

    // 7. Import the real api.main. Set COLLIMATE_RUNTIME=wasm so runtime.py
    // (and every guard downstream) takes the wasm path.
    overlay.step('Starting FastAPI (api.main)…');
    await pyodide.runPythonAsync(`
import os, sys
os.environ['COLLIMATE_RUNTIME'] = 'wasm'
sys.path.insert(0, '/mnt/app')
import main as _app_module
`);

    // 8. Run the ASGI app's startup lifespan so routes are fully wired
    // (bootstrap, state load, etc.) before we start dispatching requests.
    overlay.step('Running FastAPI lifespan startup…');
    await pyodide.runPythonAsync(`
import asyncio
_lifespan_ctx = _app_module.app.router.lifespan_context(_app_module.app)
_lifespan_enter = _lifespan_ctx.__aenter__
_lifespan_exit = _lifespan_ctx.__aexit__
await _lifespan_enter()
`);

    // 8b. Persist the bootstrap .genesis marker + seeded channel DB so the
    // first refresh picks them up instead of re-running bootstrap.
    if (PLAYGROUND.persistence) {
        await PLAYGROUND.persistence.flush();
    }

    // 9. Wire up the ASGI handler.
    overlay.step('Wiring ASGI handler…');
    await pyodide.runPythonAsync(`
import asyncio, json

async def _handle_asgi(req_json):
    req = json.loads(req_json)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": req["method"],
        "scheme": "http",
        "path": req["path"],
        "raw_path": req["path"].encode("utf-8"),
        "query_string": (req.get("query") or "").encode("utf-8"),
        "root_path": "",
        "headers": [
            (k.lower().encode("utf-8"), v.encode("utf-8"))
            for k, v in req.get("headers", {}).items()
        ],
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 0),
    }
    body_bytes = (req.get("body") or "").encode("utf-8")
    _messages = [{"type": "http.request", "body": body_bytes, "more_body": False}]
    async def receive():
        return _messages.pop(0) if _messages else {"type": "http.disconnect"}

    state = {"status": 500, "headers": [], "body": b"", "done": False, "stream": False}

    def _is_stream_headers(headers):
        for k, v in headers:
            if k.decode("utf-8").lower() == "content-type" and b"event-stream" in v.lower():
                return True
        return False

    async def send(msg):
        if state["done"]:
            return
        if msg["type"] == "http.response.start":
            state["status"] = msg["status"]
            hdrs = msg.get("headers", [])
            if _is_stream_headers(hdrs):
                # Streaming responses can't round-trip through this bridge.
                # Signal to the frontend to close (204 stops EventSource retries).
                state["status"] = 204
                state["headers"] = []
                state["stream"] = True
                state["done"] = True
                return
            state["headers"] = [(k.decode("utf-8"), v.decode("utf-8")) for k, v in hdrs]
        elif msg["type"] == "http.response.body":
            state["body"] += msg.get("body", b"") or b""
            if not msg.get("more_body", False):
                state["done"] = True

    # Safety net: any response taking longer than 15s is assumed streaming.
    try:
        await asyncio.wait_for(
            _app_module.app(scope, receive, send),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        import traceback
        state["status"] = 500
        state["body"] = ("ASGI error: " + traceback.format_exc()).encode("utf-8")

    return json.dumps({
        "status": state["status"],
        "headers": state["headers"],
        "body": state["body"].decode("utf-8", "replace"),
    })

globals()["_handle_asgi"] = _handle_asgi
`);
    PLAYGROUND._asgi = pyodide.globals.get('_handle_asgi');
    PLAYGROUND.ready = true;

    // Drain anything that piled up while we were loading.
    const queued = pendingRequests.splice(0);
    for (const { request, port } of queued) dispatchToPyodide(request, port);

    const t = ((performance.now() - performance.timeOrigin) / 1000).toFixed(1);
    overlay.step(`Ready (${queued.length ? queued.length + ' queued requests drained' : 'no queued requests'}).`);
    setTimeout(() => { overlay.done(); installPlaygroundBadge(); }, 600);
    log('playground ready in', t, 's');
    window.dispatchEvent(new Event('collimate-pyodide-ready'));
}

// ─── Persistent playground badge + expandable info card ──────────────────

function installPlaygroundBadge() {
    if (document.getElementById('collimate-playground-badge')) return;

    const badge = document.createElement('button');
    badge.id = 'collimate-playground-badge';
    badge.type = 'button';
    badge.textContent = '🧪 Playground';
    badge.title = 'Click for playground notes';
    badge.style.cssText = [
        'position:fixed', 'bottom:14px', 'right:14px',
        'padding:6px 12px',
        'background:rgba(18,18,18,0.92)',
        'color:#eee',
        'font:11px/1 ui-monospace,SFMono-Regular,monospace',
        'border:1px solid rgba(255,255,255,0.12)',
        'border-radius:4px',
        'z-index:99998',
        'cursor:pointer',
        'box-shadow:0 2px 8px rgba(0,0,0,0.35)',
        'transition:background .15s',
    ].join(';');
    badge.addEventListener('mouseenter', () => { badge.style.background = 'rgba(36,36,36,0.96)'; });
    badge.addEventListener('mouseleave', () => { badge.style.background = 'rgba(18,18,18,0.92)'; });
    badge.addEventListener('click', () => showPlaygroundCard());

    document.body.appendChild(badge);
}

function showPlaygroundCard() {
    if (document.getElementById('collimate-playground-card')) return;

    const backdrop = document.createElement('div');
    backdrop.id = 'collimate-playground-card';
    backdrop.style.cssText = [
        'position:fixed', 'inset:0',
        'background:rgba(0,0,0,0.45)',
        'backdrop-filter:blur(2px)',
        'z-index:99999',
        'display:flex', 'align-items:center', 'justify-content:center',
        'font:13px/1.5 ui-monospace,SFMono-Regular,monospace',
        'color:#eee',
    ].join(';');
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });

    const card = document.createElement('div');
    card.style.cssText = [
        'max-width:460px', 'width:90%',
        'padding:22px 26px',
        'background:#121212',
        'border:1px solid rgba(255,255,255,0.14)',
        'border-radius:8px',
        'box-shadow:0 10px 40px rgba(0,0,0,0.6)',
    ].join(';');
    card.innerHTML = [
        '<div style="font-size:16px;font-weight:600;margin-bottom:6px">🧪 Collimate playground</div>',
        '<div style="color:#aaa;font-size:11px;margin-bottom:14px">',
        'An in-browser build of the app, running on pyodide. No server — ',
        'everything happens here.',
        '</div>',
        '<div style="font-weight:600;color:#9cf;margin-bottom:4px">Try this</div>',
        '<ul style="margin:0 0 14px 18px;padding:0;color:#ccc">',
        '<li>Create a channel (top-left nav)</li>',
        '<li>Open <b>Settings → Defaults</b> and pick a bundled generator</li>',
        '<li>Import / export a <code>.colgen</code> or <code>.colrend</code></li>',
        '<li>Browse the bundled generator and renderer cells</li>',
        '</ul>',
        '<div style="font-weight:600;color:#fa9;margin-bottom:4px">Generator limits</div>',
        '<div style="color:#ccc;margin-bottom:14px">',
        'Only the bundled generators run here (<code>note</code>, <code>echo</code>, ',
        '<code>gemini-chat</code>). Generators that shell out, need Docker, or ',
        'rely on a real Python subprocess won\'t work.',
        '</div>',
        '<div style="color:#888;font-size:11px;margin-bottom:14px">',
        'Your work is persisted to this browser\'s IndexedDB (same-origin only). ',
        'Use <b>Clear IndexedDB</b> to reset to a fresh install.',
        '</div>',
        '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center">',
        '<button id="__collimate_card_clear" style="padding:6px 14px;background:#2a1a1a;color:#f99;border:1px solid #6a3a3a;border-radius:4px;cursor:pointer;font:inherit">Clear IndexedDB</button>',
        '<button id="__collimate_card_close" style="padding:6px 14px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;cursor:pointer;font:inherit">Got it</button>',
        '</div>',
    ].join('');

    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    card.querySelector('#__collimate_card_close').addEventListener('click', () => backdrop.remove());
    card.querySelector('#__collimate_card_clear').addEventListener('click', () => {
        showClearIndexedDbConfirm(backdrop);
    });
}

// ─── Clear-IndexedDB confirm modal ───────────────────────────────────────

function showClearIndexedDbConfirm(parentBackdrop) {
    if (document.getElementById('collimate-clear-idb-modal')) return;
    const modal = document.createElement('div');
    modal.id = 'collimate-clear-idb-modal';
    modal.style.cssText = [
        'position:fixed', 'inset:0',
        'background:rgba(0,0,0,0.6)',
        'backdrop-filter:blur(3px)',
        'z-index:100000',
        'display:flex', 'align-items:center', 'justify-content:center',
        'font:13px/1.5 ui-monospace,SFMono-Regular,monospace',
        'color:#eee',
    ].join(';');

    const body = document.createElement('div');
    body.style.cssText = [
        'max-width:420px', 'width:90%',
        'padding:22px 26px',
        'background:#181818',
        'border:1px solid rgba(255,255,255,0.14)',
        'border-radius:8px',
        'box-shadow:0 10px 40px rgba(0,0,0,0.7)',
    ].join(';');
    body.innerHTML = [
        '<div style="font-size:15px;font-weight:600;margin-bottom:10px;color:#f99">Clear IndexedDB?</div>',
        '<div style="color:#ccc;margin-bottom:18px">',
        'This wipes every channel, cell, and setting stored for this origin, ',
        'then reloads the page. You cannot undo this.',
        '</div>',
        '<div style="display:flex;justify-content:flex-end;gap:8px">',
        '<button id="__collimate_clear_cancel" style="padding:6px 14px;background:#333;color:#eee;border:1px solid #555;border-radius:4px;cursor:pointer;font:inherit">Cancel</button>',
        '<button id="__collimate_clear_confirm" style="padding:6px 14px;background:#5a1a1a;color:#fff;border:1px solid #a33;border-radius:4px;cursor:pointer;font:inherit">Clear and reload</button>',
        '</div>',
    ].join('');

    modal.appendChild(body);
    document.body.appendChild(modal);
    body.querySelector('#__collimate_clear_cancel').addEventListener('click', () => modal.remove());
    body.querySelector('#__collimate_clear_confirm').addEventListener('click', () => {
        body.innerHTML = '<div style="color:#ccc">Clearing…</div>';
        // Don't try to delete IDB here — pyodide has an open IDBFS
        // connection to /mnt/collimate-data and deleteDatabase blocks
        // forever waiting for it to close. Instead, raise a flag and
        // reload; the next boot runs clearAllIndexedDbDatabases BEFORE
        // IDBFS.mount, when no connection is open. (Leaving the SW
        // registered also avoids the "install & reload" path doubling
        // the user-visible reloads.)
        try { sessionStorage.setItem('collimate-playground-clear-idb', '1'); } catch (e) { /* ignore */ }
        location.reload();
    });
}

async function clearAllIndexedDbDatabases() {
    // Runs at boot time before IDBFS.mount, so no open connection blocks
    // the delete. indexedDB.databases() isn't in every browser — fall
    // back to the known IDBFS database name if it's missing.
    let names = [];
    if (indexedDB.databases) {
        try {
            const dbs = await indexedDB.databases();
            names = dbs.map(d => d.name).filter(Boolean);
        } catch { /* fall through */ }
    }
    if (names.length === 0) names = ['/mnt/collimate-data'];
    await Promise.all(names.map(n => new Promise((resolve, reject) => {
        const req = indexedDB.deleteDatabase(n);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error || new Error('deleteDatabase failed'));
        // onblocked should never fire here (no open connection yet), but
        // guard against a hang just in case — resolve after a short grace.
        req.onblocked = () => setTimeout(() => resolve(), 500);
    })));
}
