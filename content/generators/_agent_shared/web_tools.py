# SPDX-License-Identifier: MIT
"""Agent web fetch/search, readability and the shared URL safety guard."""
from __future__ import annotations
from .accounts import account_token, web_search_account

def ip_blocked(ip) -> bool:
    """Whether a fetch may not connect to ``ip``: anything that is not a
    globally routable unicast address. ``is_global`` covers what the named
    predicates miss — carrier-grade NAT 100.64.0.0/10 (tailnet peers, some
    clouds' metadata service), benchmarking and documentation ranges — and an
    IPv4-mapped IPv6 address is judged as the IPv4 address it carries."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        not ip.is_global or ip.is_loopback or ip.is_private or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def via_proxy(scheme: str, host: str) -> bool:
    """Whether an http(s) request to ``host`` leaves through the environment's
    proxy (``HTTP(S)_PROXY`` without a matching ``NO_PROXY``) — the same
    environment httpx reads."""
    import urllib.request

    try:
        return bool(urllib.request.getproxies().get(scheme)) and not urllib.request.proxy_bypass(host)
    except Exception:  # noqa: BLE001
        return False


def vet_url(url: str) -> tuple:
    """``(reason, pin)`` for fetching ``url``: ``reason`` is ``None`` when the
    URL is safe, else a human reason to refuse it; ``pin`` is the vetted
    address the request must CONNECT to, or ``None`` when it goes through a
    proxy (which resolves the name itself — pinning would hand the proxy an IP
    its host allowlist never matches).

    SSRF guard: rejects non-http(s) schemes, the hostname ``localhost``, and
    any host that is or resolves to a non-global address (:func:`ip_blocked`).
    A resolved name is judged on EVERY address it returns, and the caller
    connects to the vetted one — letting the client resolve the name again
    would let a rebinding DNS answer a public address to the check and
    127.0.0.1 to the connect.

    A name that doesn't resolve locally is still fetchable when the request
    is proxied: a proxy/none-internet container has a deliberately dead
    resolver and reaches the web only through the host's egress proxy, which
    resolves the name and enforces the run's host allowlist. Literal
    addresses are always judged here."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"unsupported URL scheme: {scheme or '(none)'}", None
    host = parts.hostname or ""
    if not host:
        return "URL has no host", None
    if host.lower() == "localhost":
        return "refusing to fetch localhost", None
    proxied = via_proxy(scheme, host)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if ip_blocked(literal):
            return f"refusing to fetch a private/loopback address ({literal})", None
        return None, (None if proxied else str(literal))
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80))
    except Exception as e:  # noqa: BLE001
        if proxied:
            return None, None
        return f"could not resolve host {host!r}: {e}", None
    pin = None
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            return f"unresolvable address for {host!r}", None
        if ip_blocked(ip):
            return f"refusing to fetch a private/loopback address ({ip})", None
        pin = pin or str(ip)
    if pin is None:
        return f"could not resolve host {host!r}", None
    return None, (None if proxied else pin)


def strip_html(html: str) -> str:
    """Stdlib fallback HTML→text: drop script/style, strip tags, collapse WS."""
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    p = _Stripper()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    return " ".join("".join(p.parts).split())


