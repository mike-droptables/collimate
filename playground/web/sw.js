// Collimate browser demo — service worker.
//
// Lives at the site root (/sw.js, scope '/') and intercepts every
// same-origin /api/* fetch the React app makes, forwarding it over a
// MessageChannel to the page that hosts the pyodide runtime. The page
// answers in pieces — {type:'start', status, headers}, then any number of
// {type:'chunk', body: ArrayBuffer}, then {type:'end'} — and the SW turns
// that into a streaming Response, so server-sent events (the channel state
// stream, run events) flow exactly as they do from a real server. When the
// consumer goes away (an EventSource closed, a navigation) the ReadableStream
// is cancelled and the SW posts {type:'cancel'} back so the ASGI app sees
// http.disconnect.
//
// Bodies are binary in both directions (ArrayBuffer, transferred).
//
// Anything that is not a same-origin /api/* request falls through to the
// network untouched.

const MAX_BOOT_WAIT_MS = 5 * 60 * 1000;   // pyodide's first boot is network-bound
const HEADERS_TIMEOUT_MS = 60 * 1000;     // after ready: time allowed until 'start'

// Statuses whose Response may not carry a body (Fetch spec "null body status").
const NULL_BODY_STATUSES = new Set([101, 204, 205, 304]);

// ─── Runtime hosts ───────────────────────────────────────────────────────
//
// Every top-level demo page runs its own pyodide, so readiness is tracked
// PER CLIENT, never globally: a reloaded tab (new client id) starts unready
// and gets the boot grace even though the previous page had reported ready,
// and a request is only ever answered by a page known to host a runtime.
// A page announces itself with {type:'page-ready'} (boot.js, and again on
// every controllerchange so a freshly activated SW learns of it).
const hosts = new Map();   // clientId → { ready: Promise, resolve }

function host(clientId) {
    let h = hosts.get(clientId);
    if (!h) {
        h = { resolve: null, ready: null };
        h.ready = new Promise((resolve) => { h.resolve = resolve; });
        hosts.set(clientId, h);
    }
    return h;
}

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('message', (event) => {
    const data = event.data || {};
    if (data.type === 'claim') {
        event.waitUntil(self.clients.claim());
    } else if (data.type === 'page-ready' && event.source && event.source.id) {
        host(event.source.id).resolve();
    }
});

// The app's module mirror (`/api/mirror/<host>/<path>`) is served by the
// real server from its vendored snapshot; the demo ships no snapshot and runs
// no server, so the worker answers those requests from the upstream CDN
// itself: a redirect for module, worker, style and font loads (the browser
// follows it), a pass-through fetch for the renderer preview's own graph
// walk (it refuses redirects and reads the raw upstream body). The hosts are
// the SDK module mirror contract's upstreams, written in by the build.
const MIRROR_PREFIX = '/api/mirror/';
const MIRROR_HOSTS = new Set(__MIRROR_HOSTS__);

function mirrorUpstream(url) {
    const rest = url.pathname.slice(MIRROR_PREFIX.length);
    const slash = rest.indexOf('/');
    const host = slash < 0 ? rest : rest.slice(0, slash);
    if (!MIRROR_HOSTS.has(host) || slash < 0) return null;
    return 'https://' + host + rest.slice(slash) + url.search;
}

async function mirrorFromUpstream(request, upstream) {
    if (request.destination !== '' && request.destination !== 'empty') return Response.redirect(upstream, 302);
    const response = await fetch(upstream, { credentials: 'omit' });
    return new Response(response.body, { status: response.status, headers: response.headers });
}

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;
    if (!url.pathname.startsWith('/api/')) return;
    if (url.pathname.startsWith(MIRROR_PREFIX)) {
        const upstream = mirrorUpstream(url);
        event.respondWith(upstream ? mirrorFromUpstream(event.request, upstream)
            : new Response('not a mirrored host', { status: 404 }));
        return;
    }
    event.respondWith(routeToPage(event, url));
});

