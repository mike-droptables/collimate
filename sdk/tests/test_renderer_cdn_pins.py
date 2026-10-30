# SPDX-License-Identifier: MIT
"""CDN pinning contract over every shipped renderer.

Renderer bundles keep external ES imports: React and its libraries load
from esm.sh (through the app's module mirror) at runtime. esm.sh derives the
JS build target from the requesting **User-Agent** unless the URL says
otherwise, so the same specifier can resolve to ``/es2022/…`` in one browser
and ``/esnext/…`` in another. Two imports that resolve to different builds
are two *module instances* — which silently breaks everything that depends
on them being one: React shared with ``/api/sdk/react.js``, the ProseMirror
``Plugin``/``Decoration`` classes the milkdown renderer hands to crepe's
editor, any library holding module state.

So every esm.sh **module** URL in this repo's renderers carries
``?target=es2022`` (``&target=es2022`` when the URL already has a query), and
an exact version. Stylesheets (``.css``) are plain assets — no build target
applies, and they are left alone.

``deps=`` splits instances the same way one level down: esm.sh resolves each
URL's own imports against the list that URL declares, so a file whose imports
declare *disagreeing* ``deps=`` strings gets a shared package resolved twice.
Within one entrypoint there is therefore one canonical list, which every
deps-carrying URL repeats — truncated from the right at most, since a lower
layer legitimately pins fewer packages than the layer above it (a URL carrying
no ``deps=`` is exempt: that is how you deliberately land on the plain build a
bundled dependency already imports).
The frozen module snapshot (the workspace's ``module-snapshot/``, which the app
serves before any upstream fetch) records what each list resolved
(``tests/test_module_snapshot.py``).

This is a contract over *this repo's* shipped renderers only. It is deliberately
not wired into the renderer handler: a user-authored renderer is free
to import however it likes. Pure text analysis — no network, no Node.
"""
from __future__ import annotations

import glob
import os
import re

from tests.conftest import RENDERER_ROOTS
from tests._workspace import BUILTINS, MODULE_SNAPSHOT, WORKSPACE

# Any esm.sh URL, as it appears in source. The character class stops at the
# quote/backtick/whitespace that closes the import specifier; trailing source
# punctuation is stripped below for the rare unquoted occurrence (comments).
_ESM_URL = re.compile(r"https://esm\.sh/[^'\"\s`]+")
_TRAILING = ");,.>"
_TARGET = re.compile(r"[?&]target=([^&]*)")
_DEPS = re.compile(r"[?&]deps=([^&]*)")


_FIX = (
    "append '?target=es2022' (or '&target=es2022' if the URL already has a "
    "query string, keeping any 'exports=' last). See "
    "shared/docs/how-to-write-a-renderer.md."
)


def _entrypoints() -> list[str]:
    """Every renderer entrypoint on disk: <renderers|builtin>/<cell>/<name>/main.*"""
    found: list[str] = []
    for root in RENDERER_ROOTS:
        for ext in ("tsx", "ts", "js"):
            found += glob.glob(os.path.join(root, "*", "*", f"main.{ext}"))
    return sorted(found)


