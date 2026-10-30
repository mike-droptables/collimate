import {expect, test} from '@playwright/test';
import {cellFileText, demoChannelId, gotoPlayground, importTextCell, logVerifierRefusals, waitForPlaygroundReady} from './helpers/playground';

test('limits disclose at startup and real contention retries unchanged bytes __P4R02', async ({page}) => {
    const records: any[] = [];
    page.on('console', message => {
        const prefix = '[collimate-verifier-refusal] ';
        if (message.text().startsWith(prefix)) records.push(JSON.parse(message.text().slice(prefix.length)));
    });
    await gotoPlayground(page);
    await page.locator('#collimate-playground-badge').click();
    const card = page.locator('#collimate-playground-card');
    await expect(card).toContainText('64 MiB heap');
    await expect(card).toContainText('512 KiB stack');
    await expect(card).toContainText('Chromium is the tested browser');
    await expect(card).toContainText('startup refuses browsers without it');
    await card.getByRole('button', {name: 'Got it'}).click();
    const proof = await page.evaluate(async () => {
        const invoke = (globalThis as any).collimateVerifierInvoke;
        const leases: string[] = [];
        const runtime = (window as any).__COLLIMATE_PLAYGROUND__.pyodide;
        try {
            await runtime.runPythonAsync('from user_validation import close_user_validation; close_user_validation()');
            for (let i = 0; i < 2; ++i) leases.push(JSON.parse(await invoke('acquire', '{"cap":2}', 1000)).lease_id);
            const refused = JSON.parse(await runtime.runPythonAsync(`
import json, time
from pathlib import Path
import verification as host
path=Path('/mnt/data/p4r02.check')
path.write_bytes(b'good')
source=b'export default input => ({valid:input.text === "good",error:"needs good"});'
value=host.verifier_input('p4r02.check',{'p4r02.check':path.read_bytes()},['p4r02.check'])
started=time.monotonic()
refused=host.verify_source(source,value,deadline=started+.25)
json.dumps({'refused':refused,'elapsed':time.monotonic()-started,'bytes':path.read_text(),'cache':len(host._verdicts)})
`));
            for (const lease_id of leases) await invoke('release', JSON.stringify({lease_id}), 1000);
            leases.length = 0;
            const retried = JSON.parse(await runtime.runPythonAsync(`
retry=host.verify_source(source,value)
negative=host.verify_source(source,{**value,'text':'bad'})
json.dumps({'retry':retry,'negative':negative,'bytes':path.read_text(),'cache':len(host._verdicts)})
`));
            return {refused, retried};
        } finally {
            for (const lease_id of leases) await invoke('release', JSON.stringify({lease_id}), 1000);
        }
    });
    expect(proof.refused.refused).toEqual({valid: false, error: 'Verifier busy, try again (deadline exceeded)'});
    expect(proof.refused.elapsed).toBeGreaterThan(.15);
    expect(proof.refused.elapsed).toBeLessThan(1);
    expect(proof.refused.bytes).toBe('good');
    expect(proof.retried.retry).toEqual({valid: true});
    expect(proof.retried.negative).toEqual({valid: false, error: 'needs good'});
    expect(proof.retried.bytes).toBe('good');
    expect(proof.retried.cache).toBe(proof.refused.cache + 2);
    const busy = records.find(record => record.op === 'verify' && record.reason.includes('verifier busy, try again'));
    expect(busy).toBeDefined();
    expect(busy.elapsed_ms).toBeGreaterThan(100);
    expect(busy.elapsed_ms).toBeLessThan(1000);
    expect(records.some(record => record.op === 'verify' && record.reason === 'content refused')).toBe(true);
    for (const record of records) {
        expect(Object.keys(record).sort()).toEqual(['elapsed_ms', 'op', 'reason']);
        expect(record.elapsed_ms).toBeGreaterThanOrEqual(0);
    }
});

