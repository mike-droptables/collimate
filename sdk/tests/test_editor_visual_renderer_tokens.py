# SPDX-License-Identifier: MIT
"""Text, Monaco, Excalidraw and Mermaid paint only from the skin tokens (R05).

No old token name, no ``var(--x, fallback)``, no colour literal, and every
``tokens.color.*`` / ``tokens.font.*`` key a renderer reads exists in the
SDK's ``tokens`` table.
"""
from __future__ import annotations

import re

import pytest

from tests._node_renderer import REPO
from tests._workspace import WORKSPACE

# Workspace-relative package folders.
RENDERERS = ["builtins/text/text", "content/renderers/documents/monaco", "content/renderers/visual/excalidraw",
             "content/renderers/visual/mermaid"]
SDK_INDEX = REPO / "packages" / "renderer-hooks" / "src" / "index.tsx"
OLD_NAMES = re.compile(
    r"void-main|obsidian|glass-panel|filament|electric-teal|plasma-pink|tungsten|signal-white|signalWhite"
    r"|comment-grey|commentGrey|obsidianPlate|tungstenDim|renderer-surface|renderer-text|--destructive"
    r"|--muted-foreground|--font-sans|--foreground|--background(?![\w-])|vs-dark'\)"
)
COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
VAR_FALLBACK = re.compile(r"var\(--[\w-]+\s*,")
# A library's own default, named so the renderer can recognise it, is not a
# colour the renderer paints: Excalidraw's default drawing ground marks a
# drawing that follows the skin (R05 W2b).
LIBRARY_DEFAULTS = {"content/renderers/visual/excalidraw": ["#ffffff"]}


def _sdk_keys(group: str) -> set[str]:
    source = SDK_INDEX.read_text()
    block = re.search(group + r": Object\.freeze\(\{(.*?)\}\)", source, re.S)
    assert block, group
    return set(re.findall(r"^\s*(\w+):", block.group(1), re.M))


@pytest.mark.parametrize("renderer", RENDERERS)
def test_renderer_paints_only_from_skin_tokens_R05(renderer):
    source = (WORKSPACE / renderer / "main.tsx").read_text()
    assert not OLD_NAMES.findall(source), renderer
    assert not VAR_FALLBACK.findall(source), renderer
    assert COLOUR_LITERAL.findall(source) == LIBRARY_DEFAULTS.get(renderer, []), renderer
    colours, fonts = _sdk_keys("color"), _sdk_keys("font")
    assert set(re.findall(r"tokens\.color\.(\w+)", source)) <= colours, renderer
    assert set(re.findall(r"tokens\.font\.(\w+)", source)) <= fonts, renderer
    names = set(re.findall(r"var\((--[\w-]+)\)", source))
    known = set(re.findall(r"'(--[\w-]+)'", SDK_INDEX.read_text().split("THEME_VAR_NAMES")[1]))
    assert names <= known, (renderer, names - known)


def test_excalidraw_names_its_one_library_default_R05_W2b():
    """The one literal in the Excalidraw renderer is the library's default
    ground, declared once as a named constant and only compared against."""
    source = (WORKSPACE / "content/renderers/visual/excalidraw/main.tsx").read_text()
    assert "export const EXCALIDRAW_DEFAULT_GROUND = '#ffffff';" in source
    assert source.count("#ffffff") == 1
