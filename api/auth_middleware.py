"""Who may drive this server: an opt-in bearer token, the Origin/Host guard,
and the cell-file read capability.

No token configured (the default) keeps the open loopback server —
local-first, zero setup — but open only to this machine's non-browser
clients and this server's own pages: ``OriginGuardMiddleware`` (below)
refuses foreign browser Origins and non-loopback Host names. With a token
set (``--token`` / ``COLLIMATE_TOKEN``), every request must present it as
``Authorization: Bearer <token>``; the operator brings transport security (a
TLS reverse proxy in front of this still-loopback server) and the desktop
shell injects the header at the network layer on every request to the server
origin (pages, fetch, SSE, WebSockets alike), so the served frontend never
knows auth exists.

Design points:

- Pure ASGI (not ``@app.middleware("http")``) so WebSocket scopes are
  covered too — terminals, chat, and the web-session control socket all ride
  the same origin.
- ``GET /health`` stays public: it's the reachability probe and reveals only
  status/version/bridge.
- Static assets are protected like everything else — the UI itself is not
  public. Browser access to a remote tokened server is out of scope; remote
  means the desktop shell.
- Refusals are a bare 401 (no detail) after a constant-time compare, and a
  pre-accept ASGI WebSocket rejection with code 4401. Browsers expose this
  rejected upgrade as 1006, not as an accepted-socket application close.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import tempfile

# GET-only public paths. /health is the desktop shell's pre-auth handshake
# (it reads version + bridge to decide whether it can talk to this server
# at all) and the natural reverse-proxy/uptime probe.
_EXEMPT_GET_PATHS = frozenset({"/health"})


class TokenAuth:
    """Holds the configured token. A process-wide singleton: `main` calls
    :meth:`configure` at import (from ``COLLIMATE_TOKEN``) and ``cli_main``
    again when ``--token`` is passed."""

    def __init__(self) -> None:
        self._token: bytes | None = None

    def configure(self, token: str | None) -> None:
        self._token = token.encode() if token else None

    @property
    def enabled(self) -> bool:
        return self._token is not None

    def matches(self, presented: str | None) -> bool:
        if self._token is None or presented is None:
            return False
        try:
            # Header values arrive as latin-1-decoded ASGI bytes; latin-1
            # recovers the bytes the client sent, which the token holds as UTF-8.
            presented_bytes = presented.encode("latin-1")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(presented_bytes, self._token)


auth = TokenAuth()


def _presented_token(scope: dict) -> str | None:
    """The bearer token the request's ``Authorization`` header presents."""
    for name, value in scope.get("headers", ()):  # bytes pairs, lowercase names
        if name == b"authorization":
            scheme, _, credential = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and credential:
                return credential.strip()
            return None
    return None


class BearerAuthMiddleware:
    """ASGI wrapper enforcing the configured token on http + websocket."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket") or not auth.enabled:
            await self.app(scope, receive, send)
            return
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path") in _EXEMPT_GET_PATHS
        ):
            await self.app(scope, receive, send)
            return
        if auth.matches(_presented_token(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # ASGI rejection before accept: browsers observe 1006, not this
            # application close code. Accepted-socket closes can expose 4401.
            await send({"type": "websocket.close", "code": 4401})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------------------
# Tokenless loopback: only this server's own pages may drive it.
# ---------------------------------------------------------------------------

# The Vite dev server's origins. Its UI talks to 127.0.0.1:5001 directly (see
# getApiUrl in frontend/src/lib/api.ts), so a server that backs it must trust
# them — and only such a server: they are trusted under COLLIMATE_DEV=1, which
# build/dev_server.sh exports. Any other page on those ports (another Vite
# project the user runs) is a foreign site to every other server.
DEV_ORIGINS = tuple(
    f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5173, 5174)
)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def allowed_origins() -> tuple[str, ...]:
    """Browser origins besides the server's own that may call a tokenless
    server: the Vite dev server's under ``COLLIMATE_DEV=1``, plus
    ``COLLIMATE_CORS_ORIGINS`` (comma-separated) for a UI served from
    elsewhere. Read from the environment, which must be set before ``main``
    is imported (its CORS middleware takes the list once)."""
    dev = DEV_ORIGINS if os.environ.get("COLLIMATE_DEV") == "1" else ()
    extra = os.environ.get("COLLIMATE_CORS_ORIGINS", "")
    return dev + tuple(o.strip().rstrip("/") for o in extra.split(",") if o.strip())


def _split_host(value: str) -> tuple[str, str]:
    """``host[:port]`` (IPv6 bracketed) -> (lowercased host, port or '')."""
    value = value.strip().lower()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else ""
    host, _, port = value.partition(":")
    return host, port


class OriginGuard:
    """Holds the hosts a tokenless server answers to. Tests add the
    TestClient's ``testserver`` name; nothing else should."""

    def __init__(self) -> None:
        self.trusted_hosts: set[str] = set(LOOPBACK_HOSTS)


