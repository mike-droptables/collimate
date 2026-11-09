# SPDX-License-Identifier: MIT
"""The chat's queue of messages sent while its run is starting, through the
actual ``useNativeChat`` hook with controlled React and socket collaborators
(the same probe shape as ``test_assistant_runtime_contract.py``).

A starting pointer (the host's ``{"starting": true, "label"}`` placeholder)
keeps the composer open and queues what is sent; the queue lives in
``message-model.ts``'s module-level store, so it survives the App's remount
onto the real pointer (a new ``chat_id:token`` key); the real session's socket
sends it in order as ``user_msg`` frames when it opens, but the messages leave
the queue only once the host's first frame confirms the attach (the host
accepts a refused attach and then closes it 4409/4404/4401); and a queue the
run never takes comes back as composer text with a banner."""
import os
from pathlib import Path
import subprocess

import pytest

_REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (_REPO / "scripts/node_modules/esbuild").exists(),
    reason="node + esbuild (npm --prefix scripts install) required for the chat queue probe",
)

_PROBE = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const repo=process.argv[2];
const {compiler}=await import(pathToFileURL(repo+'/scripts/renderer_bundle.mjs'));
const esbuild=await compiler();
// A small hook runtime: state, refs and memos live in render-order slots;
// an effect re-runs (after its cleanup) only when its deps changed, as React's.
const react=`const P={slots:[],effects:[],cursor:0,ecursor:0};globalThis.probe=P;const slots=P.slots;
const same=(a,b)=>!!a&&!!b&&a.length===b.length&&a.every((x,i)=>Object.is(x,b[i]));
exports.useState=(v)=>{const i=P.cursor++;if(i>=slots.length)slots.push(typeof v==='function'?v():v);return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;}];};
exports.useRef=v=>{const i=P.cursor++;if(i>=slots.length)slots.push({current:v});return slots[i];};
exports.useMemo=(f,d)=>{const i=P.cursor++;const s=slots[i];if(s&&s.memo&&same(s.d,d))return s.v;const v=f();slots[i]={memo:1,d,v};return v;};
exports.useCallback=(f,d)=>exports.useMemo(()=>f,d);
exports.useEffect=(f,d)=>{P.effects[P.ecursor++]={f,d};};exports.default=exports;`;
const socket=`exports.openSessionSocket=opts=>{globalThis.socket=opts;globalThis.dials=(globalThis.dials||0)+1;return{sendJson:()=>true,close(){}};};exports.sessionWsUrl=()=>'/socket';`;
const plugins=[{name:'controlled-framework',setup(b){b.onResolve({filter:/^(https:|\/api\/)/},a=>({path:a.path.includes('/api/')?'socket':a.path.startsWith('https://esm.sh/react@')?'react':'assistant',namespace:'controlled'}));b.onLoad({filter:/.*/,namespace:'controlled'},a=>({contents:a.path==='react'?react:a.path==='socket'?socket:'exports.useExternalStoreRuntime=()=>({});',loader:'js'}));}}];
const out=process.argv[3];await esbuild.build({entryPoints:[repo+'/../builtins/live/assistant/chat-runtime.ts'],bundle:true,format:'esm',outfile:out,plugins});
globalThis.requestAnimationFrame=f=>0;globalThis.cancelAnimationFrame=()=>{};globalThis.window=globalThis;
const {useNativeChat}=await import(pathToFileURL(out));
const tick=()=>new Promise(r=>setTimeout(r,0));
const api={getCellId:()=>'cell-1',getFilename:()=>'_runs/agent-1/chat/conversation.chat',
  getSiblingFile:async()=>'{"messages":[]}',onFilesChange(){return()=>{};},
  onSurfaceChange(fn){globalThis.surfaceChanged=fn;return()=>{};},
  getSurface:async()=>globalThis.surface};
