"""Per-run egress proxy — a CONNECT/HTTP forward proxy that bounds a
containerized run's outbound network to an allowlist.

Used only by the docker runner (``api/process_manager/docker_runner.py``)
for a ``docker: limited`` run whose runner permissions set ``internet: proxy``
or ``internet: none``. The docker runner:

  * creates a per-run bridge network with IP masquerade DISABLED, so the
    container has NO working route to the public internet (packets leave
    with a private source and never route back) but CAN still reach the
    host gateway on-link;
  * starts one :class:`EgressProxy` and points the container's
    ``HTTP(S)_PROXY`` at :meth:`EgressProxy.url`;
  * so the ONLY way out is through this proxy, which enforces the runner's
    ``internet_allow`` host-suffix allowlist. ``internet: none`` is exactly
    ``internet: proxy`` with an EMPTY allowlist — the proxy then denies every
    host (see :func:`host_allowed`), which is why ``none`` needs no separate
    code path (a bare ``--network none`` would also kill the TCP IPC the run
    depends on).

Who may use it: the proxy listens on every host interface, because the
container reaches the host through ``host-gateway``, which is not a host
interface on every docker flavour (Docker Desktop, rootless). So each proxy
mints a per-run credential, carried as the userinfo of the proxy URL
(``http://collimate:<token>@host:port``), which proxy-aware clients send as
``Proxy-Authorization: Basic``. It is checked with ``hmac.compare_digest``
before anything else is routed or reported: a request without it gets 407
and never reaches the allowlist, an upstream or the ``egress_denied`` hook,
so a network peer can neither relay through the run nor write into its UI.
The docker client carries the proxy URL on its argv (a proxy variable is
one the client reads itself; see ``docker_runner.env_argv``), which only a
local user can read, and a local user already shares the host's address and
gains nothing from the relay.

What IS and ISN'T bounded (honest 1.0):

  * proxy-aware traffic (anything honoring ``HTTP(S)_PROXY``: requests,
    urllib, httpx, curl, most SDKs) is allowlist-enforced here.
  * non-proxy-aware direct egress is bounded by the masquerade-off network,
    not by this proxy — a raw socket to ``1.1.1.1:443`` black-holes (times
    out) rather than being actively refused. That is the enforceable combo
    docker gives us without per-run iptables surgery; the runner's live
    integration test demonstrates both halves.

Every allowed name is also resolved HERE and refused when any address it
returns is not globally routable (:func:`addr_blocked`): the proxy connects
from the host's network namespace, so an allowlisted name answering 127.0.0.1,
a LAN or a cloud-metadata address would otherwise reach the host's loopback
services, the LAN or the metadata endpoint. The in-container fetch tools can't
make that check themselves — a proxy/none run's resolver is deliberately dead —
so this is the one place it happens. The upstream connect goes to the vetted
address, not the name, so a rebinding DNS answer can't swap it in between.

A denied request emits an ``egress_denied`` run_event (via ``on_deny``) so the
UI can surface the blocked host, then answers 403.

Plain-HTTP (absolute-form) requests are re-issued upstream in origin form
with the hop-by-hop headers stripped (the credential among them), ``Host``
set from the vetted URL, and ``Connection: close``: the upstream answers one
request and closes, so every request on a keep-alive client connection
opens a new proxy connection and is vetted on its own.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import logging
import secrets
import socket
from typing import Callable, Optional

from runner_permissions import entry_covers, normalize_host_entry

_logger = logging.getLogger("egress_proxy")

# The user half of the per-run proxy credential; the token is the password.
_PROXY_USER = "collimate"

# Callback the manager passes so a denial lands as an ``egress_denied``
# run_event. ``(host, port, method)`` of the blocked request.
DenyHook = Callable[[str, int, str], None]

# Bounds on the request head (request line + headers) read before routing:
# the peer is the untrusted run, so neither its size nor how long it takes
# to arrive may be open-ended. Tunnels and bodies are piped afterwards and
# stay unbounded in time.
_MAX_HEAD_BYTES = 64 * 1024
_HEAD_TIMEOUT = 30.0

# Request headers that describe the client→proxy hop, never forwarded
# upstream (RFC 9110 §7.6.1, plus the proxy's own credential and the
# non-standard ``Proxy-Connection``). ``Host`` is rewritten from the vetted
# URL instead (RFC 9112 §3.2.2). ``Transfer-Encoding`` is hop-by-hop too, but
# the body is relayed byte for byte, so its framing header travels with it.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authorization",
    "proxy-authenticate", "te", "upgrade", "host",
})
# The framing headers a ``Connection`` token can never strip.
_FRAMING = frozenset({"content-length", "transfer-encoding"})


class _HeadTooLarge(Exception):
    pass


def _request_line(raw: bytes) -> tuple[str, str] | None:
    """``(method, target)`` of the request line, or ``None`` when it is not
    three space-separated parts or carries a bare CR or NUL (which an
    upstream parser could read as a line break)."""
    line = raw.decode("latin-1").strip()
    if "\r" in line or "\0" in line:
        return None
    try:
        method, target, _version = line.split(" ", 2)
    except ValueError:
        return None
    return method, target


def _header_fields(block: bytes) -> list[tuple[str, str]] | None:
    """The header block as ``(name, value)`` pairs in order, or ``None`` when
    a line is malformed: no colon, whitespace before it, a bare CR or NUL, or
    an obsolete folded continuation line (RFC 9112 §5.2 lets a proxy reject
    those)."""
    fields: list[tuple[str, str]] = []
    for raw in block.decode("latin-1").split("\n"):
        line = raw.removesuffix("\r")
        if not line:
            continue
        name, sep, value = line.partition(":")
        if (not sep or not name or name != name.strip() or line[0] in " \t"
                or "\r" in line or "\0" in line):
            return None
        fields.append((name, value.strip()))
    return fields


def host_allowed(host: str, allow: tuple[str, ...]) -> bool:
    """Whether ``host`` is reachable under the ``allow`` host-suffix list.

    Matching is the grant's own grammar (``runner_permissions.entry_covers``):
    a host matches an entry when it equals it or ends with ``"." + entry`` —
    so ``api.anthropic.com`` matches ``anthropic.com`` but ``notanthropic.com``
    does NOT (the boundary is a label dot, never a bare substring). Entries
    are read in their one spelling (``runner_permissions.normalize_host_entry``),
    so ``.Anthropic.com`` == ``anthropic.com``.

    An EMPTY allowlist denies everything — this is the ``internet: none``
    equivalence (deny-all proxy). A blank/garbage host is denied.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    # Strip any :port (never present in a CONNECT authority we've already
    # split, but cheap belt-and-suspenders for absolute-form URLs).
    if h.startswith("["):  # bracketed IPv6 literal — never allowlisted
        return False
    if ":" in h:
        h = h.split(":", 1)[0]
    return any(
        e and entry_covers(e, h)
        for e in (normalize_host_entry(entry or "") for entry in allow)
    )


