# SPDX-License-Identifier: MIT
"""The style control shows only near its corner, never anywhere over the
renderer (``packages/renderer-hooks/src/style-control.tsx``).

The control owns its reach: hidden at rest; shown once a pointer move over
the watched element (its parent, or ``watch``) comes within
``STYLE_CONTROL_REACH`` of the icon's box; hidden again on a move far away,
on leaving the watched element or on entering a child frame; shown while
its popover is open or the icon has keyboard focus, whatever the pointer.
A move sets state only when nearness changes. Over a frame whose page
reports the pointer (``frame``; the html renderer's page, through the
document view's ``reportPointer``) the page's reports are measured the same
way, mapped onto the frame's box, and nothing is laid over the page.

These run the authored component under Node: the source bundled with
esbuild, React replaced by a small hook runner that keeps state between
renders and runs effects whose dependencies changed, and stand-in elements
that keep and fire their listeners. The renderers' own tests pin which
element each one watches.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / 'packages/renderer-hooks/src/style-control.tsx'
VIEW = REPO / 'packages/renderer-hooks/src/document-view.tsx'
ESBUILD = REPO / 'scripts/node_modules/esbuild'

pytestmark = pytest.mark.skipif(
    shutil.which('node') is None or not ESBUILD.exists(),
    reason='node + esbuild (npm --prefix scripts install) required for the style control tests')

# React as a hook runner: state, refs and memos persist between renders by
# call order; an effect runs after the render when its dependencies changed.
# `__sets` counts the state setter calls that changed a value.
_REACT = r'''
let slots = [], at = 0, pending = [];
export const __count = { sets: 0 };
const same = (a, b) => !!a && !!b && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
function slot(init) { if (at === slots.length) slots.push(init()); return slots[at++]; }
export function __reset() { slots = []; }
export function __render(fn) {
  at = 0; pending = [];
  const out = fn();
  for (const run of pending) run();
  return out;
}
export function useState(initial) {
  const s = slot(() => ({ v: typeof initial === 'function' ? initial() : initial }));
  return [s.v, (next) => {
    const v = typeof next === 'function' ? next(s.v) : next;
    if (!Object.is(v, s.v)) __count.sets++;
    s.v = v;
  }];
}
export function useRef(value) { return slot(() => ({ current: value })); }
export function useMemo(fn, deps) {
  const s = slot(() => ({ deps: null, v: undefined }));
  if (!deps || !same(s.deps, deps)) { s.v = fn(); s.deps = deps; }
  return s.v;
}
export function useCallback(fn, deps) { return useMemo(() => fn, deps); }
function effect(fn, deps) {
  const s = slot(() => ({ deps: null, cleanup: undefined }));
  if (deps && same(s.deps, deps)) return;
  s.deps = deps;
  pending.push(() => { if (typeof s.cleanup === 'function') s.cleanup(); s.cleanup = fn(); });
}
export const useEffect = effect;
export const useLayoutEffect = effect;
export const createContext = (value) => ({ Provider: 'Provider', value });
export const useContext = (context) => context.value;
export function createElement(type, props, ...children) { return { type, props: { ...(props || {}), children } }; }
export const Fragment = 'Fragment';
export default { createElement, Fragment, Component: class {} };
'''

_ENTRY = f'''
export {{ __render, __reset, __count }} from 'react';
export {{ StyleControl, StyleControlRow, STYLE_CONTROL_REACH, withinReach, framePoint }} from {json.dumps(str(CONTROL))};
export {{ POINTER_MESSAGE, documentSrcDoc }} from {json.dumps(str(VIEW))};
'''

# Stand-in elements: listeners kept per type, fired by hand.
_GLOBALS = r'''
globalThis.window = globalThis;
globalThis.el = (name, extra = {}) => {
  const on = {};
  return {
    name, on, ...extra,
    addEventListener(type, fn) { (on[type] ??= new Set()).add(fn); },
    removeEventListener(type, fn) { on[type]?.delete(fn); },
    fire(type, init = {}) {
      const e = { type, composedPath: () => init.path ?? [this], ...init };
      for (const fn of [...(on[type] ?? [])]) fn(e);
    },
    count(type) { return on[type] ? on[type].size : 0; },
  };
};
// The icon at the top right of a 1000px wide renderer, 8px in.
globalThis.ICON_BOX = { left: 968, top: 8, right: 992, bottom: 32 };
globalThis.find = (node, test) => {
  if (!node || typeof node !== 'object') return null;
  if (test(node)) return node;
  for (const kid of [].concat(node.props?.children ?? [])) { const hit = find(kid, test); if (hit) return hit; }
  return null;
};
globalThis.API = {
  getStyle: () => ({ theme: null, font: null, textSize: null }),
  onStyleChange: () => () => {},
  setStyle() {},
  getTheme: () => ({ themeId: 'night', scheme: 'dark', known: true, vars: {}, fonts: { ui: '', mono: '', prose: '' } }),
  onThemeChange: () => () => {},
  getFontFamilies: () => [],
};
globalThis.SIZE = { min: 0.5, max: 2, step: 0.1, default: 1 };
/** Mount the control: render, attach its root and icon, render again so
 *  the reach listeners attach. Returns a probe over the latest tree. */
globalThis.mount = (m, props, { parent = el('parent'), focusVisible = true } = {}) => {
  m.__reset();
  const doc = el('document', { defaultView: el('window') });
  const root = el('root', { parentElement: parent, ownerDocument: doc, getRootNode: () => doc });
  const icon = { getBoundingClientRect: () => ({ ...ICON_BOX }), matches: () => focusVisible, focus() {} };
  const view = () => m.StyleControl({ api: API, themes: [], font: false, size: SIZE, ...props });
  let tree = m.__render(view);
  const rootNode = () => find(tree, (n) => n.props && 'data-collimate-style-control' in n.props);
  const button = () => find(tree, (n) => n.type === 'button' && n.props['aria-label'] === 'Style');
  rootNode().props.ref(root);
  button().props.ref.current = icon;
  tree = m.__render(view);
  const probe = {
    parent, root, icon, doc,
    render() { tree = m.__render(view); return probe; },
    get tree() { return tree; },
    get rootNode() { return rootNode(); },
    get button() { return button(); },
    get shown() {
      const b = button().props.style.opacity;
      const p = rootNode().props.style.pointerEvents;
      assert.equal(b === 1, p === 'auto', 'the icon shows exactly when it takes the pointer');
      return b === 1;
    },
  };
  return probe;
};
'''


def _bundle(tmp_path) -> Path:
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'react.mjs').write_text(_REACT)
    (stubs / 'react-dom.mjs').write_text('export const createRoot = () => ({ render() {}, unmount() {} });')
    # The package compiles JSX to the automatic runtime (its tsconfig).
    (stubs / 'jsx-runtime.mjs').write_text(
        "export { Fragment } from 'react';\n"
        "export const jsx = (type, { children, ...props } = {}) => "
        "({ type, props: { ...props, children: children === undefined ? [] : children } });\n"
        "export const jsxs = jsx;\nexport const jsxDEV = jsx;\n")
    entry = tmp_path / 'entry.ts'
    entry.write_text(_ENTRY)
    out = tmp_path / 'bundle.mjs'
    script = tmp_path / 'build.cjs'
    script.write_text(f'''
const esbuild = require({json.dumps(str(ESBUILD))});
const stubs = {{
  react: {json.dumps(str(stubs / 'react.mjs'))},
  dom: {json.dumps(str(stubs / 'react-dom.mjs'))},
  jsx: {json.dumps(str(stubs / 'jsx-runtime.mjs'))},
}};
const plugin = {{ name: 'stubs', setup(build) {{
  build.onResolve({{ filter: /^react\\/jsx(-dev)?-runtime$/ }}, () => ({{ path: stubs.jsx }}));
  build.onResolve({{ filter: /^react$/ }}, () => ({{ path: stubs.react }}));
  build.onResolve({{ filter: /^react-dom(\\/client)?$/ }}, () => ({{ path: stubs.dom }}));
}} }};
esbuild.build({{
  entryPoints: [{json.dumps(str(entry))}], outfile: {json.dumps(str(out))}, bundle: true, format: 'esm',
  platform: 'neutral', target: 'es2022', jsx: 'transform', logLevel: 'error', plugins: [plugin],
}}).catch((e) => {{ console.error(e); process.exit(1); }});
''')
    built = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    return out


def _run(tmp_path, body: str) -> None:
    module = _bundle(tmp_path)
    script = tmp_path / 'run.mjs'
    script.write_text("import assert from 'node:assert/strict';\nglobalThis.assert = assert;\n" + _GLOBALS
                      + f"const m = await import({json.dumps(module.as_uri())});\n" + body)
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_reach_is_a_named_band_around_the_icon(tmp_path):
    """REACH is 40-56px; a point is near when it is within it of the box on
    any side, the edge included."""
    _run(tmp_path, r'''
const R = m.STYLE_CONTROL_REACH;
assert.ok(R >= 40 && R <= 56, String(R));
assert.equal(m.withinReach(ICON_BOX, 980, 20), true, 'on the icon');
assert.equal(m.withinReach(ICON_BOX, ICON_BOX.left - R, ICON_BOX.bottom + R), true, 'the reach is inclusive');
assert.equal(m.withinReach(ICON_BOX, ICON_BOX.left - R - 1, 20), false);
assert.equal(m.withinReach(ICON_BOX, 980, ICON_BOX.bottom + R + 1), false);
assert.equal(m.withinReach(ICON_BOX, 500, 300), false, 'over the content');
''')


def test_hidden_at_rest_shown_near_the_corner_hidden_far_away(tmp_path):
    """The parent's moves decide: anywhere over the renderer is not enough;
    near the icon shows it; far away, leaving, or entering a frame hides it.
    Moves that do not change nearness set no state."""
    _run(tmp_path, r'''
const c = mount(m, {});
assert.equal(c.shown, false, 'hidden at rest');
assert.equal(c.parent.count('pointermove'), 1, 'the parent is watched by default');
assert.equal(c.parent.count('pointerleave'), 1);

const sets = m.__count.sets;
c.parent.fire('pointermove', { clientX: 500, clientY: 300 });
c.parent.fire('pointermove', { clientX: 10, clientY: 10 });
c.parent.fire('pointermove', { clientX: 900, clientY: 200 });
assert.equal(m.__count.sets, sets, 'moves over the content set nothing');
assert.equal(c.render().shown, false, 'over the renderer, away from the corner: hidden');

c.parent.fire('pointermove', { clientX: 940, clientY: 50 });
assert.equal(m.__count.sets, sets + 1);
for (let x = 941; x < 960; x++) c.parent.fire('pointermove', { clientX: x, clientY: 50 });
assert.equal(m.__count.sets, sets + 1, 'moves within the reach set nothing more');
assert.equal(c.render().shown, true, 'within the reach: shown');

c.parent.fire('pointermove', { clientX: 600, clientY: 400 });
assert.equal(c.render().shown, false, 'a move far away hides it');

c.parent.fire('pointermove', { clientX: 990, clientY: 30 });
assert.equal(c.render().shown, true);
c.parent.fire('pointerleave');
assert.equal(c.render().shown, false, 'leaving the watched element hides it');

c.parent.fire('pointermove', { clientX: 990, clientY: 30 });
assert.equal(c.render().shown, true);
const frame = { tagName: 'IFRAME' };
c.parent.fire('pointerover', { path: [frame], target: frame });
assert.equal(c.render().shown, false, 'entering a child frame hides it: the frame swallows the moves');
c.parent.fire('pointermove', { clientX: 990, clientY: 30 });
c.parent.fire('pointerout', { relatedTarget: frame });
assert.equal(c.render().shown, false);
''')


def test_shown_while_open_and_while_keyboard_focused(tmp_path):
    """The open popover keeps the icon whatever the pointer; Tab reaches the
    transparent icon and shows it, a mouse focus does not."""
    _run(tmp_path, r'''
const c = mount(m, {});
c.button.props.onClick();
c.render();
assert.ok(find(c.tree, (n) => n.props && n.props.role === 'dialog'), 'the popover is open');
c.parent.fire('pointermove', { clientX: 100, clientY: 400 });
c.parent.fire('pointerleave');
assert.equal(c.render().shown, true, 'open: shown, wherever the pointer is');
c.doc.fire('keydown', { key: 'Escape', stopPropagation() {} });
assert.equal(c.render().shown, false, 'closed, the pointer far away: hidden');

c.button.props.onFocus({ currentTarget: c.icon });
assert.equal(c.render().shown, true, 'keyboard focus shows it');
c.button.props.onBlur();
assert.equal(c.render().shown, false);
// The icon stays in the tab order at rest: transparent, never hidden from focus.
assert.equal(c.button.props.style.visibility, undefined);
assert.equal(c.button.props.style.opacity, 0);

const mouse = mount(m, {}, { focusVisible: false });
mouse.button.props.onFocus({ currentTarget: mouse.icon });
assert.equal(mouse.render().shown, false, 'a focus without focus-visible shows nothing');
''')


def test_watch_measures_another_element(tmp_path):
    """`watch` replaces the parent: a control in a toolbar row or a header
    measures moves over the whole renderer."""
    _run(tmp_path, r'''
const wrapper = el('wrapper');
const c = mount(m, { watch: { current: wrapper } });
assert.equal(c.parent.count('pointermove'), 0);
assert.equal(wrapper.count('pointermove'), 1);
wrapper.fire('pointermove', { clientX: 960, clientY: 60 });
assert.equal(c.render().shown, true);
wrapper.fire('pointerleave');
assert.equal(c.render().shown, false);
''')


def test_a_frame_that_reports_the_pointer_is_measured_through_its_reports(tmp_path):
    """`frame`: the page's reports (POINTER_MESSAGE, its viewport position)
    are mapped onto the frame's box and measured against the icon, so the
    corner of a page shows the icon with nothing laid over the page. Only
    that frame's window is heard, only while the pointer is in the frame: a
    report that arrives after the pointer left (it waits for the page's next
    animation frame) shows nothing."""
    _run(tmp_path, r"""
