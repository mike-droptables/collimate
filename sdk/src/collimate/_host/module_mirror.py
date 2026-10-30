# SPDX-License-Identifier: MIT
"""The module mirror: upstream keys, served bodies and the shipped snapshot.

Renderers import from two allowlisted CDNs, ``https://esm.sh/`` and
``https://cdn.jsdelivr.net/`` (``module_mirror_contract.json``). The app
serves both from its own origin at ``/api/mirror/<host>/<path>``: its
document import maps send ``https://<host>/`` there, and the renderer runtime's
``mirrorUrl()`` maps the loads an import map does not cover (stylesheets,
fonts, wasm, AMD scripts, workers).

This module is the one implementation the SDK's freeze and the app's mirror
share:

- **Keys.** :func:`mirror_key` turns the route's raw path and query into the
  canonical upstream URL a module is stored under; :func:`canonical_url` does
  the same for an absolute URL.
- **Bodies.** :func:`served_body` rewrites a JavaScript body's root-relative
  and absolute specifiers for the same host into relative ones, which resolve
  to the same target against the upstream URL and against the mirror route,
  so the graph works in workers (no import map) and under any API base.
  A specifier for the other allowlisted host becomes its absolute upstream
  URL, which the document import map routes. Everything that is not
  JavaScript passes through byte for byte.
- **Snapshot.** The shipped renderers' whole graph is frozen at the
  workspace root's ``module-snapshot/`` (:func:`freeze`, with the network),
  which the app's ``collimate`` wheel ships as package data and its mirror
  serves (:func:`read_snapshot`). Its ``index.json`` maps each
  canonical URL to the content-addressed served body under ``modules/``;
  ``LICENSES.json`` names every redistributed package's license, and
  ``builders.json`` the entries a local esm.sh built (below). Those three
  are committed; ``modules/`` is git-ignored and its bodies live on the
  orphan branch ``module-snapshot`` (the workspace's
  ``build/snapshot_store.py``: ``publish`` after a freeze, ``fetch`` in a
  checkout).
- **The local esm.sh.** When public esm.sh answers a cold build with a 5xx
  (its builder runs out of disk for days at a time), ``freeze --esm-builder
  <base url>`` asks a self-hosted esm.sh of the pinned release (the
  contract's ``esm_builder_image``) for the same path and query instead. The
  key stays the public ``https://esm.sh/…`` URL (esm.sh keys encode the
  resolution, so a key names the same versions on either instance) and the
  body is rewritten exactly as a public one. Everything not yet frozen that a
  locally built module reaches is built locally too, so a family's subgraph
  comes from one builder; already-frozen shared keys stay. The local bodies
  differ byte for byte from the public instance's, so ``builders.json``
  records each locally built key; a later freeze that fetches one publicly
  drops the mark. The app never talks to the local builder.

CLI (from a workspace checkout)::

    python -m collimate._host.module_mirror freeze [--refresh] [--esm-builder URL]
                                                           # walk upstream, write the snapshot
    python -m collimate._host.module_mirror check                # verify it against the sources; non-zero
                                                           # while a root is unfrozen (an unavailable
                                                           # edge is reported, tolerated)
    python -m collimate._host.module_mirror bump [--major] [--dry-run] [--only PKG ...] [--esm-builder URL]
                                                           # move the exact pins, refreeze; a family
                                                           # whose roots stay unfrozen is reverted
                                                           # (collimate._host.module_bump)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from .esm_lexer import LexError, scan_imports

__all__ = [
    "CONTRACT", "UPSTREAMS", "HOSTS", "InvalidKey", "Rewrite", "SnapshotEntry", "Snapshot",
    "contract_hash", "mirror_key", "canonical_url", "is_floating", "is_javascript", "resolve",
    "relative", "rewrite_module", "served_body", "check_redirect", "read_snapshot",
    "snapshot_index_bytes", "shipped_roots", "package_of", "freeze",
    "snapshot_problems", "closure_problems", "instance_problems", "unavailable_problems", "unavailable_edges",
    "read_builders", "builder_problems", "check_esm_builder", "builder_tag", "BUILDERS_FILE", "ESM_SH", "SINGLE_INSTANCE",
]

_CONTRACT_BYTES = (Path(__file__).parent.parent / "module_mirror_contract.json").read_bytes()
CONTRACT: Mapping = MappingProxyType(json.loads(_CONTRACT_BYTES))
UPSTREAMS: tuple[str, ...] = tuple(CONTRACT["upstreams"])
HOSTS: tuple[str, ...] = tuple(upstream[len("https://"):-1] for upstream in UPSTREAMS)
_FLOATING = re.compile(CONTRACT["floating"])
_VERSION = re.compile(CONTRACT["version"])
_IMMUTABLE = re.compile(CONTRACT["immutable"])
_EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?")

SNAPSHOT_SCHEMA = 1
# The workspace checkout's layout, relative to its root: the frozen graph, the
# package trees whose sources name its roots, and the renderer runtime.
SNAPSHOT_DIR = "module-snapshot"
SHIPPED_TREES = ("content/renderers", "builtins")
RUNTIME_DIR = "sdk/shared/renderer-runtime"
BUILDERS_FILE = "builders.json"
ESM_SH = "https://esm.sh/"
MIRROR_ASSETS = "mirror_assets.json"
FREEZE_COMMAND = "python -m collimate._host.module_mirror freeze"
PUBLISH_COMMAND = "python3 build/snapshot_store.py publish"


def contract_hash() -> str:
    """The sha256 of the contract file, recorded in every snapshot."""
    return hashlib.sha256(_CONTRACT_BYTES).hexdigest()


class InvalidKey(ValueError):
    """A path, URL or specifier the mirror cannot key."""


# -- keys ------------------------------------------------------------------
_HEX = frozenset("0123456789abcdefABCDEF")
# Never decoded from %XX: decoding would change how the upstream reads it.
_KEEP_ENCODED = frozenset('%/?#&=+\\"<> ')
# Encoded when raw, so a browser's escaped and unescaped forms share a key.
_ENCODE_RAW = frozenset('"<> ')
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DOT = frozenset({".", "%2e"})
_DOT_DOT = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})


def _canonical_part(text: str, *, query: bool) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "%":
            digits = text[i + 1:i + 3]
            if len(digits) != 2 or not set(digits) <= _HEX:
                raise InvalidKey(f"malformed percent escape in {text!r}")
            code = int(digits, 16)
            if code == 0:
                raise InvalidKey("NUL in key")
            if not query and code in (0x2F, 0x5C):
                raise InvalidKey("encoded slash or backslash in path")
            ch = chr(code)
            out.append(ch if 0x21 <= code <= 0x7E and ch not in _KEEP_ENCODED else "%" + digits.upper())
            i += 3
            continue
        if c == "\0":
            raise InvalidKey("NUL in key")
        if c == "\\":
            raise InvalidKey("backslash in key")
        if c == "#":
            raise InvalidKey("fragment in key")
        if c == "?" and not query:
            raise InvalidKey("query separator in path")
        if c in _ENCODE_RAW:
            out.append(f"%{ord(c):02X}")
        elif "!" <= c <= "~":
            out.append(c)
        else:
            out.extend(f"%{byte:02X}" for byte in c.encode("utf-8"))
        i += 1
    return "".join(out)


def mirror_key(raw_path: str, raw_query: str = "") -> str:
    """The canonical upstream URL for a mirror route request.

    ``raw_path`` is the request's raw (still percent-encoded) path after
    ``/api/mirror/``: ``<host>/<path>``; ``raw_query`` its raw query string.
    Printable ASCII escapes are decoded except ``% / ? # & = + \\ " < >`` and
    space, so ``%5E`` and ``^`` give one key; raw ``" < >`` and space are
    encoded; anything else outside printable ASCII is percent-encoded as
    UTF-8. Refuses (:class:`InvalidKey`) a host off the allowlist, an empty
    path, ``%2F``/``%5C`` in the path, NUL, backslash, ``#``, and ``.``/``..``
    segments. The query is part of the key, unreordered.
    """
    host, slash, rest = raw_path.partition("/")
    if not slash or host not in HOSTS:
        raise InvalidKey(f"not an allowlisted upstream host: {host!r}")
    if not rest:
        raise InvalidKey("empty path")
    path = _canonical_part(rest, query=False)
    if any(segment.lower() in _DOT | _DOT_DOT for segment in path.split("/")):
        raise InvalidKey("dot segment in path")
    query = _canonical_part(raw_query, query=True) if raw_query else ""
    return f"https://{host}/{path}" + (f"?{query}" if query else "")


def _split(url: str) -> tuple[str, str, str]:
    """host, path (no leading slash), query of a canonical URL."""
    rest = url[len("https://"):]
    host, _, path_query = rest.partition("/")
    path, _, query = path_query.partition("?")
    return host, path, query


def canonical_url(url: str) -> str:
    """The canonical key of an absolute ``https://<allowlisted host>/…`` URL."""
    if url[:8].lower() != "https://":
        raise InvalidKey(f"not an https URL: {url!r}")
    rest = url[8:]
    authority, slash, path_query = rest.partition("/")
    host = authority.lower()
    if host.endswith(":443"):
        host = host[:-4]
    if "@" in host or ":" in host:
        raise InvalidKey(f"credentials or port in {url!r}")
    path, qsep, query = path_query.partition("?")
    return mirror_key(f"{host}/{path}", query if qsep else "")


