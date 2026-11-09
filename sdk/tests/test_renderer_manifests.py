# SPDX-License-Identifier: MIT
"""Extension-claim contract over the shipped renderer manifests.

The frontend resolves a filename to a renderer by extension
(``frontend/src/hooks/useRendererId.ts``): the LONGEST matching extension wins;
among the claimants of one extension, a renderer that declares it in
``default_for`` wins, and any other tie falls back to *registry order* — i.e.
to whatever order the manifests happened to be enumerated in, a channel's
renderers before the app's built-ins. A doubly-claimed extension without a
default is a silent coin-flip: for a long time two renderers listed ``.html``,
and a ``.html`` file opened as SOURCE because the code editor happened to sort
first. The fix is not to reorder the registry (nothing declares that order)
but a decision in the manifests: for the page extensions, exactly ONE renderer
claims them; elsewhere, one of a channel's renderers claiming an extension is
its ``default_for``. The code editor is never lost: every renderer whose
``default_for`` holds ``*`` is on every text file's "Open with" menu, whether
or not it claims the extension.

The assertions are derived from the manifests on disk rather than from a
hardcoded roster of renderers (adding a renderer must not have to touch this
file — adding a *second claimant of ``.html``*, or a second undeclared
claimant of any extension, must).

Deliberately NOT a global "no extension is claimed twice" rule: several
double-claims are real and intentional (``.svg`` by the code editor and the
image viewer, its default; ``.sh``/``.bash`` and ``.csv`` by a channel's
renderer and the built-in plain text, which a channel's renderer wins).

Pure filesystem + YAML — no network, no Node.
"""
from __future__ import annotations

from collimate.definitions import discover_definitions
import os
from tests.conftest import RENDERER_ROOTS

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The extensions a page renderer must own outright, and the renderer that owns
# them. The id is asserted (not merely "some single renderer") because the
# *identity* is the product decision — an HTML file opens as a rendered page.
_PAGE_EXTENSIONS = (".html", ".htm")
_PAGE_RENDERER_ID = "html"


def _manifests() -> list[tuple[str, dict]]:
    """Every visible shipped renderer definition, using shared package boundaries."""
    out: list[tuple[str, dict]] = []
    for cell in sorted(cell for root in RENDERER_ROOTS for cell in root.iterdir()):
        if not cell.is_dir():
            continue
        found = discover_definitions(p.relative_to(cell).as_posix() for p in cell.rglob("*") if p.is_file())
        assert not found.problems, found.problems
        for package in found.packages:
            if package.kind != "renderer":
                continue
            path = str(cell / package.definition_path)
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            out.append((path, data))
    return out


def _rel(path: str) -> str:
    return os.path.relpath(path, _ROOT)


def _claimants(ext: str) -> list[tuple[str, dict]]:
    """Manifests whose ``extensions`` list includes ``ext`` (case-insensitively —
    the frontend lowercases both sides before matching)."""
    return [
        (path, data)
        for path, data in _manifests()
        if any(str(e).lower() == ext for e in (data.get("extensions") or []))
    ]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_renderer_manifests_are_loadable():
    """Guard the guard: a broken glob (or an unparseable manifest) must not
    turn the claim assertions below into vacuous passes."""
    manifests = _manifests()
    assert manifests, "no renderer manifests found under renderers/ or builtin/"
    bad = [
        _rel(path)
        for path, data in manifests
        if not isinstance(data, dict) or not data.get("id") or not data.get("entrypoint")
    ]
    assert not bad, f"renderer manifest(s) missing id/entrypoint: {bad}"
    assert any(data.get("extensions") for _, data in manifests), (
        "no renderer manifest declares any extension — the manifest schema or "
        "the glob changed; update this test."
    )


def test_page_extensions_have_exactly_one_claimant():
    """One renderer claims ``.html``/``.htm``, so the default is a decision
    rather than an artifact of registry order."""
    for ext in _PAGE_EXTENSIONS:
        claimants = _claimants(ext)
        ids = sorted(str(data.get("id")) for _, data in claimants)
        assert len(claimants) == 1, (
            f"'{ext}' is claimed by {len(claimants)} renderer(s) ({ids}) — with "
            "more than one claimant the default renderer is decided by registry "
            "order, not by intent. Drop the extension from every manifest but "
            f"'{_PAGE_RENDERER_ID}'; the code editor stays reachable for these "
            "files through the 'Open with' menu, which offers every renderer "
            "whose default_for holds '*' for anything textual."
        )


def test_page_extensions_are_claimed_by_the_page_renderer():
    """And the single claimant is the iframe-embedded page renderer."""
    for ext in _PAGE_EXTENSIONS:
        claimants = _claimants(ext)
        assert claimants, f"no renderer claims '{ext}' — a page file would fall through to the generic code-editor fallback"
        path, data = claimants[0]
        assert data.get("id") == _PAGE_RENDERER_ID, (
            f"'{ext}' is claimed by renderer id '{data.get('id')}' "
            f"({_rel(path)}), expected '{_PAGE_RENDERER_ID}' — an HTML file "
            "opens as a rendered page by default, not as source."
        )
        assert data.get("embed") == "iframe", (
            f"{_rel(path)} declares embed='{data.get('embed')}' — the page "
            "renderer hosts an opaque-origin sandboxed frame and must stay an "
            "iframe renderer (a native/shadow-DOM embed shares the host realm)."
        )


def _is_builtin(path: str) -> bool:
    return os.path.realpath(path).startswith(os.path.realpath(str(RENDERER_ROOTS[1])) + os.sep)


def test_an_extension_two_channel_renderers_claim_has_one_default():
    """Two of a channel's renderers claiming one extension leave the default
    to registry order unless exactly one of them declares it in
    ``default_for``; a built-in claimant loses to a channel's renderer."""
    by_ext: dict[str, list[tuple[str, dict]]] = {}
    for path, data in _manifests():
        if _is_builtin(path):
            continue
        for ext in {str(e).lower() for e in (data.get("extensions") or [])}:
            by_ext.setdefault(ext, []).append((path, data))
    contested = {ext: claimants for ext, claimants in by_ext.items() if len(claimants) > 1}
    assert contested, "no extension is claimed twice; the default_for rule checks nothing"
    for ext, claimants in contested.items():
        defaults = [str(data.get("id")) for _, data in claimants
                    if ext in {str(e).lower() for e in (data.get("default_for") or [])}]
        ids = sorted(str(data.get("id")) for _, data in claimants)
        assert len(defaults) == 1, (
            f"'{ext}' is claimed by {ids} and is the default_for of {defaults} — exactly one "
            "claimant must declare it, or the default is an accident of registry order")


def test_some_channel_renderer_and_the_builtin_text_open_any_text_file():
    """The 'Open with' menu keeps a code editor (one of a channel's
    renderers) and plain text (a built-in) for every text file: both
    declare ``default_for: ["*"]``."""
    any_text = [(path, data) for path, data in _manifests() if "*" in (data.get("default_for") or [])]
    assert any(not _is_builtin(path) for path, _ in any_text), "no channel renderer opens any text file"
    assert [data.get("id") for path, data in any_text if _is_builtin(path)] == ["text"]
