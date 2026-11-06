# SPDX-License-Identifier: MIT
"""The document view's element pick (``useDocumentView({pickElements})`` →
``pick(signal, hint?)``, ``pickAvailable``) and the html renderer's answer to
an annotation request through it.

The view probes the page's picker on every load and arms it with the skin's
colours once it answers; the page's ``picked`` and ``abandoned`` settle the
pick, an abort or a newer pick cancels it, and a page that never answers
rejects it after ``PICK_READY_WAIT_MS``. Only the loaded page's own window
is heard. These run the authored hooks under Node
(``tests/_document_view_hooks.py``); the page bridge itself runs in
``tests/test_html_renderer_pick.py``.
"""
from tests._document_view_hooks import node_hooks_skip, run_hooks

pytestmark = node_hooks_skip

# A probe component over the real hook, its frame loaded.
_PROBE = r'''
const api = { onThemeChange() {} };
const prepare = (html) => ({ html });
let input = { pickElements: true };
const render = () => m.__render(() => m.useDocumentView({ html: '<p>x</p>', api, theme: NIGHT, dark: false, prepare, ...input }));
m.__reset();
let view = render();
const win = frame();
view.frameRef.current = win;
const page = win.contentWindow;
const fromPage = (data) => dispatchMessage({ source: page, data });
const starts = () => win.posts.filter((p) => p.type === m.PICK_MESSAGE.start);
const cancels = () => win.posts.filter((p) => p.type === m.PICK_MESSAGE.cancel);
'''


def test_the_view_probes_its_page_and_marks_the_picker_available(tmp_path):
    run_hooks(tmp_path, _PROBE + r'''
assert.ok(view.srcDoc.includes('collimate:pick-start'), 'the srcDoc carries the bridge');
assert.equal(view.pickAvailable, false);
view.onLoad();
assert.deepEqual(win.posts, [{ type: 'collimate:pick-probe' }]);
// Another window's ready is not the page's.
dispatchMessage({ source: {}, data: { type: m.PICK_MESSAGE.ready } });
view = render();
assert.equal(view.pickAvailable, false);
fromPage({ type: m.PICK_MESSAGE.ready });
view = render();
assert.equal(view.pickAvailable, true);
// A reload clears it until the new page answers.
view.onLoad();
view = render();
assert.equal(view.pickAvailable, false);
''')


def test_a_pick_arms_the_page_and_resolves_its_element(tmp_path):
    run_hooks(tmp_path, r'''
''' + _PROBE + r'''
view.onLoad();
fromPage({ type: m.PICK_MESSAGE.ready });
const picking = view.pick(new AbortController().signal, 'Click it');
const [start] = starts();
assert.deepEqual(start, { type: 'collimate:pick-start', id: start.id, hint: 'Click it', colors: m.pickColors(NIGHT) });
assert.ok(start.id);
assert.equal(m.pickColors(NIGHT).line, '#c69d64ff');
const data = { type: m.PICK_MESSAGE.picked, id: start.id, tag: 'LI', css: '#sec > li:nth-of-type(2)', elementId: 'sec',
               snippet: 'Second item', html: '<li>Second item</li>' };
// Another window, or another pick's id: ignored.
dispatchMessage({ source: {}, data });
fromPage({ ...data, id: 'other' });
fromPage(data);
assert.deepEqual(await picking, m.elementPickOf(data));
assert.equal((await picking).tag, 'li');
''')


def test_a_pick_pending_across_a_reload_re_arms_in_the_new_page(tmp_path):
    run_hooks(tmp_path, _PROBE + r'''
view.onLoad();
const picking = view.pick(new AbortController().signal);
// The bridge has not answered: nothing is posted yet.
assert.deepEqual(starts(), []);
fromPage({ type: m.PICK_MESSAGE.ready });
const [first] = starts();
assert.equal(first.hint, '');
// A live_refresh swap reloads the page mid-pick: the view probes the new
// page and arms it again with the same pick.
view.onLoad();
assert.deepEqual(win.posts.at(-1), { type: m.PICK_MESSAGE.probe });
fromPage({ type: m.PICK_MESSAGE.ready });
assert.equal(starts().length, 2);
assert.deepEqual(starts()[1], first);
fromPage({ type: m.PICK_MESSAGE.picked, id: first.id, tag: 'p', css: 'p:nth-of-type(1)', elementId: '', snippet: 'x', html: '<p>x</p>' });
assert.equal((await picking).css, 'p:nth-of-type(1)');
''')


