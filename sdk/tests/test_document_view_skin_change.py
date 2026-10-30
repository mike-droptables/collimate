# SPDX-License-Identifier: MIT
"""A skin change recolours a document view's page in place (R04 W2b).

The html renderer's page (``renderers/visual/html/main.tsx``) shows through
the SDK's document view (``packages/renderer-hooks/src/document-view.tsx``).
A skin change must not rebuild the ``srcDoc`` (that reloads the user's
page): the view posts DarkReader's new colours, with the page's variables
block (the html renderer's text-size zoom), into the loaded page instead.

These run the authored hooks under Node (``tests/_document_view_hooks.py``).
The browser half is the app's
``frontend/tests/renderers/html-darkreader-live.spec.ts``.
"""
from tests._document_view_hooks import node_hooks_skip, run_hooks

pytestmark = node_hooks_skip


def test_a_skin_change_recolours_the_html_page_in_place_R04_W2b(tmp_path):
    """The html renderer's srcDoc is built once, on the skin current then; a
    skin change that moves DarkReader's colours posts them into the loaded
    page, with the variables block it already has, and leaves the srcDoc
    alone."""
    run_hooks(tmp_path, r'''
const HtmlPage = m.htmlRenderer.component;
globalThis.__file = { content: '<p>done</p>', fileName: 'page.html',
                      api: { getFileUrl: (p) => '/api/f/' + p, onThemeChange() {} } };
globalThis.__theme = NIGHT;
m.__reset();
// The page is built on the first render, on the skin current then.
let tree = m.__render(HtmlPage);
const iframe = find(tree, 'iframe');
const built = iframe.props.srcDoc;
// The page's own text size: an empty block, its element ready for a size.
assert.ok(built.includes(`<style id="${m.DOCUMENT_VARIABLES_ID}"></style>`), built);
assert.ok(built.includes('DarkReader.enable(' + JSON.stringify(m.darkReaderOptions(NIGHT)) + ')'), built);
const parsed = parses;
// The page loads.
const win = frame();
iframe.props.ref.current = win;
iframe.props.onLoad();
assert.deepEqual(win.posts.filter((p) => p.type === m.DARKREADER_MESSAGE), [], 'the srcDoc already carries night');

// night -> night-cool: DarkReader's colours move.
globalThis.__theme = COOL;
tree = m.__render(HtmlPage);
assert.equal(find(tree, 'iframe').props.srcDoc, built, 'the same srcDoc: no reload');
assert.equal(parses, parsed, 'the prepare step did not run again');
assert.deepEqual(win.posts.filter((p) => p.type === m.DARKREADER_MESSAGE),
                 [{ type: m.DARKREADER_MESSAGE, options: m.darkReaderOptions(COOL), variables: '' }]);
// A re-render on the same skin posts nothing more.
m.__render(HtmlPage);
assert.equal(win.posts.filter((p) => p.type === m.DARKREADER_MESSAGE).length, 1);
''')


def test_dark_mode_off_in_a_dark_skin_the_html_page_sits_on_canvas(tmp_path):
    """With dark mode off (the style's As authored) in a dark skin, the html
    renderer's page sits on the browser's light Canvas, its own default
    ground. No frame is mounted until the host's theme arrives: the srcDoc
    is null until then, and an empty frame is a light page."""
    run_hooks(tmp_path, r'''
const HtmlPage = m.htmlRenderer.component;
globalThis.__style = { theme: 'authored', font: null, textSize: null, set() {}, onChange() { return () => {}; } };
globalThis.__file = { content: '<p>page</p>', fileName: 'page.html',
                      api: { getFileUrl: (p) => '/api/f/' + p, onThemeChange() {} } };
globalThis.__theme = { ...NIGHT, known: false };
m.__reset();
let tree = m.__render(HtmlPage);
assert.equal(find(tree, 'iframe'), null, 'no frame before the host theme');
assert.equal(tree.props.style.background, 'transparent');
globalThis.__theme = NIGHT;
tree = m.__render(HtmlPage);
assert.ok(find(tree, 'iframe'));
assert.equal(tree.props.style.background, 'Canvas');
assert.equal(tree.props.style.colorScheme, 'light');
''')
