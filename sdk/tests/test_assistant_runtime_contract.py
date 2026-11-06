"""Actual chat hook callbacks with controlled hook/transport collaborators."""
import os
from pathlib import Path
import subprocess

from tests._workspace import BUILTINS


def _run_hook_probe(tmp_path, mode):
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / 'probe.mjs'
    script.write_text(r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const repo=process.argv[2];
const {compiler}=await import(pathToFileURL(repo+'/scripts/renderer_bundle.mjs'));
const esbuild=await compiler();
const react=`const slots=[]; const effects=[];globalThis.probe={slots,effects,paints:0};
exports.useState=(v)=>{const i=slots.length;slots.push(typeof v==='function'?v():v);return[slots[i],v=>{slots[i]=typeof v==='function'?v(slots[i]):v;if(i===0)globalThis.probe.paints++;}];};
exports.useRef=v=>({current:v});exports.useEffect=f=>effects.push(f);exports.useMemo=f=>f();exports.useCallback=f=>f;exports.default=exports;`;
const socket=`exports.openSessionSocket=opts=>{globalThis.socket=opts;return{sendJson:frame=>{globalThis.sentFrames.push(frame);return globalThis.sendResult!==false;},close(){}};};exports.sessionWsUrl=()=>'/socket';`;
const plugins=[{name:'controlled-framework',setup(b){b.onResolve({filter:/^(https:|\/api\/)/},a=>({path:a.path.includes('/api/')?'socket':a.path.startsWith('https://esm.sh/react@')?'react':'assistant',namespace:'controlled'}));b.onLoad({filter:/.*/,namespace:'controlled'},a=>({contents:a.path==='react'?react:a.path==='socket'?socket:'exports.useExternalStoreRuntime=options=>{globalThis.adapter=options;return{};};',loader:'js'}));}}];
const out=process.argv[3];await esbuild.build({entryPoints:[repo+'/../builtins/live/assistant/chat-runtime.ts'],bundle:true,format:'esm',outfile:out,plugins});
const frames=new Map();let n=0;globalThis.requestAnimationFrame=f=>{frames.set(++n,f);return n;};globalThis.cancelAnimationFrame=id=>frames.delete(id);globalThis.window=globalThis;globalThis.sentFrames=[];
const {useNativeChat}=await import(pathToFileURL(out));
let reads=0,parseCalls=0;const originalParse=JSON.parse;JSON.parse=(raw,...args)=>{if(raw==='{"messages":[]}')parseCalls++;return originalParse(raw,...args);};const uploads=[];
const chat=useNativeChat({stageDraftFile:(path,buffer)=>new Promise(resolve=>uploads.push({path,buffer,resolve})),getSiblingFile:async()=>{reads++;return '{"messages":[]}';},onFilesChange(fn){globalThis.filesChanged=fn;return()=>{};}},{active_thread:'main',threads:[{id:'main',file:'main.json'}],ws_path:'/socket',chat_id:'test'});
const cleanups=probe.effects.map(f=>f());await new Promise(r=>setTimeout(r,0));
if(process.argv[4]==='send'){
 assert.equal('curation' in chat,false,'runtime offers no transcript mutation callbacks');
 assert.equal('messageIndex' in chat,false,'runtime offers no mutation positions');
 assert.equal('onReload' in globalThis.adapter,false,'assistant-ui cannot regenerate a reply');
 assert.equal(typeof globalThis.adapter.onNew,'function');assert.equal(typeof globalThis.adapter.onCancel,'function');
 assert.equal(globalThis.adapter.unstable_capabilities.copy,true);
 await globalThis.adapter.onNew({content:[{type:'text',text:'hello'}]});
 assert.deepEqual(globalThis.sentFrames,[{t:'user_msg',content:'hello',thread_id:'main'}]);
 assert.equal(probe.slots[0][0].text,'hello','normal send paints an optimistic prompt');
 await globalThis.adapter.onCancel();
 assert.deepEqual(globalThis.sentFrames[1],{t:'interrupt',thread_id:'main'});
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else if(process.argv[4]==='send-refusal'){
 globalThis.sendResult=false;chat.attachments.submit('unsent prompt');
 assert.equal(probe.slots[0].length,0,'failed send never paints an optimistic prompt');
 assert.match(probe.slots[3],/Connecting|Generator stopped|Still connecting/);
 assert.equal(probe.slots[5].text,'unsent prompt','failed send restores composer text');
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else if(process.argv[4]==='ids'){
 const originalNow=Date.now;Date.now=()=>100;
 chat.attachments.submit('first');chat.attachments.submit('second');Date.now=originalNow;
 assert.equal(probe.slots[0].length,2);assert.equal(new Set(probe.slots[0].map(message=>message.id)).size,2,'two fast prompts retain distinct IDs');
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else if(process.argv[4]==='refusal'){
 const start=reads;globalThis.socket.onMessage({data:JSON.stringify({t:'refused',op:'edit',thread_id:'main',message:'The conversation changed'})});
 assert.equal(probe.slots[3],'The conversation changed','explicit refusal appears as a banner');
 await new Promise(r=>setTimeout(r,0));assert.equal(reads,start,'refusal does not change the transcript');
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else if(process.argv[4]==='upload'){
 const done=chat.attachments.attach(Array.from({length:6},(_,i)=>({name:'photo.png',type:'image/png',arrayBuffer:async()=>new Uint8Array([i]).buffer})));
 const second=chat.attachments.attach(Array.from({length:2},(_,i)=>({name:'photo.png',type:'image/png',arrayBuffer:async()=>new Uint8Array([i]).buffer})));
 assert.equal(probe.slots[6].length,8,'all selected chips appear before any file completes');
 await new Promise(r=>setTimeout(r,0));assert.equal(uploads.length,3,'three concurrent uploads, not six unbounded reads');
 assert.equal(new Set(probe.slots[6].map(a=>a.path)).size,8);
 for(let i=0;i<8;i++){uploads[i].resolve(i!==1);await new Promise(r=>setTimeout(r,0));}
 await Promise.all([done,second]);assert.equal(probe.slots[6].filter(a=>a.status==='done').length,7);assert.equal(probe.slots[6].filter(a=>a.status==='error').length,1);
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else if(process.argv[4]==='reload'){
 const start=reads;for(let i=0;i<20;i++)globalThis.filesChanged({changedFiles:['main.json']});
 assert.equal(reads,start,'file notifications do not fetch before the quiet window');
 await new Promise(r=>setTimeout(r,170));assert.equal(reads,start+1,'one authoritative read for a notification burst');assert.equal(parseCalls,1,'unchanged raw transcript skips repeated parse while still reconciling');
 for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}else{
const emit=f=>globalThis.socket.onMessage({data:JSON.stringify({thread_id:'main',turn_id:'turn',...f})});
emit({t:'turn_start'});probe.paints=0;
for(let i=0;i<100;i++)emit({t:'delta',text:'x'});
assert.equal(frames.size,1,'one display-frame job for a whole stream burst');assert.equal(probe.paints,0,'no per-delta message-state writes');
emit({t:'turn_end'});assert.equal(frames.size,0,'terminal frame flushes scheduled paint');assert.equal(probe.slots[0].at(-1).text,'x'.repeat(100));
for(const cleanup of cleanups)if(typeof cleanup==='function')cleanup();
}
''')
    result = subprocess.run(['node', str(script), str(repo), str(tmp_path / 'runtime.mjs'), mode], capture_output=True, text=True, env=os.environ, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_chat_delta_paint_is_coalesced_and_end_flushes__F1770(tmp_path):
    _run_hook_probe(tmp_path, 'paint')


def test_actual_chat_file_reload_burst_is_debounced__F1754(tmp_path):
    _run_hook_probe(tmp_path, 'reload')


def test_actual_attachment_batch_stages_all_chips_and_bounds_uploads__F1775(tmp_path):
    _run_hook_probe(tmp_path, 'upload')


def test_actual_chat_refusal_shows_banner_without_changing_history__F1721(tmp_path):
    _run_hook_probe(tmp_path, 'refusal')


def test_fast_optimistic_prompts_have_distinct_ids__P3F05(tmp_path):
    _run_hook_probe(tmp_path, 'ids')


def test_chat_adapter_can_send_interrupt_and_copy_without_curation(tmp_path):
    _run_hook_probe(tmp_path, 'send')


def test_failed_send_restores_composer_without_optimistic_message(tmp_path):
    _run_hook_probe(tmp_path, 'send-refusal')


def test_bundled_assistant_has_copy_send_and_stop_but_no_transcript_controls(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    bundle = tmp_path / 'assistant.mjs'
    build = subprocess.run(
        ['node', str(repo / 'scripts/bundle_renderer_test.mjs'),
         str(BUILTINS / 'live/assistant/main.tsx'), str(bundle)],
        capture_output=True, text=True, env=os.environ, timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    code = bundle.read_text()
    for label in ('title: "Copy"', 'title: "Send message"', 'title: "Stop generating"'):
        assert label in code
    for control in ('Edit message', 'Delete message', 'Insert message below',
                    'Move up', 'Move down', 'Add message', 'Regenerate'):
        assert control not in code
    assert 'ActionBarPrimitive.Reload' not in code