def addr_blocked(ip) -> bool:
    """Whether the proxy may not connect to ``ip``: anything that is not a
    globally routable unicast address. The same rule as the SDK's fetch guard
    (``_ip_blocked`` in generators/_agent_shared) — ``is_global`` also covers
    carrier-grade NAT and documentation ranges the named predicates miss, and
    an IPv4-mapped IPv6 address is judged as the IPv4 address it carries."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        not ip.is_global or ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


async def _vetted_address(host: str, port: int) -> tuple[str | None, str]:
    """``(ip, "")`` — the address to connect to — when every address ``host``
    resolves to is public, else ``(None, reason)``. Judging every address, not
    just the first, keeps a name that answers both a public and a private
    address from being let through."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"could not resolve ({e})"
    pin = None
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            return None, "unparseable address"
        if addr_blocked(ip):
            return None, f"resolves to a non-public address ({ip})"
        pin = pin or str(ip)
    if pin is None:
        return None, "no address"
    return pin, ""


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    """``host:port`` (CONNECT target) → ``(host, port)`` with a default."""
    authority = authority.strip()
    if authority.startswith("[") and "]" in authority:  # [ipv6]:port
        host, _, rest = authority.partition("]")
        host = host[1:]
        port = int(rest.lstrip(":")) if rest.lstrip(":") else default_port
        return host, port
    if ":" in authority:
        host, _, p = authority.rpartition(":")
        try:
            return host, int(p)
        except ValueError:
            return authority, default_port
    return authority, default_port


