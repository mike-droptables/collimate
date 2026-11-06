# SPDX-License-Identifier: MIT
"""The text renderer's caret at the end and its style control (RS-04).

Bundles ``builtin/text/text/main.tsx`` with framework imports stubbed
and runs ``text.test.ts`` under node:test (``tests/_node_renderer.py``).
"""
from __future__ import annotations

from tests._node_renderer import node_renderer_skip, run_node_renderer
from tests._workspace import BUILTINS

_DIR = BUILTINS / "text" / "text"
_TEST_FILE = _DIR / "text.test.ts"
_MAIN_TSX = _DIR / "main.tsx"


@node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the text renderer test')
def test_text_caret_at_the_end_and_style_RS04(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'text_renderer_bundle.mjs', _TEST_FILE, 'TEXT_RENDERER_BUNDLE',
                      label='text renderer test')