def _urls(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return [m.group(0).rstrip(_TRAILING) for m in _ESM_URL.finditer(src)]


def _is_stylesheet(url: str) -> bool:
    return url.split("?", 1)[0].endswith(".css")


def _module_urls(path: str) -> list[str]:
    return [u for u in _urls(path) if not _is_stylesheet(u)]


def _rel(path: str) -> str:
    return os.path.relpath(path, WORKSPACE)


def _package_spec(url: str) -> str:
    """The ``name[@version]`` (or ``@scope/name[@version]``) head of an esm.sh
    path, with the query string and any subpath dropped."""
    path = url[len("https://esm.sh/"):].split("?", 1)[0]
    segs = path.split("/")
    if path.startswith("@"):
        return "/".join(segs[:2])
    return segs[0]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_renderer_tree_has_entrypoints():
    """Guard the guard: a broken glob must not turn this suite into a no-op."""
    entrypoints = _entrypoints()
    assert entrypoints, "no renderer entrypoints found under renderers/ or builtin/"
    assert any(_module_urls(p) for p in entrypoints), (
        "no esm.sh module URLs found in any renderer entrypoint — the scan "
        "regex or the import style changed; update this test."
    )


def test_every_esm_module_url_pins_target():
    """Every non-stylesheet esm.sh URL declares its build target."""
    bad: list[str] = []
    for path in _entrypoints():
        for url in _module_urls(path):
            m = _TARGET.search(url)
            if m is None or m.group(1) != "es2022":
                got = "no target=" if m is None else f"target={m.group(1)}"
                bad.append(f"  {_rel(path)}: {got}\n    {url}")
    assert not bad, (
        "esm.sh module URL(s) missing the es2022 build-target pin — esm.sh "
        "would pick the target from the User-Agent, so these can resolve to a "
        "different module instance than the rest of the file. Fix: "
        + _FIX
        + "\n"
        + "\n".join(bad)
    )


def test_target_pinning_is_atomic_per_file():
    """All-or-nothing per file: a half-pinned entrypoint is the case that
    actually splits a shared module into two instances."""
    bad: list[str] = []
    for path in _entrypoints():
        urls = _module_urls(path)
        if not urls:
            continue
        targets = {
            (m.group(1) if (m := _TARGET.search(u)) else "<unpinned>")
            for u in urls
        }
        if targets != {"es2022"}:
            bad.append(f"  {_rel(path)}: {sorted(targets)} across {len(urls)} URL(s)")
    assert not bad, (
        "renderer entrypoint(s) mix build targets across their esm.sh imports; "
        "every esm.sh module URL in a file must use target=es2022 (all of "
        "them, or none). Fix: " + _FIX + "\n" + "\n".join(bad)
    )


def test_deps_pins_are_identical_per_file():
    """One canonical ``deps=`` list per file, and every deps-carrying URL
    repeats it — truncated from the right at most.

    esm.sh resolves each URL's own imports against the ``deps=`` list that
    URL declares, so two URLs in one file declaring *disagreeing* lists get
    the shared package resolved twice: the same two-instances failure as a
    split build target, and just as silent (the milkdown renderer's
    ProseMirror/codemirror stack is the live case — one stale entry and the
    editor mounts fine but every plugin that hands it a class is talking to
    a different module).

    Truncation from the right is the layering idiom and is allowed:
    ``lucide-react`` needs ``react`` pinned but not ``react-dom``, and
    ``react-dom/client`` needs ``react`` but not the assistant-ui packages
    layered above it (builtin/live/assistant/main.tsx). What is never
    allowed is a *different* version, a different order, or a package
    dropped out of the middle — those are drift, not layering. A URL
    carrying no ``deps=`` at all is exempt: omitting it is the deliberate
    way to land on the plain build a bundled dependency already imports, so
    the two share one instance.
    """
    bad: list[str] = []
    for path in _entrypoints():
        pinned = [
            (u, m.group(1))
            for u in _module_urls(path)
            if (m := _DEPS.search(u))
        ]
        if not pinned:
            continue
        canonical = max((v for _, v in pinned), key=lambda v: len(v.split(",")))
        layers = canonical.split(",")
        off = [
            (u, v) for u, v in pinned
            if v.split(",") != layers[: len(v.split(","))]
        ]
        if off:
            lines = "\n".join(f"      deps={v}\n        {u}" for u, v in off)
            bad.append(
                f"  {_rel(path)}: canonical deps={canonical}\n"
                f"    {len(off)} URL(s) disagree with it:\n{lines}"
            )
    assert not bad, (
        "renderer entrypoint(s) mix 'deps=' lists across their esm.sh "
        "imports — every deps-carrying URL in a file must repeat the file's "
        "canonical list (same versions, same order), keeping only a leading "
        "run of it, or the shared packages resolve to separate module "
        "instances. Fix: copy the canonical deps string to each URL (drop "
        "deps= entirely from a URL meant to share a bundled instance).\n"
        + "\n".join(bad)
    )


def test_every_esm_module_url_pins_a_version():
    """A floating specifier re-resolves whenever upstream publishes."""
    bad: list[str] = []
    for path in _entrypoints():
        for url in _module_urls(path):
            spec = _package_spec(url)
            # "@" at index 0 is the npm scope marker, not a version separator.
            if "@" not in spec[1:]:
                bad.append(f"  {_rel(path)}: '{spec}' has no @version\n    {url}")
    assert not bad, (
        "esm.sh module URL(s) without a pinned version — add '@<version>' to "
        "the package specifier so the renderer does not drift when upstream "
        "publishes.\n" + "\n".join(bad)
    )


def test_assistant_radix_dependency_is_exact_in_both_modules__P5F03():
    """The shipped chat graph must not resolve Radix through a floating CDN URL.

    @assistant-ui/react declares radix-ui with a caret; a floating range once
    returned a cached CDN 404 in the actual chat gate. Pin the version the
    pinned release declares (0.15.31: ^1.8.1) in both the component and
    runtime imports so they retain the same module identity.
    """
    for name in ("main.tsx", "chat-runtime.ts"):
        path = os.path.join(BUILTINS, "live", "assistant", name)
        urls = [u for u in _module_urls(path) if _package_spec(u).startswith("@assistant-ui/")]
        assert urls, f"{name}: no assistant-ui imports found"
        for url in urls:
            match = _DEPS.search(url)
            deps = dict(pin.rsplit("@", 1) for pin in match.group(1).split(",")) if match else {}
            assert deps.get("radix-ui") == "1.8.1", (
                f"{name}: assistant-ui graph leaves Radix floating: {url}"
            )


# Packages whose older releases carry known security fixes a renderer must not
# miss: package → {major line: the lowest release on that line any shipped
# renderer may pin}. Fixes land as backports per line, so a later major is not
# automatically past a floor, and a pin on a line with no recorded floor fails
# too: whoever moves a package to a new line records that line's floor.
# Mermaid renders user- and agent-authored diagrams into the app's own document
# (the mermaid renderer is embed: native), so a label-sanitisation fix it lacks
# runs script at the app origin; the pin moved to the 12.x line at 12.2.0,
# which carries every fix published by then.
_SECURITY_FLOORS: dict[str, dict[int, tuple[int, ...]]] = {
    "mermaid": {12: (12, 2, 0)},
}


def _version_tuple(version: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if m is None:
        return None
    return tuple(int(part) for part in m.groups() if part is not None)


def _floor_violation(name: str, version: str) -> str | None:
    """Why ``name@version`` is below its security floor, or None when it is
    not (or the package has no floors)."""
    floors = _SECURITY_FLOORS.get(name)
    if floors is None:
        return None
    pinned = _version_tuple(version)
    if pinned is None:
        return f"{name}@{version} is not an exact version the floor can be checked against"
    floor = floors.get(pinned[0])
    if floor is None:
        return (f"{name}@{version} is on the {pinned[0]}.x line, which has no recorded "
                "security floor: add that line's floor to _SECURITY_FLOORS")
    # A partial version (a range) counts as its lowest release: 12.2 < 12.2.0.
    if pinned < floor:
        return f"{name}@{version} is below {name}@{'.'.join(map(str, floor))}"
    return None


def test_security_floor_rule__F1870():
    assert _floor_violation("mermaid", "12.2.0") is None
    assert _floor_violation("mermaid", "12.3.0") is None
    assert _floor_violation("mermaid", "12.1.9")
    assert _floor_violation("mermaid", "12.1")
    assert _floor_violation("mermaid", "12")
    assert _floor_violation("mermaid", "10.9.8")  # a line with no recorded floor
    assert _floor_violation("mermaid", "latest")
    assert _floor_violation("react", "18") is None  # no floors: not checked


def test_security_floor_pins__F1870():
    """No shipped renderer pins a package below its security floor."""
    bad: list[str] = []
    seen: set[str] = set()
    for path in _entrypoints():
        for url in _module_urls(path):
            spec = _package_spec(url)
            name, _, version = spec[1:].partition("@")
            name = spec[0] + name
            if name not in _SECURITY_FLOORS:
                continue
            seen.add(name)
            why = _floor_violation(name, version)
            if why:
                bad.append(f"  {_rel(path)}: {why}\n    {url}")
    assert seen, "no renderer imports a floored package — the scan or the floor table is stale"
    assert not bad, (
        "renderer(s) pin a package below its security floor — bump to at least "
        "the floor version:\n" + "\n".join(bad)
    )


# ---------------------------------------------------------------------------
# Exact pins over both mirrored upstreams
# ---------------------------------------------------------------------------
# Every upstream URL a shipped renderer or the runtime loads (the module
# mirror's roots: import specifiers, other esm.sh and cdn.jsdelivr.net
# literals, and the files each mirror_assets.json names) pins an exact version,
# so the frozen graph never re-resolves under it. The two milkdown ranges are
# the graph's own specifiers: crepe's CodeMirror graph imports exactly these
# URLs, and module identity is keyed by URL, so an exact pin would load a
# second @codemirror/language and highlighting would silently stop
# (renderers/documents/milkdown/main.tsx §1). Each must stay an edge the
# frozen graph itself imports.
GRAPH_OWN_SPECIFIERS = frozenset({
    "https://esm.sh/@codemirror/language@^6.0.0?target=es2022",
    "https://esm.sh/@lezer/highlight@^1.0.0?target=es2022",
})


def test_every_upstream_root_pins_an_exact_version():
    from collimate._host.module_mirror import is_floating, shipped_roots
    floating = {url for url in shipped_roots(WORKSPACE) if is_floating(url)}
    assert not floating - GRAPH_OWN_SPECIFIERS, (
        "shipped upstream URL(s) without an exact version (or with a range/tag); pin "
        "'@<major>.<minor>.<patch>':\n  " + "\n  ".join(sorted(floating - GRAPH_OWN_SPECIFIERS)))
    assert floating == GRAPH_OWN_SPECIFIERS, (
        "an allowlisted graph specifier is no longer shipped; drop it from GRAPH_OWN_SPECIFIERS: "
        + ", ".join(sorted(GRAPH_OWN_SPECIFIERS - floating)))


def test_graph_own_specifiers_are_edges_of_the_frozen_graph():
    from collimate._host.module_mirror import read_snapshot
    snapshot = read_snapshot(MODULE_SNAPSHOT)
    edges = {target for entry in snapshot.entries.values() for target in entry.imports}
    missing = sorted(GRAPH_OWN_SPECIFIERS - edges)
    assert not missing, (
        "an allowlisted range is no longer a specifier the frozen graph imports, so pinning "
        "it exact would no longer split an instance; pin it: " + ", ".join(missing))