const page = { name: 'page' };
// The frame fills a 1000 x 600 renderer, drawn at half scale (a 2000 x 1200
// viewport): the report is scaled onto the box.
const frame = { tagName: 'IFRAME', contentWindow: page,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 600 }) };
const c = mount(m, { frame: { current: frame } });
const win = c.doc.defaultView;
assert.equal(win.count('message'), 1, 'the control hears the page');
assert.equal(find(c.rootNode, (n) => n.type === 'div' && n.props['aria-hidden'] === 'true'), null, 'nothing over the page');
assert.equal(c.shown, false);
const report = (x, y, source = page) =>
  win.fire('message', { source, data: { type: m.POINTER_MESSAGE, x, y, w: 2000, h: 1200 } });

c.parent.fire('pointerover', { path: [frame], target: frame });
assert.equal(c.render().shown, false, 'entering the reporting frame decides nothing yet');
report(1000, 600);
assert.equal(c.render().shown, false, 'over the page, away from the corner');
report(1900, 60);
assert.equal(c.render().shown, true, 'the page reports the pointer near the corner: shown');
report(1900, 60, {});
report(1000, 600, {});
assert.equal(c.render().shown, true, 'another window is not heard');
win.fire('message', { source: page, data: { type: 'other', x: 1000, y: 600, w: 2000, h: 1200 } });
win.fire('message', { source: page, data: { type: m.POINTER_MESSAGE, x: 'a', y: 600, w: 2000, h: 1200 } });
assert.equal(c.render().shown, true, 'nothing but a well-formed report is measured');
report(400, 900);
assert.equal(c.render().shown, false, 'a report far away hides it');

