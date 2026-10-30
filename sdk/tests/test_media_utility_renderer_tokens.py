# SPDX-License-Identifier: MIT
"""Row R05 of the 2026-10-06 workbench plan for the media and utility
renderers: image, pdf, slides, audio and video (through the SDK's shared
``MediaViewer``), table and schema_form paint every surface from the skin
tokens.

Two layers. A source scan holds each file to the token contract (no colour
literal, no ``var(--token, fallback)``, no retired token name). The node
tests bundle each renderer's exported style helpers through
``scripts/bundle_renderer_test.mjs`` (the shared ``tests/_node_renderer.py``
harness) and pin what the tokens mean there: the image checkerboard, the
slide HTML builder and card, the table zebra, schema_form's colour map and
its focus rule.
"""
from __future__ import annotations

import re

import pytest

from tests._node_renderer import (
    REPO as _REPO,
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS, WORKSPACE

_SOURCES = [
    _REPO / 'packages/renderer-hooks/src/media.tsx',
    RENDERERS / 'visual/image/main.tsx',
    RENDERERS / 'visual/pdf/main.tsx',
    RENDERERS / 'visual/slides/main.tsx',
    RENDERERS / 'visual/audio/main.tsx',
    RENDERERS / 'visual/video/main.tsx',
    RENDERERS / 'utility/table/main.tsx',
    RENDERERS / 'utility/schema_form/main.tsx',
]

_LITERAL = re.compile(r'#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(')
_FALLBACK = re.compile(r'var\(--[\w-]+\s*,')
_RETIRED = re.compile(
    r'--(?:void-main|obsidian-plate|glass-panel|filament-amber|filament-hot|electric-teal|'
    r'plasma-pink|tungsten-dim|signal-white|comment-grey|renderer-surface|muted|'
    r'muted-foreground|destructive|primary|font-sans|radius-sm|radius-md|radius-lg)\b'
)

# The contract both scans check against: the token CSS names and the ``tokens``
# table, parsed from the SDK's runtime source so the test follows it.
_HOOKS = (_REPO / 'packages/renderer-hooks/src/index.tsx').read_text()


def _theme_var_names() -> frozenset[str]:
    block = re.search(r'THEME_VAR_NAMES[^=]*=\s*Object\.freeze\(\[(.*?)\]', _HOOKS, re.S)
    assert block, 'THEME_VAR_NAMES not found in packages/renderer-hooks/src/index.tsx'
    return frozenset(re.findall(r"'--([\w-]+)'", block.group(1)))


def _token_table() -> dict[str, frozenset[str]]:
    start = re.search(r'export const tokens\s*=\s*Object\.freeze\(\{', _HOOKS)
    assert start, 'tokens table not found in packages/renderer-hooks/src/index.tsx'
    body = _HOOKS[start.end():_HOOKS.index('\n});', start.end())]
    table = {}
    for group in ('color', 'font'):
        inner = re.search(rf'\b{group}:\s*Object\.freeze\(\{{(.*?)\}}\)', body, re.S)
        assert inner, f'tokens.{group} not found'
        table[group] = frozenset(re.findall(r'^\s*(\w+)\s*:', inner.group(1), re.M))
    table['tokens'] = frozenset(re.findall(r'^    (\w+)\s*:', body, re.M))
    return table


THEME_VAR_NAMES = _theme_var_names()
TOKENS = _token_table()

_VAR_USE = re.compile(r'var\(--([\w-]+)')
_GROUP_USE = re.compile(r'(?<![\w.])(?:tokens\.)?(color|font)\.(\w+)')
_TOP_USE = re.compile(r'(?<![\w.])tokens\.(\w+)')
_ALIAS = re.compile(r'\bconst\s+(\w+)\s*=\s*tokens\.(color|font)\s*;')


def test_token_contract_parses_R05():
    assert len(THEME_VAR_NAMES) == 25, sorted(THEME_VAR_NAMES)
    assert {'ink', 'sheet2', 'line2', 'red', 'teal'} <= TOKENS['color']
    assert TOKENS['font'] == {'ui', 'mono', 'prose'}
    assert {'color', 'font', 'radius', 'shadow', 'lift'} <= TOKENS['tokens']


def _unknown_tokens(text: str) -> list[tuple[int, str]]:
    aliases = {m.group(1): m.group(2) for m in _ALIAS.finditer(text)}
    alias_use = re.compile(rf'(?<![\w.])({"|".join(map(re.escape, aliases))})\.(\w+)') if aliases else None
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        bad += [(n, f'var(--{m})') for m in _VAR_USE.findall(line) if m not in THEME_VAR_NAMES]
        bad += [(n, f'{g}.{k}') for g, k in _GROUP_USE.findall(line) if k not in TOKENS[g]]
        bad += [(n, f'tokens.{k}') for k in _TOP_USE.findall(line) if k not in TOKENS['tokens']]
        if alias_use:
            bad += [(n, f'{a}.{k}') for a, k in alias_use.findall(line) if k not in TOKENS[aliases[a]]]
    return bad


@pytest.mark.parametrize('source', _SOURCES, ids=lambda p: str(p.relative_to(WORKSPACE)))
def test_media_and_utility_renderers_use_only_skin_tokens_R05(source):
    text = source.read_text()
    problems = [
        f'{source.name}:{n}: {line.strip()}'
        for n, line in enumerate(text.splitlines(), 1)
        if _LITERAL.search(line) or _FALLBACK.search(line) or _RETIRED.search(line)
    ]
    problems += [f'{source.name}:{n}: unknown token {ref}' for n, ref in _unknown_tokens(text)]
    assert not problems, 'colour literal, token fallback, retired or unknown token:\n' + '\n'.join(problems)


@pytest.mark.parametrize('snippet', [
    "background: 'var(--sheet4)'",
    "color: 'var(--surface)'",
    'color: color.muted',
    'fontFamily: tokens.font.sans',
    'borderRadius: tokens.radiusMd',
    'const F = tokens.font;\nconst x = F.serif;',
])
def test_token_scan_rejects_unknown_names_R05(snippet):
    assert _unknown_tokens(snippet), snippet


def test_token_scan_rejects_retired_names_R05():
    for name in ('--radius-md', '--glass-panel', '--plasma-pink'):
        assert _RETIRED.search(f'var({name})'), name


_NODE_CASES = {
    'image': ('visual/image', 'IMAGE_RENDERER_BUNDLE'),
    'slides': ('visual/slides', 'SLIDES_RENDERER_BUNDLE'),
    'table': ('utility/table', 'TABLE_RENDERER_BUNDLE'),
    'schema_form': ('utility/schema_form', 'SCHEMA_FORM_BUNDLE'),
}


@node_renderer_skip(RENDERERS / 'visual/slides/theme.test.ts',
                    'node + esbuild + tsx (npm --prefix scripts install) required for the renderer token tests')
@pytest.mark.parametrize('name', sorted(_NODE_CASES))
def test_media_and_utility_renderer_style_helpers_R05(name, tmp_path):
    folder, env_key = _NODE_CASES[name]
    package = RENDERERS / folder
    run_node_renderer(package / 'main.tsx', tmp_path / f'{name}_bundle.mjs', package / 'theme.test.ts', env_key,
                      label=f'{name} renderer token test', bundle_label=f'content/renderers/{folder}/main.tsx')
