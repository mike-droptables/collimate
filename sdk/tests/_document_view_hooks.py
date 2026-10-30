# SPDX-License-Identifier: MIT
"""Run the document view's hooks, and the html renderer that renders through
them, under Node (``tests/test_document_view_skin_change.py``,
``tests/test_document_view_pick.py``).

The sources are bundled with esbuild, React replaced by a small hook runner
that keeps state between renders and runs effects whose dependencies
changed, the SDK by the real document view with the file, the skin record
and the style under the test's control, and a stand-in DOM just big enough
for the html renderer's prepare step.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._workspace import RENDERERS


REPO = Path(__file__).resolve().parents[1]
VIEW = REPO / 'packages/renderer-hooks/src/document-view.tsx'
HTML_MAIN = RENDERERS / 'visual/html/main.tsx'
ESBUILD = REPO / 'scripts/node_modules/esbuild'

node_hooks_skip = pytest.mark.skipif(
    shutil.which('node') is None or not ESBUILD.exists(),
    reason='node + esbuild (npm --prefix scripts install) required for the document view hook tests')

# React as a hook runner: state, refs and memos persist between renders by
# call order; an effect runs after the render when its dependencies changed.
_REACT = r'''
let slots = [], at = 0, pending = [];
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
  return [s.v, (next) => { s.v = typeof next === 'function' ? next(s.v) : next; }];
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

# The SDK as a renderer imports it: the real document view, with the file,
# the skin record and the style under the test's control. The annotation
# hook hands the test the renderer's latest handler.
_SDK = f'''
export * from {json.dumps(str(VIEW))};
export const defineRenderer = (definition) => definition;
export const useCollimateFile = () => globalThis.__file;
export const useTheme = () => globalThis.__theme;
export const useStyle = () => globalThis.__style
  ?? {{ theme: null, font: null, textSize: null, set() {{}}, onChange() {{ return () => {{}}; }} }};
export const StyleControl = () => null;
export const useAnnotationRequest = (api, handler) => {{ globalThis.__annotationHandler = handler; }};
export const annotationUnavailable = (message) => Object.assign(new Error(message), {{ name: 'AnnotationUnavailable' }});
'''

_ENTRY = f'''
export {{ __render, __reset }} from 'react';
export {{ default as htmlRenderer, elementContext }} from {json.dumps(str(HTML_MAIN))};
export {{
  PICK_MESSAGE, PICK_READY_WAIT_MS, PICK_UNREACHABLE, pickColors, elementPickOf,
  darkReaderOptions, DARKREADER_MESSAGE, DOCUMENT_VARIABLES_ID, useDocumentView,
}} from {json.dumps(str(VIEW))};
'''

# A window, storage and a parser just big enough for the html renderer's
# prepare step: elements with attributes and children that serialize, and a
# parse that counts itself.
_GLOBALS = r'''
globalThis.window = globalThis;
globalThis.location = { href: 'http://127.0.0.1:8000/' };
const listeners = {};
globalThis.addEventListener = (type, fn) => { (listeners[type] ??= new Set()).add(fn); };
globalThis.removeEventListener = (type, fn) => { listeners[type]?.delete(fn); };
globalThis.dispatchEvent = () => true;
/** Deliver a message event to every window listener. */
globalThis.dispatchMessage = (event) => { for (const fn of [...(listeners.message ?? [])]) fn(event); };
globalThis.messageListeners = () => (listeners.message?.size ?? 0);
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.text = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({ name, value })); }
  set id(v) { this.attrs.id = v; }
  set href(v) { this.attrs.href = v; }
  get textContent() { return this.text; }
  set textContent(v) { this.text = v; }
  get firstChild() { return this.kids[0] ?? null; }
  insertBefore(node, ref) {
    const i = ref ? this.kids.indexOf(ref) : -1;
    if (i < 0) this.kids.push(node); else this.kids.splice(i, 0, node);
    return node;
  }
  appendChild(node) { this.kids.push(node); return node; }
  prepend(node) { this.kids.unshift(node); }
  get outerHTML() {
    const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
    if (this.tag === 'meta' || this.tag === 'base') return `<${this.tag}${attrs}>`;
    return `<${this.tag}${attrs}>${this.text}${this.kids.map((k) => k.outerHTML).join('')}</${this.tag}>`;
  }
}
globalThis.parses = 0;
globalThis.DOMParser = class {
  parseFromString(src) {
    globalThis.parses++;
    const whole = /^<html><head><\/head><body>([\s\S]*)<\/body><\/html>$/.exec(src);
    const root = new El('html'), head = new El('head'), body = new El('body');
    body.text = whole ? whole[1] : src;
    root.kids = [head, body];
    return { documentElement: root, head, body, createElement: (tag) => new El(tag), querySelectorAll: () => [] };
  }
};
const SKIN = (id, sheet, ink, brass) => ({
  themeId: id, scheme: 'dark', known: true, fonts: { ui: '', mono: '', prose: '' },
  vars: { '--sheet': sheet, '--ink': ink, '--brass': brass, '--brass-soft': brass + '29' },
});
globalThis.NIGHT = SKIN('night', '#1c1a17', '#e2ddd4', '#c69d64');
globalThis.COOL = SKIN('night-cool', '#171d23', '#dbdfe3', '#b8a07a');
/** A loaded frame: what the view posts into it. */
globalThis.frame = () => {
  const posts = [];
  return { posts, contentWindow: { postMessage: (message) => posts.push(message) } };
};
/** The first node of `type` in a rendered tree. */
globalThis.find = (node, type) => {
  if (!node || typeof node !== 'object') return null;
  if (node.type === type) return node;
  for (const kid of [].concat(node.props?.children ?? [])) { const hit = find(kid, type); if (hit) return hit; }
  return null;
};
/** Capture window timers instead of running them: `fire(ms)` runs the
 *  latest one set for `ms`. */
globalThis.captureTimers = () => {
  const timers = [];
  globalThis.setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
  globalThis.clearTimeout = () => {};
  return { timers, fire(ms) { const hit = timers.filter(([, t]) => t === ms).at(-1); assert.ok(hit, `no ${ms} ms timer`); hit[0](); } };
};
'''


def _bundle(tmp_path) -> Path:
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'react.mjs').write_text(_REACT)
    (stubs / 'sdk.mjs').write_text(_SDK)
    entry = tmp_path / 'entry.ts'
    entry.write_text(_ENTRY)
    out = tmp_path / 'bundle.mjs'
    script = tmp_path / 'build.cjs'
    script.write_text(f'''
const esbuild = require({json.dumps(str(ESBUILD))});
const stubs = {{
  react: {json.dumps(str(stubs / 'react.mjs'))},
  sdk: {json.dumps(str(stubs / 'sdk.mjs'))},
}};
const plugin = {{ name: 'stubs', setup(build) {{
  build.onResolve({{ filter: /^(react|https:\\/\\/esm\\.sh\\/react@.*)$/ }}, () => ({{ path: stubs.react }}));
  build.onResolve({{ filter: /^\\/api\\/sdk\\/react\\.js$/ }}, () => ({{ path: stubs.sdk }}));
}} }};
esbuild.build({{
  entryPoints: [{json.dumps(str(entry))}], outfile: {json.dumps(str(out))}, bundle: true, format: 'esm',
  platform: 'neutral', target: 'es2022', jsx: 'transform', logLevel: 'error', plugins: [plugin],
}}).catch((e) => {{ console.error(e); process.exit(1); }});
''')
    built = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    return out


def run_hooks(tmp_path, body: str) -> None:
    """Run ``body`` (ESM, top-level await allowed) with the bundle as ``m``
    and ``assert`` from node:assert/strict."""
    module = _bundle(tmp_path)
    script = tmp_path / 'run.mjs'
    script.write_text("import assert from 'node:assert/strict';\n" + _GLOBALS
                      + f"const m = await import({json.dumps(module.as_uri())});\n" + body
                      + "\nprocess.exit(0);\n")
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
