# SPDX-License-Identifier: MIT
"""The website and webview renderers' chrome on the skin tokens (R05).

Each renderer exports its chrome's style helpers from ``main.tsx``; this
bundles the entrypoint with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``, which keeps the SDK's real
``tokens``) and runs the folder's ``chrome.test.ts`` under node:test via the
tsx loader. A source scan backs it: neither entrypoint carries a colour
literal, a ``var(--name, fallback)`` or a name outside the skin vocabulary.
The pages they show (the site in the frame or guest) are never themed.
"""
from __future__ import annotations

import re

from tests._node_renderer import (
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS, WORKSPACE

_LIVE = BUILTINS / "live"
_WEBSITE = _LIVE / "website"
_WEBVIEW = _LIVE / "webview"

# The skin's CSS names (packages/renderer-hooks THEME_VAR_NAMES).
_SKIN_VARS = {
    "--canvas", "--sheet", "--sheet-2", "--sheet-3", "--line", "--line-2",
    "--ink", "--ink-2", "--ink-3", "--brass", "--brass-soft",
    "--teal", "--teal-soft", "--teal-sheet", "--red", "--red-soft", "--green",
    "--code-bg", "--prose-ink", "--shadow", "--lift",
    "--font-ui", "--font-mono", "--font-prose", "--radius",
}

_skip = node_renderer_skip(
    _WEBSITE / "chrome.test.ts",
    "node + esbuild + tsx (npm --prefix scripts install) required for the web chrome tests",
)


@_skip
def test_website_chrome_on_skin_tokens_R05(tmp_path):
    run_node_renderer(_WEBSITE / "main.tsx", tmp_path / "website_bundle.mjs",
                      _WEBSITE / "chrome.test.ts", "WEBSITE_BUNDLE",
                      label="website chrome test")


@_skip
def test_webview_chrome_on_skin_tokens_R05(tmp_path):
    run_node_renderer(_WEBVIEW / "main.tsx", tmp_path / "webview_bundle.mjs",
                      _WEBVIEW / "chrome.test.ts", "WEBVIEW_BUNDLE",
                      label="webview chrome test")


def test_web_renderer_sources_carry_no_colour_literal_or_old_token_R05():
    for entry in (_WEBSITE / "main.tsx", _WEBVIEW / "main.tsx"):
        src = entry.read_text(encoding="utf-8")
        where = entry.relative_to(WORKSPACE)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src), f"{where}: colour literal"
        assert not re.search(r"\b(rgba?|hsla?)\(", src), f"{where}: colour literal"
        assert not re.search(r"var\(--[\w-]+\s*,", src), f"{where}: var() fallback"
        unknown = set(re.findall(r"var\((--[\w-]+)\)", src)) - _SKIN_VARS
        assert not unknown, f"{where}: names outside the skin vocabulary: {sorted(unknown)}"
        assert "tokens.color.brass" not in src and "c.brass" not in src, (
            f"{where}: brass is the selection accent only")
