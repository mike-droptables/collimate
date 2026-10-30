"""The codebase map (docs/codemap) against the code it describes.

The map's Interfaces view is the reference for the HTTP surface, and the
committed page, ``codebase-map.html``, is what a reader opens. After the
cookie session route was deleted (F1945), the cell-file capability added
(F1941), the cell header's meta PATCH added (F0546) and
``make_authed_ipc_handler`` moved into ``backends.py`` (F0128), the map's
data files were corrected but the page was not rebuilt, so it still showed
the retired surface. These tests hold the map to the code:

- the HTTP API node lists exactly the routes the app serves, and every route
  count the map states agrees with that list;
- the contract counts (IPC ops, SSE event types, renderer api members,
  OpenAPI operations and paths) are no data file's literals: ``build.mjs``
  reads them from the contracts, substitutes each ``{{name}}`` and stamps
  them into the page, and the test holds the stamp to the contracts; the
  IPC, SSE and renderer api nodes list exactly the ops, events and members;
- a file reference whose note names symbols points at the file that defines
  them;
- the committed page was built from the current sources: ``build.mjs``
  stamps their digest into it, and the test recomputes it.

``docs/`` is not part of the public mirror (``.github/sync-exclude.txt``),
so the tests skip where the map is absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CODEMAP = REPO / "docs" / "codemap"
DATA_FILES = ("arch.ts", "concepts.ts", "interfaces.ts")
REBUILD = "rebuild it: cd docs/codemap && npm ci && node build.mjs"
OPENAPI = REPO / "api" / "tests" / "_snapshots" / "openapi.json"

pytestmark = pytest.mark.skipif(
    not CODEMAP.is_dir(), reason="docs/ is not part of the public mirror",
)

_STRING = re.compile(r"'((?:[^'\\\n]|\\.)*)'")
_GAP = re.compile(r"\s*,?\s*")


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", r"\1", literal)


def _data(name: str) -> str:
    return (CODEMAP / "data" / name).read_text(encoding="utf-8")


def _page_counts() -> dict[str, int]:
    """The counts the committed page states: build.mjs read them from the
    contracts and stamped them into its ``codemap-counts`` meta."""
    page = (CODEMAP / "codebase-map.html").read_text(encoding="utf-8")
    stamp = re.search(r'<meta name="codemap-counts" content="([^"]*)">', page)
    assert stamp, f"codebase-map.html carries no counts; {REBUILD}"
    return {key: int(n) for key, n in (pair.split("=", 1) for pair in stamp.group(1).split())}


def _stated(name: str) -> str:
    """A data file as the page states it: each ``{{count}}`` is the number
    the page was built with."""
    counts = _page_counts()

    def count(m: re.Match) -> str:
        assert m.group(1) in counts, f"{name}: the page carries no count {m.group(1)}; {REBUILD}"
        return str(counts[m.group(1)])
    return re.sub(r"\{\{(\w+)\}\}", count, _data(name))


def _expect(pattern: str, src: str, at: int) -> re.Match:
    m = re.compile(pattern).match(src, at)
    assert m, f"unexpected map data at {src[at:at + 60]!r} (wanted {pattern!r})"
    return m


def _items(src: str, node_id: str) -> list[tuple[str, list[str]]]:
    """A node's ``items`` as ``[(title, entries)]``, read from the
    ``{ title: '…', entries: ['…', …] }`` literals the data files use."""
    at = src.index("items: [", src.index(f"id: '{node_id}'")) + len("items: [")
    groups: list[tuple[str, list[str]]] = []
    while True:
        at = _GAP.match(src, at).end()
        if src[at] == "]":
            return groups
        title = _expect(r"\{\s*title:\s*" + _STRING.pattern, src, at)
        at = _expect(r"\s*,\s*entries:\s*\[", src, title.end()).end()
        entries: list[str] = []
        while True:
            at = _GAP.match(src, at).end()
            if src[at] == "]":
                break
            entry = _expect(_STRING.pattern, src, at)
            entries.append(_unescape(entry.group(1)))
            at = entry.end()
        at = _expect(r"\]\s*\}", src, at).end()
        groups.append((_unescape(title.group(1)), entries))


def _caption(src: str, node_id: str) -> str:
    m = re.search(r"id: '" + re.escape(node_id) + r"'.*?caption: " + _STRING.pattern, src, re.S)
    assert m, f"no caption on {node_id}"
    return _unescape(m.group(1))


_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WS"}


def _routes_of(entry: str) -> set[tuple[str, str]]:
    """``(verb, path)`` for each route an entry names ahead of its note:
    ``GET /a — …``, ``GET / · GET /b``, ``GET/POST /c``, ``WS /d``,
    ``mount / → …`` (verb ``MOUNT``)."""
    head = entry.split(" — ", 1)[0].split(" → ", 1)[0]
    routes = set()
    for part in head.split(" · "):
        verbs, _, path = part.strip().partition(" ")
        assert path.startswith("/"), f"not a route entry: {entry!r}"
        if verbs == "mount":
            routes.add(("MOUNT", path))
            continue
        for verb in verbs.split("/"):
            assert verb in _VERBS, f"not a route entry: {entry!r}"
            routes.add((verb, path))
    return routes


def _served(app) -> tuple[set[tuple[str, str]], set[str]]:
    """Every ``(verb, path)`` the app registers (``{name:path}`` written
    ``{name}``, as the map writes it), and the routers they come from."""
    from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
    from starlette.routing import Mount

    routes: set[tuple[str, str]] = set()
    routers: set[str] = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        path = re.sub(r"\{(\w+):\w+\}", r"{\1}", getattr(route, "path", ""))
        if isinstance(route, APIRoute):
            # The map's premise: every router is mounted without a prefix,
            # so the decorator path is the served path.
            assert context.path == route.path, f"{route.path} is served under a prefix"
            routes.update((method, path) for method in route.methods)
        elif isinstance(route, APIWebSocketRoute):
            routes.add(("WS", path))
        elif isinstance(route, Mount):
            routes.add(("MOUNT", path or "/"))
        else:
            continue  # FastAPI's own /openapi.json and /docs pages
        module = getattr(getattr(route, "endpoint", None), "__module__", "") or ""
        if module.startswith("routes."):
            routers.add(module.split(".")[1])
    return routes, routers


# main.py registers these only over a built UI (api/static), which a test
# checkout may lack; they are compared with the app only where it is built.
_UI_ROUTES = {("GET", "/"), ("GET", "/index.html"), ("MOUNT", "/")}


def test_http_node_lists_exactly_the_routes_the_app_serves__F1945_F0546_F1941(api_app):
    from auth_middleware import _CAP_FILE_PATH

    groups = _items(_data("interfaces.ts"), "i:http")
    listed = set().union(*(_routes_of(e) for _, entries in groups for e in entries))
    served, _ = _served(api_app)

    # The capability route is OriginGuard's, not a router's: it rewrites
    # /api/cellcap/<cap>/<cell-file path> onto the plain cell-file route.
    cap_routes = {(verb, path) for verb, path in listed if _CAP_FILE_PATH.match(path)}
    assert cap_routes, "the map lists no cell-file capability route"
    for verb, path in cap_routes:
        assert (verb, "/api/" + path.split("/", 4)[4]) in served, path

    listed -= cap_routes
    assert _UI_ROUTES <= listed
    if not os.path.isdir(sys.modules["main"]._static_dir):
        listed -= _UI_ROUTES
    assert sorted(listed - served) == [], "the map lists routes the app does not serve"
    assert sorted(served - listed) == [], "the app serves routes the map does not list"


def test_every_route_count_the_map_states_matches_its_route_list__F1945_F0546_F1941(api_app):
    from auth_middleware import _CAP_FILE_PATH

    src = _data("interfaces.ts")
    groups = _items(src, "i:http")
    endpoints: set[tuple[str, str]] = set()
    for title, entries in groups:
        routes = {r for e in entries for r in _routes_of(e)
                  if r[0] != "MOUNT" and not _CAP_FILE_PATH.match(r[1])}
        endpoints |= routes
        counts = {name: int(n) for name, n in re.findall(r"([\w./]+) \((\d+)\)", title)}
        socket_count = counts.pop("WebSockets", None)
        if socket_count is not None:
            assert socket_count == sum(verb == "WS" for verb, _ in routes), title
        if counts:
            assert sum(counts.values()) == sum(verb != "WS" for verb, _ in routes), title

    http = sum(verb != "WS" for verb, _ in endpoints)
    sockets = len(endpoints) - http
    streams = sum(e.split(" — ", 1)[1:] == ["SSE"] for _, entries in groups for e in entries)
    _, routers = _served(api_app)
    assert _caption(src, "i:http") == (
        f"{len(endpoints)} endpoints · {len(routers)} routers · {streams} SSE · {sockets} WebSocket"
    )
    text = "\n".join(_stated(name) for name in DATA_FILES)
    assert {int(n) for n in re.findall(r"\b(\d+) endpoint", text)} == {len(endpoints)}
    assert {int(n) for n in re.findall(r"\((\d+) HTTP\b", text)} == {http}
    stated = re.findall(r"\b(\d+) routers\b|include_router × (\d+)", text)
    assert {int(a or b) for a, b in stated} == {len(routers)}

    # The OpenAPI counts are build.mjs's substitutions: what the page states.
    snapshot = json.loads(OPENAPI.read_text())
    operations = sum(len(methods) for methods in snapshot["paths"].values())
    assert re.findall(r"(\d+) operations are OpenAPI-visible", text) == [str(operations)]
    assert re.findall(r"pins (\d+) operations over (\d+) paths", text) == [
        (str(operations), str(len(snapshot["paths"])))
    ]


def _renderer_members() -> list[str]:
    """The members of the SDK's ``RendererAPI``, read line-wise (build.mjs
    reads them structurally; the counts test compares the two)."""
    source = _sdk_file("packages/renderer-hooks/src/types.ts").read_text(encoding="utf-8")
    body = source.split("export interface RendererAPI {", 1)[1].split("\n}", 1)[0]
    return re.findall(r"^    ([A-Za-z_$][\w$]*)\??[(:]", body, re.M)


def _contract_counts() -> dict[str, int]:
    """build.mjs's COUNTS, from the sources its contracts are generated from."""
    from collimate._host.channel_events import EVENT_TYPES
    from collimate._host.ipc import OPS

    kinds = Counter(spec.kind for spec in OPS.values())
    paths = json.loads(OPENAPI.read_text())["paths"]
    return {
        "ops": len(OPS),
        "requestOps": kinds["request"],
        "notifyOps": kinds["notify"],
        "envOps": sum(op.startswith("env_") for op in OPS),
        "events": len(EVENT_TYPES),
        "rendererMembers": len(_renderer_members()),
        "httpOps": sum(len(methods) for methods in paths.values()),
        "httpPaths": len(paths),
    }