origin_guard = OriginGuard()


# ---------------------------------------------------------------------------
# The cell-file read capability: what an opaque-origin frame may read.
# ---------------------------------------------------------------------------

class CellFileCaps:
    """Mints and checks read capabilities for one cell's files.

    A renderer's inner preview frame (the html renderer's page) is
    sandboxed without ``allow-same-origin``, so its
    module scripts, fonts and ``fetch()`` of sibling cell files are CORS
    requests with ``Origin: null`` — and so is anything any other web page
    builds in a sandboxed frame of its own. The browser tells the two apart
    by nothing it sends (both carry ``Sec-Fetch-Site: cross-site`` and no
    Referer), so the preview's reads carry a capability in the URL path::

        /api/cellcap/<cap>/channels/<channel>/cells/<cell id>/files/<path>

    ``<cap>`` is an HMAC of the immutable channel id and cell id under a random key
    kept in ``.collimate/cell_file_caps.key``: unguessable, stable across
    restarts (a page caches it), and good for that one cell only. The route
    answers any Origin that presents it, so a leaked cap (a page sending its
    own URLs off the machine) is a standing read grant on that cell — its
    later accepted versions included — until the key changes: deleting the
    key file while the server is stopped revokes every cap. Being in the
    path, a relative URL inside a served file (a module importing
    ``./util.js``) inherits it. The renderer hosts ask
    ``GET /api/channels/{channel}/cells/{cell_id}/file_cap`` — a same-origin
    call — and build every ``api.getFileUrl`` URL on it."""

    KEY_PATH = os.path.join(".collimate", "cell_file_caps.key")
    _KEY_BYTES = 32

    def __init__(self) -> None:
        # Keyed by the key file's absolute path: every persistent path is
        # CWD-relative (the data dir), and tests chdir per test.
        self._keys: dict[str, bytes] = {}

    def _key(self) -> bytes:
        path = os.path.abspath(self.KEY_PATH)
        key = self._keys.get(path)
        if key is not None:
            return key
        try:
            with open(path, "rb") as fh:
                key = fh.read()
        except FileNotFoundError:
            key = b""
        if len(key) != self._KEY_BYTES:
            # Missing, or cut short by a crash mid-write: a fresh key (every
            # cap minted before it stops matching, which a reload re-mints).
            key = secrets.token_bytes(self._KEY_BYTES)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # mkstemp: a fresh name opened O_EXCL | O_NOFOLLOW, mode 0600, so
            # nothing planted beside the key is followed or truncated.
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
        self._keys[path] = key
        return key

    @staticmethod
    def _subject(channel: str, cell_id: str) -> bytes:
        # Capabilities bind a live UUID independently of its display label.
        from state import require_channel, channel_db_name
        return f"{channel_db_name(require_channel(channel))}\n{cell_id}".encode()

    def cap(self, channel: str, cell_id: str) -> str:
        subject = self._subject(channel, cell_id)
        digest = hmac.new(self._key(), subject, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def matches(self, cap: str, channel: str, cell_id: str) -> bool:
        try:
            return hmac.compare_digest(cap.encode(), self.cap(channel, cell_id).encode())
        except Exception:
            return False


cell_file_caps = CellFileCaps()

_CAP_FILE_PATH = re.compile(
    r"^/api/cellcap/(?P<cap>[^/]+)/channels/(?P<channel>[^/]+)/(?:cells|versions)/(?P<cell>[^/]+)/files/"
)
_PLAIN_FILE_PATH = re.compile(r"^/api/channels/[^/]+/(?:cells|versions)/[^/]+/files/")


def _cap_file_read(scope) -> dict | None:
    """For a request on the capability route: the scope rewritten onto the
    plain cell/version-file route when the cap matches, ``{}`` when it doesn't (or
    the request is not a GET/HEAD read), ``None`` for any other path."""
    path = scope.get("path", "")
    m = _CAP_FILE_PATH.match(path)
    if m is None:
        return None
    if (
        scope["type"] != "http"
        or scope.get("method") not in ("GET", "HEAD")
        or not cell_file_caps.matches(m["cap"], m["channel"], m["cell"])
    ):
        return {}
    prefix = f"/api/cellcap/{m['cap']}/"
    raw = scope.get("raw_path")
    raw_prefix = prefix.encode()
    return {
        **scope,
        "path": "/api/" + path[len(prefix):],
        "raw_path": (
            b"/api/" + raw[len(raw_prefix):]
            if isinstance(raw, bytes) and raw.startswith(raw_prefix)
            else ("/api/" + path[len(prefix):]).encode()
        ),
    }


def _with_open_cors(send):
    """Wraps ``send`` so the response carries ``Access-Control-Allow-Origin: *``
    (the inner CORSMiddleware never lists "null", so it adds nothing)."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            headers = [
                (k, v) for k, v in message.get("headers", ())
                if k.lower() not in (b"access-control-allow-origin", b"access-control-allow-credentials")
            ]
            headers.append((b"access-control-allow-origin", b"*"))
            message = {**message, "headers": headers}
        await send(message)
    return wrapped


class OriginGuardMiddleware:
    """Refuses cross-site use of the server, on http and websocket.

    Without a token, loopback is the whole trust boundary: anything that can
    reach the port gets file read/write and runs. A web page in the user's
    browser can reach it too, so:

    - a Host that is not loopback is refused, so a DNS-rebinding page (its
      own name resolved to 127.0.0.1) never gets an answer;
    - an Origin that is neither this server's own (loopback, same port) nor
      an allowed origin (:func:`allowed_origins`) is refused. CORS alone only
      hides responses and never covers WebSockets, so the terminal, chat and
      proxy sockets need this check too.

    Requests without an Origin (same-origin GETs, non-browser clients) pass,
    except cross-site plain file reads: image/script loads can omit Origin.

    With a token, the token is the boundary and Host/Origin are a reverse
    proxy's public name, so neither is matched. The opaque ``Origin: null``
    is still refused there: the desktop shell presents the token on every
    request to the server origin whichever frame sends it, and an opaque
    origin is untrusted content inside the app (a rendered page's sandboxed
    frame), never the app itself.

    In both modes the capability route (:class:`CellFileCaps`) answers a
    GET/HEAD from any Origin, with ``Access-Control-Allow-Origin: *`` and no
    credentials: the cap in the path is the grant. It is one of the two
    things an opaque origin may do; the other is a GET/HEAD of the bundled
    fonts under ``/fonts/``, which ``main.PublicFontsMiddleware`` answers
    before this guard runs. Nothing else on the server answers one — the
    generator proxy included, so a site framed without
    ``allow-same-origin`` gets 403 (4403 on a socket) for its own requests
    back through ``/proxy/``.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = origin = fetch_site = None
        for name, value in scope.get("headers", ()):
            if name == b"host":
                host = value.decode("latin-1")
            elif name == b"origin":
                origin = value.decode("latin-1")
            elif name == b"sec-fetch-site":
                fetch_site = value.decode("latin-1").strip().lower()
        cap_scope = _cap_file_read(scope)
        if cap_scope is not None:
            if cap_scope and (auth.enabled or self._trusted_host(host)):
                await self.app(cap_scope, receive, _with_open_cors(send))
                return
        elif auth.enabled:
            if origin is None or origin.strip() != "null":
                await self.app(scope, receive, send)
                return
        elif self._allowed(host, origin) and not (
            scope["type"] == "http" and scope.get("method") in ("GET", "HEAD")
            and fetch_site == "cross-site" and _PLAIN_FILE_PATH.match(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    def _trusted_host(host: str | None) -> bool:
        return host is None or _split_host(host)[0] in origin_guard.trusted_hosts

    @staticmethod
    def _allowed(host: str | None, origin: str | None) -> bool:
        host_name, host_port = _split_host(host) if host else ("", "")
        if host is not None and host_name not in origin_guard.trusted_hosts:
            return False
        if origin is None:
            return True
        origin = origin.strip().rstrip("/")
        if origin in allowed_origins():
            return True
        scheme, sep, rest = origin.partition("://")
        if not sep or scheme not in ("http", "https") or "/" in rest:
            return False  # includes the opaque "null" origin
        origin_name, origin_port = _split_host(rest)
        origin_port = origin_port or ("443" if scheme == "https" else "80")
        host_port = host_port or ("443" if scheme == "https" else "80")
        # Same server: a loopback name on this port (localhost and 127.0.0.1
        # are interchangeable here, both reach this socket).
        return origin_name in origin_guard.trusted_hosts and origin_port == host_port
