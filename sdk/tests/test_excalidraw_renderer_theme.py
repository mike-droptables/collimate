# SPDX-License-Identifier: MIT
"""Excalidraw's chrome variables and drawing ground, from the skin (R05, W2b).

Bundles ``renderers/visual/excalidraw/main.tsx`` with framework imports
stubbed and runs ``theme.test.ts`` under node:test (``tests/_node_renderer.py``).
"""
from __future__ import annotations

from tests._node_renderer import node_renderer_skip, run_node_renderer
from tests._workspace import RENDERERS

_DIR = RENDERERS / "visual" / "excalidraw"
_TEST_FILE = _DIR / "theme.test.ts"
_MAIN_TSX = _DIR / "main.tsx"


@node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the excalidraw theme test')
def test_excalidraw_chrome_and_ground_from_the_skin_R05_W2b(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'excalidraw_renderer_bundle.mjs', _TEST_FILE, 'EXCALIDRAW_RENDERER_BUNDLE',
                      label='excalidraw renderer theme test')


def test_excalidraw_wires_the_ground_and_the_sheet_R05_W2b():
    """The component mounts the file's ground through sceneGround, writes it
    back through savedGround, renders the token sheet while the skin is the
    look (RS-04) and keeps the library's scheme switch."""
    source = _MAIN_TSX.read_text()
    assert "{!own && <style>{skinCss}</style>}" in source
    assert "viewBackgroundColor: sceneGround(appState.viewBackgroundColor, ownRef.current)" in source
    assert "viewBackgroundColor: savedGround(s.state.viewBackgroundColor, fileStateRef.current.viewBackgroundColor)" in source
    assert "theme={scheme}" in source
    assert "viewBackgroundColor: s.state.viewBackgroundColor," not in source


def test_excalidraw_repaints_the_dark_selection_on_every_skin_R05_W2b():
    """The dark selection colour is rebuilt from the record on every skin
    change, and the interactive canvas is made to redraw with it: a change
    within one scheme leaves Excalidraw's props as they were."""
    source = _MAIN_TSX.read_text()
    assert "const theme = useTheme(api);" in source
    assert "const selectionCss = own ? '' : darkSelectionCss(theme);" in source
    assert "<style>{selectionCss}</style>" in source
    assert "excalidrawAPI={keepExcalidraw}" in source
    assert ("excalidraw.updateScene({ appState: { selectedElementIds: "
            "{ ...excalidraw.getAppState().selectedElementIds } } });") in source
    assert "}, [selectionCss, excalidraw]);" in source