def test_the_counts_the_page_states_are_the_contracts():
    assert _page_counts() == _contract_counts(), (
        f"the page states other counts than the contracts: run python3 sdk/scripts/sync_contracts.py, then {REBUILD}"
    )


# The phrasings in which a data file states a contract count. Each is a
# placeholder build.mjs fills ({{ops}}, {{envOps}}, {{requestOps}},
# {{notifyOps}}, {{events}}, {{rendererMembers}}, {{httpOps}},
# {{httpPaths}}); a number written there by hand is one the next op, event,
# member or route makes wrong.
_HAND_COUNTS = (
    re.compile(r"\b\d+ (?:IPC |generator )?ops\b"),
    re.compile(r"\b\d+ (?:requests|notifications|notify ops)\b"),
    re.compile(r"\b\d+ (?:SSE |channel )?(?:event types?|events|records)\b"),
    re.compile(r"\b\d+ (?:renderer |api )?members\b"),
    re.compile(r"\b\d+ (?:OpenAPI(?:-visible)? )?operations\b"),
    re.compile(r"\b\d+ (?:OpenAPI )?paths\b"),
)


def test_no_data_file_states_a_contract_count_by_hand():
    found = [f"{name}: {m.group(0)!r}" for name in DATA_FILES
             for pattern in _HAND_COUNTS for m in pattern.finditer(_data(name))]
    assert found == [], "write the count's {{placeholder}} (build.mjs COUNTS), not its number"


