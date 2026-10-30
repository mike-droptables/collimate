import {expect, test} from '@playwright/test';
import {api, demoChannelId, gotoPlayground, importTextCell} from './helpers/playground';

test('accepted verification boots, flags user bytes, times out and drains __P4F09', async ({page}) => {
    await gotoPlayground(page);
    const channel = await demoChannelId(page);

    // Admission already executed the canonical shipped verifiers during boot.
    // A person's invalid typed file remains saved and receives a real verdict.
    const made = await importTextCell(page, 'Verifier regression',
        [{path: 'report.options_stack', text: 'options: 3'}]);
    expect(made.status).toBe(200);
    await expect.poll(async () => {
        const response = await api<{pending: boolean; verdicts: Record<string, {valid: boolean; error?: string}>}>(
            page, 'GET', `/channels/${channel}/cells/${made.body.cell_id}/validation`);
        return response.body.pending ? null : response.body.verdicts['report.options_stack'];
    }, {timeout: 45000}).toMatchObject({valid: false, error: expect.stringContaining('options must be array')});

    const proof = await page.evaluate(async channel => {
        const runtime = (window as any).__COLLIMATE_PLAYGROUND__.pyodide;
        let ticks = 0;
        const timer = setInterval(() => ++ticks, 10);
        (globalThis as any).__verifierTestDelay = () => new Promise(resolve => setTimeout(resolve, 100));
        try {
            const encoded = await runtime.runPythonAsync(`
import asyncio, json, time
from pyodide.ffi import run_sync
from js import __verifierTestDelay
from verification import verify_source
from user_validation import ValidationService
value={'path':'sample.check','text':'ok','package':{'sample.check':'ok'},'cell_paths':['sample.check']}
source=b'export default input => ({valid:input.text === "ok",error:"bad text"});'
valid=verify_source(source,value)
invalid=verify_source(source,{**value,'text':'bad'})
began=time.monotonic()
hostile=verify_source(b'export default () => {while(true){}}',value,deadline=began+.25)
elapsed=time.monotonic()-began
events=[]
service=ValidationService()
def work(key):
    events.append('work-enter')
    run_sync(__verifierTestDelay())
    events.append('work-exit')
def during():
    events.append('close-enter')
    service.wake('${channel}','probe')
    service._step()
    service.close()
    events.append('close-exit')
service._validate=work
service.pending[('${channel}','probe')]=time.monotonic()
service._arm()
asyncio.get_running_loop().call_later(.02,during)
await asyncio.sleep(.2)
json.dumps({'valid':valid,'invalid':invalid,'hostile':hostile,'elapsed':elapsed,'events':events,
            'stopped':service.stopping and not service.busy and not service.pending and not service.results})
`);
            return {...JSON.parse(encoded), ticks};
        } finally {
            clearInterval(timer);
            delete (globalThis as any).__verifierTestDelay;
        }
    }, channel);
    expect(proof.valid).toEqual({valid: true});
    expect(proof.invalid).toEqual({valid: false, error: 'bad text'});
    expect(proof.hostile.valid).toBe(false);
    expect(proof.hostile.error).toMatch(/Verifier|budget/);
    expect(proof.elapsed).toBeLessThan(2);
    expect(proof.ticks).toBeGreaterThan(2);
    expect(proof.events).toEqual(['work-enter', 'close-enter', 'work-exit', 'close-exit']);
    expect(proof.stopped).toBe(true);
});


test('fatal verifier teardown refuses, terminates and recovers __P4F09', async ({page}) => {
    // Observe ownership only: every operation delegates to the real browser Worker.
    await page.addInitScript(() => {
        const Original = globalThis.Worker;
        const events: any[] = [];
        (globalThis as any).__verifierOwnership = events;
        let serial = 0;
        globalThis.Worker = class extends Original {
            proofId: number;
            constructor(url: string | URL, options?: WorkerOptions) {
                super(url, options);
                this.proofId = ++serial;
            }
            postMessage(data: any, options?: any) {
                if (data?.op === 'evaluate') events.push({
                    event: 'evaluate', worker: this.proofId,
                    callback: data.code?.includes('CALLBACK_PROOF'),
                    recursive: data.code?.includes('const recur'),
                    recovery: data.code?.includes('GOOD_RECOVERY'),
                });
                return super.postMessage(data, options);
            }
            terminate() {
                events.push({event: 'terminate', worker: this.proofId});
                return super.terminate();
            }
        };
    });
    await gotoPlayground(page);
    const proof = await page.evaluate(async () => {
        const runtime = (window as any).__COLLIMATE_PLAYGROUND__.pyodide;
        const raw = await runtime.runPythonAsync(`
import json, time
from verification import verify_source, get_verifier_pool
value={'path':'recursion-recovery.check','text':'GOOD_RECOVERY','package':{},'cell_paths':[]}
callback=('import {bundle} from "collimate/verify"; export default ()=>{const CALLBACK_PROOF=bundle('+json.dumps({'main.tsx':"import {x} from './helper'; export default {__collimate_renderer__:true,component:()=>x};",'helper.ts':'export const x=null;'})+',"main.tsx"); return {valid:typeof CALLBACK_PROOF.code === "string"}}').encode()
recursive=b'export default ()=>{const recur=()=>recur();recur();return {valid:true}}'
with get_verifier_pool().lease(time.monotonic()+30) as lease:
    before=lease.token
    callback_result=verify_source(callback,value,lease=lease)
    recursion_result=verify_source(recursive,value,lease=lease)
    discarded=lease.token
    good_result=verify_source(b'export default input=>({valid:input.text === "GOOD_RECOVERY"})',value,lease=lease)
    recovered=lease.token
json.dumps({'callback':callback_result,'recursion':recursion_result,'good':good_result,
            'before':before,'discarded':discarded,'recovered':recovered})
`);
        return {...JSON.parse(raw), events: (globalThis as any).__verifierOwnership};
    });
    expect(proof.callback).toEqual({valid: true});
    expect(proof.recursion.valid).toBe(false);
    expect(proof.recursion.error).toMatch(/^Verifier worker protocol failed: .*gc_obj_list/);
    expect(proof.recursion.error.length).toBeLessThanOrEqual(4096);
    expect(proof.discarded).toBeNull();
    expect(proof.recovered).not.toBe(proof.before);
    expect(proof.good).toEqual({valid: true});
    const callback = proof.events.find((event: any) => event.callback);
    const recursive = proof.events.find((event: any) => event.recursive);
    const recovery = proof.events.find((event: any) => event.recovery);
    expect(callback).toBeDefined();
    expect(recursive).toBeDefined();
    expect(recovery).toBeDefined();
    expect(recursive.worker).toBe(callback.worker);
    const failureIndex = proof.events.indexOf(recursive);
    const terminatedIndex = proof.events.findIndex((event: any, index: number) =>
        index > failureIndex && event.event === 'terminate' && event.worker === recursive.worker);
    expect(terminatedIndex).toBeGreaterThan(failureIndex);
    expect(proof.events.indexOf(recovery)).toBeGreaterThan(terminatedIndex);
    expect(recovery.worker).not.toBe(recursive.worker);
});
