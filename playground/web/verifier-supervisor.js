// The page owns deadlines; authored JavaScript only executes inside a killable
// worker's bounded QuickJS heap. Pyodide JSPI suspends Python while we supervise.
const MAX_FRAME = 64 * 1024 ** 2;
const FILE_MS = 5000;
const STARTUP_MS = 30000;
const CAP = 2;
const encoder = new TextEncoder();
const now = () => performance.now();
const BUSY = 'Verifier busy, try again (deadline exceeded)';
export const VERIFIER_LIMITS = 'File verification has a 64 MiB heap and 512 KiB stack per evaluation. Chromium is the tested browser. WebAssembly Promise Integration (JSPI) is required; startup refuses browsers without it.';

function recordStartupRefusal(started) {
    const reason = typeof WebAssembly.Suspending !== 'function' || typeof WebAssembly.promising !== 'function'
        ? 'JSPI unavailable' : 'startup refused';
    console.warn('[collimate-verifier-refusal] ' + JSON.stringify({
        op: 'startup', reason, elapsed_ms: Math.max(0, Math.round(now() - started)),
    }));
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function failure(message) {
    return new Error(String(message || 'Verifier worker protocol failed').slice(0, 4096));
}

function check(deadline, cancelled) {
    if (cancelled && cancelled()) throw failure('Verifier cancelled');
    if (!Number.isFinite(deadline) || now() >= deadline) throw failure(BUSY);
}

function frame(text) {
    if (typeof text !== 'string' || encoder.encode(text).length > MAX_FRAME) {
        throw failure('Verifier frame exceeds byte limit');
    }
    return text;
}

class WorkerClient {
    constructor(url) {
        this.worker = new Worker(url, {type: 'module'});
        this.dead = false;
        this.serial = 0;
        this.calls = 0;
        this.pending = null;
        this.worker.onmessage = ({data}) => {
            const pending = this.pending;
            if (!pending) return; // a settled generation cannot affect a later job
            if (!data || data.id !== pending.id || typeof data.ok !== 'boolean') {
                this.terminate('Verifier worker protocol failed');
                return;
            }
            try {
                frame(JSON.stringify(data));
                check(pending.deadline, pending.cancelled);
                if (!data.ok) throw failure(data.error);
                pending.finish(null, data.result);
            } catch (error) {
                pending.finish(error);
            }
        };
        this.worker.onerror = event => {
            event.preventDefault();
            this.terminate('Verifier worker exited before returning a verdict');
        };
        this.worker.onmessageerror = () => this.terminate('Verifier worker protocol failed');
    }

    request(op, payload, deadline, cancelled = null) {
        check(deadline, cancelled);
        if (this.dead) throw failure('Verifier worker exited before returning a verdict');
        if (this.pending) throw failure('Verifier worker busy');
        const id = ++this.serial;
        return new Promise((resolve, reject) => {
            let timeout, cancellation;
            const finish = (error, value) => {
                if (this.pending?.id !== id) return;
                this.pending = null;
                clearTimeout(timeout);
                clearInterval(cancellation);
                if (error) reject(error); else resolve(value);
            };
            this.pending = {id, finish, deadline, cancelled};
            timeout = setTimeout(() => this.terminate(BUSY), Math.max(0, deadline - now()));
            if (cancelled) cancellation = setInterval(() => {
                try { check(deadline, cancelled); }
                catch (error) { this.terminate(error.message); }
            }, 20);
            try {
                this.worker.postMessage({id, op, ...payload, remaining_ms: Math.max(0, deadline - now())});
            } catch (error) {
                this.terminate('Verifier worker protocol failed');
            }
        });
    }

    terminate(reason = 'Verifier pool is shutting down') {
        if (this.dead) return;
        this.dead = true;
        this.worker.terminate();
        this.pending?.finish(failure(reason));
    }
}

/** Prewarm every engine under one bounded startup budget, before file budgets. */
export async function createVerifierSupervisor(baseURL = new URL('./verifier/', import.meta.url)) {
    const started = now();
    try {
        return await startVerifierSupervisor(baseURL);
    } catch (error) {
        recordStartupRefusal(started);
        throw error;
    }
}

async function startVerifierSupervisor(baseURL) {
    if (typeof WebAssembly.Suspending !== 'function' || typeof WebAssembly.promising !== 'function') {
        throw failure('This browser cannot run the demo verifier. ' + VERIFIER_LIMITS);
    }
    const deadline = now() + STARTUP_MS;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), STARTUP_MS);
    let assets;
    try {
        const read = async name => {
            const response = await fetch(new URL(name, baseURL), {signal: controller.signal});
            if (!response.ok) throw failure(`Verifier resource unavailable: ${name}`);
            const bytes = await response.arrayBuffer();
            if (bytes.byteLength > MAX_FRAME) throw failure('Verifier resource exceeds byte limit');
            return bytes;
        };
        const [esbuild_wasm, quickjs_wasm, contractBytes] = await Promise.all([
            read('esbuild.wasm'), read('quickjs.wasm'), read('renderer_bundle_contract.json'),
        ]);
        assets = {esbuild_wasm, quickjs_wasm, contract: JSON.parse(new TextDecoder().decode(contractBytes))};
    } finally {
        clearTimeout(timer);
    }

    let closed = false;
    let serial = 0;
    let compilerBusy = false;
    const clients = new Set();
    const leases = new Map();
    const slots = Array.from({length: CAP}, () => ({client: null, ready: false, error: null}));
    const workerURL = new URL('verifier-worker.js', baseURL);

    const start = async (slot, end) => {
        if (closed) throw failure('Verifier pool is shutting down');
        slot.client?.terminate();
        if (slot.client) clients.delete(slot.client);
        slot.ready = false;
        slot.error = null;
        const client = new WorkerClient(workerURL);
        slot.client = client;
        clients.add(client);
        try {
            await client.request('init', assets, end);
            if (closed) throw failure('Verifier pool is shutting down');
            slot.ready = true;
        } catch (error) {
            slot.error = error;
            client.terminate(error.message);
            throw error;
        }
    };
    const compiler = {client: null, ready: false, error: null};
    const close = () => {
        closed = true;
        for (const client of clients) client.terminate();
        clients.clear();
        leases.clear();
        return '{}';
    };
    try {
        await Promise.all([start(compiler, deadline), ...slots.map(slot => start(slot, deadline))]);
    } catch (error) {
        close();
        throw error;
    }

    const guard = (end, cancelled) => {
        if (closed) throw failure('Verifier pool is shutting down');
        check(end, cancelled);
    };

    async function dispatch(op, payloadJSON, remainingMS, cancelled = null) {
        let payload;
        const encoded = frame(payloadJSON);
        try { payload = JSON.parse(encoded); }
        catch { throw failure('Verifier input is not valid JSON'); }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw failure('Verifier input is not valid JSON');
        if (op === 'close') return close();
        // Cleanup is idempotent even after shutdown or a file deadline expired.
        if (op === 'release') {
            const slot = leases.get(payload.lease_id);
            if (slot) {
                leases.delete(payload.lease_id);
                if (payload.discard || slot.client.dead || slot.client.calls >= 8) {
                    slot.client.terminate();
                    slot.ready = false;
                    if (!closed) void start(slot, now() + STARTUP_MS).catch(() => {});
                }
            }
            return '{}';
        }
        if (!Number.isFinite(remainingMS) || remainingMS <= 0) throw failure(BUSY);
        const end = now() + Math.min(remainingMS, op === 'acquire' ? 30000 : FILE_MS);
        guard(end, cancelled);
        if (op === 'acquire') {
            if (payload.cap !== CAP) throw failure('Verifier pool capacity differs from the supported bound');
            while (true) {
                guard(end, cancelled);
                const slot = slots.find(candidate => candidate.ready && !candidate.client.dead && ![...leases.values()].includes(candidate));
                if (slot) {
                    const lease_id = `lease-${++serial}`;
                    leases.set(lease_id, slot);
                    return JSON.stringify({lease_id});
                }
                const failed = slots.find(candidate => candidate.error && ![...leases.values()].includes(candidate));
                if (failed) throw failure(failed.error.message);
                await delay(Math.min(20, Math.max(0, end - now())));
            }
        }
        if (op === 'compile') {
            // Run publication acquires its verifier lease BEFORE compilation.
            // A separate bounded compiler slot avoids waiting on that own lease.
            while (compilerBusy) {
                guard(end, cancelled);
                await delay(Math.min(20, Math.max(0, end - now())));
            }
            guard(end, cancelled);
            compilerBusy = true;
            try {
                if (!compiler.ready || compiler.client.dead) await start(compiler, end);
                const result = await compiler.client.request('compile', payload, end, cancelled);
                guard(end, cancelled);
                return frame(JSON.stringify(result));
            } finally {
                compilerBusy = false;
            }
        }
        if (op === 'evaluate') {
            const slot = leases.get(payload.lease_id);
            if (!slot || !slot.ready || slot.client.dead) throw failure('Verifier lease is unavailable');
            slot.client.calls += 1;
            const result = await slot.client.request('evaluate', payload, end, cancelled);
            guard(end, cancelled);
            if (!result || typeof result.verdict_json !== 'string' || Array.from(result.verdict_json).length > 4096 + 128) {
                slot.client.terminate('Verifier returned an invalid verdict');
                throw failure('Verifier returned an invalid verdict');
            }
            const verdict = JSON.parse(result.verdict_json);
            if (!verdict || typeof verdict.valid !== 'boolean' || (!verdict.valid && (typeof verdict.error !== 'string' || !verdict.error))) {
                slot.client.terminate('Verifier returned an invalid verdict');
                throw failure('Verifier returned an invalid verdict');
            }
            return result.verdict_json;
        }
        throw failure('Verifier worker protocol failed');
    }


    return {invoke: dispatch, close};
}