def test_the_contract_nodes_list_every_op_event_and_member():
    from collimate._host.channel_events import EVENT_TYPES
    from collimate._host.ipc import OPS

    def names(entries: list[str]) -> list[str]:
        return [re.match(r"[A-Za-z_]\w*", e).group(0) for e in entries]

    src = _data("interfaces.ts")
    ipc = dict(_items(src, "i:ipc"))
    listed = names([e for title, entries in ipc.items()
                    if title not in ("Host to generator", "Run environment") for e in entries])
    assert sorted(listed) == sorted(OPS), "the IPC node lists other ops than collimate._host.ipc.OPS"

    (events,) = [entries for title, entries in _items(src, "i:sse") if title.startswith("Event types")]
    assert sorted(names(events)) == sorted(EVENT_TYPES), "the SSE node lists other events than EVENT_TYPES"

    groups = [(title, entries) for title, entries in _items(src, "i:renderer-api")
              if re.fullmatch(r"(?:api|native-only) members .*", title)]
    assert len(groups) == 2
    for title, entries in groups:
        assert title.endswith(f"({len(entries)})"), title
    members = names([e for _, entries in groups for e in entries])
    assert sorted(members) == sorted(_renderer_members()), (
        "the renderer api node lists other members than RendererAPI")


def test_accounts_node_lists_every_harness_rule_and_plan_login():
    from collimate.account_taxonomy import MODEL_HARNESS_RULES, PLAN_LOGINS

    src = _data("interfaces.ts")
    groups = _items(src, "i:accounts")
    caption = _caption(src, "i:accounts")
    for prefix, rows, stated in (("MODEL_HARNESS_RULES", MODEL_HARNESS_RULES, "harness rules"),
                                 ("PLAN_LOGINS rows", PLAN_LOGINS, "plan logins")):
        (title, entries), = [(t, e) for t, e in groups if t.startswith(prefix + " (")]
        listed = [e.split(" — ", 1)[0] for e in entries if not e.startswith("(")]
        assert sorted(listed) == sorted(rows), f"{title} lists other harnesses than account_taxonomy"
        assert title == f"{prefix} ({len(rows)})"
        assert f"{len(rows)} {stated}" in caption.split(" · "), caption


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_APP_REF = re.compile(r"\bapp\(\s*'([^']+)'\s*,\s*" + _STRING.pattern + r"\s*\)")
_PY_DEFS = (
    re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)", re.M),
    re.compile(r"^[ \t]*class[ \t]+(\w+)", re.M),
    re.compile(r"^(\w+)[ \t]*(?::[^=\n]*)?=(?!=)", re.M),
)
_TS_DEFS = (re.compile(r"\b(?:function\*?|class|interface|type|enum|const|let|var)\s+(\w+)"),)


