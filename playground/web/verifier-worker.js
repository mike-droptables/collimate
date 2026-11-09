// P4F09: separately supervised, authority-free verifier/compiler adapter.
// Bundle this module with its pinned dependencies; init supplies both WASM bytes.
import variant from '@jitl/quickjs-wasmfile-release-asyncify';
import {newVariant, newQuickJSAsyncWASMModuleFromVariant} from 'quickjs-emscripten-core';
import esbuild from 'esbuild-wasm/lib/browser.js';
import {createRendererCompiler, packagePath} from './renderer_bundle_core.mjs';

const MAX_BYTES = 32 * 1024 ** 2, MAX_FRAME = 64 * 1024 ** 2;
const encoder = new TextEncoder();
let engine, contract, rendererCompiler;
const bytes = text => encoder.encode(text).length;
const bounded = (text, limit = MAX_BYTES) => {
  if (typeof text !== 'string' || bytes(text) > limit) throw Error('Verifier input exceeds byte limit');
  return text;
};
const diagnostic = error => String(error?.message || 'Verifier engine failed').slice(0, 4096);
function checkFrame(value) {
  if (bytes(JSON.stringify(value)) > MAX_FRAME) throw Error('Verifier frame exceeds byte limit');
}
function decodeFiles(pairs) {
  if (!Array.isArray(pairs) || !pairs.length || pairs.length > 100000) throw Error('Invalid compiler file map');
  const files = new Map(); let size = 0;
  for (const pair of pairs) {
    if (!Array.isArray(pair) || pair.length !== 2) throw Error('Invalid compiler file');
    const [name, base64] = pair; packagePath(name);
    if (files.has(name) || typeof base64 !== 'string') throw Error('Invalid compiler file');
    const raw = Uint8Array.from(atob(base64), char => char.charCodeAt(0));
    size += raw.length; if (size > MAX_BYTES) throw Error('Compiler input exceeds byte limit');
    files.set(name, raw);
  }
  return files;
}
function extension(name) { const base = name.split('/').at(-1); return base.slice(base.lastIndexOf('.')); }
async function transform(files, entrypoint, policy) {
  const loader = policy.options.loader[extension(entrypoint)];
  if (!loader) throw Error('Unsupported renderer entrypoint type');
  const o = policy.options;
  const result = await esbuild.transform(files.get(entrypoint), {loader, target:o.target,
    jsx:o.jsx, jsxFactory:o.jsxFactory, jsxFragment:o.jsxFragment,
    legalComments:o.legalComments, sourcefile:entrypoint, logLevel:'silent'});
  return bounded(result.code);
}
async function compile({files: pairs, entrypoint, policy, verifier}) {
  const files = decodeFiles(pairs); packagePath(entrypoint);
  if (!files.has(entrypoint)) throw Error('Missing compiler entrypoint');
  const expected = structuredClone(contract);
  if (verifier) { expected.options.format = 'iife'; expected.global_name = '__collimate_verifier'; expected.external_prefixes = []; expected.imports = ['collimate/verify']; }
  const canonical = value => value && typeof value === 'object' ? Array.isArray(value) ? value.map(canonical) : Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])])) : value;
  if (JSON.stringify(canonical(policy)) !== JSON.stringify(canonical(expected))) throw Error('Unsupported compiler contract');
  if (!verifier) {
    const result = await rendererCompiler.bundleRenderer({files, entrypoint});
    return {code:bounded(result.code), entrypoint_js:await transform(files, entrypoint, policy)};
  }
  if (entrypoint !== 'verify.ts' || files.size !== 2 || !files.has('prelude.js')) throw Error('Invalid verifier compiler package');
  const plugin = {name:'collimate-verifier', setup(build) {
    build.onResolve({filter:/.*/}, args => {
      if (args.kind === 'entry-point') return {path:'verify.ts',namespace:'collimate-verifier'};
      if (args.namespace !== 'collimate-verifier' || args.importer !== 'verify.ts' || args.path !== 'collimate/verify') return {errors:[{text:'A verifier may import only collimate/verify'}]};
      return {path:'prelude.js',namespace:'collimate-verifier'};
    });
    build.onLoad({filter:/.*/,namespace:'collimate-verifier'}, args => ({contents:files.get(args.path),loader:policy.options.loader[extension(args.path)]}));
  }};
  const result = await esbuild.build({...policy.options,globalName:'__collimate_verifier',entryPoints:['verify.ts'],write:false,logLevel:'silent',plugins:[plugin]});
  if (result.outputFiles?.length !== 1) throw Error('Compiler must return exactly one in-memory module');
  return {code:bounded(result.outputFiles[0].text),entrypoint_js:''};
}
async function bundleCallback(filesJSON, entrypointJSON) {
  try {
    bounded(filesJSON);
    if (typeof entrypointJSON !== 'string' || Array.from(entrypointJSON).length > 4096) throw Error('bundle arguments exceed byte limit');
    const values = JSON.parse(filesJSON), entrypoint = JSON.parse(entrypointJSON);
    if (!values || typeof values !== 'object' || Array.isArray(values) || !Object.keys(values).length || Object.keys(values).length > 10000) throw Error('bundle needs a bounded virtual file map');
    try { packagePath(entrypoint); } catch { throw Error('bundle entrypoint must name a virtual package file'); }
    if (!Object.hasOwn(values, entrypoint)) throw Error('bundle entrypoint must name a virtual package file');
    const files = new Map(); let total = 0;
    for (const [name, text] of Object.entries(values)) {
      try { packagePath(name); } catch { throw Error('bundle accepts relative text files only; opaque content is unavailable'); }
      if (typeof text !== 'string') throw Error('bundle accepts relative text files only; opaque content is unavailable');
      const raw = encoder.encode(text); total += raw.length;
      if (total > MAX_BYTES) throw Error('bundle arguments exceed byte limit');
      files.set(name, raw);
    }
    const result = await rendererCompiler.bundleRenderer({files,entrypoint});
    bounded(result.code); result.entrypoint_js = await transform(files,entrypoint,contract);
    return bounded(JSON.stringify({ok:true,result}));
  } catch (error) { return JSON.stringify({ok:false,error:diagnostic(error)}); }
}
async function evaluate({code,input_json,boundary}) {
  bounded(code); bounded(input_json); bounded(boundary);
  if (!JSON.parse(input_json) || Array.isArray(JSON.parse(input_json)) || typeof JSON.parse(input_json) !== 'object') throw Error('Invalid verifier input');
  const runtime = engine.newRuntime(); runtime.setMemoryLimit(64 * 1024 ** 2); runtime.setMaxStackSize(512 * 1024);
  const context = runtime.newContext();
  try {
    const host = context.newAsyncifiedFunction('__collimate_host_bundle', async (files, entrypoint) => {
      // Only canonical boundary passes captured JSON strings. Never dump guest objects.
      if (context.typeof(files) !== 'string' || context.typeof(entrypoint) !== 'string') return context.newString(JSON.stringify({ok:false,error:'bundle arguments must be JSON strings'}));
      return context.newString(await bundleCallback(context.getString(files),context.getString(entrypoint)));
    });
    context.setProp(context.global,'__collimate_host_bundle',host); host.dispose();
    // Indirect captured eval prevents authored code reaching our lexical normalizer.
    // The boundary itself is evaluated byte-for-byte as supplied by canonical Python.
    const harness = `(()=>{const evaluate=eval; const normalize=evaluate(${JSON.stringify(boundary)}); evaluate(${JSON.stringify(code)}); return normalize(${JSON.stringify(input_json)});})()`;
    const result = await context.evalCodeAsync(harness,'verifier-invocation.js');
    if (result.error) { result.error.dispose(); return {verdict_json:JSON.stringify({valid:false,error:'verifier engine failed or exhausted its resource limit'})}; }
    try {
      if (context.typeof(result.value) !== 'string') throw Error('Verifier output exceeds byte limit');
      const verdict_json = context.getString(result.value);
      if (verdict_json.length > 4096 + 128) throw Error('Verifier output exceeds byte limit');
      return {verdict_json};
    } finally { result.value.dispose(); }
   } finally {
    context.dispose();
    // Pinned 0.32 Asyncify deletes the runtime callback registry before freeing
    // its JS host refs (the sync variant frees first). Keep that registry alive
    // for FreeRuntime, then remove it. This touches trusted engine bookkeeping
    // only and also handles guest-retained/cyclic references to bundle.
    const callbacks = engine.callbacks, remove = callbacks.deleteRuntime;
    const pointer = runtime.rt.value;
    callbacks.deleteRuntime = () => {};
    try { runtime.dispose(); }
    finally { callbacks.deleteRuntime = remove; remove.call(callbacks, pointer); }
  }
}
export async function dispatch(request) {
  checkFrame(request);
  if (request.op === 'init') {
    if (engine) throw Error('Verifier worker already initialized');
    contract = structuredClone(request.contract);
    // The SDK core refuses a contract that pins another esbuild than the one
    // bundled here, before any engine is allocated.
    rendererCompiler = createRendererCompiler({contract,compiler:esbuild,digest:async raw=>new Uint8Array(await crypto.subtle.digest('SHA-256',raw))});
    if (!(request.quickjs_wasm instanceof ArrayBuffer) || !(request.esbuild_wasm instanceof ArrayBuffer) || request.quickjs_wasm.byteLength > MAX_BYTES || request.esbuild_wasm.byteLength > MAX_BYTES) throw Error('Engine bytes required');
    engine = await newQuickJSAsyncWASMModuleFromVariant(newVariant(variant,{wasmBinary:request.quickjs_wasm}));
    const wasmModule = await WebAssembly.compile(request.esbuild_wasm);
    await esbuild.initialize({wasmModule,worker:false});
    return {ready:true};
  }
  if (!engine) throw Error('Verifier worker unavailable');
  if (!(request.remaining_ms > 0 && Number.isFinite(request.remaining_ms))) throw Error('verifier budget exceeded');
  if (request.op === 'compile') return compile(request);
  if (request.op === 'evaluate') return evaluate(request);
  throw Error('Unknown verifier worker operation');
}
let busy = false;
if (typeof self !== 'undefined' && typeof self.postMessage === 'function') self.onmessage = async ({data}) => {
  if (busy) { self.postMessage({id:data.id,ok:false,error:'Verifier worker busy'}); return; }
  busy = true;
  try { const result = await dispatch(data); checkFrame(result); self.postMessage({id:data.id,ok:true,result}); }
  catch (error) { self.postMessage({id:data.id,ok:false,error:diagnostic(error)}); }
  finally { busy = false; }
};
