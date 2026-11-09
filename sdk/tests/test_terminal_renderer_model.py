# SPDX-License-Identifier: MIT
"""The terminal renderer's pure model under node:test: the ``.terminal``
pointer parse (session coords + the optional toolbar ``buttons``, where
Claude's terminal declares Capture Chat, each with an optional tooltip and
confirm), the toolbar's buttons per session status (none once
ended), a button's click — confirmed first when it asks, then emitted as
a renderer action — the mono face's load timing (S03), and the skin (R05):
the xterm theme built from the skin tokens over one ANSI palette per scheme,
the chrome on tokens with a teal call to action, and no colour literal
outside the palette table — see ``builtin/live/terminal/terminal.test.ts``.

The renderer is a single self-contained module, so this shim first bundles
``main.tsx`` with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) and hands the bundle path to the node
test via ``TERMINAL_RENDERER_BUNDLE`` — the shared
``tests/_node_renderer.py`` harness. Skips when Node, esbuild, or the tsx loader
isn't installed (``npm --prefix scripts install``)."""
from __future__ import annotations


from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS

_TEST_FILE = BUILTINS / "live" / "terminal" / "terminal.test.ts"
_MAIN_TSX = BUILTINS / "live" / "terminal" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the terminal renderer model test')


def test_terminal_renderer_model(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'terminal_bundle.mjs', _TEST_FILE, 'TERMINAL_RENDERER_BUNDLE',
                      label='terminal renderer model test', bundle_label='main.tsx')
