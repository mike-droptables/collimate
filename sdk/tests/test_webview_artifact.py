# SPDX-License-Identifier: MIT
"""Regression coverage for the webview renderer's half of the
``tab-<n>.webview`` v4 contract: artifact parsing (the one flat shape, its
device and viewport, + the ``requires`` gate) and the RPC answer mapping whose wire shapes live in
``generators/_web_shared/protocol.py``.

``parseArtifact`` / ``unmetRequirements`` / ``answerRpc`` / ``guestFromHandle``
/ ``bytesToBase64`` are exported from the renderer package entrypoint
``builtin/live/webview/main.tsx``. To exercise them
under plain Node this test first bundles main.tsx with its CDN/SDK imports
stubbed (``scripts/bundle_renderer_test.mjs``) into a temporary importable ESM
module, then runs ``artifact.test.ts`` under node:test via the tsx loader with
``WEBVIEW_BUNDLE`` pointing at that bundle. Skips when Node, esbuild, or the tsx
loader isn't available (e.g. a Python-only CI job that never ran
``npm --prefix scripts install``); uses ``tests/_node_renderer.py``.
"""
from __future__ import annotations

import os
import subprocess

from tests._node_renderer import (
    REPO as _REPO,
    node_renderer_skip, run_node_renderer,
)

from tests._workspace import BUILTINS

_TEST_FILE = BUILTINS / "live" / "webview" / "artifact.test.ts"
_MAIN_TSX = BUILTINS / "live" / "webview" / "main.tsx"

pytestmark = node_renderer_skip(_TEST_FILE, 'node + esbuild + tsx (npm --prefix scripts install) required for the webview artifact test')


def test_webview_artifact_and_rpc_contract(tmp_path):
    run_node_renderer(_MAIN_TSX, tmp_path / 'webview_bundle.mjs', _TEST_FILE, 'WEBVIEW_BUNDLE',
                      label='webview artifact test', bundle_label='main.tsx')


# The renderer's mount and device-switch effects, run through the actual
# SessionView with a controlled hook runtime (state/refs/memos in render-order
# slots; an effect re-runs, after its cleanup, only when its deps changed, as
# React's) and a recording desktop surface.
_DEVICE_PROBE = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const repo=process.argv[2];
const {compiler}=await import(pathToFileURL(repo+'/scripts/renderer_bundle.mjs'));
const esbuild=await compiler();
const react=`const P={slots:[],effects:[],cursor:0,ecursor:0};globalThis.probe=P;const slots=P.slots;
const same=(a,b)=>!!a&&!!b&&a.length===b.length&&a.every((x,i)=>Object.is(x,b[i]));
exports.useState=(v)=>{const i=P.cursor++;if(i>=slots.length)slots.push(typeof v==='function'?v():v);return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}];};
exports.useRef=v=>{const i=P.cursor++;if(i>=slots.length)slots.push({current:v});return slots[i];};
exports.useMemo=(f,d)=>{const i=P.cursor++;const s=slots[i];if(s&&s.memo&&same(s.d,d))return s.v;const v=f();slots[i]={memo:1,d,v};return v;};
exports.useCallback=(f,d)=>exports.useMemo(()=>f,d);
exports.useEffect=(f,d)=>{P.effects[P.ecursor++]={f,d};};
exports.createElement=(type,props,...children)=>({type,props:{...(props||{}),children}});
exports.Fragment='fragment';exports.default=exports;`;
const token=`const deep=()=>new Proxy(function(){},{get:(t,k)=>k===Symbol.toPrimitive?()=>'var(--probe)':k==='toString'?()=>'var(--probe)':deep()});`;
const sdk=token+`exports.tokens=deep();exports.defineRenderer=d=>d;exports.useCollimateFile=()=>globalThis.file;`;
const session=`exports.apiOrigin=()=>'';exports.sessionWsUrl=()=>'/socket';exports.openSessionSocket=()=>({sendJson(){return true;},close(){}});`;
const plugins=[{name:'controlled-framework',setup(b){b.onResolve({filter:/^(https:|\/api\/)/},a=>({path:a.path.includes('/api/sdk/react')?'sdk':a.path.includes('/api/sdk/session')?'session':'react',namespace:'controlled'}));b.onLoad({filter:/.*/,namespace:'controlled'},a=>({contents:a.path==='react'?react:a.path==='sdk'?sdk:session,loader:'js'}));}}];
const out=process.argv[3];
await esbuild.build({entryPoints:[repo+'/../builtins/live/webview/main.tsx'],bundle:true,format:'esm',outfile:out,plugins,jsx:'transform',jsxFactory:'React.createElement',jsxFragment:'React.Fragment'});
globalThis.window=globalThis;
const WebView=(await import(pathToFileURL(out))).default.component;
const calls={mount:[],setDevice:[],destroy:0};
const handle={on:()=>()=>{},currentUrl:async()=>'',setDevice:async d=>{calls.setDevice.push(d);},destroy(){calls.destroy++;}};
const desktop={capabilities:{nativeGuests:true},openExternal:async()=>{},
  mountGuest:(host,opts)=>{calls.mount.push(opts);return handle;}};