class EgressProxy:
    """A single run's forward proxy. Bind with :meth:`start`, tear down with
    :meth:`stop`; both are idempotent. One instance per run — the allowlist,
    deny hook and credential are fixed at construction."""

    def __init__(
        self,
        run_id: str,
        allow: tuple[str, ...] | list[str],
        *,
        on_deny: Optional[DenyHook] = None,
        bind_host: str = "0.0.0.0",
    ):
        self.run_id = run_id
        self.allow: tuple[str, ...] = tuple(allow or ())
        self.on_deny = on_deny
        self.bind_host = bind_host
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        # URL-safe, so it sits in the proxy URL's userinfo unescaped.
        self._token = secrets.token_urlsafe(32)
        self._credential = f"{_PROXY_USER}:{self._token}".encode("ascii")

    def url(self, host: str) -> str:
        """The proxy URL a client reaching this proxy as ``host`` is given:
        ``http://collimate:<token>@host:port``. Valid once :meth:`start` has
        bound the port."""
        if self.port is None:
            raise RuntimeError("egress proxy is not started")
        return f"http://{_PROXY_USER}:{self._token}@{host}:{self.port}"

    async def start(self) -> int:
        """Bind an ephemeral port and begin serving. Returns the port."""
        self._server = await asyncio.start_server(
            self._handle_client, self.bind_host, 0,
        )
        port = int(self._server.sockets[0].getsockname()[1])
        self.port = port
        _logger.info(
            "egress proxy for run %s listening on %s:%s (allow=%s)",
            self.run_id, self.bind_host, self.port, list(self.allow) or "DENY-ALL",
        )
        return port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                _logger.debug("egress proxy %s close raised", self.run_id, exc_info=True)
            self._server = None

    # -- connection handling ------------------------------------------------

    def _authorized(self, fields: list[tuple[str, str]]) -> bool:
        """Whether the request carries this proxy's credential as
        ``Proxy-Authorization: Basic``, compared in constant time."""
        for name, value in fields:
            if name.lower() != "proxy-authorization":
                continue
            scheme, _, param = value.partition(" ")
            if scheme.lower() != "basic":
                continue
            try:
                given = base64.b64decode(param.strip(), validate=True)
            except (binascii.Error, ValueError):
                continue
            if hmac.compare_digest(given, self._credential):
                return True
        return False

    def _deny(
        self, host: str, port: int, method: str, why: str = "not in allowlist",
    ) -> None:
        _logger.info(
            "egress DENY run=%s %s %s:%s (%s)",
            self.run_id, method, host, port, why,
        )
        if self.on_deny is not None:
            try:
                self.on_deny(host, port, method)
            except Exception:
                _logger.debug("egress on_deny hook raised", exc_info=True)

    async def _open_upstream(
        self, host: str, port: int, method: str, writer: asyncio.StreamWriter,
    ):
        """Allowlist + address check, then connect to the vetted address.
        ``None`` (after answering the client) when the request is refused or
        the upstream is unreachable."""
        if not host_allowed(host, self.allow):
            self._deny(host, port, method)
            await self._respond(writer, 403, "Forbidden (egress not allowed)")
            return None
        ip, why = await _vetted_address(host, port)
        if ip is None:
            self._deny(host, port, method, why)
            await self._respond(writer, 403, "Forbidden (egress not allowed)")
            return None
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=15,
            )
        except Exception:
            await self._respond(writer, 502, "Bad Gateway")
            return None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                head = await asyncio.wait_for(self._read_head(reader), _HEAD_TIMEOUT)
            except asyncio.TimeoutError:
                return
            except _HeadTooLarge:
                await self._respond(writer, 431, "Request Header Fields Too Large")
                return
            if head is None:
                return
            request_line, headers = head
            parsed, fields = _request_line(request_line), _header_fields(headers)
            if parsed is None or fields is None:
                await self._respond(writer, 400, "Bad Request")
                return
            method, target = parsed
            # The credential gates everything after parsing: an unauthenticated
            # peer never reaches the allowlist, an upstream or the deny hook.
            if not self._authorized(fields):
                _logger.debug("egress proxy %s: refused an unauthenticated request", self.run_id)
                await self._respond(
                    writer, 407, "Proxy Authentication Required",
                    extra=f'Proxy-Authenticate: Basic realm="{_PROXY_USER}"\r\n',
                )
                return

            if method.upper() == "CONNECT":
                await self._handle_connect(target, reader, writer)
            elif "://" in target:
                await self._handle_absolute(method, target, fields, reader, writer)
            else:
                # Origin-form request with no proxy target — nothing to route.
                await self._respond(writer, 400, "Bad Request (expected a proxy request)")
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.debug("egress client handler error", exc_info=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_head(
        self, reader: asyncio.StreamReader,
    ) -> tuple[bytes, bytes] | None:
        """The request line and the header block up to the blank line, or
        ``None`` when the client closed before sending a request line. Raises
        :class:`_HeadTooLarge` once the head passes ``_MAX_HEAD_BYTES``."""
        request_line = await reader.readline()
        if not request_line:
            return None
        total = len(request_line)
        headers = bytearray()
        while True:
            line = await reader.readline()
            total += len(line)
            if total > _MAX_HEAD_BYTES:
                raise _HeadTooLarge
            headers += line
            if line in (b"\r\n", b"\n", b""):
                break
        return request_line, bytes(headers)

    async def _handle_connect(
        self, authority: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        host, port = _split_authority(authority, 443)
        upstream = await self._open_upstream(host, port, "CONNECT", writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _handle_absolute(
        self, method: str, url: str, fields: list[tuple[str, str]],
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        # Parse ``http://host[:port]/path`` — HTTPS never arrives here (it uses
        # CONNECT). Only plain-HTTP absolute-form.
        rest = url.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        path = "/" + path
        # Userinfo in a request target is the origin's business, not a host.
        authority = authority.rpartition("@")[2]
        host, port = _split_authority(authority, 80)
        upstream = await self._open_upstream(host, port, method.upper(), writer)
        if upstream is None:
            return
        up_reader, up_writer = upstream
        up_writer.write(_origin_form_head(method, path, authority, fields))
        await up_writer.drain()
        # The request body, then the one response, ride the relay below; the
        # upstream closes after answering (``Connection: close``), which ends
        # it, so a later request on the client's connection is never sent to
        # this upstream unvetted.
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _pipe(
        self,
        c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional byte relay until either side closes."""
        async def _copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            _copy(c_reader, u_writer),
            _copy(u_reader, c_writer),
            return_exceptions=True,
        )

    async def _respond(
        self, writer: asyncio.StreamWriter, code: int, reason: str, *, extra: str = "",
    ) -> None:
        body = reason.encode("utf-8")
        try:
            writer.write(
                f"HTTP/1.1 {code} {reason}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"{extra}"
                f"Content-Type: text/plain\r\n\r\n".encode("latin-1")
                + body
            )
            await writer.drain()
        except Exception:
            pass


def _origin_form_head(
    method: str, path: str, authority: str, fields: list[tuple[str, str]],
) -> bytes:
    """The request head sent upstream for an absolute-form request: origin
    form, ``Host`` from the vetted URL, the client's end-to-end headers, and
    ``Connection: close``. Hop-by-hop headers are dropped, with any header a
    ``Connection`` token names (never a framing header)."""
    named = {
        token.strip().lower()
        for name, value in fields if name.lower() == "connection"
        for token in value.split(",")
    } - _FRAMING
    kept = [
        f"{name}: {value}\r\n" for name, value in fields
        if name.lower() not in _HOP_BY_HOP and name.lower() not in named
    ]
    return (
        f"{method} {path} HTTP/1.1\r\nHost: {authority}\r\n"
        + "".join(kept)
        + "Connection: close\r\n\r\n"
    ).encode("latin-1")


__all__ = ["EgressProxy", "addr_blocked", "host_allowed"]
