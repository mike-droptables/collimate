// Collimate browser demo — page-side bootstrap.
//
// Runs as a module script in dist/playground/index.html, after the inline
// gate (sw-gate.js) and alongside the React bundle. It:
//   1. installs the service-worker → page request handler BEFORE anything
//      async, so no /api/* request is ever lost (they queue until ready);
//   2. loads pyodide, the packages the app needs, the SDK wheel and the app
//      tarball, then runs collimate_playground.boot (lifespan + demo seed);
//   3. wires the two bridges — streaming HTTP for the service worker and the
//      WebSocket transport for the inline gate — and drains both queues;
//   4. shows a progress overlay while booting and a small "Browser demo"
//      badge afterwards.
//
// The filesystem is pyodide's in-memory MEMFS: a reload is a fresh demo.

import {createVerifierSupervisor, VERIFIER_LIMITS} from './verifier-supervisor.js';

const PYODIDE_VERSION = '314.0.7';
const PYODIDE_INDEX = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
// Replaced by playground/build/assemble.py: the SDK wheel it built and the
// pure-Python wheel it vendored beside it (pyodide's distribution has no
// websockets), so no package index is consulted at boot and the only hosts
// contacted are this origin and the pyodide CDN.
const SDK_WHEEL = '__SDK_WHEEL__';
const VENDORED_WHEELS = __VENDORED_WHEELS__;
const APP_ROOT = '/mnt/app';
const DATA_DIR = '/mnt/data';

const DEMO = { ready: false, pyodide: null, shim: null, channel: null };
let verifierSupervisor;
window.__COLLIMATE_PLAYGROUND__ = DEMO;

const log = (...a) => console.log('[collimate]', ...a);
const err = (...a) => console.error('[collimate]', ...a);

// ─── Service worker → page: streaming HTTP bridge ────────────────────────

const pendingHttp = [];

function onSwMessage(event) {
    const data = event.data || {};
    const port = event.ports && event.ports[0];
    if (data.type !== 'http' || !data.request || !port) return;
    if (!DEMO.ready) {
        pendingHttp.push({ request: data.request, port });
        return;
    }
    dispatchHttp(data.request, port);
}

function dispatchHttp(request, port) {
    // The sink the Python bridge writes into; each call becomes one message
    // on the request's port. Chunks are transferred, not copied.
    const sink = {
        start(status, headers) { port.postMessage({ type: 'start', status, headers: headers.toJs ? headers.toJs() : headers }); },
        chunk(bytes) {
            const buf = toArrayBuffer(bytes);
            port.postMessage({ type: 'chunk', body: buf }, [buf]);
        },
        end() { port.postMessage({ type: 'end' }); port.close(); release(); },
        error(message) { port.postMessage({ type: 'error', message: String(message) }); port.close(); release(); },
    };
    let handle;
    // The Python handle is a PyProxy; free it once the response is over.
    // Posting on a port the other side already closed is a silent no-op, so
    // a late end() after a cancel costs nothing.
    const release = () => { if (handle && handle.destroy) { try { handle.destroy(); } catch (e) { /* freed */ } handle = null; } };
    try {
        handle = DEMO.shim.http(
            request.method, request.path, request.query || '',
            JSON.stringify(request.headers || []),
            request.body ? new Uint8Array(request.body) : undefined,
            sink,
        );
    } catch (e) {
        err('dispatch failed:', e);
        sink.error(e && e.message ? e.message : String(e));
        return;
    }
    port.onmessage = (e) => {
        if (e.data && e.data.type === 'cancel') {
            // The consumer is gone: the ASGI task gets http.disconnect (its
            // end() still runs and would close the port; closing here too
            // covers a task cancelled before it ever started).
            if (handle) { try { handle.cancel(); } catch (ex) { /* already done */ } }
            release();
            port.close();
        }
    };
}

// Python bytes arrive as a Uint8Array (pyodide.ffi.to_js) or, defensively,
// as a PyProxy exposing a buffer. Either way: a fresh, transferable buffer.
function toArrayBuffer(bytes) {
    if (bytes instanceof Uint8Array) {
        return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    }
    if (bytes && typeof bytes.getBuffer === 'function') {
        const view = bytes.getBuffer();
        try { return view.data.slice().buffer; } finally { view.release(); bytes.destroy(); }
    }
    return new Uint8Array(bytes).buffer;
}

function announceReady() {
    const controller = 'serviceWorker' in navigator ? navigator.serviceWorker.controller : null;
    if (DEMO.ready && controller) controller.postMessage({ type: 'page-ready' });
}

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', onSwMessage);
    // A worker activated after this page booted (a deploy) starts with no
    // record of it; tell the new controller this page hosts a ready runtime.
    navigator.serviceWorker.addEventListener('controllerchange', announceReady);
}

