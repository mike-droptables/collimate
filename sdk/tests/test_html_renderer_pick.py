# SPDX-License-Identifier: MIT
"""The element picker the html renderer answers annotation requests with
(``renderers/visual/html/main.tsx`` through the SDK's document view,
``packages/renderer-hooks/src/document-view.tsx``).

Uses the shared ``tests/_node_renderer.py`` harness: ``main.tsx`` and the
document view are bundled with their CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) into a temporary importable ESM
module, then ``pick.test.ts`` runs under node:test via the tsx loader with
``HTML_RENDERER_BUNDLE`` pointing at that bundle. It runs the page bridge
(``PICK_BRIDGE_JS``) against a stand-in window and document, and checks the
renderer's context format (``elementContext``). The hook half is
``tests/test_document_view_pick.py``.

Skips when Node, esbuild, or the tsx loader isn't available (e.g. a Python-only
CI job that never ran ``npm --prefix scripts install``).
"""
from __future__ import annotations

import json

from tests._node_renderer import (
    REPO as _REPO,
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_TEST_FILE = RENDERERS / "visual" / "html" / "pick.test.ts"
_MAIN_TSX = RENDERERS / "visual" / "html" / "main.tsx"
_VIEW = _REPO / "packages" / "renderer-hooks" / "src" / "document-view.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the html renderer pick test')


def test_html_renderer_element_pick(tmp_path):
    """pick.test.ts against main.tsx and the SDK's document view."""
    entry = tmp_path / "html_entry.ts"
    entry.write_text(f"export * from {json.dumps(str(_MAIN_TSX))};\nexport * from {json.dumps(str(_VIEW))};\n")
    run_node_renderer(entry, tmp_path / 'html_renderer_bundle.mjs', _TEST_FILE, 'HTML_RENDERER_BUNDLE',
                      label='html renderer element pick test', bundle_label='main.tsx')