def _sources(root: str, suffixes: tuple[str, ...], skip: set[str]) -> list[Path]:
    """The app's own source files under ``root``: no tests, no vendored or
    installed trees."""
    found = []
    for folder, dirs, files in os.walk(REPO / root):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        found += [Path(folder, f) for f in files if f.endswith(suffixes) and ".test." not in f]
    return found


def _definitions(paths: list[Path], patterns) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in patterns:
            for m in pattern.finditer(text):
                owners[m.group(1)].add(path.relative_to(REPO).as_posix())
    return owners


def test_symbols_a_file_reference_names_are_defined_in_that_file__F0128():
    python = _sources("api", (".py",), {"tests", "tests_live", "static", "__pycache__"})
    typescript = [p for root in ("frontend/src", "desktop/src")
                  for p in _sources(root, (".ts", ".tsx"), {"__tests__"})]
    owners = {".py": _definitions(python, _PY_DEFS), ".ts": _definitions(typescript, _TS_DEFS)}
    owners[".tsx"] = owners[".ts"]

    checked = []
    for name in DATA_FILES:
        for path, note in _APP_REF.findall(_data(name)):
            symbols = [s.strip() for s in _unescape(note).split(",")]
            index = owners.get(Path(path).suffix)
            if index is None or not all(_IDENTIFIER.fullmatch(s) for s in symbols):
                continue
            for symbol in symbols:
                # A name the app defines must be defined in the file the map
                # attributes it to. (Notes that name tables or routes match
                # no definition and are not symbols.)
                if symbol in index:
                    assert path in index[symbol], (
                        f"{name}: {symbol} is noted on {path} but defined in {sorted(index[symbol])}"
                    )
                    checked.append(symbol)
    assert "make_authed_ipc_handler" in checked