// From the page onto the icon: the frame's pointerout, then the host's own
// moves over the icon; a report that arrives late is ignored.
report(1900, 60);
c.parent.fire('pointerout', { path: [frame], target: frame, relatedTarget: c.icon });
c.parent.fire('pointermove', { clientX: 980, clientY: 20 });
report(400, 900);
assert.equal(c.render().shown, true, 'a late report after the pointer left the frame is ignored');
// Back into the frame from the icon: no hide; the reports decide again.
c.parent.fire('pointerout', { path: [c.icon], target: c.icon, relatedTarget: frame });
c.parent.fire('pointerover', { path: [frame], target: frame });
assert.equal(c.render().shown, true);
report(400, 900);
assert.equal(c.render().shown, false);
// Leaving the renderer from the page's corner: a late report shows nothing.
report(1900, 60);
c.parent.fire('pointerleave');
report(1900, 60);
assert.equal(c.render().shown, false, 'left: a report still in flight is ignored');
// Another frame (not the reporting one) still hides the icon.
c.parent.fire('pointerover', { path: [frame], target: frame });
report(1900, 60);
const other = { tagName: 'IFRAME' };
c.parent.fire('pointerover', { path: [other], target: other });
assert.equal(c.render().shown, false);

// Without `frame` nobody listens for reports.
const plain = mount(m, {});
assert.equal(plain.doc.defaultView.count('message'), 0);
// The mapping: viewport position scaled onto the box, null for anything else.
assert.deepEqual(m.framePoint({ left: 10, top: 20, width: 400, height: 300 },
  { type: m.POINTER_MESSAGE, x: 200, y: 150, w: 400, h: 300 }), { x: 210, y: 170 });