// The page that hosts pyodide for this request: the requesting client when
// it is a top-level window (it hosts its own runtime, booting or ready — a
// demo page's own first fetch is what registers it in `hosts`). Otherwise —
// a renderer sandbox iframe fetching a cell file, a worker — the Clients API
// has no link to the parent page, so pick among the top-level windows known
// to host a runtime: a visible one first (the iframe's tab is the one being
// looked at), then a focused one, then any. A page that never made an /api
// request of its own (the landing page) is never picked; with no runtime
// host at all the request gets a 503 instead of a guess.
async function pickClient(event) {
    let own = null;
    try { own = event.clientId ? await self.clients.get(event.clientId) : null; } catch (e) { own = null; }
    if (own && own.frameType === 'top-level') return own;
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const live = new Set(all.map((c) => c.id));
    for (const id of Array.from(hosts.keys())) if (!live.has(id)) hosts.delete(id);
    const pool = all.filter((c) => c.frameType === 'top-level' && hosts.has(c.id));
    return pool.find((c) => c.visibilityState === 'visible' && c.focused)
        || pool.find((c) => c.visibilityState === 'visible')
        || pool.find((c) => c.focused)
        || pool[0]
        || null;
}

async function routeToPage(event, url) {
    const request = event.request;
    const client = await pickClient(event);
    if (!client) {
        return jsonResponse(503, { detail: 'No page is hosting the demo runtime.' });
    }

    const headers = [];
    request.headers.forEach((v, k) => { headers.push([k, v]); });
    // A request on the wire always names its Host; the Headers a worker sees
    // never do. The app's origin guard compares it with the Origin.
    headers.push(['host', url.host]);
    const body = (request.method === 'GET' || request.method === 'HEAD')
        ? null
        : await request.arrayBuffer();

    const payload = {
        type: 'http',
        request: { method: request.method, path: url.pathname, query: url.search.slice(1), headers, body },
    };

    return new Promise((resolve) => {
        const channel = new MessageChannel();
        const port = channel.port1;
        let settled = false;
        let controller = null;
        let timer = null;
        let bootTimer = null;

        const finish = (response) => {
            if (settled) return;
            settled = true;
            if (timer) clearTimeout(timer);
            if (bootTimer) clearTimeout(bootTimer);
            resolve(response);
        };
        // Give up on the page: tell it to stop (the ASGI task sees
        // http.disconnect) and drop our end of the port.
        const abandon = () => {
            try { port.postMessage({ type: 'cancel' }); } catch (err) { /* port gone */ }
            controller = null;
            port.close();
        };

        // The per-request clock only starts once the host page reports
        // ready; a request queued during boot is legitimately slow.
        Promise.race([host(client.id).ready, new Promise((r) => { bootTimer = setTimeout(r, MAX_BOOT_WAIT_MS); })]).then(() => {
            clearTimeout(bootTimer);
            if (settled) return;
            timer = setTimeout(() => {
                abandon();
                finish(jsonResponse(504, { detail: 'The demo runtime did not answer in time.' }));
            }, HEADERS_TIMEOUT_MS);
        });

        port.onmessage = (e) => {
            const msg = e.data || {};
            if (msg.type === 'start') {
                const hdrs = new Headers();
                for (const [k, v] of msg.headers) hdrs.append(k, v);
                if (NULL_BODY_STATUSES.has(msg.status)) {
                    // A Response with one of these statuses cannot take a
                    // body (constructing one throws); whatever the page still
                    // sends for it is dropped at 'end'.
                    finish(new Response(null, { status: msg.status, headers: hdrs }));
                    return;
                }
                const stream = new ReadableStream({
                    start(c) { controller = c; },
                    cancel() {
                        // The consumer is gone (EventSource.close(), navigation).
                        abandon();
                    },
                });
                finish(new Response(stream, { status: msg.status, headers: hdrs }));
            } else if (msg.type === 'chunk') {
                if (controller) {
                    try { controller.enqueue(new Uint8Array(msg.body)); } catch (err) { controller = null; }
                }
            } else if (msg.type === 'end') {
                if (controller) {
                    try { controller.close(); } catch (err) { /* already closed */ }
                    controller = null;
                }
                port.close();
            } else if (msg.type === 'error') {
                if (!settled) {
                    finish(jsonResponse(500, { detail: String(msg.message || 'bridge error') }));
                } else if (controller) {
                    try { controller.error(new Error(String(msg.message || 'bridge error'))); } catch (err) { /* noop */ }
                    controller = null;
                }
                port.close();
            }
        };

        const transfer = body ? [channel.port2, body] : [channel.port2];
        client.postMessage(payload, transfer);
    });
}

function jsonResponse(status, obj) {
    return new Response(JSON.stringify(obj), {
        status,
        headers: { 'Content-Type': 'application/json' },
    });
}