def _host(url: str) -> str:
    return url[len("https://"):].partition("/")[0]


def is_floating(url: str) -> bool:
    """Whether a canonical URL names something upstream can change: a range or
    moving tag, or no exact version or content hash in its path (the preview's
    ``pinnedURL`` rule). The mirror freezes such a key at first fetch and
    serves it ``no-cache``; exact keys are ``immutable``."""
    _, path, query = _split(url)
    decoded_path = "/" + urllib.parse.unquote(path)
    if _FLOATING.search(decoded_path + ("?" + urllib.parse.unquote(query) if query else "")):
        return True
    return not (_VERSION.search(decoded_path) or _IMMUTABLE.search(decoded_path))


_JAVASCRIPT_TYPES = frozenset({
    "application/javascript", "text/javascript", "application/x-javascript",
    "application/ecmascript", "text/ecmascript", "application/x-ecmascript",
})


def is_javascript(media_type: str) -> bool:
    """Whether a Content-Type is JavaScript (the one kind the mirror rewrites)."""
    return media_type.split(";", 1)[0].strip().lower() in _JAVASCRIPT_TYPES


def _remove_dot_segments(path: str) -> str:
    segments = path.split("/")[1:]
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        low = segment.lower()
        if low in _DOT_DOT:
            if out:
                out.pop()
            if last:
                out.append("")
        elif low in _DOT:
            if last:
                out.append("")
        else:
            out.append(segment)
    return "/" + "/".join(out)


def _reference(base: str, ref: str) -> str | None:
    """Resolve a URL reference against a canonical base, WHATWG-style:
    the canonical URL when it lands on an allowlisted host, else None."""
    if ref[:6].lower() == "https:" and ref[6:8] in ("//", "\\\\", "/\\", "\\/"):
        absolute = "https://" + ref[8:]
    elif _SCHEME.match(ref):
        return None
    elif ref[:2] in ("//", "\\\\", "/\\", "\\/"):
        absolute = "https://" + ref[2:]
    else:
        absolute = None
    if absolute is not None:
        authority = absolute[8:].replace("\\", "/").partition("/")[0].lower()
        if authority.removesuffix(":443") not in HOSTS:
            return None
    ref_main, hash_sep, _ = ref.partition("#")
    if hash_sep:
        raise InvalidKey(f"fragment in {ref!r}")
    if absolute is not None:
        head, qsep, query = absolute.partition("?")
        host_path = head[8:].replace("\\", "/")
        host, _, path = host_path.partition("/")
        return canonical_url("https://" + host + _remove_dot_segments("/" + path) + (qsep + query))
    ref_path, qsep, ref_query = ref_main.partition("?")
    ref_path = ref_path.replace("\\", "/")
    host, base_path, base_query = _split(base)
    if ref_path.startswith("/"):
        path = ref_path
    elif ref_path:
        directory = "/" + base_path[:base_path.rfind("/") + 1]
        path = directory + ref_path
    else:
        path = "/" + base_path
        if not qsep:
            ref_query, qsep = base_query, "?" if base_query else ""
    return mirror_key(host + _remove_dot_segments(path), ref_query if qsep else "")


def _is_relative(specifier: str) -> bool:
    return specifier.startswith("./") or specifier.startswith("../") or specifier in (".", "..")


def resolve(specifier: str, base: str) -> str | None:
    """The canonical upstream URL an import specifier names from the module at
    ``base``, or None for other origins, ``data:``, ``blob:`` and bare
    specifiers. Raises :class:`InvalidKey` for an allowlisted target the
    mirror cannot key (a fragment, an empty path)."""
    if _SCHEME.match(specifier):
        if specifier[:6].lower() != "https:":
            return None
        return _reference(base, specifier)
    if specifier.startswith("/") or _is_relative(specifier):
        return _reference(base, specifier)
    return None