assert.equal(m.framePoint({ left: 0, top: 0, width: 1, height: 1 }, { type: m.POINTER_MESSAGE, x: 1, y: 1, w: 0, h: 1 }), null);
assert.equal(m.framePoint({ left: 0, top: 0, width: 1, height: 1 }, null), null);
""")


def test_the_page_bootstrap_reports_into_the_control(tmp_path):
    """End to end off-DOM: the srcDoc the document view builds with
    `reportPointer` runs in a stand-in page whose parent is the control's
    window; a move near the page's top-right corner shows the icon after the
    page's animation frame, one far away hides it."""
    _run(tmp_path, r"""
const page = { name: 'page' };
const frame = { tagName: 'IFRAME', contentWindow: page,
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1000, height: 600 }) };
const c = mount(m, { frame: { current: frame } });
const win = c.doc.defaultView;
const srcDoc = m.documentSrcDoc({ html: '<html><head></head><body></body></html>' }, false, {}, '', null, true);
const body = /<script>([\s\S]*?)<\/script>/.exec(srcDoc)[1];
let move = null, frameCb = null;
Object.assign(page, {
  innerWidth: 1000, innerHeight: 600,
  parent: { postMessage: (data) => win.fire('message', { source: page, data }) },
  addEventListener: (type, fn) => { if (type === 'pointermove') move = fn; },
  requestAnimationFrame: (cb) => { frameCb = cb; return 1; },
});
new Function('window', 'document', 'DarkReader', body)(page, {}, undefined);
assert.equal(typeof move, 'function', 'the page listens for moves');
move({ clientX: 990, clientY: 30 });
assert.equal(c.render().shown, false, 'nothing before the page\'s frame');
frameCb();
assert.equal(c.render().shown, true, 'near the corner: shown');
move({ clientX: 500, clientY: 300 });
frameCb();
assert.equal(c.render().shown, false, 'over the page: hidden');
""")