const api={desktop,injectCss(){},emitAction:async()=>true};
const tab=device=>JSON.stringify({v:4,tab:'tab-1',url:'https://example.com/',device,
  viewport:device==='phone'?{width:412}:null,control:{wsBase:'/p/run/'},requires:['nativeGuests'],
  native:{partition:'persist:collimate-default',device},buttons:[]});
let ran=[];
// One render pass: WebView, then the SessionView element it returns.
const render=()=>{
  probe.cursor=0;probe.ecursor=0;probe.effects.length=0;
  const el=WebView();
  assert.equal(el.type.name,'SessionView');
  const tree=el.type(el.props);
  probe.effects.forEach(({f,d},i)=>{
    const prev=ran[i];
    if(prev&&d&&prev.d&&d.length===prev.d.length&&d.every((x,k)=>Object.is(x,prev.d[k])))return;
    if(prev&&typeof prev.cleanup==='function')prev.cleanup();
    ran[i]={d,cleanup:f()};
  });
  return tree;
};
const findRef=node=>{
  if(!node||typeof node!=='object')return null;
  if(Array.isArray(node)){for(const n of node){const r=findRef(n);if(r)return r;}return null;}
  if(node.props&&typeof node.props.ref==='function')return node.props.ref;
  return findRef(node.props&&node.props.children);
};
globalThis.file={content:tab('desktop'),api};
const tree=render();
// The guest host node appears (the callback ref), and the guest mounts once.
findRef(tree)({});
render();
assert.equal(calls.mount.length,1);
assert.equal(calls.mount[0].device,'desktop');
assert.equal(calls.mount[0].keepAlive,true);
assert.equal(calls.mount[0].userAgent,undefined);
// The daemon rewrote the tab file at phone: the same guest switches in place.
globalThis.file={content:tab('phone'),api};
render();
await new Promise(r=>setTimeout(r,0));
assert.equal(calls.mount.length,1,'a device change never remounts the guest');
assert.equal(calls.destroy,0,'a device change never destroys the guest');
assert.deepEqual(calls.setDevice,['phone']);
// A render with the same device switches nothing again.
globalThis.file={content:tab('phone'),api};
render();
assert.deepEqual(calls.setDevice,['phone']);
// And back to desktop, still on the same guest.
globalThis.file={content:tab('desktop'),api};
render();
assert.deepEqual(calls.setDevice,['phone','desktop']);
assert.equal(calls.mount.length,1);
assert.equal(calls.destroy,0);
'''


def test_webview_switches_the_device_in_place_without_remounting_GN04(tmp_path):
    script = tmp_path / "device_probe.mjs"
    script.write_text(_DEVICE_PROBE)
    result = subprocess.run(
        ["node", str(script), str(_REPO), str(tmp_path / "webview_probe.mjs")],
        capture_output=True, text=True, env=os.environ, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