def relative(from_url: str, to_url: str) -> str:
    """The relative specifier from one canonical URL to another on the same
    host: always ``./`` or ``../``, query kept, never above the host root."""
    from_host, from_path, _ = _split(from_url)
    to_host, to_path, to_query = _split(to_url)
    if from_host != to_host:
        raise ValueError(f"relative() across hosts: {from_url} -> {to_url}")
    from_dirs = from_path.split("/")[:-1]
    to_segments = to_path.split("/")
    to_dirs, to_last = to_segments[:-1], to_segments[-1]
    common = 0
    while common < len(from_dirs) and common < len(to_dirs) and from_dirs[common] == to_dirs[common]:
        common += 1
    ups = len(from_dirs) - common
    rest = "/".join(to_dirs[common:] + [to_last])
    return ("../" * ups if ups else "./") + rest + (f"?{to_query}" if to_query else "")


def _climbs_above_root(specifier: str, url: str) -> bool:
    """Whether a relative specifier would climb above the host root from
    ``url``: upstream clamps there, the mirror route would leave its prefix."""
    path = specifier.partition("?")[0].replace("\\", "/")
    depth = len(_split(url)[1].split("/")) - 1
    segments = path.split("/")
    for index, segment in enumerate(segments):
        low = segment.lower()
        if low in _DOT_DOT:
            if depth == 0:
                return True
            depth -= 1
        elif low not in _DOT and index < len(segments) - 1:
            depth += 1
    return False


def check_redirect(location: str, current: str) -> str:
    """The canonical URL an upstream redirect from ``current`` names; refuses
    (:class:`InvalidKey`) one that leaves the upstream allowlist."""
    target = _reference(canonical_url(current), location.strip())
    if target is None:
        raise InvalidKey(f"redirect leaves the upstream allowlist: {location!r}")
    return target


# -- bodies ------------------------------------------------------------------
@dataclass(frozen=True)
class Rewrite:
    """A rewritten JavaScript body and the upstream graph edges it names.

    ``imports``/``dynamic`` are the canonical upstream targets of its static
    and literal dynamic imports; ``computed`` counts ``import()`` calls with
    no literal specifier; ``external`` lists literal specifiers that are not
    upstream URLs (other origins, ``data:``, bare names), left untouched."""

    body: bytes
    imports: tuple[str, ...]
    dynamic: tuple[str, ...]
    computed: int
    external: tuple[str, ...]


def _literal_safe(specifier: str, quote: str) -> str:
    if quote in specifier:
        specifier = specifier.replace(quote, f"%{ord(quote):02X}")
    if quote == "`" and "${" in specifier:
        specifier = specifier.replace("${", "%24{")
    return specifier


