# SPDX-License-Identifier: MIT
"""The markdown editor's skin CSS under node:test (R02): every ``--crepe-*``
variable Crepe's sheets read mapped onto a skin token, no colour or font
literal in what the frame injects, the mock's ``.md`` prose values, Crepe's
floating chrome in the menu vocabulary, the injection order, and Insert with
every Crepe upload path off (§3b) — see
``renderers/documents/milkdown/milkdown.test.ts``.

The shim bundles ``main.tsx`` with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) and hands the bundle path to the node
test via ``MILKDOWN_RENDERER_BUNDLE`` — the shared ``tests/_node_renderer.py``
harness. Skips when Node, esbuild, or the tsx loader isn't installed
(``npm --prefix scripts install``)."""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_TEST_FILE = RENDERERS / "documents" / "milkdown" / "milkdown.test.ts"
_MAIN_TSX = RENDERERS / "documents" / "milkdown" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the milkdown renderer model test')


def test_milkdown_skin_css_R02(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'milkdown_bundle.mjs', _TEST_FILE, 'MILKDOWN_RENDERER_BUNDLE',
                      label='milkdown renderer model test', bundle_label='main.tsx')
