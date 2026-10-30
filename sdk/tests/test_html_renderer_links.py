# SPDX-License-Identifier: MIT
"""The page renderer's links (``renderers/visual/html/links.ts``, re-exported
by ``main.tsx``) under node:test: which file of the cell a reference names
(beside the page, ``../`` within the cell, ``/`` the cell root), the page's
``<base>``, the renderer's reading of a posted address and the click bridge's
message — see ``renderers/visual/html/links.test.ts``.

Uses the shared ``tests/_node_renderer.py`` harness. Skips when Node,
esbuild, or the tsx loader isn't installed (``npm --prefix scripts install``).
"""
from __future__ import annotations

import json

from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_TEST_FILE = RENDERERS / "visual" / "html" / "links.test.ts"
_MAIN_TSX = RENDERERS / "visual" / "html" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the html renderer links test')


def test_html_renderer_links(tmp_path):
    entry = tmp_path / "html_links_entry.ts"
    entry.write_text(f"export * from {json.dumps(str(_MAIN_TSX))};\n")
    run_node_renderer(entry, tmp_path / 'html_links_bundle.mjs', _TEST_FILE, 'HTML_RENDERER_BUNDLE',
                      label='html renderer links test', bundle_label='main.tsx')


def test_html_renderer_wires_the_link_bridge():
    """The prepare step puts the bridge beside the ``<base>`` (the page's
    folder), and the renderer hears only its own page's window, reads the
    address itself and opens the file in a new tab."""
    main = _MAIN_TSX.read_text()
    assert ("bridge.textContent = linkBridge(cellBase);\n    doc.head.prepend(bridge);\n"
            "    const base = doc.createElement('base');\n"
            "    base.href = pageFolderUrl(cellBase, docPath);\n    doc.head.prepend(base);") in main
    assert "const path = url == null ? null : resolveCellPath(docPath, url);" in main
    assert "if (!frame || event.source !== frame.contentWindow) return;" in main
    assert "const path = cellPathOfUrl(data.href, cellFileUrl('', api));" in main
    assert "if (path) api.revealFile(path, { newTab: true });" in main
    assert "const prepare = useCallback((html: string) => preparePage(html, api, fileName), [api, fileName]);" in main
