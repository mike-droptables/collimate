"""Portable compiler uses the same real Node/WASM graph and package identity."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests._workspace import BUILTINS, RENDERERS


ROOT = Path(__file__).resolve().parents[1]


def test_browser_compiler_core_bundles_for_the_browser__F1868_F1280():
    """The core the app's browser worker imports (scripts/renderer_bundle_core.mjs)
    compiles for a browser target: real browser-target compilation refuses
    accidental Node builtins."""
    result = subprocess.run(['node', '--input-type=module', '-e', '''
import esbuild from './scripts/node_modules/esbuild/lib/main.js';
await esbuild.build({entryPoints:[process.argv[1]],bundle:true,platform:'browser',write:false});
''', str(ROOT / 'scripts/renderer_bundle_core.mjs')], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_actual_node_and_wasm_compile_same_graphs_and_all_shipped_packages__F1868_F1280_P0F21__D14(tmp_path):
    result = subprocess.run(['node', '--input-type=module', '-e', '''
import {createRendererCompiler} from './scripts/renderer_bundle_core.mjs';
import native from './scripts/node_modules/esbuild/lib/main.js';
import wasm from './scripts/node_modules/esbuild-wasm/lib/main.js';
import {CONTRACT, collectPackage, bundleRenderer} from './scripts/renderer_bundle.mjs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const digest = bytes => new Uint8Array(crypto.createHash('sha256').update(bytes).digest());
const compile = createRendererCompiler({contract:CONTRACT,compiler:wasm,digest}).bundleRenderer;
const requests = JSON.parse(fs.readFileSync(0,'utf8'));
for (const request of requests) {
  const input = {files:collectPackage(request.directory),entrypoint:request.entrypoint};
  assert.deepEqual(await compile(input), await bundleRenderer(input));
}
const files = new Map(Object.entries({
  'main.ts': 'import {value} from "./folder"; export default <number>value;',
  'folder/index.ts': 'import x from "../data.json"; export const value=x.value;',
  'data.json': '{"value":3}',
  '\\uE000.txt':'BMP', '\\u{10000}.txt':'non-BMP',
}).map(([k,v])=>[k,new TextEncoder().encode(v)]));
assert.deepEqual(await compile({files,entrypoint:'main.ts'}),await bundleRenderer({files,entrypoint:'main.ts'}));
for (const spec of ['../outside','/etc/passwd','./_runs/private','react','bad/api/sdk/react.js']) {
  const bad = new Map([['main.ts',new TextEncoder().encode(`import x from ${JSON.stringify(spec)}; export default x;`)]]);
  await assert.rejects(compile({files:bad,entrypoint:'main.ts'}));
  await assert.rejects(bundleRenderer({files:bad,entrypoint:'main.ts'}));
}
assert.throws(()=>createRendererCompiler({contract:CONTRACT,compiler:{...native,version:'wrong'},digest}));
wasm.stop(); native.stop();
console.log(JSON.stringify({shipped:requests.length,fixture:'ts-json-directory-unicode',hostile:5}));
'''], cwd=ROOT, input=json.dumps(_requests()), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['shipped'] == 19


def _requests():
    from collimate._host.renderer_bundle import _build_requests
    return _build_requests(RENDERERS) + _build_requests(BUILTINS)


def test_async_hashing_cannot_mix_changed_input_bytes_with_old_identity__F1868(tmp_path):
    result = subprocess.run(['node', '--input-type=module', '-e', '''
import {createRendererCompiler} from './scripts/renderer_bundle_core.mjs';
import native from './scripts/node_modules/esbuild/lib/main.js';
import {CONTRACT,bundleRenderer} from './scripts/renderer_bundle.mjs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
const source=new TextEncoder().encode('export default "original";');
const files=new Map([['main.ts',source]]);
const expected=await bundleRenderer({files,entrypoint:'main.ts'});
let release;
const pause=new Promise(resolve=>{release=resolve;});
const digest=async bytes=>{await pause; return new Uint8Array(crypto.createHash('sha256').update(bytes).digest());};
const compiler=createRendererCompiler({contract:CONTRACT,compiler:native,digest});
const pending=compiler.bundleRenderer({files,entrypoint:'main.ts'});
source.fill(0);
files.set('main.ts',new TextEncoder().encode('export default "changed";'));
release();
assert.deepEqual(await pending,expected);
native.stop();
'''], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
