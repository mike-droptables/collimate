# SPDX-License-Identifier: MIT
"""Mermaid's base theme variables, built from the skin record (R05).

Bundles ``renderers/visual/mermaid/main.tsx`` with framework imports stubbed
and runs ``theme.test.ts`` under node:test (``tests/_node_renderer.py``).
"""
from __future__ import annotations

from tests._node_renderer import node_renderer_skip, run_node_renderer
from tests._workspace import RENDERERS

_DIR = RENDERERS / "visual" / "mermaid"
_TEST_FILE = _DIR / "theme.test.ts"
_MAIN_TSX = _DIR / "main.tsx"


@node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the mermaid theme test')
def test_mermaid_theme_variables_from_the_record_R05(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'mermaid_renderer_bundle.mjs', _TEST_FILE, 'MERMAID_RENDERER_BUNDLE',
                      label='mermaid renderer theme test')


def test_mermaid_rerenders_on_a_theme_change_R05():
    source = _MAIN_TSX.read_text()
    assert "mermaid.initialize(JSON.parse(configJson));" in source
    assert "}, [local, configJson]);" in source
    assert "theme: scheme === 'light' ? 'default' : 'dark'" not in source
