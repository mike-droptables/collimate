import { displayFile, putTextFiles, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { expect, test } from '@playwright/test';

for (const embed of ['native', 'iframe']) for (const broken of [false, true]) {
  test(`actual unbuilt ${embed}${broken ? ' syntax error recovers then' : ''} package compiles in local worker and persists across pages__F0251_F0295_F1868_F1280`, async ({ page, request, baseURL }, testInfo) => {
    const name = `P3 worker ${embed} ${Date.now()}`;
    const channelId = await createSeededChannel(request, name);
    // A renderer package of the channel's own (the shipped ones are the
    // app's and read-only): added to Capabilities, then edited in place to
    // the unbuilt probe, as an author would.
    const id = `p3-worker-${embed}${broken ? '-broken' : ''}`;
    const candidate = await importCell(request, channelId, 'P3 worker candidate', [
      {name: 'probe/probe.renderer', content: `id: ${id}\nname: P3 worker probe\nsummary: Unbuilt worker compile fixture\nentrypoint: main.tsx\nembed: ${embed}\nextensions: [.p3worker]\n`},
      {name: 'probe/main.tsx', content: "import {defineRenderer} from '/api/sdk/react.js'; export default defineRenderer({component: () => null});"},
    ]);
    const args = {source_cell_id: candidate.cell_id, definition_path: 'probe/probe.renderer', target_cell_id: null};
    const preview = await request.post(`/api/channels/${channelId}/packages/preview`, {data: args});
    expect(preview.ok(), await preview.text()).toBe(true);
    const prepared = await preview.json();
    const added = await request.post(`/api/channels/${channelId}/packages/add`, {data: {...args, expected_content_hash: prepared.content_hash, expected_target_revision: prepared.target_revision, replace: false}});
    expect(added.ok(), await added.text()).toBe(true);
    const packageCell = (await added.json()).cell_id as string;
    const prefix = `${prepared.folder}/`;
    await putTextFiles(request, channelId, packageCell, [
      {name: `${prefix}main.tsx`, content: "import {text} from './inner'; export default {mount(node: HTMLElement,api:any){node.textContent=text;api.ready();}};", language: 'typescript'},
      {name: `${prefix}inner.ts`, content: broken ? 'export const text = "unterminated' : `export const text = 'P3 worker ${embed} ready';\r\n// ${name}\r\n`, language: 'typescript'},
    ]);
    const find = async () => (await (await request.get('/api/renderers?include_disabled=true')).json()).find((r: {id: string}) => r.id === id);
    let current = await find();
    expect(current).toBeDefined();
    expect((await request.get(`/api/renderers/${id}/source?v=${current.content_hash}`)).status()).toBe(404);
    const created = await importCell(request, channelId, 'Worker output', [{name:'sample.p3worker',content:'input'}]);
    await displayFile(request, channelId, created.cell_id, 'sample.p3worker', id);
    const appOrigin = new URL(baseURL!).origin;
    const compilerCDN: string[] = [];
    const workers: string[] = [];
    page.on('worker', worker => workers.push(worker.url()));
    page.on('request', req => {if (/babel|https?:\/\/[^/]+\/.*esbuild/.test(req.url()) && new URL(req.url()).origin !== appOrigin) compilerCDN.push(req.url());});
    await page.goto('/');
    if (broken) {
      await expect.poll(async () => {
        for (const frame of page.frames()) if ((await frame.locator('body').innerText()).includes('Unterminated string literal')) return true;
        return false;
      }, {timeout:15000}).toBe(true);
      expect((await request.get(`/api/renderers/${id}/source?v=${current.content_hash}`)).status()).toBe(404);
      await putTextFiles(request, channelId, packageCell, [
        {name: `${prefix}inner.ts`, content: `export const text = 'P3 worker ${embed} ready';\r\n// ${name}\r\n`, language: 'typescript'},
      ]);
      current = await find();
      await page.reload();
    }
    await expect.poll(async () => {
      for (const frame of page.frames()) if (await frame.getByText(`P3 worker ${embed} ready`, {exact:true}).count()) return true;
      return false;
    }, {timeout:15000}).toBe(true);
    expect(workers.length).toBeGreaterThan(0);
    expect(compilerCDN).toEqual([]);
    const cached = await request.get(`/api/renderers/${id}/source?v=${current.content_hash}`);
    expect(cached.status()).toBe(200);
    const snapshot = await (await request.get(`/api/renderers/${id}/package?v=${current.content_hash}`)).json();
    const { bundleRenderer } = await import(pathToFileURL(path.resolve('../sdk/scripts/renderer_bundle.mjs')).href);
    const reference = await bundleRenderer({entrypoint:snapshot.entrypoint,
      files:new Map(Object.entries(snapshot.files).map(([name,bytes]) => [name,Buffer.from(bytes as string,'base64')]))});
    expect(reference.source_hash).toBe(current.content_hash);
    expect(await cached.text(), 'actual browser worker output equals pinned SDK Node bundle').toBe(reference.code);
    const beforeWorkers = workers.length;
    await page.reload();
    await expect.poll(async () => {
      for (const frame of page.frames()) if (await frame.getByText(`P3 worker ${embed} ready`, {exact:true}).count()) return true;
      return false;
    }).toBe(true);
    expect(workers.length).toBe(beforeWorkers);
    await testInfo.attach('worker-identity',{contentType:'application/json',body:Buffer.from(JSON.stringify({embed,source_hash:current.content_hash,workers,compilerCDN}))});
    await testInfo.attach('actual-worker-renderer',{contentType:'image/png',body:await page.screenshot()});
  });
}
