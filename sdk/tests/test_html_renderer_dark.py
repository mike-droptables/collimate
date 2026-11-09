# SPDX-License-Identifier: MIT
"""Regression coverage for the page renderer (``renderers/visual/html/main.tsx``):
DarkReader injection onto the skin's colours, the ``known`` theme gate and
the token chrome (R04, R04 W2b).

Uses the shared ``tests/_node_renderer.py`` harness: the helpers are EXPORTED
from the renderer entrypoint. To exercise them under plain Node this test bundles ``main.tsx`` with its CDN/SDK imports stubbed
(``scripts/bundle_renderer_test.mjs``) into a temporary importable ESM module,
then runs ``dark.test.ts`` under node:test via the tsx loader with
``HTML_RENDERER_BUNDLE`` pointing at that bundle.

Skips when Node, esbuild, or the tsx loader isn't available (e.g. a Python-only
CI job that never ran ``npm --prefix scripts install``).
"""
from __future__ import annotations

import json
import re

from tests._node_renderer import (
    REPO as _REPO,
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import RENDERERS

_TEST_FILE = RENDERERS / "visual" / "html" / "dark.test.ts"
_MAIN_TSX = RENDERERS / "visual" / "html" / "main.tsx"
_VIEW = _REPO / "packages" / "renderer-hooks" / "src" / "document-view.tsx"
# The html renderer's view: its page reports the pointer for the style
# control and runs the element picker for annotation requests.
_VIEW_CALL = ('useDocumentView({\n'
              '        html: content, api, theme, dark: pageDark(style.theme), prepare, variables, reportPointer: true,\n'
              '        pickElements: true,\n'
              '    });')

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the html renderer dark-mode test')


def test_html_renderer_dark_injection(tmp_path):
    """dark.test.ts against main.tsx and the SDK's document view it renders
    through (packages/renderer-hooks/src/document-view.tsx)."""
    entry = tmp_path / "html_entry.ts"
    entry.write_text(f"export * from {json.dumps(str(_MAIN_TSX))};\nexport * from {json.dumps(str(_VIEW))};\n")
    run_node_renderer(entry, tmp_path / 'html_renderer_bundle.mjs', _TEST_FILE, 'HTML_RENDERER_BUNDLE',
                      label='html renderer frame/theme/dark-mode test', bundle_label='main.tsx')


def test_document_view_colour_literals_R04_W2b():
    """No colour literal in the document view or the renderer. A darkened
    page is darkened onto the skin (DarkReader's ground and text are the
    skin's ``--sheet`` and ``--ink``, resolved from the record), and the
    backing behind it paints the same token. The one colour that is not the
    skin's is the documented authored-page case, DarkReader off in a dark
    skin: the page's own default ground, the browser's light ``Canvas``
    keyword, in ``frameBacking`` only. The picker's colours are the skin's
    too (``pickColors``), with the system's ``Highlight`` keywords as the
    page's fallback. DarkReader's filters stay at its defaults, which leave
    the skin colours untouched."""
    view = _VIEW.read_text()
    main = _MAIN_TSX.read_text()
    for source in (view, main):
        literals = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", source)
        assert literals == [], literals
    assert re.findall(r"'Canvas'", view) == ["'Canvas'"]
    assert "'Canvas'" not in main
    assert "return { background: 'Canvas', colorScheme: 'light' };" in view
    assert 'frameBacking(settled, dark, scheme)' in view
    assert 'page' + 'Scheme' not in view
    for setting in ('brightness:', 'contrast:', 'sepia:'):
        assert setting not in view, setting
    assert 'resolveVar(record, poles.ground)' in view
    assert 'resolveVar(record, poles.text)' in view


def test_document_view_preview_and_repaint_wiring_R04_W2b():
    """useDocumentView decides through the exported helpers dark.test.ts
    pins, and keeps the live repaint wired. Every page waits for the host's
    skin record, an authored one too. ``documentSkinPost`` and
    ``deliveredAfterLoad`` drive ``deliver``, which runs on every skin
    change (DarkReader's colours or the variables block) and again on each
    load of a swapped srcDoc; the srcDoc bakes the skin through a ref, so a
    skin change never rebuilds it. The hooks run in
    ``test_document_view_skin_change.py``; the browser half is the app's
    ``frontend/tests/renderers/html-darkreader-live.spec.ts``."""
    source = _VIEW.read_text()
    body = source[source.index('export function useDocumentView('):]
    assert 'const settled = pushed || waited;' in body
    assert 'const pushed = themeSettled(theme, api);' in body
    assert 'documentSkinPost(listening, delivered.current, skinKey)' in body
    assert 'const listening = dark || hasBlock;' in body
    assert body.count('delivered.current = deliveredAfterLoad(preview && preview.key);') == 2
    assert 'useEffect(deliver, [deliver]);' in body
    on_load = body[body.index('const onLoad = useCallback('):]
    on_load = on_load[:on_load.index('}, [preview, deliver, pickElements, postToPage, awaitBridge]);')]
    assert 'deliveredAfterLoad(' in on_load and 'deliver();' in on_load
    # Every load probes the page's picker, so a pick pending across a
    # reload re-arms in the new page.
    assert 'postToPage({ type: PICK_MESSAGE.probe })' in on_load
    assert 'documentSrcDoc(prepare(html), dark, baked.options, src, baked.block, reportPointer, pickElements)' in body
    assert '}, [html, prepare, dark, settled, src, hasBlock, reportPointer, pickElements]);' in body
    assert "if (!settled || html === null) return null;" in body
    # The dark choice is the caller's (its style's theme), else the skin's.
    assert "const dark = typeof choice === 'boolean' ? choice : scheme === 'dark';" in body


def test_html_renderer_renders_through_the_document_view_polish():
    """The html renderer keeps only its prepare step, its frame and its style
    control: the view and the backing are the SDK's, and no frame is drawn.
    The page's look is the style's theme (RS-04)."""
    main = _MAIN_TSX.read_text()
    page = main[main.index('function HtmlPage()'):]
    assert _VIEW_CALL in page
    assert '<div style={view.pageStyle}>' in page
    assert 'onLoad={view.onLoad}' in page and 'ref={view.frameRef}' in page
    assert 'style={DOCUMENT_VIEW_STYLE.iframe}' in page
    # The page's frame swallows the pointer, so the page reports it and the
    # control measures the reports over its frame; nothing covers the page
    # and the page keeps no hover state.
    assert '<StyleControl api={api} themes={PAGE_THEMES} font={false} size={PAGE_TEXT_SIZE} frame={view.frameRef} />' in page
    assert 'reach=' not in page
    for gone in ('visible={', 'setOver', 'onMouseEnter'):
        assert gone not in page, gone
    for gone in ('padding: 12', 'borderRadius', "border: '1px solid", 'injectDarkReader', 'localStorage',
                 'cdn.jsdelivr.net'):
        assert gone not in main, gone


def test_html_renderer_answers_annotation_picks():
    """The page answers the host's annotation requests with an element pick:
    a pick arms the document view's picker and the element clicked is the
    context (``elementContext``); a selection, and a page whose picker never
    answers, are refused as unavailable. The variables block is the text
    size's zoom alone, and the prepare step hands the view the page only."""
    main = _MAIN_TSX.read_text()
    assert 'useAnnotationRequest, annotationUnavailable,' in main
    assert "import type { AnnotationRequest, AnnotationContext, ElementPick } from '/api/sdk/react.js';" in main
    assert 'useAnnotationRequest(api, answer);' in main
    assert "if (request.mode !== 'pick') throw annotationUnavailable(" in main
    assert 'return picked ? elementContext(fileName, picked) : null;' in main
    assert 'try { picked = await pick(signal, request.hint); }' in main
    assert 'return { html: rewriteSiblingRefs(html, api, docPath) };' in main
    assert 'const variables = pageZoomRule(style.textSize);' in main
    assert 'const prepare = useCallback((html: string) => preparePage(html, api, fileName), [api, fileName]);' in main
    for gone in ('status' + 'Variable', 'darkReader' + 'Fixes', 'STATUS' + '_', 'page' + 'Scheme'):
        assert gone not in main, gone
