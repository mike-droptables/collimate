import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {Worker} from 'node:worker_threads';
import {execFileSync} from 'node:child_process';
import http from 'node:http';
import {createRequire} from 'node:module';
const app=path.resolve(import.meta.dirname,'../../..');
const sdk=path.join(app,'sdk');
const scratch=path.join(app,'tmp');fs.mkdirSync(scratch,{recursive:true});
const root=fs.mkdtempSync(path.join(scratch,'verifier-worker-P4F09-'));
after(()=>fs.rmSync(root,{recursive:true,force:true}));
fs.mkdirSync(path.join(root,'tests'));
fs.copyFileSync(path.join(app,'playground/web/verifier-worker.js'),path.join(root,'verifier-worker.js'));
fs.copyFileSync(path.join(sdk,'scripts/renderer_bundle_core.mjs'),path.join(root,'renderer_bundle_core.mjs'));
fs.copyFileSync(path.join(sdk,'src/collimate/renderer_bundle_contract.json'),path.join(root,'renderer_bundle_contract.json'));
fs.copyFileSync(path.join(import.meta.dirname,'verifier-node-worker.mjs'),path.join(root,'tests/verifier-node-worker.mjs'));
fs.symlinkSync(path.join(app,'playground/verifier-runtime/node_modules'),path.join(root,'node_modules'),'dir');
fs.writeFileSync(path.join(root,'package.json'),JSON.stringify({private:true,type:'module'}));
const fixture=JSON.parse(execFileSync('python3',['-c',`import ast,json,pathlib
p=pathlib.Path(${JSON.stringify(sdk)})
t=ast.parse((p/'src/collimate/_host/verification.py').read_text())
b=next(ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and any(isinstance(x,ast.Name) and x.id=='VERIFICATION_BOUNDARY' for x in n.targets))
print(json.dumps({'boundary':b,'prelude':(p/'shared/verify/prelude.js').read_text()}))`]));
const native=JSON.parse(execFileSync(path.join(app,'.venv/bin/python'),['-c',`import sys,json,pathlib
sys.path.insert(0,${JSON.stringify(sdk+'/src')})
from collimate._host.renderer_bundle import compile_verifier,bundle_renderer
p=pathlib.Path(${JSON.stringify(sdk)})
b=compile_verifier((pathlib.Path(${JSON.stringify(app)})/'builtins/definitions/generator/derived/verify.ts').read_bytes(),prelude=(p/'shared/verify/prelude.js').read_bytes())
r=bundle_renderer({'main.tsx':b'import {x} from "./helper"; export default {__collimate_renderer__:true,component:()=>x};','helper.ts':b'export const x=null;'},entrypoint='main.tsx')
print(json.dumps({'verifier':{'code':b.code,'source_hash':b.source_hash,'bundle_hash':b.bundle_hash},'renderer':{'code':r.code,'source_hash':r.source_hash,'bundle_hash':r.bundle_hash,'entrypoint_js':r.entrypoint_js}}))`],{maxBuffer:8*1024*1024}));
const contract=JSON.parse(fs.readFileSync(path.join(root,'renderer_bundle_contract.json')));
const raw=p=>{const b=fs.readFileSync(path.join(root,p));return b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength)};
const init=()=>({op:'init',contract,quickjs_wasm:raw('node_modules/@jitl/quickjs-wasmfile-release-asyncify/dist/emscripten-module.wasm'),esbuild_wasm:raw('node_modules/esbuild-wasm/esbuild.wasm')});
const policy=structuredClone(contract); policy.options.format='iife';policy.global_name='__collimate_verifier';policy.external_prefixes=[];policy.imports=['collimate/verify'];
const compile=source=>({op:'compile',files:[['verify.ts',Buffer.from(source).toString('base64')],['prelude.js',Buffer.from(fixture.prelude).toString('base64')]],entrypoint:'verify.ts',policy,verifier:true,remaining_ms:5000});
const evaluate=(code,input={})=>({op:'evaluate',code,input_json:JSON.stringify(input),boundary:fixture.boundary,remaining_ms:5000});
function client(worker) {
 let id=0; const pending=new Map();
 worker.on('message',m=>{const p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.timer);m.ok?p.resolve(m.result):p.reject(Error(m.error));}});
 worker.on('error',error=>{for(const p of pending.values()){clearTimeout(p.timer);p.reject(error);}pending.clear();});
 return request=>new Promise((resolve,reject)=>{const key=++id;const timer=setTimeout(()=>{pending.delete(key);worker.terminate();reject(Error('P4F09 worker response timed out'));},15000);pending.set(key,{resolve,reject,timer});worker.postMessage({...request,id:key});});
}
test('P4F09 real Node worker compiler and canonical bounded normalizer',async()=>{
 const worker=new Worker(path.join(root,'tests/verifier-node-worker.mjs'));const call=client(worker);
 try {
  await call(init());
  const canonicalSource=fs.readFileSync(path.join(app,'builtins/definitions/generator/derived/verify.ts'),'utf8');
  const canonicalCompiled=await call(compile(canonicalSource));assert.equal(canonicalCompiled.code,native.verifier.code);
  const files=[['main.tsx',Buffer.from('import {x} from "./helper"; export default {__collimate_renderer__:true,component:()=>x};').toString('base64')],['helper.ts',Buffer.from('export const x=null;').toString('base64')]];
  const graph=await call({op:'compile',files,entrypoint:'main.tsx',policy:contract,verifier:false,remaining_ms:5000});assert.equal(graph.code,native.renderer.code);assert.equal(graph.entrypoint_js,native.renderer.entrypoint_js);
  const callback=await call(compile('import {bundle} from "collimate/verify"; export default ()=>{const r=bundle({"main.tsx":'+JSON.stringify(Buffer.from(files[0][1],'base64').toString())+',"helper.ts":"export const x=null;"},"main.tsx"); return {valid:r.source_hash==='+JSON.stringify(native.renderer.source_hash)+'&&r.bundle_hash==='+JSON.stringify(native.renderer.bundle_hash)+'}}'));
  assert.equal(JSON.parse((await call(evaluate(callback.code))).verdict_json).valid,true);
  const good=await call(compile('export default ()=>({valid:true,toJSON(){throw Error("must not run")}})'));
  assert.deepEqual(JSON.parse((await call(evaluate(good.code))).verdict_json),{valid:true});
  for(const spec of ['./helper.ts','https://example.invalid/x.js','node:fs','bare']) await assert.rejects(call(compile(`import x from ${JSON.stringify(spec)}; export default ()=>({valid:!!x});`)),/only collimate\/verify/);
  for(const body of ['{valid:true,then(){}}','null','[]','{}','{valid:1}','Promise.resolve({valid:true})','{valid:false,error:42}']) {
   const result=await call(compile(`export default ()=>(${body})`));const verdict=JSON.parse((await call(evaluate(result.code))).verdict_json);assert.equal(verdict.valid,false);assert.ok(verdict.error);
  }
  for (const [body,expected] of [
   ['let n=0;return {get valid(){return n++===0?true:"bad"}}',true],
   ['JSON.stringify=()=>"bad";Array.isArray=()=>true;return {valid:true}',true],
   ['globalThis.leaked=__collimate_bundle;globalThis.secret=42;return {valid:true}',true],
   ['return {valid:globalThis.secret===undefined && typeof process==="undefined" && typeof fetch==="undefined" && typeof globalThis.require==="undefined" && typeof __collimate_host_bundle==="undefined",error:JSON.stringify([globalThis.secret,typeof process,typeof fetch,typeof globalThis.require,typeof __collimate_host_bundle])}',true],
   ['return {valid:false,error:"あ".repeat(10000)}',false],
   ['return {valid:true,toJSON(){while(true){}}}',true],
   ['return {valid:!!new ArrayBuffer(70*1024*1024)}',false]
  ]) { const compiled=await call(compile(`export default ()=>{${body}}`));const verdict=JSON.parse((await call(evaluate(compiled.code))).verdict_json);assert.equal(verdict.valid,expected,body+': '+verdict.error);if(body.includes('repeat'))assert.equal(verdict.error.length,4096); }
  // A fatal engine teardown is a failed protocol envelope, not a guest verdict.
  // The artifact E2E pairs this narrow allowance with actual Python refusal,
  // failed-worker termination and successful evaluation on another worker.
  const recursive=await call(compile('export default ()=>{const recur=()=>recur();recur();return {valid:true}}'));
  let result, fatal=false;
  try { result=await call(evaluate(recursive.code)); }
  catch(error) {
   fatal=true;
   assert.ok(error instanceof Error);
   assert.ok(error.message.length>0 && error.message.length<=4096);
   assert.match(error.message,/^Aborted\(Assertion failed: list_empty\(&rt->gc_obj_list\)/);
  }
  if(!fatal) {
   const verdict=JSON.parse(result.verdict_json);
   assert.equal(verdict.valid,false);
   assert.match(verdict.error,/stack overflow/);
  }
 } finally {await worker.terminate();}
});
test('P9 init refuses a contract that pins another esbuild than the bundled one',async()=>{
 const worker=new Worker(path.join(root,'tests/verifier-node-worker.mjs'));const call=client(worker);
 try {
  await assert.rejects(call({...init(),contract:{...contract,esbuild_version:'0.0.0'}}),/differs from SDK pin 0\.0\.0$/);
  await assert.rejects(call(evaluate('globalThis.__collimate_verifier={default:()=>({valid:true})}')),/^Error: Verifier worker unavailable$/);
  assert.deepEqual(await call(init()),{ready:true});
 } finally {await worker.terminate();}
});
test('P4F09 actual Chromium worker shipped handler and hostile parity',async()=>{
 const require=createRequire(path.join(app,'playground/package.json'));const {chromium}=require('@playwright/test');
 const builder=createRequire(path.join(app,'frontend/package.json'))('esbuild');
 const output=path.join(root,'browser-worker.js');await builder.build({entryPoints:[path.join(root,'verifier-worker.js')],outfile:output,bundle:true,format:'esm',platform:'browser',external:['./renderer_bundle_core.mjs']});
 const server=http.createServer((req,res)=>{
  if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>P4F09</title>');return;}
  const target=path.resolve(root,'.'+decodeURIComponent(req.url));if(!target.startsWith(root+'/')){res.writeHead(404);res.end();return;}
  try{res.setHeader('Content-Type',target.endsWith('.wasm')?'application/wasm':'text/javascript');res.end(fs.readFileSync(target));}catch{res.writeHead(404);res.end();}
 });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});const page=await browser.newPage();
 try {
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.evaluate(async contract=>{
   window.spawn=async()=>{const w=new Worker('/browser-worker.js',{type:'module'});let id=0;const p=new Map();w.onmessage=({data})=>{const pending=p.get(data.id);p.delete(data.id);data.ok?pending.resolve(data.result):pending.reject(Error(data.error));};const call=r=>new Promise((resolve,reject)=>{const key=++id;p.set(key,{resolve,reject});w.postMessage({...r,id:key});});await call({op:'init',contract,quickjs_wasm:await(await fetch('/node_modules/@jitl/quickjs-wasmfile-release-asyncify/dist/emscripten-module.wasm')).arrayBuffer(),esbuild_wasm:await(await fetch('/node_modules/esbuild-wasm/esbuild.wasm')).arrayBuffer()});return {w,call};};window.active=await window.spawn();
  },contract);
  const call=request=>page.evaluate(r=>window.active.call(r),request);
  for(const name of ['generator','runner','handler','options','renderer']) {
   const tree='builtins/definitions';
   const source=fs.readFileSync(path.join(app,tree,name,'derived/verify.ts'),'utf8');const compiled=await call(compile(source));
   const folder=path.join(app,tree,name,'examples/basic');const files={};
   function collect(dir){for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const full=path.join(dir,entry.name);if(entry.isDirectory())collect(full);else files[path.relative(folder,full)]=fs.readFileSync(full,'utf8');}}collect(folder);
   const ext={generator:'.generator',runner:'.runner',handler:'.handler',options:'.options_stack',renderer:'.renderer'}[name];const filename=Object.keys(files).find(n=>n.endsWith(ext));
   const input={path:filename,text:files[filename],package:files,cell_paths:Object.keys(files)};
   const positive=JSON.parse((await call(evaluate(compiled.code,input))).verdict_json);assert.equal(positive.valid,true,`${name}: ${positive.error}`);
   const negative=JSON.parse((await call(evaluate(compiled.code,{...input,text:'{}'}))).verdict_json);assert.equal(negative.valid,false,name);
   if(name==='renderer') {const package2={...files,'main.tsx':'throw Error("DO NOT EXECUTE"); export default {__collimate_renderer__:true,component:()=>null};'};assert.equal(JSON.parse((await call(evaluate(compiled.code,{...input,package:package2}))).verdict_json).valid,true);}
  }
  for(const code of ['globalThis.__collimate_verifier={default(){while(true){}}}','globalThis.__collimate_verifier={default(){return {get valid(){while(true){}}}}}','globalThis.__collimate_verifier={default(){return new Proxy({}, {get(){while(true){}}})}}','globalThis.__collimate_verifier={default(){throw {get message(){while(true){}}}}}']) {
   const started=Date.now();await page.evaluate(r=>{window.active.call(r).catch(()=>{});},evaluate(code));await new Promise(r=>setTimeout(r,150));await page.evaluate(async()=>{window.active.w.terminate();window.active=await window.spawn();});assert.ok(Date.now()-started<3000);
   assert.equal(JSON.parse((await call(evaluate('globalThis.__collimate_verifier={default:()=>({valid:true})}'))).verdict_json).valid,true);
  }
 } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
});