def test_extra_rows_follow_the_style_rows_and_alone_make_the_same_corner_control(tmp_path):
    """`extra`: the renderer's own rows, in the popover under the style rows
    (a rule between the two). With no style row the control still shows for
    them alone: the same icon, hidden at rest, shown near the corner, by
    keyboard focus and while open. A render function gets `close`, which
    closes the popover. No style row and no extra: nothing renders."""
    _run(tmp_path, r"""
const dialog = (c) => find(c.tree, (n) => n.props && n.props.role === 'dialog');
const text = (node) => node == null || typeof node === 'boolean' ? ''
  : typeof node !== 'object' ? String(node)
  : [].concat(node.props?.children ?? []).map(text).join('');
// The popover's direct rows, fragments opened (components are not rendered).
const kids = (node) => [].concat(node.props.children).flat(Infinity).filter((k) => k && typeof k === 'object')
  .flatMap((k) => (k.type === 'Fragment' ? kids(k) : [k]));
const row = m.StyleControlRow({ label: 'Page', children: '1 / 3' });
const marker = { type: 'span', props: { 'data-extra': 'page', children: [] } };

// Nothing to offer: no control at all.
m.__reset();
assert.equal(m.__render(() => m.StyleControl({ api: API, themes: [], font: false, size: false })), null);
assert.equal(m.__render(() => m.StyleControl({ api: API, themes: [], font: false, size: false, extra: null })), null);

// Extra alone: the same corner control, hidden at rest, near the corner shown.
const only = mount(m, { size: false, extra: marker });
assert.equal(only.shown, false, 'hidden at rest');
assert.equal(only.button.props['aria-label'], 'Style');
assert.equal(only.rootNode.props.style.position, 'absolute');
assert.equal(only.rootNode.props.style.top, '0.5rem');
assert.equal(only.rootNode.props.style.right, '0.5rem');
assert.equal(only.button.props.style.width, '1.5rem');
only.parent.fire('pointermove', { clientX: 500, clientY: 300 });
assert.equal(only.render().shown, false, 'over the content: hidden');
only.parent.fire('pointermove', { clientX: 960, clientY: 60 });
assert.equal(only.render().shown, true, 'within the reach: shown');
only.parent.fire('pointerleave');
only.button.props.onFocus({ currentTarget: only.icon });
assert.equal(only.render().shown, true, 'keyboard focus shows it');
only.button.props.onBlur();
only.button.props.onClick();
only.render();
const d = dialog(only);
assert.ok(d, 'open');
assert.ok(find(d, (n) => n.props && n.props['data-extra'] === 'page'), 'the extra node is in the popover');
assert.equal(find(d, (n) => n.props && n.props.role === 'separator'), null, 'no rule without style rows');
assert.ok(!text(d).includes('Text size') && !text(d).includes('Theme'), 'no style rows');
only.parent.fire('pointerleave');
assert.equal(only.render().shown, true, 'open: shown wherever the pointer is');

// Under the style rows, after a rule.
const both = mount(m, { extra: row });
both.button.props.onClick();
both.render();
const order = kids(dialog(both));
const size = order.findIndex((n) => text(n) === 'Text size');
const rule = order.findIndex((n) => n.props.role === 'separator');
const page = order.findIndex((n) => text(n) === 'Page');
assert.ok(size >= 0 && rule > size && page > rule, `style rows, the rule, then the extra rows: ${[size, rule, page]}`);
assert.equal(dialog(both).props.style.gridTemplateColumns, '4.25rem 1fr', 'the extra rows share the label column');

// A render function receives close, which closes the popover.
let ctx = null;
const fn = mount(m, { size: false, extra: (c) => { ctx = c; return marker; } });
assert.equal(ctx, null, 'not rendered while closed');
fn.button.props.onClick();
fn.render();
assert.equal(typeof ctx.close, 'function');
assert.ok(find(dialog(fn), (n) => n.props && n.props['data-extra'] === 'page'));
ctx.close();
fn.render();
assert.equal(dialog(fn), null, 'close() closes the popover');

// StyleControlRow: the label in the label column, the controls in a row.
const [label, cell] = [].concat(row.props.children);
assert.equal(label.type, 'span');
assert.equal(text(label), 'Page');
assert.equal(label.props.style.color, 'var(--ink-2)');
assert.equal(cell.type, 'div');
assert.equal(cell.props.style.display, 'flex');
assert.equal(text(cell), '1 / 3');
""")