def rewrite_module(raw: bytes, *, url: str, final_url: str | None = None) -> Rewrite:
    """Rewrite one JavaScript module served at ``url``.

    Specifiers are resolved against ``final_url`` (where an upstream redirect
    landed; ``url`` when None) and emitted relative to ``url``. Root-relative
    and absolute specifiers for ``url``'s host become relative; an existing
    relative specifier is kept byte for byte unless a redirect moved the base
    or it climbs above the host root. A specifier for the other allowlisted
    host becomes its absolute upstream URL. Raises :class:`LexError` for an
    unlexable body and :class:`ValueError` for non-UTF-8 bytes or an
    upstream specifier the mirror cannot key.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"module body is not UTF-8: {error}") from None
    url = canonical_url(url)
    base = canonical_url(final_url) if final_url else url
    host = _host(url)
    offset = 1 if text.startswith("﻿") else 0  # the browser strips a BOM before parsing
    imports: set[str] = set()
    dynamic: set[str] = set()
    external: set[str] = set()
    computed = 0
    edits: list[tuple[int, int, str]] = []
    for record in scan_imports(text[offset:]):
        name = record.name
        if name is None:
            computed += 1
            continue
        target = resolve(name, base)
        if target is None:
            external.add(name)
            continue
        (dynamic if record.dynamic else imports).add(target)
        if _host(target) == host:
            if _is_relative(name) and base == url and not _climbs_above_root(name, url):
                continue
            specifier = relative(url, target)
        elif name[:8].lower() == "https://":
            continue  # the document import map routes the other upstream
        else:
            specifier = target
        if specifier != name:
            start = offset + record.start
            edits.append((start, offset + record.end, _literal_safe(specifier, text[start - 1])))
    if edits:
        parts: list[str] = []
        cursor = 0
        for start, end, specifier in edits:
            parts.append(text[cursor:start])
            parts.append(specifier)
            cursor = end
        parts.append(text[cursor:])
        body = "".join(parts).encode("utf-8")
    else:
        body = raw
    return Rewrite(body, tuple(sorted(imports)), tuple(sorted(dynamic)), computed, tuple(sorted(external)))


def served_body(raw: bytes, media_type: str, *, url: str,
                final_url: str | None = None) -> tuple[bytes, Rewrite | None]:
    """The bytes the mirror serves for an upstream response: a JavaScript body
    rewritten (:func:`rewrite_module`), anything else byte for byte."""
    if is_javascript(media_type):
        rewrite = rewrite_module(raw, url=url, final_url=final_url)
        return rewrite.body, rewrite
    return raw, None


# -- the snapshot --------------------------------------------------------------
@dataclass(frozen=True)
class SnapshotEntry:
    """One frozen module: its served body's sha256 (``modules/<hash>``), the
    upstream Content-Type, the upstream body's sha256, where any redirect
    landed, and its static and literal dynamic upstream edges."""

    hash: str
    media_type: str
    upstream_hash: str
    final_url: str
    imports: tuple[str, ...]
    dynamic: tuple[str, ...]


@dataclass(frozen=True)
class Snapshot:
    contract: str
    roots: tuple[str, ...]
    entries: Mapping[str, SnapshotEntry]
    unavailable: Mapping[str, str]


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTRY_FIELDS = {"hash", "media_type", "upstream_hash", "final_url", "imports", "dynamic"}


def _urls(value, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{what}: not a list of URLs")
    if value != sorted(set(value)):
        raise ValueError(f"{what}: not sorted and unique")
    for item in value:
        if canonical_url(item) != item:
            raise ValueError(f"{what}: not a canonical URL: {item}")
    return tuple(value)


def read_snapshot(directory: Path, *, verify: bool = False) -> Snapshot:
    """Parse and validate ``<directory>/index.json``. With ``verify``, also
    check every module file is present with its recorded sha256, and that
    ``modules/`` holds nothing else. Raises ValueError on any defect."""
    directory = Path(directory)
    try:
        index = json.loads((directory / "index.json").read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"module snapshot index unreadable: {error}") from None
    if not isinstance(index, dict) or set(index) != {"schema", "contract", "roots", "entries", "unavailable"}:
        raise ValueError("module snapshot index has the wrong fields")
    if index["schema"] != SNAPSHOT_SCHEMA:
        raise ValueError(f"module snapshot schema {index['schema']!r} is not {SNAPSHOT_SCHEMA}")
    if not isinstance(index["contract"], str) or not _SHA256.fullmatch(index["contract"]):
        raise ValueError("module snapshot contract is not a sha256")
    roots = _urls(index["roots"], "roots")
    if not isinstance(index["entries"], dict) or not isinstance(index["unavailable"], dict):
        raise ValueError("module snapshot entries/unavailable are not objects")
    entries: dict[str, SnapshotEntry] = {}
    for url, raw in index["entries"].items():
        if canonical_url(url) != url:
            raise ValueError(f"entry key is not a canonical URL: {url}")
        if not isinstance(raw, dict) or set(raw) != _ENTRY_FIELDS:
            raise ValueError(f"entry {url} has the wrong fields")
        for name in ("hash", "upstream_hash"):
            if not isinstance(raw[name], str) or not _SHA256.fullmatch(raw[name]):
                raise ValueError(f"entry {url}: {name} is not a sha256")
        if not isinstance(raw["media_type"], str) or not raw["media_type"]:
            raise ValueError(f"entry {url}: no media type")
        if not isinstance(raw["final_url"], str) or canonical_url(raw["final_url"]) != raw["final_url"]:
            raise ValueError(f"entry {url}: final_url is not canonical")
        entries[url] = SnapshotEntry(raw["hash"], raw["media_type"], raw["upstream_hash"], raw["final_url"],
                                     _urls(raw["imports"], f"{url} imports"),
                                     _urls(raw["dynamic"], f"{url} dynamic"))
    unavailable: dict[str, str] = {}
    for url, reason in index["unavailable"].items():
        if canonical_url(url) != url or not isinstance(reason, str) or not reason:
            raise ValueError(f"unavailable entry {url!r} is malformed")
        if url in entries:
            raise ValueError(f"{url} is both an entry and unavailable")
        unavailable[url] = reason
    if verify:
        modules = directory / "modules"
        hashes = {entry.hash for entry in entries.values()}
        present = {path.name for path in modules.iterdir()} if modules.is_dir() else set()
        if present != hashes:
            raise ValueError(f"modules/ differs from the index: {len(hashes - present)} missing, "
                             f"{len(present - hashes)} unreferenced")
        for digest in sorted(hashes):
            if hashlib.sha256((modules / digest).read_bytes()).hexdigest() != digest:
                raise ValueError(f"module file modules/{digest} does not match its hash")
    return Snapshot(index["contract"], roots, MappingProxyType(entries), MappingProxyType(unavailable))


def snapshot_index_bytes(snapshot: Snapshot) -> bytes:
    """The canonical ``index.json`` bytes of a snapshot."""
    index = {
        "schema": SNAPSHOT_SCHEMA,
        "contract": snapshot.contract,
        "roots": list(snapshot.roots),
        "entries": {url: {"hash": e.hash, "media_type": e.media_type, "upstream_hash": e.upstream_hash,
                          "final_url": e.final_url, "imports": list(e.imports), "dynamic": list(e.dynamic)}
                    for url, e in snapshot.entries.items()},
        "unavailable": dict(snapshot.unavailable),
    }
    return (json.dumps(index, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def read_builders(directory: Path) -> Mapping[str, str]:
    """``builders.json``: the snapshot entries a local esm.sh built, ``{url:
    "local"}`` (no file: none). Raises ValueError on a malformed file."""
    path = Path(directory) / BUILDERS_FILE
    if not path.is_file():
        return MappingProxyType({})
    record = json.loads(path.read_bytes())
    if (not isinstance(record, dict) or set(record) != {"schema", "image", "entries"} or record["schema"] != 1
            or not isinstance(record["image"], str) or not isinstance(record["entries"], dict)
            or any(not isinstance(url, str) or value != "local" for url, value in record["entries"].items())):
        raise ValueError(f"{BUILDERS_FILE} is malformed")
    if record["image"] != CONTRACT["esm_builder_image"]:
        raise ValueError(f"{BUILDERS_FILE} names {record['image']}, not the contract's {CONTRACT['esm_builder_image']}")
    return MappingProxyType(dict(record["entries"]))


def builder_problems(directory: Path, snapshot: Snapshot) -> list[str]:
    """``builders.json`` against the snapshot: well formed, the contract's
    image, and every locally built key an esm.sh entry of the snapshot."""
    try:
        built = read_builders(directory)
    except ValueError as error:
        return [str(error)]
    return [f"{BUILDERS_FILE} names a key the snapshot does not hold as an esm.sh entry: {url}"
            for url in sorted(built) if url not in snapshot.entries or not url.startswith(ESM_SH)]


# -- the shipped roots ---------------------------------------------------------
_CODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_UPSTREAM_LITERAL = re.compile(
    "([\"'`])((?:" + "|".join(re.escape(upstream) for upstream in UPSTREAMS) + r")[^\"'`\s]*)\1")
_RUNTIME_FILES = ("react.js", "session.js")


def _is_test_source(path: Path) -> bool:
    return (".test." in path.name or ".spec." in path.name
            or any(part in ("test", "tests", "__tests__") for part in path.parts))


def _upstream_literals(text: str) -> Iterable[str]:
    for match in _UPSTREAM_LITERAL.finditer(text):
        if "${" not in match.group(2):
            yield match.group(2)


def read_mirror_assets(package: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A renderer package's ``mirror_assets.json``: the files a loader fetches
    by computed name, as (bases, canonical asset URLs). Its shape::

        {"assets": [{"base": "https://cdn.jsdelivr.net/npm/pkg@1.2.3/dir/",
                     "files": ["a.js", "sub/b.css"]}]}
    """
    path = Path(package) / MIRROR_ASSETS
    if not path.is_file():
        return (), ()
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict) or set(document) != {"assets"} or not isinstance(document["assets"], list):
        raise ValueError(f"{path}: expected {{\"assets\": [...]}}")
    bases: list[str] = []
    urls: list[str] = []
    for group in document["assets"]:
        if not isinstance(group, dict) or set(group) != {"base", "files"}:
            raise ValueError(f"{path}: each asset group is {{\"base\", \"files\"}}")
        base, files = group["base"], group["files"]
        if not isinstance(base, str) or not base.endswith("/") or canonical_url(base + "x") != base + "x":
            raise ValueError(f"{path}: base must be a canonical upstream URL ending in '/': {base!r}")
        if (not isinstance(files, list) or not files or files != sorted(set(files))
                or not all(isinstance(name, str) for name in files)):
            raise ValueError(f"{path}: files under {base} must be a sorted, unique, non-empty list")
        for name in files:
            if name.startswith("/") or "?" in name or name != name.strip():
                raise ValueError(f"{path}: {name!r} is not a relative file path")
            urls.append(canonical_url(base + name))
        bases.append(base)
    return tuple(bases), tuple(urls)