// ─── WebSocket transport for the inline gate's BridgedWebSocket ──────────

function connectSocket(sock) {
    // The gate's socket object already implements _accept/_message/_close;
    // hand it (and the URL) to the Python bridge and give it back a handle.
    const bridged = {
        _accept: (protocol) => sock._accept(protocol),
        _message: (data) => sock._message(data instanceof Uint8Array ? data : (typeof data === 'string' ? data : new Uint8Array(toArrayBuffer(data)))),
        _close: (code, reason) => {
            sock._close(code, reason);
            if (handle && handle.destroy) { try { handle.destroy(); } catch (e) { /* freed */ } handle = null; }
        },
    };
    let handle;
    try {
        handle = DEMO.shim.ws_connect(sock.url, JSON.stringify(sock._protocols || []), bridged);
    } catch (e) {
        err('websocket bridge failed:', e);
        sock._close(1006, 'bridge error');
        return;
    }
    sock._attach({
        send: (data) => { if (handle) handle.send(data); },
        close: (code, reason) => { if (handle) handle.close(code, reason); },
    });
}

function installSocketTransport() {
    const gate = window.__COLLIMATE_WS_GATE__;
    if (!gate) {
        err('inline gate missing — WebSocket bridge unavailable');
        return;
    }
    gate.connect = connectSocket;
    const queued = gate.pending.splice(0);
    for (const sock of queued) connectSocket(sock);
    return queued.length;
}

// ─── Progress overlay ────────────────────────────────────────────────────

const overlay = createOverlay();

function createOverlay() {
    const ESTIMATED_STEPS = 10;
    const install = () => {
        const wrap = document.createElement('div');
        wrap.id = 'collimate-playground-overlay';
        wrap.style.cssText = [
            'position:fixed', 'inset:0', 'display:flex', 'align-items:center', 'justify-content:center',
            'background:radial-gradient(ellipse at center, rgba(15,15,18,0.92), rgba(8,8,10,0.98))',
            'z-index:99999', 'transition:opacity .45s ease',
            'font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', 'color:#e8e8ec',
        ].join(';');
        const card = document.createElement('div');
        card.style.cssText = [
            'min-width:360px', 'max-width:520px', 'width:90%', 'padding:32px 36px 28px',
            'background:rgba(24,24,28,0.95)', 'border:1px solid rgba(255,255,255,0.08)', 'border-radius:12px',
            'box-shadow:0 24px 60px rgba(0,0,0,0.55)',
        ].join(';');
        const title = document.createElement('div');
        title.textContent = 'Starting the browser demo';
        title.style.cssText = 'font-size:18px;font-weight:600;letter-spacing:-0.01em;margin-bottom:6px;';
        const subtitle = document.createElement('div');
        subtitle.textContent = 'The app runs in your browser on a Python runtime (~15 MB, cached after the first visit). Nothing is uploaded and nothing is saved.';
        subtitle.style.cssText = 'font-size:13px;color:#9a9aa3;margin-bottom:22px;line-height:1.45;';
        const barWrap = document.createElement('div');
        barWrap.style.cssText = 'height:6px;border-radius:999px;background:rgba(255,255,255,0.06);overflow:hidden;margin-bottom:14px;';
        const bar = document.createElement('div');
        bar.style.cssText = 'height:100%;width:0%;background:linear-gradient(90deg,#5a8bff,#8b5dff);border-radius:999px;transition:width .35s ease;';
        barWrap.appendChild(bar);
        const stepRow = document.createElement('div');
        stepRow.style.cssText = 'display:flex;justify-content:space-between;align-items:baseline;font-size:12px;';
        const stepText = document.createElement('div');
        stepText.style.cssText = 'color:#cfcfd6;font-family:ui-monospace,SFMono-Regular,monospace;';
        const elapsedText = document.createElement('div');
        elapsedText.style.cssText = 'color:#6e6e78;font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,monospace;';
        stepRow.append(stepText, elapsedText);
        const limits = document.createElement('p');
        limits.textContent = VERIFIER_LIMITS;
        limits.style.cssText = 'font-size:12px;color:#9a9aa3;margin:0 0 18px;line-height:1.45;';
        limits.id = 'collimate-verifier-limits';
        card.append(title, subtitle, limits, barWrap, stepRow);
        wrap.appendChild(card);
        document.body.appendChild(wrap);
        return { wrap, title, subtitle, bar, stepText, elapsedText };
    };
    const state = { nodes: null, startedAt: performance.now(), currentStep: '', completed: 0, timer: null, lastName: null, lastStart: 0 };
    function ensure() {
        if (state.nodes) return;
        if (document.body) { state.nodes = install(); tick(); }
        else document.addEventListener('DOMContentLoaded', () => { state.nodes = install(); tick(); render(); }, { once: true });
    }
    function tick() { if (!state.timer) state.timer = setInterval(renderElapsed, 1000); }
    function renderElapsed() {
        if (state.nodes) state.nodes.elapsedText.textContent = `${((performance.now() - state.startedAt) / 1000).toFixed(0)}s`;
    }
    function render() {
        if (!state.nodes) return;
        state.nodes.stepText.textContent = state.currentStep || '…';
        state.nodes.bar.style.width = (Math.min(0.95, state.completed / ESTIMATED_STEPS) * 100).toFixed(1) + '%';
        renderElapsed();
    }
    function logPrev() {
        if (state.lastName !== null) console.log(`[collimate-timing] '${state.lastName}': ${(performance.now() - state.lastStart).toFixed(0)}ms`);
    }
    return {
        step(msg) { logPrev(); state.lastName = msg; state.lastStart = performance.now(); state.currentStep = msg; state.completed += 1; ensure(); render(); },
        done() {
            logPrev(); state.lastName = null;
            console.log(`[collimate-timing] total boot: ${(performance.now() - state.startedAt).toFixed(0)}ms`);
            if (state.timer) { clearInterval(state.timer); state.timer = null; }
            if (!state.nodes) return;
            state.nodes.bar.style.width = '100%';
            state.nodes.stepText.textContent = 'Ready';
            setTimeout(() => {
                if (!state.nodes) return;
                state.nodes.wrap.style.opacity = '0';
                setTimeout(() => { state.nodes && state.nodes.wrap.remove(); state.nodes = null; }, 500);
            }, 200);
        },
        error(e) {
            ensure();
            if (state.timer) { clearInterval(state.timer); state.timer = null; }
            if (!state.nodes) return;
            state.nodes.title.textContent = 'The demo failed to start';
            state.nodes.title.style.color = '#ff8585';
            state.nodes.subtitle.textContent = String((e && e.message) || e);
            state.nodes.subtitle.style.color = '#cfcfd6';
            state.nodes.bar.style.background = '#ff5a5a';
        },
    };
}