def _sources_digest() -> str:
    """build.mjs's stamp: sha256 over every .ts/.tsx/.css file in the map's
    folder and data/, plus build.mjs, tsconfig.json and package-lock.json,
    in sorted path order, each as ``<path>\\0<content, CRLF as LF>\\0``."""
    named = {"build.mjs", "tsconfig.json", "package-lock.json"}
    files = [p.name for p in CODEMAP.iterdir() if p.is_file()]
    files += [f"data/{p.name}" for p in (CODEMAP / "data").iterdir() if p.is_file()]
    digest = hashlib.sha256()
    for rel in sorted(f for f in files if f.endswith((".ts", ".tsx", ".css")) or f in named):
        content = (CODEMAP / rel).read_bytes().replace(b"\r\n", b"\n")
        digest.update(rel.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def test_committed_page_is_built_from_the_current_sources__F1945_F1941_F0546_F0128():
    page = (CODEMAP / "codebase-map.html").read_text(encoding="utf-8")
    stamp = re.search(r'<meta name="codemap-sources" content="sha256:([0-9a-f]{64})">', page)
    assert stamp, f"codebase-map.html carries no source stamp; {REBUILD}"
    assert stamp.group(1) == _sources_digest(), (
        f"codebase-map.html was built from other sources than data/*.ts and the viewer; {REBUILD}"
    )


def _sdk_file(rel: str) -> Path:
    """A file of the SDK source tree (this repo's sdk/), or a skip."""
    path = REPO / "sdk" / rel
    if path.is_file():
        return path
    pytest.skip(f"no SDK source file {rel}")


def test_renderer_api_node_describes_the_sdk_theme_contract_R06():
    """The renderer api contract node states the W2 theme record, its message
    and the runtime's theme exports as the SDK ships them: every member of
    the SDK's ``tokens`` table is named (``lift`` was once left out), the
    export list is the runtime's, and no node links the retired private
    palette note."""
    groups = dict(_items(_data("interfaces.ts"), "i:renderer-api"))

    def group(prefix: str) -> list[str]:
        (entries,) = [e for title, e in groups.items() if title.startswith(prefix)]
        return entries

    every_host = group("api members in every host")
    assert any(e.startswith("getTheme(): {themeId, scheme, known, vars, fonts: {ui, mono, prose}}") for e in every_host)
    assert "onThemeChange(cb): unsubscribe" in every_host
    assert any(e.startswith("SET_THEME {themeId, scheme, known, vars (keyed by CSS name), fonts {ui, mono, prose}}")
               for e in group("postMessage: host → iframe"))

    exports_title = next(title for title in groups if title.startswith("/api/sdk/react.js exports"))
    entries = groups[exports_title]
    runtime = _sdk_file("shared/renderer-runtime/react.js").read_text(encoding="utf-8")
    block = runtime.rsplit("export {", 1)[1].split("};", 1)[0]
    exported = {name.strip() for name in block.split(",") if name.strip()}
    assert exports_title == f"/api/sdk/react.js exports ({len(exported)})"
    listed = {re.match(r"[A-Za-z_]\w*", e).group(0) for e in entries}
    assert listed == exported, f"map lists {sorted(listed ^ exported)} differently from the runtime"
    assert any(e.startswith("useTheme(api) → {themeId, scheme, known, vars, fonts, onChange}") for e in entries)

    # Every key of the SDK's tokens table is named in the tokens entry.
    hooks = _sdk_file("packages/renderer-hooks/src/index.tsx").read_text(encoding="utf-8")
    table = hooks[hooks.index("export const tokens"):]
    table = table[:table.index("\n});")]
    keys = set(re.findall(r"^\s*(\w+):\s*(?:'var\(|Object\.freeze)", table, re.M))
    assert {"color", "font", "radius", "shadow", "lift"} <= keys
    (tokens_entry,) = [e for e in entries if e.startswith("tokens — ")]
    for key in keys:
        assert re.search(rf"\b{key}\b", tokens_entry), f"tokens.{key} is not in the map's tokens entry"

    for name in DATA_FILES:
        assert "project_renderer_palette_convention" not in _data(name), name