def package_sources(package: Path) -> Iterable[Path]:
    """The shipped code files of a renderer or built-in package: its
    non-test JavaScript and TypeScript sources outside ``node_modules`` and
    ``derived``."""
    for path in sorted(Path(package).rglob("*")):
        rel = path.relative_to(package)
        if (path.is_file() and path.suffix in _CODE_SUFFIXES and not _is_test_source(rel)
                and "node_modules" not in rel.parts and "derived" not in rel.parts):
            yield path


def shipped_packages(workspace: Path) -> Iterable[Path]:
    """Every renderer and built-in package folder of the workspace
    (``content/renderers/*/*``, ``builtins/*/*``)."""
    for tree in SHIPPED_TREES:
        for package in sorted((Path(workspace) / tree).glob("*/*")):
            if package.is_dir():
                yield package


def _package_roots(package: Path) -> set[str]:
    bases, assets = read_mirror_assets(package)
    prefixes = set(bases) | {base.rstrip("/") for base in bases}
    roots = set(assets)
    for path in package_sources(package):
        for literal in _upstream_literals(path.read_text(encoding="utf-8")):
            if literal not in prefixes:
                roots.add(canonical_url(literal))
    return roots


def shipped_roots(workspace: Path) -> tuple[str, ...]:
    """Every upstream URL the shipped renderers and the runtime load: quoted
    ``https://esm.sh/…`` and ``https://cdn.jsdelivr.net/…`` literals (an
    import specifier is one) in the non-test sources of each package under
    ``content/renderers/`` and ``builtins/``, the files each package's
    ``mirror_assets.json`` names (a declared base is a prefix, not a root),
    and the imports and literals of ``sdk/shared/renderer-runtime/{react,session}.js``."""
    workspace = Path(workspace)
    roots: set[str] = set()
    for package in shipped_packages(workspace):
        roots |= _package_roots(package)
    for name in _RUNTIME_FILES:
        text = (workspace / RUNTIME_DIR / name).read_text(encoding="utf-8")
        for record in scan_imports(text):
            if record.name and record.name.startswith(UPSTREAMS):
                roots.add(canonical_url(record.name))
        roots.update(canonical_url(literal) for literal in _upstream_literals(text))
    return tuple(sorted(roots))


# -- licenses ------------------------------------------------------------------
_ESM_SH_PACKAGE = re.compile(r"((?:@[^/@?]+/)?[^/@?]+)@([^/?]+)")
_JSDELIVR_PACKAGE = re.compile(r"npm/((?:@[^/@?]+/)?[^/@?]+)@([^/?]+)")
# esm.sh's own Node built-in polyfills (/node/*.mjs) are esm.sh's source.
ESM_SH_POLYFILLS = "esm.sh/node"
_ESM_SH_LICENSE = {"license": "MIT", "source": "https://github.com/esm-dev/esm.sh"}
# Packages whose npm metadata names no license, read from the LICENSE file the
# package itself ships (the tarball at the recorded source).
LICENSE_FILES = {
    "khroma@2.1.0": "MIT",  # LICENSE: "The MIT License (MIT)", Fabio Spampinato, Andrew Maney
}


def package_of(url: str) -> str | None:
    """The ``name@version`` an exact-version module URL redistributes (the
    esm.sh polyfills are :data:`ESM_SH_POLYFILLS`), or None for esm.sh's own
    floating re-export stubs."""
    host, path, _ = _split(url)
    if host == "esm.sh":
        if path.startswith("node/"):
            return ESM_SH_POLYFILLS
        match = _ESM_SH_PACKAGE.match(path)
    else:
        match = _JSDELIVR_PACKAGE.match(path)
    if match is None:
        return None
    name, version = match.group(1), urllib.parse.unquote(match.group(2))
    return f"{name}@{version}" if _EXACT_VERSION.fullmatch(version) else None


def _license_expression(metadata: dict) -> str:
    value = metadata.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    if isinstance(value, str) and value.strip():
        return value.strip()
    legacy = metadata.get("licenses")
    if isinstance(legacy, list):
        names = [item.get("type") if isinstance(item, dict) else item for item in legacy]
        names = [name for name in names if isinstance(name, str) and name]
        if names:
            return names[0] if len(names) == 1 else "(" + " OR ".join(names) + ")"
    return "UNKNOWN"