def html_headings(html: str) -> list:
    """The page's ``<title>`` and first ``<h1>`` texts (deduplicated, in that
    order) — what names the page."""
    from html.parser import HTMLParser

    class _Heads(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.found: dict = {}
            self.cur = None

        def handle_starttag(self, tag, attrs):
            if tag in ("title", "h1") and tag not in self.found:
                self.cur, self.found[tag] = tag, []

        def handle_endtag(self, tag):
            if tag == self.cur:
                self.cur = None

        def handle_data(self, data):
            if self.cur:
                self.found[self.cur].append(data)

    p = _Heads()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        pass
    out: list = []
    for tag in ("title", "h1"):
        text = " ".join("".join(p.found.get(tag, [])).split())
        if text and text not in out:
            out.append(text)
    return out


def extract_readable(html: str) -> str:
    """HTML → readable text via trafilatura, falling back to ``strip_html`` when
    trafilatura is unavailable or extracts nothing.

    trafilatura keeps the article BODY and drops the page's title and ``<h1>``,
    which are often what the model fetched the page for (what it is called).
    Those headings are put back on top unless the extraction already carries
    them."""
    try:
        import trafilatura
        extracted = trafilatura.extract(html)
        if extracted:
            heads = [h for h in html_headings(html) if h not in extracted[:500]]
            return "\n".join([*(f"# {h}" for h in heads), extracted]) if heads else extracted
    except Exception:  # noqa: BLE001
        pass
    return strip_html(html)


def fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch an http/https URL and return its readable text.

    HTML is reduced to readable article text; other ``text/*`` content is
    returned raw; non-text content is refused. The output is truncated to
    ``max_chars``. Blocks private / loopback / link-local / metadata addresses
    (an SSRF guard re-checked on every redirect, each hop connecting to the
    address it vetted) and follows at most 3 redirects.
    Every failure returns an ``ERROR: ...`` string (never raises)."""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return "ERROR: httpx unavailable"
    max_chars = max(1, int(max_chars))
    try:
        cur = url
        for _hop in range(4):
            blocked, pin = vet_url(cur)
            if blocked:
                return f"ERROR: {blocked}"
            target, headers, extensions = httpx.URL(cur), {}, {}
            if pin:
                # Connect to the address the guard vetted, not to whatever the
                # name resolves to next. The Host header and the TLS server
                # name (SNI + certificate check) stay the original host.
                headers["Host"] = target.netloc.decode("ascii")
                if target.scheme == "https":
                    extensions["sni_hostname"] = target.host
                target = target.copy_with(host=pin)
            # A pinned request was judged direct (no proxy applies), so the
            # environment's proxy settings must not re-route the IP URL.
            with httpx.Client(follow_redirects=False, timeout=15.0,
                              trust_env=not pin) as client:
                with client.stream("GET", target, headers=headers,
                                   extensions=extensions) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location")
                        if not loc:
                            return f"ERROR: redirect with no Location ({resp.status_code})"
                        cur = str(httpx.URL(cur).join(loc))
                        continue
                    ctype = (resp.headers.get("content-type") or "").lower()
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) >= 2_000_000:
                            break
                    raw = bytes(body)
            if "html" in ctype:
                text = extract_readable(raw.decode("utf-8", errors="replace"))
            elif ctype.startswith("text/") or not ctype:
                text = raw.decode("utf-8", errors="replace")
            else:
                return f"(non-text content-type: {ctype})"
            if len(text) > max_chars:
                text = text[:max_chars] + "…[truncated]"
            return text
        return "ERROR: too many redirects"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def search(query: str, max_results: int = 5) -> str:
    """Search the web and return ranked results.

    Each result is ``N. title — url`` followed by an indented snippet. Uses
    Tavily when the optional ``web_search`` account is selected, else
    DuckDuckGo via the ``ddgs`` package.
    Capped to ``max_results`` results and 8 KB. Returns an ``ERROR: ...`` string
    when no engine is available (never raises)."""
    max_results = max(1, int(max_results))
    try:
        account = web_search_account()
        key = account_token(account) if account else None
    except (LookupError, PermissionError, ValueError) as exc:
        return f"ERROR: {exc}"
    if account:
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return "ERROR: httpx unavailable"
        try:
            resp = httpx.post(
                (account.get("endpoint") or "https://api.tavily.com").rstrip("/") + "/search",
                json={"api_key": key, "query": query, "max_results": max_results},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {e}"
        results = (data or {}).get("results") or []
        lines = [
            f"{i}. {(r.get('title') or '').strip()} — {(r.get('url') or '').strip()}\n"
            f"   {(r.get('content') or '').strip()}"
            for i, r in enumerate(results[:max_results], 1)
        ]
        return ("\n".join(lines) if lines else "(no results)")[:8192]

    try:
        from ddgs import DDGS
    except Exception:  # noqa: BLE001
        return "ERROR: no web search engine available (select a web_search account in Key Manager or install ddgs)"
    try:
        lines = []
        with DDGS() as ddgs:
            for i, r in enumerate(ddgs.text(query, max_results=max_results), 1):
                title = (r.get("title") or "").strip()
                u = (r.get("href") or r.get("url") or "").strip()
                snippet = (r.get("body") or "").strip()
                lines.append(f"{i}. {title} — {u}\n   {snippet}")
        return ("\n".join(lines) if lines else "(no results)")[:8192]
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"

