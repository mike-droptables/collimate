// SPDX-License-Identifier: MIT
//
// transcript.test.ts — the assistant renderer's leg of the open-transcript
// contract: parseTranscript must read the format the Python writer
// (collimate/transcript.py) emits.
//
// Runs under node:test via the tsx loader (see tests/test_assistant_transcript.py).
// The actual framework-free helpers live in message-model.ts. The harness
// bundles that module and passes its ESM path via ASSISTANT_BUNDLE.

import test, { before } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const BUNDLE = process.env.ASSISTANT_BUNDLE;
if (!BUNDLE) {
    throw new Error(
        'ASSISTANT_BUNDLE is not set — bundle message-model.ts first via ' +
        'scripts/bundle_renderer_test.mjs (tests/test_assistant_transcript.py does this).',
    );
}

// Populated from the bundled renderer in a `before` hook — the tsx loader
// transpiles this file to CommonJS, where top-level await is unsupported.
let parseTranscript: (raw: string) => any[];
let reconcileTranscript: (prev: any[], parsed: any[], live: any, usage: any) => any[];
let reduceFrame: (stream: any, frame: any) => any;
let connStatusOf: (st: string) => string;
let m: any;

before(async () => {
    const mod: any = await import(pathToFileURL(path.resolve(BUNDLE)).href);
    m = mod;
    ({ parseTranscript, reconcileTranscript, connStatusOf, reduceFrame } = mod);
});

// The ACP-typed extras (kind / locations / content) survive the parse.
test('tool typed extras parse from the writer shape', () => {
    const raw = JSON.stringify({ messages: [{
        role: 'assistant', content: 'done',
        tools: [{
            name: 'Edit', status: 'done', input: 'path=a.py',
            kind: 'edit',
            locations: [{ path: 'a.py', line: 3 }],
            content: [
                { type: 'diff', path: 'a.py', oldText: 'x = 1', newText: 'x = 2' },
                { type: 'terminal', terminalId: 'term_1' },
            ],
        }],
    }] });
    const t = parseTranscript(raw)[0].tools[0];
    assert.strictEqual(t.kind, 'edit');
    assert.deepStrictEqual(t.locations, [{ path: 'a.py', line: 3 }]);
    assert.deepStrictEqual(t.content, [
        { type: 'diff', path: 'a.py', oldText: 'x = 1', newText: 'x = 2' },
        { type: 'terminal', terminalId: 'term_1' },
    ]);
});

test('tools/stopped are dropped from non-assistant messages on parse', () => {
    const raw = JSON.stringify({ messages: [
        { role: 'user', content: 'q', stopped: true,
          tools: [{ name: 'x', status: 'done' }] },
    ] });
    const [m] = parseTranscript(raw);
    assert.strictEqual(m.role, 'user');
    assert.strictEqual(m.tools, undefined);
    assert.strictEqual(m.stopped, undefined);
});

test('parse synthesizes stable tool ids (h<i>-t<k>) and drops nameless entries', () => {
    const raw = JSON.stringify({ messages: [
        { role: 'user', content: 'q' },
        { role: 'assistant', content: 'a', tools: [
            { name: 'search', status: 'running', input: 'q' },
            { status: 'done' },                       // no name — dropped
            { name: 'fetch', status: 'done', result: 'ok' },
        ] },
    ] });
    const msgs = parseTranscript(raw);
    assert.deepStrictEqual(msgs[1].tools.map((t: any) => t.id), ['h1-t0', 'h1-t2']);
    assert.deepStrictEqual(
        msgs[1].tools.map((t: any) => t.name), ['search', 'fetch']);
});

test('parses system / user / assistant with thinking, stopped and tools', () => {
    const raw = JSON.stringify({ messages: [
        { role: 'system', content: 'be nice' },
        { role: 'user', content: 'hi' },
        { role: 'assistant', content: 'hello', thinking: 'reasoning', stopped: true,
          tools: [{ name: 'calc', status: 'done', input: '2+2', result: '4' }] },
    ] });
    const msgs = parseTranscript(raw);
    assert.deepStrictEqual(
        msgs.map((m: any) => [m.role, m.text]),
        [['system', 'be nice'], ['user', 'hi'], ['assistant', 'hello']],
    );
    assert.strictEqual(msgs[2].stopped, true);
    assert.strictEqual(msgs[2].thinking, 'reasoning');
    assert.deepStrictEqual(
        { name: msgs[2].tools[0].name, input: msgs[2].tools[0].input, result: msgs[2].tools[0].result },
        { name: 'calc', input: '2+2', result: '4' },
    );
});