# -- the freeze ----------------------------------------------------------------
class _Unavailable(Exception):
    """An upstream fetch that failed; ``retry`` marks the transient ones and
    ``status`` is the HTTP status when there was one."""

    def __init__(self, reason: str, *, retry: bool, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry = retry
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


def _builder_url(url: str, builder: str) -> str:
    """The local esm.sh URL for public ``https://esm.sh/<path>?<query>``."""
    if not url.startswith(ESM_SH):
        raise ValueError(f"only esm.sh builds have a local builder: {url}")
    return builder.rstrip("/") + "/" + url[len(ESM_SH):]


def _from_builder(location: str, builder: str) -> str:
    """A local builder's redirect ``Location`` on the public host: an absolute
    one under the builder base and a root-relative one name esm.sh paths."""
    base = builder.rstrip("/") + "/"
    if location.startswith(base):
        return ESM_SH + location[len(base):]
    if location.startswith("/") and not location.startswith("//"):
        return ESM_SH + location[1:]
    return location


def builder_tag() -> str:
    """The pinned local esm.sh release, the tag of ``esm_builder_image``."""
    return CONTRACT["esm_builder_image"].rsplit(":", 1)[1]


def check_esm_builder(builder: str) -> None:
    """Refuse a local builder that is not the contract's esm.sh release (its
    ``/status.json`` names the version it runs)."""
    try:
        with urllib.request.urlopen(builder.rstrip("/") + "/status.json",
                                    timeout=float(CONTRACT["timeout_seconds"])) as response:
            version = json.loads(response.read(65536)).get("version")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise SystemExit(f"the local esm.sh at {builder} does not answer /status.json: {error}") from None
    if version != builder_tag():
        raise SystemExit(f"the local esm.sh at {builder} runs {version!r}, not {builder_tag()!r}: run "
                         f"docker run -d -p 127.0.0.1:18080:80 {CONTRACT['esm_builder_image']}")


def _get_once(url: str, builder: str | None = None) -> tuple[bytes, str, str]:
    """One upstream GET: (body, media type, final URL), following redirects
    manually through :func:`check_redirect`. With ``builder`` the request goes
    to that local esm.sh, while every URL, redirects included, stays a public
    ``https://esm.sh/`` one."""
    current = url
    for _ in range(int(CONTRACT["max_redirects"]) + 1):
        target = _builder_url(current, builder) if builder else current
        request = urllib.request.Request(target, headers={"User-Agent": CONTRACT["user_agent"], "Accept": "*/*"})
        try:
            response = _OPENER.open(request, timeout=float(CONTRACT["timeout_seconds"]))
        except urllib.error.HTTPError as error:
            if error.code in _REDIRECTS and error.headers.get("Location"):
                location = error.headers["Location"]
                current = check_redirect(_from_builder(location, builder) if builder else location, current)
                if builder and not current.startswith(ESM_SH):
                    raise _Unavailable(f"local builder redirects off esm.sh: {location}", retry=False) from None
                continue
            detail = error.read(300).decode("utf-8", "replace").strip().splitlines()
            reason = ("local builder " if builder else "") + f"HTTP {error.code}" + (
                f": {detail[0][:200]}" if detail else "")
            raise _Unavailable(reason, retry=error.code >= 500 or error.code == 429, status=error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _Unavailable(f"network: {error}", retry=True) from None
        with response:
            limit = int(CONTRACT["max_module_bytes"])
            try:
                body = response.read(limit + 1)
            except (TimeoutError, OSError) as error:
                raise _Unavailable(f"network: {error}", retry=True) from None
            if len(body) > limit:
                raise _Unavailable(f"larger than {limit} bytes", retry=False)
            media_type = response.headers.get("Content-Type", "application/octet-stream").strip()
        return body, media_type, current
    raise _Unavailable("too many redirects", retry=False)


def _get(url: str, retries: int, builder: str | None = None) -> tuple[bytes, str, str]:
    for attempt in range(retries + 1):
        try:
            return _get_once(url, builder)
        except _Unavailable as failure:
            if not failure.retry or attempt == retries:
                raise
            time.sleep(min(30.0, 2.0 ** attempt) + random.random())
    raise AssertionError("unreachable")


def _registry_metadata(package: str, retries: int) -> dict:
    name, _, version = package.rpartition("@")
    url = "https://registry.npmjs.org/" + name.replace("/", "%2F") + "/" + version
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": CONTRACT["user_agent"], "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=float(CONTRACT["timeout_seconds"])) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise _Unavailable(f"npm registry HTTP {error.code} for {package}", retry=False) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass
        if attempt < retries:
            time.sleep(min(30.0, 2.0 ** attempt) + random.random())
    raise _Unavailable(f"npm registry unreachable for {package}", retry=True)


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def freeze(workspace: Path, *, workers: int = 16, retries: int = 4, refresh: bool = False,
           refresh_floating: bool = False, rounds: int = 3, round_delay: float = 60.0,
           esm_builder: str | None = None, log: Callable[[str], None] = print) -> Snapshot:
    """Walk the shipped graph upstream and write the workspace's ``module-snapshot/``.

    Starts from :func:`shipped_roots` and follows every static and literal
    dynamic edge. Unless ``refresh``, entries of the existing snapshot (same
    contract) are kept without a fetch, so a re-run only completes what is
    missing; ``refresh`` re-resolves everything, and ``refresh_floating``
    only the floating keys (:func:`is_floating`), so every range in the graph
    resolves at one moment and a package shared through ranges stays one
    version (:func:`instance_problems`).
    Each fetch retries transient failures with backoff; whatever still fails
    is retried in up to ``rounds`` later passes spaced ``round_delay``
    seconds apart. A refetched key that still fails keeps its existing
    entry; one with none is recorded under ``unavailable`` with its reason.
    The licenses of every exact package come from npm registry metadata.

    With ``esm_builder`` (the base URL of a local esm.sh of the contract's
    ``esm_builder_image``), an esm.sh URL public esm.sh answers with a 5xx
    after its retries is fetched from the local builder under the same key,
    and so is every not-yet-frozen URL a locally built module reaches; the
    locally built keys are recorded in ``builders.json``.
    """
    workspace = Path(workspace)
    directory = workspace / SNAPSHOT_DIR
    if esm_builder:
        check_esm_builder(esm_builder)
    roots = shipped_roots(workspace)
    kept: Mapping[str, SnapshotEntry] = {}
    previous_licenses: dict = {}
    previous_local: set[str] = set()
    if (directory / "index.json").is_file():
        try:
            old = read_snapshot(directory, verify=True)
            if old.contract == contract_hash():
                kept = old.entries
                previous_licenses = json.loads((directory / "LICENSES.json").read_bytes()).get("packages", {})
                previous_local = set(read_builders(directory))
        except (ValueError, OSError):
            kept = {}
    previous = {url: entry for url, entry in kept.items()
                if not refresh and not (refresh_floating and is_floating(url))}
    bodies: dict[str, bytes] = {}
    entries: dict[str, SnapshotEntry] = {}
    failures: dict[str, str] = {}
    local: set[str] = set()  # the entries of this snapshot a local esm.sh built
    builder_scope: set[str] = set()  # not-yet-frozen keys a locally built module reaches
    lock = threading.Lock()

    def reuse(url: str, entry: SnapshotEntry) -> tuple[str, SnapshotEntry, None, bool]:
        body = (directory / "modules" / entry.hash).read_bytes()
        with lock:
            bodies[entry.hash] = body
        return url, entry, None, url in previous_local

    def fetch(url: str) -> tuple[bytes, str, str, bool]:
        """The body from public upstream, or from the local builder for a key
        a locally built module reaches or one public esm.sh cannot build."""
        if esm_builder and url in builder_scope:
            return *_get(url, retries, esm_builder), True
        try:
            return *_get(url, retries), False
        except _Unavailable as failure:
            if not esm_builder or not url.startswith(ESM_SH) or (failure.status or 0) < 500:
                raise
            try:
                return *_get(url, retries, esm_builder), True
            except _Unavailable as local_failure:
                raise _Unavailable(f"{failure.reason}; {local_failure.reason}", retry=local_failure.retry,
                                   status=local_failure.status) from None

    def visit(url: str) -> tuple[str, SnapshotEntry | None, str | None, bool]:
        if url in previous:
            return reuse(url, previous[url])
        try:
            raw, media_type, final_url, built_locally = fetch(url)
            body, rewrite = served_body(raw, media_type, url=url, final_url=final_url)
        except _Unavailable as failure:
            return reuse(url, kept[url]) if url in kept else (url, None, failure.reason, False)
        except (LexError, ValueError) as error:
            return url, None, f"unservable: {error}", False
        digest = hashlib.sha256(body).hexdigest()
        with lock:
            bodies[digest] = body
        return url, SnapshotEntry(digest, media_type, hashlib.sha256(raw).hexdigest(), final_url,
                                  rewrite.imports if rewrite else (), rewrite.dynamic if rewrite else ()), \
            None, built_locally

    def walk(frontier: list[str], seen: set[str]) -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while frontier:
                following: list[str] = []
                for url, entry, reason, built_locally in pool.map(visit, frontier):
                    if entry is None:
                        failures[url] = reason or "unknown failure"
                        continue
                    failures.pop(url, None)
                    entries[url] = entry
                    if built_locally:
                        local.add(url)
                    for target in entry.imports + entry.dynamic:
                        # One builder per subgraph: what a local build reaches and
                        # is not frozen yet is built locally too.
                        if built_locally and target not in entries and target not in previous:
                            builder_scope.add(target)
                        if target not in seen:
                            seen.add(target)
                            following.append(target)
                log(f"freeze: {len(entries)} modules, {len(failures)} failing, {len(following)} queued")
                frontier = sorted(following)

    seen = set(roots)
    walk(sorted(roots), seen)
    for attempt in range(rounds):
        if not failures:
            break
        log(f"freeze: retry round {attempt + 1}/{rounds} in {round_delay:.0f}s for {len(failures)} URL(s)")
        time.sleep(round_delay)
        walk(sorted(failures), seen)

    licenses: dict[str, dict] = {}
    packages = sorted({package for url in entries if (package := package_of(url))})
    for package in packages:
        if package == ESM_SH_POLYFILLS:
            licenses[package] = dict(_ESM_SH_LICENSE)
        elif package in previous_licenses and license_allowed(str(previous_licenses[package].get("license"))):
            licenses[package] = previous_licenses[package]  # a refused or unknown one is looked up again
    missing = [package for package in packages if package not in licenses]

    def license_of(package: str) -> tuple[str, dict]:
        try:
            metadata = _registry_metadata(package, retries)
        except _Unavailable as failure:
            return package, {"license": "UNKNOWN", "source": failure.reason}
        tarball = (metadata.get("dist") or {}).get("tarball") or f"https://www.npmjs.com/package/{package}"
        expression = _license_expression(metadata)
        if expression == "UNKNOWN" and package in LICENSE_FILES:
            expression = LICENSE_FILES[package]
        return package, {"license": expression, "source": tarball}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        licenses.update(pool.map(license_of, missing))

    snapshot = Snapshot(contract_hash(), roots, MappingProxyType(dict(sorted(entries.items()))),
                        MappingProxyType(dict(sorted(failures.items()))))
    modules = directory / "modules"
    modules.mkdir(parents=True, exist_ok=True)
    referenced = {entry.hash for entry in entries.values()}
    for digest in sorted(referenced):
        target = modules / digest
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            _write_atomic(target, bodies[digest])
    for stale in modules.iterdir():
        if stale.name not in referenced:
            stale.unlink()
    _write_atomic(directory / "LICENSES.json",
                  _json_bytes({"schema": 1, "packages": dict(sorted(licenses.items()))}))
    built = sorted(url for url in entries if url in local)
    _write_atomic(directory / BUILDERS_FILE, _json_bytes({
        "schema": 1, "image": CONTRACT["esm_builder_image"], "entries": {url: "local" for url in built}}))
    _write_atomic(directory / "index.json", snapshot_index_bytes(snapshot))
    log(f"freeze: wrote {len(entries)} modules ({sum(len(bodies[d]) for d in referenced)} bytes), "
        f"{len(failures)} unavailable, {len(built)} built locally, {len(licenses)} packages")
    return snapshot


# -- checks --------------------------------------------------------------------
# SPDX identifiers the shipped graph may redistribute; an OR expression needs
# one allowed member, an AND expression all of them.
ALLOWED_LICENSES = frozenset({
    "MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "MPL-2.0", "EPL-2.0",
    "0BSD", "CC0-1.0", "Unlicense", "Zlib", "Python-2.0",
})
_SPDX_TOKEN = re.compile(r"\s*(\(|\)|[^\s()]+)")


def license_allowed(expression: str) -> bool:
    """Whether an SPDX expression is satisfied by :data:`ALLOWED_LICENSES`."""
    tokens = [match.group(1) for match in _SPDX_TOKEN.finditer(expression)]
    position = 0

    def parse_or() -> bool:
        nonlocal position
        value = parse_and()
        while position < len(tokens) and tokens[position].upper() == "OR":
            position += 1
            value = parse_and() or value
        return value

    def parse_and() -> bool:
        nonlocal position
        value = parse_atom()
        while position < len(tokens) and tokens[position].upper() == "AND":
            position += 1
            value = parse_atom() and value
        return value

    def parse_atom() -> bool:
        nonlocal position
        if position >= len(tokens):
            raise ValueError("truncated")
        token = tokens[position]
        position += 1
        if token == "(":
            value = parse_or()
            if position >= len(tokens) or tokens[position] != ")":
                raise ValueError("unbalanced")
            position += 1
            return value
        if token in ("(", ")") or token.upper() in ("OR", "AND", "WITH"):
            raise ValueError(f"unexpected {token}")
        return token in ALLOWED_LICENSES

    try:
        value = parse_or()
    except ValueError:
        return False
    return value and position == len(tokens)


def closure_problems(snapshot: Snapshot) -> list[str]:
    """Roots missing from the entries (a root is never a tolerated gap: a
    local esm.sh can build what public esm.sh cannot), and static or dynamic
    edges in neither the entries nor ``unavailable``. An edge the freeze could
    not fetch is recorded under ``unavailable`` with its reason: a reported
    gap (:func:`unavailable_edges`), not a hole in the graph."""
    problems = []
    for root in snapshot.roots:
        if root not in snapshot.entries:
            problems.append(f"root not frozen: {root} ({snapshot.unavailable.get(root, 'absent')})")
    for url, entry in snapshot.entries.items():
        for target in entry.imports:
            if target not in snapshot.entries and target not in snapshot.unavailable:
                problems.append(f"static edge neither frozen nor unavailable: {target} <- {url}")
        for target in entry.dynamic:
            if target not in snapshot.entries and target not in snapshot.unavailable:
                problems.append(f"dynamic edge neither frozen nor unavailable: {target} <- {url}")
    return problems


def unavailable_problems(snapshot: Snapshot) -> list[str]:
    """Every URL the last freeze could not fetch, with its reason. Each is a
    gap the next :func:`freeze` that reaches upstream (or a local esm.sh)
    fills."""
    return [f"unavailable upstream: {url}: {reason}" for url, reason in sorted(snapshot.unavailable.items())]


def unavailable_edges(snapshot: Snapshot) -> list[str]:
    """The tolerated gaps ``check`` reports without failing: unavailable URLs
    that are edges, not roots (an unavailable root is a
    :func:`closure_problems` hole)."""
    return [f"unavailable edge (tolerated): {url}: {reason}"
            for url, reason in sorted(snapshot.unavailable.items()) if url not in snapshot.roots]


# Packages the graph must load once. Two versions of one are two module
# instances: React's hook dispatcher, ProseMirror's and CodeMirror's classes
# and facets, a plugin registry. A name ending in "/" is a scope, one ending
# in "-" a prefix. The renderers pin each family with one ``deps=`` list per
# file (``tests/test_renderer_cdn_pins.py``); this holds what the frozen graph
# resolved: one version of each, whatever the ranges it did not pin declare.
SINGLE_INSTANCE = ("react", "react-dom", "scheduler", "@assistant-ui/", "@milkdown/", "prosemirror-",
                   "@codemirror/", "@lezer/", "@embedpdf/", "@xterm/")


def _single_instance(name: str) -> bool:
    return any(name == pattern or (pattern.endswith(("/", "-")) and name.startswith(pattern))
               for pattern in SINGLE_INSTANCE)


def instance_problems(snapshot: Snapshot) -> list[str]:
    """Each :data:`SINGLE_INSTANCE` package the frozen graph holds at more
    than one exact version."""
    versions: dict[str, set[str]] = {}
    for url in snapshot.entries:
        package = package_of(url)
        if package and package != ESM_SH_POLYFILLS:
            name, _, version = package.rpartition("@")
            if _single_instance(name):
                versions.setdefault(name, set()).add(version)
    return [f"two module instances: {name} resolves to {', '.join(sorted(found))} in the frozen graph"
            for name, found in sorted(versions.items()) if len(found) > 1]


def license_problems(directory: Path, snapshot: Snapshot) -> list[str]:
    """Packages without a LICENSES.json record, and records whose license is
    not allowed."""
    document = json.loads((Path(directory) / "LICENSES.json").read_bytes())
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, dict) or document.get("schema") != 1:
        return ["LICENSES.json is not {\"schema\": 1, \"packages\": {...}}"]
    problems = []
    needed = {package for url in snapshot.entries if (package := package_of(url))}
    for package in sorted(needed - set(packages)):
        problems.append(f"no license recorded for {package}")
    for package in sorted(set(packages) - needed):
        problems.append(f"license recorded for a package the snapshot does not ship: {package}")
    for package, record in sorted(packages.items()):
        license_name = record.get("license") if isinstance(record, dict) else None
        if not isinstance(license_name, str) or not license_allowed(license_name):
            problems.append(f"license not allowed: {package}: {license_name!r}")
    return problems


