// Inline gate for the browser demo. Injected by playground/build as the
// FIRST <script> in dist/playground/index.html, so it runs before the React
// bundle issues its first /api/* fetch or opens a WebSocket.
//
// Two jobs:
//   1. WebSocket bridge. A service worker cannot see WebSockets, so every
//      same-origin ws(s)://…/api/* socket the live renderers open (chat,
//      terminal) becomes a BridgedWebSocket: a WebSocket-shaped object
//      whose frames travel to the pyodide ASGI app through the page (boot.js
//      supplies the transport in window.__COLLIMATE_WS_GATE__.connect). Any
//      other URL gets the browser's own WebSocket.
//   2. Fetch/XHR queue. Until the service worker controls this page, /api/*
//      requests would hit the static host and 404 — so they are held and
//      released on controllerchange (or after a safety timeout).
(function () {
    'use strict';

    // ── 1. WebSocket bridge ─────────────────────────────────────────────
    var NativeWebSocket = window.WebSocket;
    var gate = window.__COLLIMATE_WS_GATE__ = { pending: [], connect: null };

    function isBridgedUrl(url) {
        try {
            var u = new URL(String(url), window.location.href);
            var sameHost = u.host === window.location.host;
            var wsScheme = u.protocol === 'ws:' || u.protocol === 'wss:';
            return sameHost && wsScheme && u.pathname.indexOf('/api/') === 0;
        } catch (e) {
            return false;
        }
    }

    function BridgedWebSocket(url, protocols) {
        var self = this;
        this._target = new EventTarget();
        this.url = new URL(String(url), window.location.href).toString();
        this.protocol = '';
        this.extensions = '';
        this.readyState = BridgedWebSocket.CONNECTING;
        this.binaryType = 'blob';
        this.bufferedAmount = 0;
        this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
        this._protocols = protocols == null ? [] : (Array.isArray(protocols) ? protocols : [protocols]);
        this._handle = null;
        this._closeRequested = null;
        this._pendingSends = [];
        // The transport arrives when pyodide is ready; until then the socket
        // stays CONNECTING, exactly like a slow handshake.
        if (gate.connect) gate.connect(self); else gate.pending.push(self);
    }
    BridgedWebSocket.CONNECTING = 0; BridgedWebSocket.OPEN = 1;
    BridgedWebSocket.CLOSING = 2; BridgedWebSocket.CLOSED = 3;
    var P = BridgedWebSocket.prototype;
    P.CONNECTING = 0; P.OPEN = 1; P.CLOSING = 2; P.CLOSED = 3;
    P.addEventListener = function (t, l, o) { this._target.addEventListener(t, l, o); };
    P.removeEventListener = function (t, l, o) { this._target.removeEventListener(t, l, o); };
    P.dispatchEvent = function (ev) {
        var handler = this['on' + ev.type];
        if (typeof handler === 'function') {
            try { handler.call(this, ev); } catch (e) { console.error(e); }
        }
        return this._target.dispatchEvent(ev);
    };
    P.send = function (data) {
        if (this.readyState === BridgedWebSocket.CONNECTING) {
            throw new DOMException('Still in CONNECTING state.', 'InvalidStateError');
        }
        if (this.readyState !== BridgedWebSocket.OPEN) return;
        var self = this;
        if (data instanceof Blob) {
            data.arrayBuffer().then(function (buf) { self._handle && self._handle.send(new Uint8Array(buf)); });
        } else if (data instanceof ArrayBuffer) {
            this._handle.send(new Uint8Array(data.slice(0)));
        } else if (ArrayBuffer.isView(data)) {
            this._handle.send(new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)));
        } else {
            this._handle.send(String(data));
        }
    };
    P.close = function (code, reason) {
        if (this.readyState === BridgedWebSocket.CLOSING || this.readyState === BridgedWebSocket.CLOSED) return;
        this.readyState = BridgedWebSocket.CLOSING;
        if (this._handle) {
            this._handle.close(code == null ? 1000 : code, reason || '');
        } else {
            // Never reached the transport (pyodide still booting): close now.
            this._closeRequested = { code: code == null ? 1000 : code, reason: reason || '' };
            var idx = gate.pending.indexOf(this);
            if (idx >= 0) gate.pending.splice(idx, 1);
            this._onClose(this._closeRequested.code, this._closeRequested.reason, true);
        }
    };
    // ── transport callbacks (boot.js / the pyodide bridge call these) ──
    P._attach = function (handle) { this._handle = handle; };
    P._accept = function (protocol) {
        if (this.readyState !== BridgedWebSocket.CONNECTING) return;
        this.protocol = protocol || '';
        this.readyState = BridgedWebSocket.OPEN;
        this.dispatchEvent(new Event('open'));
    };
    P._message = function (data) {
        if (this.readyState !== BridgedWebSocket.OPEN) return;
        var payload = data;
        if (data instanceof Uint8Array) {
            var buf = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
            payload = this.binaryType === 'arraybuffer' ? buf : new Blob([buf]);
        }
        this.dispatchEvent(new MessageEvent('message', { data: payload, origin: new URL(this.url).origin }));
    };
    P._close = function (code, reason) { this._onClose(code, reason, code === 1000 || code === 1005 || (code >= 4000 && code < 5000)); };
    P._onClose = function (code, reason, wasClean) {
        if (this.readyState === BridgedWebSocket.CLOSED) return;
        var wasConnecting = this.readyState === BridgedWebSocket.CONNECTING;
        this.readyState = BridgedWebSocket.CLOSED;
        this._handle = null;
        if (wasConnecting) this.dispatchEvent(new Event('error'));
        this.dispatchEvent(new CloseEvent('close', { code: code, reason: reason || '', wasClean: !!wasClean }));
    };

    function WebSocket(url, protocols) {
        if (!(this instanceof WebSocket)) throw new TypeError("Failed to construct 'WebSocket': Please use the 'new' operator.");
        if (isBridgedUrl(url)) return new BridgedWebSocket(url, protocols);
        return protocols === undefined ? new NativeWebSocket(url) : new NativeWebSocket(url, protocols);
    }
    WebSocket.CONNECTING = 0; WebSocket.OPEN = 1; WebSocket.CLOSING = 2; WebSocket.CLOSED = 3;
    WebSocket.prototype = NativeWebSocket.prototype;
    WebSocket.__native = NativeWebSocket;
    WebSocket.__bridged = BridgedWebSocket;
    window.WebSocket = WebSocket;

    // ── 2. Fetch / XHR queue until the service worker controls the page ──
    if (!('serviceWorker' in navigator)) return;
    if (navigator.serviceWorker.controller) return;

    var origFetch = window.fetch.bind(window);
    var released = false;
    var pending = [];

    function isApiRequest(input) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        try {
            var u = new URL(String(url), window.location.href);
            return u.host === window.location.host && u.pathname.indexOf('/api/') === 0;
        } catch (e) {
            return false;
        }
    }

    function release() {
        if (released) return;
        released = true;
        window.fetch = origFetch;
        var q = pending;
        pending = [];
        for (var i = 0; i < q.length; i++) {
            try { q[i](); } catch (e) { /* each promise handles its own rejection */ }
        }
    }

    window.fetch = function (input, init) {
        if (released || !isApiRequest(input)) return origFetch(input, init);
        return new Promise(function (resolve, reject) {
            pending.push(function () { origFetch(input, init).then(resolve, reject); });
        });
    };

    var XHRProto = XMLHttpRequest.prototype;
    var origOpen = XHRProto.open;
    var origSend = XHRProto.send;
    XHRProto.open = function (method, url) {
        this.__gatedUrl = url;
        return origOpen.apply(this, arguments);
    };
    XHRProto.send = function () {
        var self = this, args = arguments;
        if (released || !isApiRequest(self.__gatedUrl)) return origSend.apply(self, args);
        pending.push(function () { try { origSend.apply(self, args); } catch (e) { /* swallow */ } });
    };

    // boot.js asks an already-active worker to claim this page when the
    // registration alone does not make it the controller (a hard reload);
    // here it is enough to wait for the resulting controllerchange.
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).then(function () {
        if (navigator.serviceWorker.controller) {
            release();
        } else {
            navigator.serviceWorker.addEventListener('controllerchange', release, { once: true });
        }
    }).catch(function (err) {
        console.error('[collimate] service worker registration failed:', err);
        release();
    });

    // A service worker that never claims the page must not hang the UI.
    setTimeout(release, 10000);
})();