// A fresh run seeds nothing into its transcript: the thread is empty, which
// is what makes the renderer show its welcome (ThreadWelcome on isEmpty).
test('a fresh thread parses to no messages (the welcome shows)', () => {
    assert.deepStrictEqual(parseTranscript(JSON.stringify({ messages: [] })), []);
    assert.deepStrictEqual(parseTranscript(''), []);
});

// ── the reload reconcile ───────────────────────────────────────────────
// Unchanged messages keep their object (assistant-ui caches conversions by
// identity); an unchanged list comes back as the same array.

const RECON_RAW = JSON.stringify({ messages: [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'hello' },
    { role: 'user', content: 'more' },
] });

test('reconcile returns prev itself when nothing changed', () => {
    const prev = parseTranscript(RECON_RAW);
    assert.strictEqual(reconcileTranscript(prev, parseTranscript(RECON_RAW), undefined, undefined), prev);
});

test('reconcile shares unchanged messages and replaces a changed one', () => {
    const prev = parseTranscript(RECON_RAW);
    const parsed = parseTranscript(RECON_RAW.replace('hello', 'hello!'));
    const out = reconcileTranscript(prev, parsed, undefined, undefined);
    assert.notStrictEqual(out, prev);
    assert.strictEqual(out[0], prev[0]);
    assert.strictEqual(out[2], prev[2]);
    assert.notStrictEqual(out[1], prev[1]);
    assert.strictEqual(out[1].text, 'hello!');
});

test('reconcile keeps optimistic ids that the reload confirms', () => {
    const raw = JSON.stringify({ messages: [
        { role: 'user', content: 'q' }, { role: 'assistant', content: 'a' },
    ] });
    const prev = [
        { id: 'u1', role: 'user', text: 'q', thinking: '' },
        { id: 'a-t1', role: 'assistant', text: 'a', thinking: '' },
    ];
    const out = reconcileTranscript(prev, parseTranscript(raw), undefined, undefined);
    assert.deepStrictEqual(out.map((m: any) => m.id), ['u1', 'a-t1']);
});

test('reconcile re-attaches usage to the newest assistant only', () => {
    const raw = JSON.stringify({ messages: [
        { role: 'assistant', content: 'one' }, { role: 'assistant', content: 'two' },
    ] });
    const prev = parseTranscript(raw);
    prev[0] = { ...prev[0], usage: { old: 1 } };
    const out = reconcileTranscript(prev, parseTranscript(raw), undefined, { tokens: 5 });
    assert.strictEqual(out[0].usage, undefined);
    assert.notStrictEqual(out[0], prev[0]);
    assert.deepStrictEqual(out[1].usage, { tokens: 5 });
});

test('reconcile keeps the live bubble last and drops its old copy', () => {
    const prev = [...parseTranscript(RECON_RAW),
        { id: 'a-live', role: 'assistant', text: 'par', thinking: '' }];
    const live = { id: 'a-live', role: 'assistant', text: 'partial', thinking: '', tools: [] };
    const out = reconcileTranscript(prev, parseTranscript(RECON_RAW), live, undefined);
    assert.strictEqual(out.length, 4);
    assert.strictEqual(out[3], live);
    assert.strictEqual(out.filter((m: any) => m.id === 'a-live').length, 1);
});

// ── the connection status ──────────────────────────────────────────────
// A transient close redials: it must not read as "Generator stopped".

test('connStatusOf: only ended reads as closed', () => {
    assert.strictEqual(connStatusOf('closed'), 'connecting');
    assert.strictEqual(connStatusOf('connecting'), 'connecting');
    assert.strictEqual(connStatusOf('ended'), 'closed');
    assert.strictEqual(connStatusOf('busy'), 'busy');
    assert.strictEqual(connStatusOf('open'), 'open');
});

test('persisted prefix keeps stable ids and optimistic queued suffix during a live turn __F1774', () => {
    const durable = parseTranscript(JSON.stringify({messages:[{role:'user',content:'first'},{role:'assistant',content:'done'}]}));
    const prev = [{...durable[0],id:'u-stable'},{...durable[1],id:'a-stable'},
        {id:'u-optimistic',role:'user',text:'new prompt',optimistic:true},
        {id:'u-queued',role:'user',text:'queued prompt',optimistic:true}];
    const live={id:'a-live',role:'assistant',text:'partial'};
    const next=reconcileTranscript([...prev,live],durable,live,undefined);
    assert.deepEqual(next.map(m=>m.id),['u-stable','a-stable','u-optimistic','u-queued','a-live']);
    assert.equal(next[0],prev[0]);assert.equal(next[2],prev[2]);
});