def test_abort_a_newer_pick_and_esc_resolve_null(tmp_path):
    run_hooks(tmp_path, _PROBE + r'''
view.onLoad();
fromPage({ type: m.PICK_MESSAGE.ready });
// Abort: the page is told, the pick resolves null.
const controller = new AbortController();
const aborted = view.pick(controller.signal);
const [one] = starts();
controller.abort();
assert.equal(await aborted, null);
assert.deepEqual(cancels(), [{ type: m.PICK_MESSAGE.cancel, id: one.id }]);
// A pick already aborted posts nothing.
const dead = new AbortController(); dead.abort();
assert.equal(await view.pick(dead.signal), null);
assert.equal(starts().length, 1);
// A newer pick replaces the pending one.
const older = view.pick(new AbortController().signal);
const newer = view.pick(new AbortController().signal);
assert.equal(await older, null);
const [, two, three] = starts();
assert.deepEqual(cancels().at(-1), { type: m.PICK_MESSAGE.cancel, id: two.id });
assert.notEqual(two.id, three.id);
// Esc in the page abandons it.
fromPage({ type: m.PICK_MESSAGE.abandoned, id: three.id });
assert.equal(await newer, null);
''')


def test_a_page_that_never_answers_rejects_the_pick(tmp_path):
    run_hooks(tmp_path, r'''
const clock = captureTimers();
''' + _PROBE + r'''
view.onLoad();
const picking = view.pick(new AbortController().signal);
clock.fire(m.PICK_READY_WAIT_MS);
await assert.rejects(picking, { message: m.PICK_UNREACHABLE });
// A view built without the picker refuses a pick and listens for nothing.
input = { pickElements: false };
m.__reset();
const before = messageListeners();
view = render();
assert.equal(view.srcDoc.includes('collimate:pick-start'), false);
assert.equal(messageListeners(), before);
await assert.rejects(view.pick(new AbortController().signal), { message: 'This view was built without pickElements.' });
''')


def test_the_html_renderer_answers_a_pick_request_with_the_element(tmp_path):
    run_hooks(tmp_path, r'''
const HtmlPage = m.htmlRenderer.component;
globalThis.__file = { content: '<p>page</p>', fileName: 'docs/page.html',
                      api: { getFileUrl: (p) => '/api/f/' + p, onThemeChange() {} } };
globalThis.__theme = NIGHT;
m.__reset();
const tree = m.__render(HtmlPage);
const iframe = find(tree, 'iframe');
assert.ok(iframe.props.srcDoc.includes('collimate:pick-start'));
const win = frame();
iframe.props.ref.current = win;
iframe.props.onLoad();
const handler = globalThis.__annotationHandler;
assert.equal(typeof handler, 'function');
const answering = handler({ id: 'r1', mode: 'pick', hint: 'Pick a row' }, new AbortController().signal);
dispatchMessage({ source: win.contentWindow, data: { type: m.PICK_MESSAGE.ready } });
const start = win.posts.find((p) => p.type === m.PICK_MESSAGE.start);
assert.equal(start.hint, 'Pick a row');
const data = { type: m.PICK_MESSAGE.picked, id: start.id, tag: 'li', css: '#sec > ul:nth-of-type(1) > li:nth-of-type(2)',
               elementId: 'sec', snippet: 'Second item', html: '<li>Second item</li>' };
dispatchMessage({ source: win.contentWindow, data });
assert.deepEqual(await answering, m.elementContext('docs/page.html', m.elementPickOf(data)));
// Esc in the page: the request is abandoned.
const abandoned = handler({ id: 'r2', mode: 'pick' }, new AbortController().signal);
const second = win.posts.filter((p) => p.type === m.PICK_MESSAGE.start).at(-1);
dispatchMessage({ source: win.contentWindow, data: { type: m.PICK_MESSAGE.abandoned, id: second.id } });
assert.equal(await abandoned, null);
// A selection is not this page's to answer.
await assert.rejects(handler({ id: 'r3', mode: 'selection' }, new AbortController().signal),
                     { name: 'AnnotationUnavailable', message: 'This page answers element picks only.' });
''')


def test_the_html_renderer_refuses_a_pick_its_page_cannot_answer(tmp_path):
    run_hooks(tmp_path, r'''
const clock = captureTimers();
const HtmlPage = m.htmlRenderer.component;
globalThis.__file = { content: '<p>page</p>', fileName: 'page.html',
                      api: { getFileUrl: (p) => '/api/f/' + p, onThemeChange() {} } };
globalThis.__theme = NIGHT;
m.__reset();
const tree = m.__render(HtmlPage);
const iframe = find(tree, 'iframe');
iframe.props.ref.current = frame();
iframe.props.onLoad();
const answering = globalThis.__annotationHandler({ id: 'r1', mode: 'pick' }, new AbortController().signal);
clock.fire(m.PICK_READY_WAIT_MS);
await assert.rejects(answering, { name: 'AnnotationUnavailable', message: m.PICK_UNREACHABLE });
''')
