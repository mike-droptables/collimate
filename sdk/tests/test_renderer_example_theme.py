# SPDX-License-Identifier: MIT
"""The sealed renderer preview's theme bridge and the runtime's default diff
and error views on the skin tokens (row R05).

``builtin/definitions/renderer-example/preview.test.ts`` runs under
node:test against a bundle of ``main.tsx`` (``tests/_node_renderer.py``); the
runtime check reads ``packages/renderer-hooks/src/platform.tsx``.
"""
from __future__ import annotations

import re

from tests._node_renderer import REPO, node_renderer_skip, run_node_renderer
from tests._workspace import BUILTINS

_FOLDER = BUILTINS / 'definitions' / 'renderer-example'
_TEST_FILE = _FOLDER / 'preview.test.ts'
_PLATFORM = REPO / 'packages' / 'renderer-hooks' / 'src' / 'platform.tsx'


@node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the sealed preview test')
def test_sealed_preview_mirrors_theme_and_loads_fonts_R05(tmp_path):
    run_node_renderer(_FOLDER / 'main.tsx', tmp_path / 'renderer_example_bundle.mjs', _TEST_FILE,
                      'RENDERER_EXAMPLE_BUNDLE', label='sealed preview theme test')


def test_sealed_preview_sources_carry_no_colour_literal_R05():
    for path in sorted(_FOLDER.glob('*.js')) + [_FOLDER / 'main.tsx']:
        source = path.read_text()
        assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\bwhite\b', source), path.name
        assert '--renderer-text' not in source, path.name


def test_runtime_diff_and_error_views_on_tokens_R05():
    source = _PLATFORM.read_text()
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(', source)
    # A var() with a fallback hides a missing token behind a private colour.
    assert not re.search(r'var\(--[\w-]+\s*,', source)
    used = set(re.findall(r'var\((--[\w-]+)\)', source))
    assert {'--red', '--red-soft', '--green', '--sheet', '--sheet-2', '--line', '--ink', '--font-mono'} <= used
    known = {'--canvas', '--sheet', '--sheet-2', '--sheet-3', '--line', '--line-2', '--ink', '--ink-2',
             '--ink-3', '--brass', '--brass-soft', '--teal', '--teal-soft', '--teal-sheet', '--red',
             '--red-soft', '--green', '--code-bg', '--prose-ink', '--shadow', '--lift', '--font-ui',
             '--font-mono', '--font-prose', '--radius'}
    assert used <= known, used - known