def snapshot_problems(workspace: Path) -> list[str]:
    """Everything ``check`` verifies: the snapshot's shape and file hashes,
    its contract, its roots against the sources, its closure (every root
    frozen; an edge frozen or a reported ``unavailable`` gap, which
    :func:`unavailable_edges` lists), one version of each single-instance
    package, its size budget, its licenses and its ``builders.json``."""
    workspace = Path(workspace)
    directory = workspace / SNAPSHOT_DIR
    try:
        snapshot = read_snapshot(directory, verify=True)
    except ValueError as error:
        return [str(error)]
    problems = []
    if (directory / "index.json").read_bytes() != snapshot_index_bytes(snapshot):
        problems.append("index.json is not in canonical form")
    if snapshot.contract != contract_hash():
        problems.append(f"snapshot contract {snapshot.contract} is not the current contract; run {FREEZE_COMMAND}")
    roots = shipped_roots(workspace)
    if snapshot.roots != roots:
        added = sorted(set(roots) - set(snapshot.roots))
        removed = sorted(set(snapshot.roots) - set(roots))
        problems.append(f"snapshot roots differ from the shipped sources (+{added} -{removed}); run {FREEZE_COMMAND}")
    problems += closure_problems(snapshot)
    problems += instance_problems(snapshot)
    size = sum((directory / "modules" / digest).stat().st_size
               for digest in {entry.hash for entry in snapshot.entries.values()})
    if size > int(CONTRACT["snapshot_max_bytes"]):
        problems.append(f"snapshot is {size} bytes, over the {CONTRACT['snapshot_max_bytes']}-byte budget")
    problems += license_problems(directory, snapshot)
    problems += builder_problems(directory, snapshot)
    return problems


