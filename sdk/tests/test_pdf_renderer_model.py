# SPDX-License-Identifier: MIT
"""The PDF renderer's pure model under node:test: the one EmbedPDF release
across its esm.sh pins and the PDFium build the engine downloads (a jsdelivr
pin, which ``test_renderer_cdn_pins.py`` holds exact like every mirrored
upstream URL), the shared deps
list that keeps the plugins on one core instance, the chrome's skin tokens,
the view controls' labels and the viewer's box — see
``renderers/visual/pdf/theme.test.ts``.

The shim bundles ``main.tsx`` with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) and hands the bundle and the source
path to the node test via ``PDF_RENDERER_BUNDLE`` / ``PDF_RENDERER_SOURCE`` —
the shared ``tests/_node_renderer.py`` harness. Skips when Node, esbuild, or
the tsx loader isn't installed (``npm --prefix scripts install``)."""
from __future__ import annotations

from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_PACKAGE = RENDERERS / 'visual' / 'pdf'
_TEST_FILE = _PACKAGE / 'theme.test.ts'
_MAIN_TSX = _PACKAGE / 'main.tsx'

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the pdf renderer model test')


def test_pdf_renderer_model(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'pdf_bundle.mjs', _TEST_FILE, 'PDF_RENDERER_BUNDLE',
                      label='pdf renderer model test', bundle_label='content/renderers/visual/pdf/main.tsx',
                      env={'PDF_RENDERER_SOURCE': str(_MAIN_TSX)})
