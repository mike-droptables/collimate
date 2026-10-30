# SPDX-License-Identifier: MIT
"""A diff view's current side follows its file (packages/renderer-hooks/src/platform.tsx).

The app keeps a diff open against a live file: a "Compare with…" tab shows
the cell's own file (the current side) against another file (the base). The
file keeps changing under it (a save from its own tab, a run, another
writer), so defineRenderer's diff mount follows host-selected content while
the base side stays the mount's snapshot.

These run the authored source under Node: platform.tsx bundled with esbuild,
React replaced by a small hook runner that re-renders on a state change and
runs effects whose dependencies changed.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
PLATFORM = REPO / 'packages/renderer-hooks/src/platform.tsx'
ESBUILD = REPO / 'scripts/node_modules/esbuild'

pytestmark = pytest.mark.skipif(
    shutil.which('node') is None or not ESBUILD.exists(),
    reason='node + esbuild (npm --prefix scripts install) required for the diff mount tests')

# React as a hook runner: hooks keep their state by call order, a state
# change re-renders the tree synchronously, and an effect runs after the
# render when its dependencies changed. Context providers set the value
# their subtree reads.
_REACT = r'''
let slots = [], at = 0, effects = [], root = null, rendering = false, dirty = false;
const same = (a, b) => !!a && !!b && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
function slot(init) { if (at === slots.length) slots.push(init()); return slots[at++]; }
function walk(node) {
  if (node == null || typeof node !== 'object') return;
  if (Array.isArray(node)) { node.forEach(walk); return; }
  const { type, props } = node;
  if (type && type.__context) {
    const outer = type.__context.value;
    type.__context.value = props.value;
    walk(props.children);
    type.__context.value = outer;
    return;
  }
  if (typeof type === 'function') {
    walk(type.prototype && typeof type.prototype.render === 'function' ? new type(props).render() : type(props));
    return;
  }
  walk(props.children);
}
function draw() {
  if (rendering) { dirty = true; return; }
  rendering = true;
  try {
    do {
      dirty = false; at = 0; effects = [];
      walk(root);
      for (const run of effects) run();
    } while (dirty);
  } finally { rendering = false; }
}
export function __mount(element) { root = element; draw(); }
export function __unmount() {
  for (const s of slots) if (s && typeof s.cleanup === 'function') s.cleanup();
  slots = []; root = null;
}
export function useState(initial) {
  const s = slot(() => ({ v: typeof initial === 'function' ? initial() : initial }));
  return [s.v, (next) => {
    if (!root) { globalThis.lateSets = (globalThis.lateSets ?? 0) + 1; return; }
    const v = typeof next === 'function' ? next(s.v) : next;
    if (Object.is(v, s.v)) return;
    s.v = v;
    draw();
  }];
}
export function useRef(value) { return slot(() => ({ current: value })); }
export function useEffect(fn, deps) {
  const s = slot(() => ({ deps: null, cleanup: undefined }));
  if (deps && same(s.deps, deps)) return;
  s.deps = deps;
  effects.push(() => { if (typeof s.cleanup === 'function') s.cleanup(); s.cleanup = fn(); });
}
export const useLayoutEffect = useEffect;
export function useMemo(fn, deps) {
  const s = slot(() => ({ deps: null, v: undefined }));
  if (!deps || !same(s.deps, deps)) { s.v = fn(); s.deps = deps; }
  return s.v;
}
export function useCallback(fn, deps) { return useMemo(() => fn, deps); }
export function createContext(value) { const context = { value }; context.Provider = { __context: context }; return context; }
export function useContext(context) { return context.value; }
export function createElement(type, props, ...children) {
  return { type, props: { ...(props || {}), children: children.length <= 1 ? children[0] : children } };
}
class Component { constructor(props) { this.props = props; } }
export default { createElement, Component, Fragment: 'Fragment' };
'''

_DOM_CLIENT = r'''
import { __mount, __unmount } from 'react';
export function createRoot() { return { render: (element) => __mount(element), unmount: () => __unmount() }; }
'''

# A host api for one diff mount of notes.md: the file's text on "disk"
# (what getSiblingFile reads, optionally held back by a gate), and the
# files-changed and set-content subscribers the test fires by hand.
_HOST = r'''
globalThis.host = ({ push = true } = {}) => {
  const h = { disk: 'after v1', reads: 0, gate: null, readies: 0, filesChanged: new Set(), pushes: new Set() };
  h.api = {
    getContent: async () => 'after v1',
    getDiffContent: async () => 'before',
    getDiffLabels: () => ({ base: 'CellTwo · notes.md', current: 'CellOne · notes.md' }),
    getFilename: () => 'notes.md',
    getSiblingFile: async (path) => {
      assert.equal(path, 'notes.md');
      h.reads++;
      const text = h.disk;
      if (h.gate) await h.gate;
      return text;
    },
    onFilesChange: (cb) => { h.filesChanged.add(cb); return () => h.filesChanged.delete(cb); },
    ready: () => { h.readies++; },
    isReadOnly: () => true,
  };
  if (push) h.api.onSetContent = (cb) => { h.pushes.add(cb); return () => h.pushes.delete(cb); };
  h.changed = (...changedFiles) => { for (const cb of [...h.filesChanged]) cb({ cellId: 'c1', changedFiles }); };
  h.push = (text) => { for (const cb of [...h.pushes]) cb(text); };
  return h;
};
globalThis.settle = () => new Promise((resolve) => setTimeout(resolve, 0));
'''


def _bundle(tmp_path) -> Path:
    stubs = tmp_path / 'stubs'
    stubs.mkdir()
    (stubs / 'react.mjs').write_text(_REACT)
    (stubs / 'react-dom-client.mjs').write_text(_DOM_CLIENT)
    entry = tmp_path / 'entry.ts'
    entry.write_text(f'export {{ defineRenderer, useCollimateFile }} from {json.dumps(str(PLATFORM))};\n')
    out = tmp_path / 'bundle.mjs'
    script = tmp_path / 'build.cjs'
    script.write_text(f'''
const esbuild = require({json.dumps(str(ESBUILD))});
esbuild.buildSync({{
  entryPoints: [{json.dumps(str(entry))}], outfile: {json.dumps(str(out))}, bundle: true, format: 'esm',
  platform: 'neutral', target: 'es2022', jsx: 'transform', logLevel: 'error',
  alias: {{ 'react': {json.dumps(str(stubs / 'react.mjs'))}, 'react-dom/client': {json.dumps(str(stubs / 'react-dom-client.mjs'))} }},
}});
''')
    built = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    return out


def _run(tmp_path, body: str) -> None:
    module = _bundle(tmp_path)
    script = tmp_path / 'run.mjs'
    script.write_text(
        "import assert from 'node:assert/strict';\nglobalThis.assert = assert;\n" + _HOST
        + f"const m = await import({json.dumps(module.as_uri())});\n"
        + "const seen = [];\n"
        + "const last = () => seen[seen.length - 1];\n"
        + "const Diff = () => { seen.push(m.useCollimateFile()); return null; };\n"
        + "const renderer = m.defineRenderer({ component: () => null, diffComponent: Diff });\n"
        + body)
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_built_in_diff_view_is_ready_at_first_paint_even_when_the_editor_defers(tmp_path):
    # deferReady governs the renderer's own components: its editor (milkdown)
    # or a custom diffComponent (monaco) calls api.ready() itself, but the
    # built-in diff view has painted at its first commit. Without the ready
    # the host keeps the frame hidden until its fallback timer.
    _run(tmp_path, r'''
const builtIn = host();
const unmountBuiltIn = await m.defineRenderer({ component: () => null, deferReady: true }).mountDiff({}, builtIn.api);
assert.equal(builtIn.readies, 1);
unmountBuiltIn();
const custom = host();
const unmountCustom = await m.defineRenderer({ component: () => null, diffComponent: Diff, deferReady: true }).mountDiff({}, custom.api);
assert.equal(custom.readies, 0);
assert.equal(last().content, 'after v1');
unmountCustom();
''')
