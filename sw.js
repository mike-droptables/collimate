// Collimate playground service worker.
//
// Lives at the site origin root (/sw.js) so its default scope is '/'.
// Intercepts every same-origin /api/* fetch made by the React app and
// forwards it to the pyodide runtime on the controlling page via a
// MessageChannel. The response built by pyodide becomes the fetch response.
//
// Anything that is not an /api/* request falls through to the network
// untouched.

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('message', (event) => {
    // Boot-time nudge from the page so we claim this client without
    // requiring a reload.
    if (event.data && event.data.type === 'claim') {
        event.waitUntil(self.clients.claim());
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
        const timeout = setTimeout(() => {
            resolve(jsonResponse(504, { detail: 'Pyodide response timed out.' }));
        }, 30000);

        channel.port1.onmessage = (e) => {
            clearTimeout(timeout);
            if (e.data && e.data.error) {
                resolve(jsonResponse(500, { detail: e.data.error }));
                return;
            }
            const { status, headers: respHeaders, body } = e.data.response;
            const hdrs = new Headers();
            for (const [k, v] of respHeaders) hdrs.set(k, v);
            resolve(new Response(body, { status, headers: hdrs }));
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