def _workspace_root() -> Path:
    """The workspace checkout this SDK member sits in, which holds the
    snapshot and the trees it is frozen from."""
    root = Path(__file__).resolve().parents[4]
    if not all((root / path).is_dir() for path in (*SHIPPED_TREES, RUNTIME_DIR)):
        raise FileNotFoundError(f"the module snapshot is frozen and checked in a workspace checkout, not {root}")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m collimate._host.module_mirror", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    freeze_parser = commands.add_parser("freeze", help="walk the shipped graph upstream and write the snapshot")
    freeze_parser.add_argument("--refresh", action="store_true", help="refetch every module, not only missing ones")
    freeze_parser.add_argument("--workers", type=int, default=16)
    builder_help = ("base URL of a local esm.sh of the contract's esm_builder_image (docker run -d -p "
                    "127.0.0.1:18080:80 " + CONTRACT["esm_builder_image"] + "), asked for what public esm.sh "
                    "answers with a 5xx")
    freeze_parser.add_argument("--esm-builder", metavar="URL", help=builder_help)
    commands.add_parser("check", help="verify the snapshot against the sources")
    bump_parser = commands.add_parser("bump", help="move the shipped exact pins to newer releases and refreeze")
    bump_parser.add_argument("--major", action="store_true", help="also apply major moves")
    bump_parser.add_argument("--dry-run", action="store_true", help="print the table only")
    bump_parser.add_argument("--only", nargs="+", default=[], metavar="PACKAGE",
                             help="move only these packages (and their families)")
    bump_parser.add_argument("--esm-builder", metavar="URL", help=builder_help)
    args = parser.parse_args(argv)
    try:
        root = _workspace_root()
    except FileNotFoundError as error:
        raise SystemExit(str(error)) from error
    if args.command == "bump":
        from .module_bump import bump
        return bump(root, allow_major=args.major, dry_run=args.dry_run, only=args.only,
                    esm_builder=args.esm_builder)
    if args.command == "freeze":
        freeze(root, workers=args.workers, refresh=args.refresh, esm_builder=args.esm_builder)
    problems = snapshot_problems(root)
    for problem in problems:
        print(problem)
    try:
        for note in unavailable_edges(read_snapshot(root / SNAPSHOT_DIR)):
            print(note)
    except ValueError:
        pass  # already reported as a problem
    if not problems:
        print("module snapshot: complete")
        if args.command == "freeze":
            print(f"module snapshot: now {PUBLISH_COMMAND}, and push the module-snapshot branch")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
