// Collimate playground service worker.
//
// Lives at the site origin root (/sw.js) so its default scope is '/'.
// Intercepts every same-origin /api/* fetch made by the React app and
// forwards it to the pyodide runtime on the controlling page via a
// MessageChannel. The response built by pyodide becomes the fetch response.
//
// Anything that is not an /api/* request falls through to the network
// untouched.
//
// Ready handshake. Pyodide first-boot is dominated by network I/O
// (~10 MB runtime + a handful of micropip wheels + the SDK wheel + the
// app tarball) and easily exceeds any sane single per-request timeout.
// To avoid synthetic 504s during boot, the SW tracks a `pageReady`
// promise: the page sends `{type: 'page-booting'}` as soon as the SW
// starts controlling, and `{type: 'page-ready'}` once pyodide finishes
// initialising. Per-request timers don't start until ready, gated by
// MAX_BOOT_WAIT_MS as a backstop so a stuck/missing page doesn't hang
// fetches forever.

const MAX_BOOT_WAIT_MS = 5 * 60 * 1000;
const POST_READY_TIMEOUT_MS = 60 * 1000;

let _readyResolve = null;
let readyPromise = new Promise((resolve) => { _readyResolve = resolve; });

function resetReady() {
    if (_readyResolve === null) {
        readyPromise = new Promise((resolve) => { _readyResolve = resolve; });
    }
}

function markReady() {
    if (_readyResolve) {
        _readyResolve();
        _readyResolve = null;
    }
}

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    // Fresh SW activation = no live page yet, regardless of any prior
    // ready state. Force a new readyPromise so the next page must
    // re-signal.
    resetReady();
    event.waitUntil(self.clients.claim());
});

self.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'claim') {
        // Boot-time nudge from the page so we claim this client without
        // requiring a reload.
        event.waitUntil(self.clients.claim());
    } else if (data.type === 'page-booting') {
        resetReady();
    } else if (data.type === 'page-ready') {
        markReady();
    }
});

// Paths that server real SSE streams in native mode. Pyodide can't drive a
// streaming ASGI response through this bridge, so we short-circuit these
// with 204 — the standard "don't retry" signal EventSource honors.
const STREAMING_PATTERNS = [
    /\/state\/stream$/,
    /\/runs\/[^/]+\/events$/,
];

function isStreamingPath(pathname) {
    return STREAMING_PATTERNS.some((p) => p.test(pathname));
}

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;
    if (!url.pathname.startsWith('/api/')) return;
    if (isStreamingPath(url.pathname)) {
        event.respondWith(new Response(null, { status: 204 }));
        return;
    }
    event.respondWith(routeToPyodide(event.request, url));
});

async function routeToPyodide(request, url) {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const client = clients.find((c) => c.frameType === 'top-level') || clients[0];
    if (!client) {
        return jsonResponse(503, { detail: 'No controlling client — pyodide runtime not available.' });
    }

    const headers = {};
    request.headers.forEach((v, k) => { headers[k] = v; });
    const body = request.method === 'GET' || request.method === 'HEAD'
        ? ''
        : await request.text();

    const payload = {
        method: request.method,
        path: url.pathname,
        query: url.search.slice(1),
        headers,
        body,
    };

    return new Promise((resolve) => {
        const channel = new MessageChannel();
        let resolved = false;
        let timeout;

        const finish = (response) => {
            if (resolved) return;
            resolved = true;
            clearTimeout(timeout);
            resolve(response);
        };

        // Don't start the per-request timer until the page has signalled
        // ready (or MAX_BOOT_WAIT_MS has elapsed as a backstop). During
        // boot the page-side queue holds the request anyway, so a tight
        // timer here just produces synthetic 504s.
        Promise.race([
            readyPromise,
            new Promise((r) => setTimeout(r, MAX_BOOT_WAIT_MS)),
        ]).then(() => {
            if (resolved) return;
            timeout = setTimeout(() => {
                finish(jsonResponse(504, { detail: 'Pyodide response timed out.' }));
            }, POST_READY_TIMEOUT_MS);
        });

        channel.port1.onmessage = (e) => {
            if (e.data && e.data.error) {
                finish(jsonResponse(500, { detail: e.data.error }));
                return;
            }
            const { status, headers: respHeaders, body } = e.data.response;
            const hdrs = new Headers();
            for (const [k, v] of respHeaders) hdrs.set(k, v);
            finish(new Response(body, { status, headers: hdrs }));
        };

        client.postMessage({ request: payload }, [channel.port2]);
    });
}

function jsonResponse(status, obj) {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}
