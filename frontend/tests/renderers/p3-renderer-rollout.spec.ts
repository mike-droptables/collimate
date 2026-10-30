import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { expect, test } from '@playwright/test';
import { fixtureFiles, rendererIds } from './fixtures';
import { ready } from './readiness';

test('the readiness matrix covers every shipped renderer__F0251_F0295', async ({ request }) => {
  const response = await request.get('/__renderer_rollout/roster');
  expect(response.ok()).toBeTruthy();
  const roster: { id: string; embed: string }[] = await response.json();
  expect(roster.map(r => r.id).sort()).toEqual([...rendererIds].sort());
  expect(roster.filter(r => r.embed === 'iframe')).toHaveLength(4);
});

for (const id of rendererIds) {
  test(`actual shipped ${id} uses its prebuilt bundle and mounts meaningful content__F0251_F0295${id === 'image' ? '_P3F01' : id === 'excalidraw' ? '_F1871' : id === 'monaco' ? '_F1872' : id === 'renderer_example' ? '_D14' : ''}`, async ({ page, request }, testInfo) => {
    const channel = await createSeededChannel(request, `P3 rollout ${id} ${Date.now()}`);

    const registryResponse = await request.get('/api/renderers?include_disabled=true');
    expect(registryResponse.ok()).toBeTruthy();
    const registry: { id: string; content_hash: string; embed: string }[] = await registryResponse.json();
    const renderer = registry.find(r => r.id === id);
    expect(renderer, `actual seeded channel has renderer ${id}`).toBeDefined();
    expect(renderer!.content_hash).toMatch(/^[a-f0-9]{64}$/);
    const source = await request.get(`/api/renderers/${id}/source?v=${renderer!.content_hash}`);
    expect(source.status(), 'shipped version has a prebuilt cache entry').toBe(200);
    expect(source.headers()['x-entrypoint']).toBe('module.js');
    expect(source.headers()['etag']).toBe(`"${renderer!.content_hash}"`);
    expect(source.headers()['cache-control']).toContain('immutable');
    expect(source.headers()['content-type']).toContain('javascript');
    const staleSource = await request.get(`/api/renderers/${id}/source?v=${'0'.repeat(64)}`);
    expect(staleSource.status(), 'a stale source identity cannot serve the current bundle').toBe(404);
    const files = await fixtureFiles(id, request);
    const { cell_id } = await importCell(request, channel, `Rollout ${id}`, files);
    const path = files[0].name;
    await displayFile(request, channel, cell_id, path, id);
    const errors: string[] = [];
    const terminalReplayFrames: string[] = [];
    const sourceResponses: { url: string; status: number; entrypoint?: string }[] = [];
    const pdfResources: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('websocket', socket => {
      if (new URL(socket.url()).pathname === '/__renderer_rollout/terminal') {
        socket.on('framereceived', event => terminalReplayFrames.push(event.payload.toString()));
      }
    });
    page.on('response', response => {
      if (new URL(response.url()).pathname.endsWith('/sample.pdf') && [200, 206].includes(response.status())) {
        pdfResources.push(response.url());
      }
      if (new URL(response.url()).pathname === `/api/renderers/${id}/source`) {
        sourceResponses.push({ url: response.url(), status: response.status(), entrypoint: response.headers()['x-entrypoint'] });
      }
    });
    const forbiddenCompilerRequests: string[] = [];
    page.on('request', request => {
      if (/babel\/standalone|@babel\/standalone/.test(request.url())) forbiddenCompilerRequests.push(request.url());
    });
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.locator(`[data-cell-id="${cell_id}"]`)).toBeVisible();
    await expect.poll(() => ready(page, id), { message: `${id}: actual shipped DOM readiness` }).toBe(true);
    if (id === 'renderer_example') {
      await expect(page.locator(`[data-cell-id="${cell_id}"]`).locator('iframe[title="Sealed renderer preview"]')).toHaveAttribute('sandbox', 'allow-scripts');
    }
    if (id === 'terminal') {
      await expect.poll(() => terminalReplayFrames.some(frame => frame.includes('P3 terminal replay ready'))).toBe(true);
    }
    if (id === 'pdf') {
      // The EmbedPDF renderer loads api.getFileUrl(fileName) into its engine:
      // the page fetched that mounted resource URL (recorded above). Prove it
      // is the capability route serving PDF bytes, and that the corner
      // popover opens from the keyboard with the page controls.
      const cell = page.locator(`[data-cell-id="${cell_id}"]`);
      await cell.getByRole('button', { name: 'Style', exact: true }).focus();
      await page.keyboard.press('Enter');
      const popover = cell.getByRole('dialog', { name: 'Style' });
      await expect(popover.getByTitle('Next page')).toBeVisible();
      // The host also reads the file over its channel route, so pick the
      // capability fetch rather than the first sample.pdf response.
      const mounted = () => pdfResources.find(url => new URL(url).pathname.startsWith('/api/cellcap/'));
      await expect.poll(mounted, { message: 'the engine fetched the mounted capability PDF URL' }).toBeTruthy();
      const mountedUrl = mounted()!;
      const resource = await request.get(mountedUrl);
      expect(resource.ok()).toBeTruthy();
      expect(resource.headers()['content-type']).toContain('application/pdf');
      expect((await resource.body()).subarray(0, 5).toString()).toBe('%PDF-');
    }
    expect(sourceResponses.some(r => r.status === 200), 'host actually requests the real source route').toBeTruthy();
    expect(forbiddenCompilerRequests, 'prebuilt route must not invoke Babel').toEqual([]);
    expect(errors, 'uncaught module/mount failures').toEqual([]);
    const firstMountResponses = sourceResponses.length;
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect.poll(() => ready(page, id), { message: `${id}: warm real-app remount` }).toBe(true);
    expect(sourceResponses.length, 'the next app load still uses the actual cached-bundle route').toBeGreaterThan(firstMountResponses);
    expect(sourceResponses.every(r => r.status === 200 && r.entrypoint === 'module.js')).toBeTruthy();
    expect(forbiddenCompilerRequests).toEqual([]);
    expect(errors).toEqual([]);
    // Dependency upgrades must still perform real editable work after warm mount.
    if (id === 'monaco') {
      let editorFrame = page.mainFrame();
      for (const frame of page.frames()) {
        if (await frame.locator('.monaco-editor textarea.inputarea').count()) { editorFrame = frame; break; }
      }
      const input = editorFrame.locator('.monaco-editor textarea.inputarea').first();
      await input.focus();
      await page.keyboard.press('ControlOrMeta+A');
      await page.keyboard.insertText('P3 Monaco edit serialized __F1872');
      const fileUrl = `/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${encodeURIComponent(path)}`;
      await expect.poll(async () => (await request.get(fileUrl)).text()).toBe('P3 Monaco edit serialized __F1872');
    }
    if (id === 'excalidraw') {
      // The real host intentionally blocks iframe pointers until its cell is focused.
      await page.locator(`[data-cell-id="${cell_id}"]`).getByRole('tab').filter({hasText:path}).click();
      let drawingFrame = page.mainFrame();
      for (const frame of page.frames()) {
        if (frame !== page.mainFrame()) {
          const host = await frame.frameElement();
          const live = await host.evaluate(node => {
            if (!(node instanceof Element)) return false;
            const style = getComputedStyle(node);
            return style.pointerEvents !== 'none' && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
          });
          if (!live) continue;
        }
        if (await frame.locator('.excalidraw canvas').count()) { drawingFrame = frame; break; }
      }
      const canvas = drawingFrame.locator('.excalidraw canvas').first();
      const box = await canvas.boundingBox();
      expect(box, 'actual drawing surface').not.toBeNull();
      const rectangle = drawingFrame.getByRole('radio', { name: 'Rectangle', exact: true });
      await rectangle.locator('..').click();
      await expect(rectangle).toBeChecked();
      await page.mouse.move(box!.x + 180, box!.y + 160);
      await page.mouse.down();
      await page.mouse.move(box!.x + 300, box!.y + 260, { steps: 8 });
      await page.mouse.up();
      const fileUrl = `/api/channels/${encodeURIComponent(channel)}/cells/${cell_id}/files/${encodeURIComponent(path)}`;
      await expect.poll(async () => {
        const scene = await (await request.get(fileUrl)).json();
        return scene.elements?.some((element: {type:string;isDeleted?:boolean}) => element.type === 'rectangle' && !element.isDeleted);
      }, { message: 'actual Excalidraw drawing is serialized to the cell __F1871' }).toBe(true);
    }
    await testInfo.attach('rollout-result', {
      contentType: 'application/json',
      body: Buffer.from(JSON.stringify({ id, embed: renderer!.embed, contentHash: renderer!.content_hash, sourceResponses,
        scope: id === 'terminal' ? 'Synthetic local socket replay, not a live PTY/provider result'
          : ['assistant'].includes(id) ? 'Document/transcript readiness, not a live provider session'
            : id === 'pdf' ? 'Valid PDF delivery; loaded page image and the corner popover view controls'
              : id === 'webview' ? 'Real browser fallback, not desktop native guest' : 'Shipped renderer mounted through actual app/API',
      }, null, 2)),
    });
    await testInfo.attach('actual-renderer', { contentType: 'image/png', body: await page.screenshot() });
  });
}