const starting={chat_id:'',ws_path:'',token:'',label:'Chat',threads_dir:'',active_thread:'main',threads:[],starting:true};
const live={chat_id:'c1',ws_path:'/socket',token:'t',label:'Chat',threads_dir:'',active_thread:'main',threads:[{id:'main',file:'main.json'}],starting:false};
let ran=[];   // per effect position: {d, cleanup}
// mount: a fresh tree (new state, every effect runs); rerender: the same
// tree's next render (state kept; an effect whose deps changed re-runs).
const render=(pointer,fresh)=>{
  if(fresh){for(const r of ran)if(r&&typeof r.cleanup==='function')r.cleanup();ran=[];probe.slots.length=0;}
  probe.cursor=0;probe.ecursor=0;probe.effects.length=0;
  const chat=useNativeChat(api,pointer);
  probe.effects.forEach(({f,d},i)=>{
    const prev=ran[i];
    if(prev&&d&&prev.d&&d.length===prev.d.length&&d.every((x,k)=>Object.is(x,prev.d[k])))return;
    if(prev&&typeof prev.cleanup==='function')prev.cleanup();
    ran[i]={d,cleanup:f()};
  });
  return chat;
};
const mount=pointer=>render(pointer,true);
const rerender=pointer=>render(pointer,false);
const frame=f=>globalThis.socket.onMessage({data:JSON.stringify(f)});
const shown=chat=>chat.messages.map(m=>[m.text,!!m.optimistic,!!m.queued]);
const mode=process.argv[4];
if(mode==='flush'){
  // The placeholder: composer open, no socket, sends queue.
  let chat=mount(starting);await tick();
  assert.equal(chat.status,'starting');
  assert.equal(chat.queueing,true);
  assert.equal(globalThis.dials,undefined,'a starting chat dials no socket');
  chat.attachments.submit('first');chat.attachments.submit('second');
  // The real pointer: a new key, so the runtime tree remounts — the queue is
  // still there, shown as queued bubbles under the conversation.
  chat=mount(live);
  assert.equal(chat.queueing,false);
  assert.deepEqual(chat.messages.map(m=>[m.role,m.text,!!m.queued]),
    [['user','first',true],['user','second',true]]);
  assert.equal(globalThis.dials,1);
  // The open sends the queue in order, as user_msg frames on the thread...
  const sent=[];
  globalThis.socket.onStatus('open');
  globalThis.socket.onOpen({send:d=>sent.push(JSON.parse(d))});
  assert.deepEqual(sent,[{t:'user_msg',content:'first',thread_id:'main'},
                         {t:'user_msg',content:'second',thread_id:'main'}]);
  // ...but an open alone does not take them: they stay queued...
  chat=rerender(live);
  assert.deepEqual(shown(chat),[['first',false,true],['second',false,true]]);
  // ...until the host's first frame confirms the attach; then they are the
  // ordinary optimistic bubbles, ahead of the reply they started.
  frame({t:'turn_start',turn_id:'t1',thread_id:'main'});
  chat=rerender(live);
  assert.deepEqual(shown(chat),[['first',true,false],['second',true,false]]);
  // A reconnect (a second open) sends nothing again.
  globalThis.socket.onStatus('closed');globalThis.socket.onStatus('connecting');
  const again=[];globalThis.socket.onStatus('open');globalThis.socket.onOpen({send:d=>again.push(d)});
  assert.deepEqual(again,[]);
  // And a later mount shows no queued bubble.
  chat=mount(live);
  assert.deepEqual(chat.messages.filter(m=>m.queued),[]);
}else if(mode==='refused'){
  // The host accepts an attach it refuses (another view holds the chat) and
  // then closes it 4409: what that open sent was never read.
  let chat=mount(starting);await tick();
  chat.attachments.submit('first');chat.attachments.submit('second');
  chat=mount(live);
  const lost=[];
  globalThis.socket.onStatus('open');
  globalThis.socket.onOpen({send:d=>lost.push(JSON.parse(d).content)});
  assert.deepEqual(lost,['first','second']);
  globalThis.socket.onStatus('busy');
  chat=rerender(live);
  assert.equal(chat.status,'busy');
  assert.deepEqual(shown(chat),[['first',false,true],['second',false,true]],
    'a refused attach leaves the queue intact');
  // The takeover redial is accepted: the queue goes out again, in order,
  // and the host's first frame takes it.
  globalThis.socket.onStatus('connecting');
  const sent=[];
  globalThis.socket.onStatus('open');
  globalThis.socket.onOpen({send:d=>sent.push(JSON.parse(d).content)});
  assert.deepEqual(sent,['first','second']);
  frame({t:'turn_start',turn_id:'t1',thread_id:'main'});
  chat=rerender(live);
  assert.deepEqual(shown(chat),[['first',true,false],['second',true,false]]);
}else if(mode==='ended'){
  // A session the host says is gone (accepted, then closed 4404) never read
  // the queue: it goes back to the composer with the banner.
  let chat=mount(starting);await tick();
  chat.attachments.submit('never read');
  chat=mount(live);
  globalThis.socket.onStatus('open');
  globalThis.socket.onOpen({send:()=>{}});
  globalThis.socket.onStatus('ended');
  chat=rerender(live);
  assert.equal(chat.status,'closed');
  assert.ok(probe.slots.some(v=>typeof v==='string'&&/The run ended before it took your queued messages/.test(v)),
    'the banner says what happened');
  assert.ok(probe.slots.some(v=>v&&typeof v==='object'&&v.text==='never read'&&v.n===1),
    'the text goes back into the composer');
  chat=rerender(live);
  assert.deepEqual(chat.messages.filter(m=>m.queued||m.optimistic),[]);
}else if(mode==='abandon'){
  let chat=mount(starting);await tick();
  chat.attachments.submit('never taken');
  // The claim is still starting: nothing happens.
  globalThis.surface={state:'starting'};
  globalThis.surfaceChanged();await tick();
  assert.ok(!probe.slots.some(v=>typeof v==='string'&&/back in the composer/.test(v)));
  // The host released the run's claim without the run registering it: the
  // run ended before it took the queue.
  globalThis.surface=null;
  globalThis.surfaceChanged();await tick();
  assert.ok(probe.slots.some(v=>typeof v==='string'&&/The run ended before it took your queued messages/.test(v)),
    'the banner says what happened');
  assert.ok(probe.slots.some(v=>v&&typeof v==='object'&&v.text==='never taken'&&v.n===1),
    'the text goes back into the composer');
  // The queue is gone: the next mount shows nothing queued.
  chat=mount(starting);
  assert.deepEqual(chat.messages.filter(m=>m.queued),[]);
}
for(const r of ran)if(r&&typeof r.cleanup==='function')r.cleanup();
'''


def _run_probe(tmp_path, mode):
    script = tmp_path / "probe.mjs"
    script.write_text(_PROBE)
    result = subprocess.run(
        ["node", str(script), str(_REPO), str(tmp_path / "runtime.mjs"), mode],
        capture_output=True, text=True, env=os.environ, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_queued_messages_survive_the_remount_and_are_taken_on_a_confirmed_attach_GN03(tmp_path):
    _run_probe(tmp_path, "flush")


def test_a_queue_the_run_never_takes_returns_to_the_composer_with_a_banner_GN03(tmp_path):
    _run_probe(tmp_path, "abandon")


def test_a_refused_attach_keeps_the_queue_for_the_next_confirmed_one_GN03(tmp_path):
    _run_probe(tmp_path, "refused")


def test_an_ended_session_gives_the_unconfirmed_queue_back_GN03(tmp_path):
    _run_probe(tmp_path, "ended")
