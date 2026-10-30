import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../web/verifier-supervisor.js', import.meta.url), 'utf8');
const {createVerifierSupervisor} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fixture(run) {
    const old = {Worker: globalThis.Worker, fetch: globalThis.fetch,
        Suspending: WebAssembly.Suspending, promising: WebAssembly.promising, warn: console.warn};
    const refusals = [];
    console.warn = message => {
        assert.ok(message.startsWith('[collimate-verifier-refusal] '));
        refusals.push(JSON.parse(message.slice('[collimate-verifier-refusal] '.length)));
    };
    const workers = [];
    let behavior = 'reply';
    class Worker {
        constructor() { this.dead = false; workers.push(this); }
        terminate() { this.dead = true; }
        postMessage(message) {
            if (message.op === 'evaluate' && behavior === 'hang') return;
            if (message.op === 'evaluate' && behavior === 'hostile-error') {
                queueMicrotask(() => this.onmessage({data: {id: message.id, ok: false, error: 'SECRET_SENTINEL_83'}}));
                return;
            }
            const result = message.op === 'init' ? {ready: true}
                : message.op === 'compile' ? {code: 'compiled', entrypoint_js: 'source'}
                    : {verdict_json: behavior === 'negative' ? '{"valid":false,"error":"SECRET_SENTINEL_83"}' : '{"valid":true}'};
            queueMicrotask(() => {
                if (!this.dead) this.onmessage({data: {id: behavior === 'bad-id' && message.op === 'evaluate' ? -1 : message.id, ok: true, result}});
            });
        }
    }
    globalThis.Worker = Worker;
    globalThis.fetch = async () => ({ok: true, arrayBuffer: async () => new TextEncoder().encode('{}').buffer});
    WebAssembly.Suspending = function () {};
    WebAssembly.promising = function () {};
    let supervisor;
    try {
        supervisor = await createVerifierSupervisor(new URL('https://runtime.invalid/verifier/'));
        const invoke = (op, payload = {}, ms = 1000, cancelled = null) => supervisor.invoke(op, JSON.stringify(payload), ms, cancelled);
        const acquire = async () => JSON.parse(await invoke('acquire', {cap: 2})).lease_id;
        await run({supervisor, invoke, acquire, workers, refusals, setBehavior: value => {behavior = value;}});
    } finally {
        supervisor?.close();
        console.warn = old.warn;
        globalThis.Worker = old.Worker;
        globalThis.fetch = old.fetch;
        WebAssembly.Suspending = old.Suspending;
        WebAssembly.promising = old.promising;
    }
    assert.ok(workers.every(worker => worker.dead), 'all owned worker generations terminate');
}

test('leases cap at two while compilation remains available __P4F09', async () => fixture(async ({invoke, acquire, workers}) => {
    const first = await acquire(), second = await acquire();
    await assert.rejects(invoke('acquire', {cap: 2}, 35), /Verifier busy, try again/);
    assert.equal(workers.length, 3, 'two verifier workers and one independent compiler');
    assert.deepEqual(JSON.parse(await invoke('compile')), {code: 'compiled', entrypoint_js: 'source'});
    await invoke('release', {lease_id: first});
    const third = await acquire();
    assert.notEqual(third, first);
    await invoke('release', {lease_id: second});
    await invoke('release', {lease_id: third});
}));

test('timeout terminates the worker and a replacement can verify __P4F09', async () => fixture(async ({invoke, acquire, workers, setBehavior}) => {
    const lease_id = await acquire();
    setBehavior('hang');
    await assert.rejects(invoke('evaluate', {lease_id}, 35), /Verifier busy, try again/);
    assert.equal(workers.filter(worker => worker.dead).length, 1);
    await invoke('release', {lease_id, discard: true});
    setBehavior('reply');
    const next = await acquire();
    assert.equal(await invoke('evaluate', {lease_id: next}), '{"valid":true}');
    await invoke('release', {lease_id: next});
}));

