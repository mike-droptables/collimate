# SPDX-License-Identifier: MIT
"""The Monaco renderer's 'collimate' theme, built from the skin record (R05).

Bundles ``renderers/documents/monaco/main.tsx`` with framework imports stubbed
and runs ``theme.test.ts`` under node:test (``tests/_node_renderer.py``).
"""
from __future__ import annotations

import re

from tests._node_renderer import node_renderer_skip, run_node_renderer
from tests._workspace import RENDERERS

_DIR = RENDERERS / "documents" / "monaco"
_TEST_FILE = _DIR / "theme.test.ts"
_MAIN_TSX = _DIR / "main.tsx"


@node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the monaco theme test')
def test_monaco_theme_builder_resolves_every_colour_R05(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'monaco_renderer_bundle.mjs', _TEST_FILE, 'MONACO_RENDERER_BUNDLE',
                      label='monaco renderer theme test')


def test_monaco_editor_and_diff_use_the_rebuilt_theme_R05():
    source = _MAIN_TSX.read_text()
    # Both editors are created on the style's theme (the skin's one theme
    # unless the user chose one of Monaco's own) and rebuild it on every
    # change.
    assert source.count("theme: monacoThemeName(styleRef.current.theme),") == 2
    assert "onThemeChange((t) => applyMonacoTheme(monaco, t, editor, styleRef.current, base))" in source
    assert "onThemeChange((t) => applyMonacoTheme(monaco, t, diffEditor, styleRef.current, base))" in source
    assert "monaco.editor.setTheme(monacoThemeName(style.theme));" in source
    # The scheme only picks the base the record's colours sit on; Monaco's
    # own themes are named only as the style control's choices (RS-04).
    choices = source[source.index("export const MONACO_THEMES"):source.index("export function monacoThemeName(")]
    rest = source.replace(choices, "")
    assert re.findall(r"'vs(?:-dark)?'", rest) == ["'vs'", "'vs-dark'"]
    assert "base: theme.scheme === 'light' ? 'vs' : 'vs-dark'" in source


def test_monaco_editor_and_diff_draw_code_in_the_mono_font_R05():
    source = _MAIN_TSX.read_text()
    # Both create calls take the style's family, else the record's mono
    # font, the record carries it ...
    assert source.count("...monacoEditorFont(themeRef.current, styleRef.current.fontStack),") == 2
    assert "return fontStack ? { fontFamily: fontStack } : monacoFontOptions(theme);" in source
    assert source.count("themeRef.current = { scheme, vars, fonts };") == 2
    # ... a skin change reapplies it to the live editor, and glyphs are
    # measured again once the face has loaded.
    assert "try { editor.updateOptions(options); } catch {}" in source
    assert "monaco.editor.remeasureFonts()" in source
    assert ".then(() => fonts.ready)" in source


def test_monaco_theme_takes_its_syntax_colours_from_the_skin_palette_R05_W2b():
    source = _MAIN_TSX.read_text()
    # The token rules come from the SDK's shared palette, so Monaco and every
    # other highlighter agree; Monaco's base is inherited only without a palette.
    assert re.search(r"^\s+syntaxPalette,$", source, re.M)
    assert "const palette: SyntaxPalette | null = syntaxPalette(theme as any);" in source
    # A palette replaces the base's rules; without one the base stands in.
    assert "inherit: palette === null," in source
    assert "rules: monacoTokenRules(palette)," in source
    # No colour literal anywhere in the renderer.
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", source)