test('actual pure frame reducer preserves sections and paired typed tools __F1756', () => {
 const empty={id:'a-turn',text:'',thinking:'',tools:[],thinkingSections:[],inThinking:false};
 const thought=reduceFrame(empty,{t:'thinking_delta',text:'first'});
 const tool=reduceFrame(thought,{t:'tool',id:'t',name:'edit',status:'running',input:'old'});
 const done=reduceFrame(tool,{t:'tool',id:'t',status:'done',result:'ok',kind:'edit',content:[{type:'diff',path:'x',oldText:'a',newText:'b'}]});
 const second=reduceFrame(done,{t:'thinking_delta',text:'second'});
 const reply=reduceFrame(second,{t:'delta',text:'answer'});
 assert.equal(empty.thinking,'');assert.deepEqual(reply.thinkingSections,['first','second']);assert.equal(reply.tools.length,1);
 assert.equal(reply.tools[0].name,'edit');assert.equal(reply.tools[0].input,'old');assert.equal(reply.tools[0].result,'ok');assert.equal(reply.text,'answer');
 assert.equal(reply.inThinking,false);assert.equal(tool.tools[0].status,'running');
});

test('a completed durable reply keeps the next queued user until confirmation __F1774', () => {
 const prev=[{id:'u-first',role:'user',text:'first'}, {id:'a-first',role:'assistant',text:'done'},
 {id:'u-next',role:'user',text:'second',optimistic:true}];
 const parsed=parseTranscript(JSON.stringify({messages:[{role:'user',content:'first'},{role:'assistant',content:'done'}]}));
 assert.deepEqual(reconcileTranscript(prev,parsed,undefined,undefined).map(m=>m.id),['u-first','a-first','u-next']);
 const confirmed=parseTranscript(JSON.stringify({messages:[{role:'user',content:'first'},{role:'assistant',content:'done'},{role:'user',content:'second'}]}));
 const next=reconcileTranscript(prev,confirmed,undefined,undefined);
 assert.equal(next[2].id,'u-next');assert.equal(next[2].optimistic,undefined);
});

// ---------------------------------------------------------------------------
// The starting placeholder and the queue of messages sent before the chat
// exists (GN-03)
// ---------------------------------------------------------------------------

test('a starting pointer parses as starting, with no session yet (GN-03)', () => {
    const p = m.parsePointer(JSON.stringify({ starting: true, label: 'Guide' }));
    assert.strictEqual(p.starting, true);
    assert.strictEqual(p.label, 'Guide');
    assert.strictEqual(p.ws_path, '');
    assert.strictEqual(p.chat_id, '');
    const live = m.parsePointer(JSON.stringify({ chat_id: 'c1', ws_path: '/ws', token: 't', label: 'Chat' }));
    assert.strictEqual(live.starting, false);
    assert.strictEqual(m.parsePointer(JSON.stringify({ starting: 'yes' })).starting, false);
});