test('cancellation and shutdown settle active work and free all workers __P4F09', async () => fixture(async ({supervisor, invoke, acquire, workers, setBehavior}) => {
    const lease_id = await acquire();
    setBehavior('hang');
    let cancelled = false;
    const active = invoke('evaluate', {lease_id}, 1000, () => cancelled);
    cancelled = true;
    await assert.rejects(active, /Verifier cancelled/);
    await invoke('release', {lease_id, discard: true});
    const next = await acquire();
    const pending = invoke('evaluate', {lease_id: next});
    await sleep(1);
    supervisor.close();
    await assert.rejects(pending, /shutting down/);
    await invoke('release', {lease_id: next});
    assert.ok(workers.every(worker => worker.dead));
}));

test('a mismatched reply cannot satisfy a live request __P4F09', async () => fixture(async ({invoke, acquire, setBehavior}) => {
    const lease_id = await acquire();
    setBehavior('bad-id');
    await assert.rejects(invoke('evaluate', {lease_id}), /protocol failed/);
    await invoke('release', {lease_id, discard: true});
}));

test('a browser lacking JSPI refuses before workers start __P4F09', async () => {
    const old = WebAssembly.Suspending;
    try {
        WebAssembly.Suspending = undefined;
        await assert.rejects(createVerifierSupervisor(new URL('https://runtime.invalid/')), /WebAssembly Promise Integration/);
    } finally { WebAssembly.Suspending = old; }
});


test('mechanism returns authored detail without copying untrusted input/error/op to logs __P4R02', async () => fixture(async ({supervisor, invoke, acquire, refusals, setBehavior}) => {
    const first = await acquire(), second = await acquire();
    await assert.rejects(invoke('acquire', {cap: 2}, 40), /Verifier busy, try again/);
    await invoke('release', {lease_id: second});
    const next = await acquire();
    setBehavior('negative');
    assert.equal(await invoke('evaluate', {lease_id: next, code: 'SECRET_SENTINEL_83', input_json: 'SECRET_SENTINEL_83'}), '{"valid":false,"error":"SECRET_SENTINEL_83"}');
    setBehavior('hostile-error');
    await assert.rejects(invoke('evaluate', {lease_id: next}), /SECRET_SENTINEL_83/);
    await assert.rejects(invoke('SECRET_SENTINEL_83'), /protocol failed/);
    await assert.rejects(supervisor.invoke('compile', '{SECRET_SENTINEL_83', 1000), /Verifier input is not valid JSON/);
    assert.deepEqual(refusals, [], 'the Python outer attempt observes refusals; nested JS never duplicates or leaks');
    await invoke('release', {lease_id: first});
    await invoke('release', {lease_id: next});
}));

test('startup worker diagnostics stay out of refusal records __P4R02', async () => {
    const old = {Worker: globalThis.Worker, fetch: globalThis.fetch, warn: console.warn,
        Suspending: WebAssembly.Suspending, promising: WebAssembly.promising};
    const records = [];
    try {
        WebAssembly.Suspending = function () {};
        WebAssembly.promising = function () {};
        globalThis.fetch = async () => ({ok:true, arrayBuffer:async () => new TextEncoder().encode('{}').buffer});
        globalThis.Worker = class {
            terminate() {}
            postMessage(message) {
                queueMicrotask(() => this.onmessage({data:{id:message.id,ok:false,error:'SECRET_STARTUP_83'}}));
            }
        };
        console.warn = message => records.push(message);
        await assert.rejects(createVerifierSupervisor(new URL('https://runtime.invalid/')), /SECRET_STARTUP_83/);
        assert.equal(records.length, 1);
        const record = JSON.parse(records[0].slice('[collimate-verifier-refusal] '.length));
        assert.equal(record.op, 'startup');
        assert.equal(record.reason, 'startup refused');
        assert.ok(record.elapsed_ms >= 0);
        assert.ok(!JSON.stringify(records).includes('SECRET_STARTUP_83'));
    } finally {
        globalThis.Worker = old.Worker;
        globalThis.fetch = old.fetch;
        console.warn = old.warn;
        WebAssembly.Suspending = old.Suspending;
        WebAssembly.promising = old.promising;
    }
});