// ─── Boot ────────────────────────────────────────────────────────────────

boot().catch((e) => { verifierSupervisor?.close(); err('boot failed:', e); overlay.error(e); });

async function boot() {
    overlay.step('Starting…');

    if ('serviceWorker' in navigator) {
        overlay.step('Waiting for the service worker…');
        const reg = await navigator.serviceWorker.register(new URL('/sw.js', window.location.origin).toString(), { scope: '/' });
        await navigator.serviceWorker.ready;
        if (!navigator.serviceWorker.controller) {
            // An active worker does not control a page loaded past it (a hard
            // reload bypasses the SW): ask it to claim us, THEN wait for the
            // controllerchange that claim produces.
            const claimed = new Promise((resolve) => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
            if (reg.active) reg.active.postMessage({ type: 'claim' });
            await claimed;
        }
    } else {
        overlay.step('Service workers are unavailable — the demo cannot route requests');
    }

    // The index's preload hints already started the wheel downloads; the
    // tarball is fetched here so its bytes stream while the runtime downloads.
    const wheelUrl = new URL(`./${SDK_WHEEL}`, import.meta.url).toString();
    const tarUrl = new URL('../app.tar.gz', import.meta.url).toString();
    const tarPromise = fetch(tarUrl).then((r) => r.ok ? r.arrayBuffer() : Promise.reject(new Error(`app.tar.gz: HTTP ${r.status}`)));

    overlay.step('Preparing file verification…');
    verifierSupervisor = await createVerifierSupervisor();
    globalThis.collimateVerifierInvoke = verifierSupervisor.invoke;
    window.addEventListener('pagehide', () => verifierSupervisor.close(), {once: true});

    overlay.step('Downloading the Python runtime…');
    const { loadPyodide } = await import(/* @vite-ignore */ `${PYODIDE_INDEX}pyodide.mjs`);
    const pyodide = await loadPyodide({
        indexURL: PYODIDE_INDEX,
        stdout: (s) => console.log('[py]', s),
        stderr: (s) => console.warn('[py]', s),
    });
    DEMO.pyodide = pyodide;

    // Everything pyodide ships natively comes in one batched load (sqlite3
    // is part of its standard library); the vendored wheel (websockets) and
    // the SDK wheel are same-origin files installed by URL with deps=False,
    // so micropip never resolves anything against a package index.
    overlay.step('Installing FastAPI and pydantic…');
    await pyodide.loadPackage(['micropip', 'pydantic', 'pyyaml', 'fastapi', 'starlette', 'httpx', 'anyio', 'typing-extensions']);

    overlay.step('Installing websockets and the Collimate SDK…');
    const wheelUrls = [...VENDORED_WHEELS.map((name) => new URL(`./${name}`, import.meta.url).toString()), wheelUrl];
    await pyodide.runPythonAsync(`
import micropip
await micropip.install(${JSON.stringify(wheelUrls)}, deps=False)
`);

    overlay.step('Unpacking the app…');
    const tarBuf = await tarPromise;
    pyodide.FS.mkdirTree(APP_ROOT);
    pyodide.unpackArchive(tarBuf, 'tar.gz', { extractDir: APP_ROOT });

    overlay.step('Starting the app…');
    pyodide.runPython(`import sys; sys.path.insert(0, ${JSON.stringify(APP_ROOT)})`);
    const shim = pyodide.pyimport('collimate_playground.boot');
    DEMO.shim = shim;
    const info = await shim.boot(APP_ROOT, DATA_DIR, `${APP_ROOT}/_demo`, (line) => log(line), window.location.hostname);
    const bootInfo = info.toJs ? info.toJs({ dict_converter: Object.fromEntries }) : info;
    if (info.destroy) info.destroy();
    DEMO.channel = bootInfo.channel || null;

    overlay.step('Connecting…');
    DEMO.ready = true;
    announceReady();
    const queuedHttp = pendingHttp.splice(0);
    for (const { request, port } of queuedHttp) dispatchHttp(request, port);
    const queuedSockets = installSocketTransport() || 0;
    log(`ready — drained ${queuedHttp.length} request(s), ${queuedSockets} socket(s)`);

    setTimeout(() => { overlay.done(); installBadge(); }, 400);
}

// ─── "Browser demo" badge + card ─────────────────────────────────────────

function installBadge() {
    if (document.getElementById('collimate-playground-badge')) return;
    const badge = document.createElement('button');
    badge.id = 'collimate-playground-badge';
    badge.type = 'button';
    badge.textContent = 'Browser demo';
    badge.title = 'About this demo';
    badge.style.cssText = [
        'position:fixed', 'bottom:14px', 'right:14px', 'padding:5px 10px',
        'background:rgba(18,18,18,0.88)', 'color:#cfcfd6',
        'font:11px/1 ui-monospace,SFMono-Regular,monospace', 'letter-spacing:0.04em',
        'border:1px solid rgba(255,255,255,0.12)', 'border-radius:4px', 'z-index:99998', 'cursor:pointer',
        'box-shadow:0 2px 8px rgba(0,0,0,0.35)', 'opacity:0.8', 'transition:opacity .15s',
    ].join(';');
    badge.addEventListener('mouseenter', () => { badge.style.opacity = '1'; });
    badge.addEventListener('mouseleave', () => { badge.style.opacity = '0.8'; });
    badge.addEventListener('click', showCard);
    document.body.appendChild(badge);
}

function showCard() {
    if (document.getElementById('collimate-playground-card')) return;
    const backdrop = document.createElement('div');
    backdrop.id = 'collimate-playground-card';
    backdrop.style.cssText = [
        'position:fixed', 'inset:0', 'background:rgba(0,0,0,0.45)', 'z-index:99999',
        'display:flex', 'align-items:center', 'justify-content:center',
        'font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif', 'color:#e8e8ec',
    ].join(';');
    const card = document.createElement('div');
    card.style.cssText = [
        'max-width:440px', 'width:90%', 'padding:22px 26px', 'background:#141418',
        'border:1px solid rgba(255,255,255,0.14)', 'border-radius:8px', 'box-shadow:0 10px 40px rgba(0,0,0,0.6)',
    ].join(';');
    const title = document.createElement('div');
    title.textContent = 'This is a browser demo';
    title.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:8px;';
    const text = document.createElement('p');
    text.style.cssText = 'margin:0 0 16px;color:#b9b9c2;';
    text.textContent = 'The real Collimate app is running inside this tab on a Python runtime — no server, nothing leaves your browser. '
        + 'The three generators are scripted stand-ins that play back a shell session, a chat and a file-editing agent so you can see how runs, live cells and versions behave; '
        + 'they do no real work and call no APIs. There is one channel, and a reload starts over: nothing is saved. '
        + VERIFIER_LIMITS;
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = 'Got it';
    close.style.cssText = 'padding:6px 14px;background:#2a2a30;color:#eee;border:1px solid #555;border-radius:4px;cursor:pointer;font:inherit;float:right;';
    // Keyboard: Escape dismisses, focus lands on the button and returns to
    // the badge afterwards — the card is a dialog, never a focus trap.
    const dismiss = () => {
        document.removeEventListener('keydown', onKey, true);
        backdrop.remove();
        const badge = document.getElementById('collimate-playground-badge');
        if (badge) badge.focus();
    };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); dismiss(); } };
    document.addEventListener('keydown', onKey, true);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    close.addEventListener('click', dismiss);
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-label', 'About this demo');
    card.append(title, text, close);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    close.focus();
}