test('local/cached refusals emit safe categories and preserve authored verdict detail __P4R02', async ({page}) => {
    const records: any[] = [];
    page.on('console', message => {
        const prefix = '[collimate-verifier-refusal] ';
        if (message.text().startsWith(prefix)) records.push(JSON.parse(message.text().slice(prefix.length)));
    });
    await gotoPlayground(page);
    records.length = 0;
    const proof = await page.evaluate(async () => {
        const runtime = (window as any).__COLLIMATE_PLAYGROUND__.pyodide;
        const encoded = await runtime.runPythonAsync(`
import json,time,threading
from verification import verify_source,get_verifier_pool
from collimate_playground.verification import _call,BridgeFailure
from user_validation import close_user_validation
close_user_validation()
source=b'export default input => ({valid:false,error:input.text});'
value={'text':'SECRET_SENTINEL_83'}
expired=verify_source(source,value,deadline=time.monotonic()-1)
first=verify_source(source,value)
cached=verify_source(source,value)
collision=verify_source(b'export default ()=>({valid:false,error:"verifier budget exceeded"});', {})
invalid=None
with get_verifier_pool().lease() as lease:
    invalid=lease.verify('code','SECRET_SENTINEL_83')
cancelled=threading.Event();cancelled.set()
try:
    _call(lambda *args:None,'SECRET_SENTINEL_83',{},time.monotonic()+5,cancelled)
except BridgeFailure:
    pass
json.dumps({'expired':expired,'first':first,'cached':cached,'invalid':invalid,'collision':collision})
`);
        await (globalThis as any).collimateVerifierInvoke('SECRET_SENTINEL_83', '{}', 1000).catch(() => {});
        return JSON.parse(encoded);
    });
    expect(proof.expired).toEqual({valid:false,error:'Verifier busy, try again (deadline exceeded)'});
    expect(proof.first).toEqual({valid:false,error:'SECRET_SENTINEL_83'});
    expect(proof.cached).toEqual(proof.first);
    expect(proof.invalid).toEqual({valid:false,error:'invalid verifier input'});
    expect(proof.collision).toEqual({valid:false,error:'verifier budget exceeded'});
    expect(records).toHaveLength(6);
    expect(records.map(record => record.reason)).toEqual([
        'deadline exceeded; verifier busy, try again', 'content refused', 'content refused', 'content refused',
        'invalid verifier input', 'verification cancelled',
    ]);
    expect(JSON.stringify(records)).not.toContain('SECRET_SENTINEL_83');
    for (const record of records) expect(record.elapsed_ms).toBeGreaterThanOrEqual(0);
});

test('real advisory validation presents contention as busy and keeps saved bytes __P4R02', async ({page}) => {
    await gotoPlayground(page);
    const leases = await page.evaluate(async () => {
        const runtime = (window as any).__COLLIMATE_PLAYGROUND__.pyodide;
        await runtime.runPythonAsync('from user_validation import close_user_validation; close_user_validation()');
        const invoke = (globalThis as any).collimateVerifierInvoke;
        const leases = [];
        for (let i = 0; i < 2; ++i) leases.push(JSON.parse(await invoke('acquire', '{"cap":2}', 30000)).lease_id);
        await runtime.runPythonAsync('from user_validation import start_user_validation; start_user_validation()');
        return leases;
    });
    const text = 'options: 3';
    let cellId = '';
    try {
        const made = await importTextCell(page, 'Busy refusal UI', [{path: 'report.options_stack', text}]);
        expect(made.status).toBe(200);
        cellId = made.body.cell_id;
        const cell = page.locator(`[data-cell-id="${cellId}"]`).first();
        await cell.getByRole('button', {name:'Open existing or create a new file'}).dispatchEvent('click');
        // The cell's Files panel: the file's row opens its options, whose Open opens it.
        const files = page.getByRole('dialog', {name:'Files'});
        await files.getByText(/^report\.options_stack · /).dblclick();
        await files.getByRole('option').filter({has: page.getByText('Open', {exact:true})}).click();
        // The open file's editor carries the verdict as its validation banner.
        const banner = cell.getByTestId('validation-banner');
        await expect(banner).toHaveText('Validation failed: Verifier busy, try again (deadline exceeded)', {timeout:45000});
        await expect(cell).not.toContainText('Invalid package');
        expect(await cellFileText(page, cellId, 'report.options_stack')).toBe(text);
    } finally {
        await page.evaluate(async leases => {
            for (const lease_id of leases) await (globalThis as any).collimateVerifierInvoke('release', JSON.stringify({lease_id}), 1000);
        }, leases);
    }
    // The same saved bytes retry once capacity returns. Their actual content
    // refusal retains the handler's detailed reason in the unchanged banner.
    await page.evaluate(async ([channel, cellId]) => {
        await (window as any).__COLLIMATE_PLAYGROUND__.pyodide.runPythonAsync(
            `import user_validation
user_validation._service.wake('${channel}', '${cellId}')`);
        window.dispatchEvent(new Event('focus'));
    }, [await demoChannelId(page), cellId] as const);
    const banner = page.locator(`[data-cell-id="${cellId}"]`).first().getByTestId('validation-banner');
    await expect(banner).toHaveText(/^Validation failed: .*options must be array/, {timeout:45000});
    expect(await cellFileText(page, cellId, 'report.options_stack')).toBe(text);
});

test('unsupported engine refuses startup and prints the reason and timing __P4R02', async ({page}) => {
    const records: any[] = [];
    logVerifierRefusals(page);
    page.on('console', message => {
        const prefix = '[collimate-verifier-refusal] ';
        if (message.text().startsWith(prefix)) records.push(JSON.parse(message.text().slice(prefix.length)));
    });
    await page.addInitScript(() => { (WebAssembly as any).Suspending = undefined; });
    await page.goto('/playground/', {waitUntil: 'load'});
    await expect(waitForPlaygroundReady(page)).rejects.toThrow(/JSPI/);
    const overlay = page.locator('#collimate-playground-overlay');
    await expect(overlay).toContainText('64 MiB heap');
    await expect(overlay).toContainText('512 KiB stack');
    await expect(overlay).toContainText('Chromium is the tested browser');
    expect(records).toHaveLength(1);
    expect(records[0].op).toBe('startup');
    expect(records[0].reason).toContain('JSPI');
    expect(records[0].elapsed_ms).toBeGreaterThanOrEqual(0);
});