test('queued messages keep their order and flush once, in order (GN-03)', () => {
    const key = 'cell-1:_runs/agent-1/chat/conversation.chat';
    assert.deepStrictEqual(m.queuedChat(key), []);
    let notified = 0;
    const off = m.subscribeChatQueue(key, () => { notified++; });
    const a = m.enqueueChat(key, 'first');
    const b = m.enqueueChat(key, 'second', [{ path: 'attachments/x.png', name: 'x.png', media_type: 'image/png' }]);
    assert.strictEqual(notified, 2);
    assert.deepStrictEqual(m.queuedChat(key).map((q: any) => q.text), ['first', 'second']);
    assert.notStrictEqual(a.id, b.id);
    // Shown as queued bubbles (never optimistic: nothing was sent yet).
    const bubble = m.queuedBubble(b);
    assert.strictEqual(bubble.role, 'user');
    assert.strictEqual(bubble.queued, true);
    assert.strictEqual(bubble.optimistic, undefined);
    assert.deepStrictEqual(bubble.attachments, b.attachments);
    // An open dial sends them as user_msg frames, in order, on the thread...
    const dial = m.newChatDial();
    const frames: any[] = [];
    const sent = m.flushChatQueue(key, (f: any) => { frames.push(f); return true; }, 'main', dial);
    assert.deepStrictEqual(sent.map((q: any) => q.text), ['first', 'second']);
    assert.deepStrictEqual(frames, [
        { t: 'user_msg', content: 'first', thread_id: 'main' },
        { t: 'user_msg', content: 'second', thread_id: 'main', attachments: b.attachments },
    ]);
    // ...and they stay queued, in flight, until its attach is confirmed.
    assert.deepStrictEqual(m.queuedChat(key).map((q: any) => [q.text, q.sentBy]), [['first', dial], ['second', dial]]);
    // Once: the same dial sends nothing again.
    assert.deepStrictEqual(m.flushChatQueue(key, () => true, 'main', dial), []);
    const taken = m.confirmChatQueue(key, dial);
    assert.deepStrictEqual(taken.map((q: any) => q.text), ['first', 'second']);
    assert.strictEqual(taken[0].sentBy, undefined);
    assert.deepStrictEqual(m.queuedChat(key), []);
    assert.deepStrictEqual(m.confirmChatQueue(key, dial), []);
    off();
    m.enqueueChat(key, 'after');
    assert.ok(notified >= 4, 'flush and confirm are heard');
    const heard = notified;
    m.enqueueChat(key, 'later');
    assert.strictEqual(notified, heard, 'an unsubscribed listener hears nothing');
    m.abandonChatQueue(key);
});

test('a refused send keeps it and everything after it queued (GN-03)', () => {
    const key = 'cell-2:c.chat';
    for (const t of ['one', 'two', 'three']) m.enqueueChat(key, t);
    let budget = 1;
    const dial = m.newChatDial();
    const sent = m.flushChatQueue(key, () => budget-- > 0, 'main', dial);
    assert.deepStrictEqual(sent.map((q: any) => q.text), ['one']);
    m.confirmChatQueue(key, dial);
    assert.deepStrictEqual(m.queuedChat(key).map((q: any) => q.text), ['two', 'three']);
    m.abandonChatQueue(key);
});

test('a dial closed before its attach is confirmed gives its messages back, in place (GN-03)', () => {
    // The host accepts a refused attach (4409/4404/4401), then closes it: an
    // open dial's sends are not taken until a frame confirms the attach.
    const key = 'cell-4:c.chat';
    for (const t of ['one', 'two']) m.enqueueChat(key, t);
    const refused = m.newChatDial();
    const other = m.newChatDial();
    assert.notStrictEqual(refused, other);
    assert.deepStrictEqual(m.flushChatQueue(key, () => true, 'main', refused).map((q: any) => q.text), ['one', 'two']);
    // Another view's open socket sends nothing while they are in flight.
    const otherFrames: any[] = [];
    assert.deepStrictEqual(m.flushChatQueue(key, (f: any) => { otherFrames.push(f); return true; }, 'main', other), []);
    assert.deepStrictEqual(otherFrames, []);
    // The refused dial closes: they wait again, in order, for the next socket.
    m.requeueChatQueue(key, refused);
    assert.deepStrictEqual(m.queuedChat(key).map((q: any) => [q.text, q.sentBy]), [['one', undefined], ['two', undefined]]);
    assert.deepStrictEqual(m.confirmChatQueue(key, refused), []);
    assert.deepStrictEqual(m.flushChatQueue(key, (f: any) => { otherFrames.push(f); return true; }, 'main', other).map((q: any) => q.text), ['one', 'two']);
    assert.deepStrictEqual(otherFrames.map((f: any) => f.content), ['one', 'two']);
    assert.deepStrictEqual(m.confirmChatQueue(key, other).map((q: any) => q.text), ['one', 'two']);
    assert.deepStrictEqual(m.queuedChat(key), []);
});

test('a queue the run never takes comes back as composer text (GN-03)', () => {
    const key = 'cell-3:c.chat';
    assert.strictEqual(m.abandonChatQueue(key), '');
    m.enqueueChat(key, 'first');
    m.enqueueChat(key, 'second');
    assert.strictEqual(m.abandonChatQueue(key), 'first\n\nsecond');
    assert.deepStrictEqual(m.queuedChat(key), []);
    assert.match(m.QUEUE_ABANDONED_TEXT, /back in the composer/);
    // Queues are per file.
    assert.strictEqual(m.chatQueueKey({ getCellId: () => 'c', getFilename: () => 'x.chat' }), 'c:x.chat');
});
