import { createServer, type Server } from 'node:http';
import { rmSync, writeFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import type { CollimateDesktop } from '../../src/lib/desktop';
import { launchShell, workbenchWindow, type LaunchedShell } from './desktop';

type BridgeWindow = Window & { collimateDesktop: CollimateDesktop; zoomEvents?: number[] };
test.describe.configure({ mode: 'serial' });
let shell: LaunchedShell;
let page: Page;
let fixture: Server;
let fixtureUrl: string;
let foreignRequests = 0;
const guestId = 'policy-guest';

test.beforeAll(async ({ browserName: _browserName }, info) => {
  fixture = createServer((request, response) => {
    if (request.url === '/foreign') foreignRequests++;
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end(`<!doctype html><style>body{margin:0;background:linear-gradient(135deg,#123,#c8d);color:white;font:24px sans-serif}canvas{width:100%;height:100%;position:absolute;inset:0}h1,p{position:relative}</style><canvas></canvas><h1>Actual guest capture</h1><p>Pixel and text detail at two device pixels per DIP.</p><script>const c=document.querySelector('canvas');c.width=1600;c.height=1200;const x=c.getContext('2d');for(let i=0;i<4000;i++){x.fillStyle='hsl('+i%360+' 55% 45%)';x.fillRect(i*137%1600,i*67%1200,11,9);}</script>`);
  });
  await new Promise<void>((resolve) => fixture.listen(0, '127.0.0.1', resolve));
  const address = fixture.address();
  if (!address || typeof address === 'string') throw new Error('fixture failed to bind');
  fixtureUrl = `http://127.0.0.1:${address.port}`;
  shell = await launchShell({ deviceScaleFactor: 2 });
  try { page = await workbenchWindow(shell.app); }
  catch (error) { await shell.trace.finish(info, shell.app.windows()[0], true); throw error; }
  await page.evaluate(async ({ guestId, url }) => {
    const desktop = (window as BridgeWindow).collimateDesktop;
    const ready = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { unsubscribe(); reject(new Error('guest ready timeout')); }, 15_000);
      const unsubscribe = desktop.onGuestEvent((event) => {
        if (event.guestId === guestId && event.kind === 'ready') { clearTimeout(timer); unsubscribe(); resolve(); }
      });
    });
    await desktop.guest.createGuest({ guestId, partition: 'persist:collimate-policy-test', url });
    await ready;
  }, { guestId, url: fixtureUrl });
});
test.beforeEach(async ({ browserName: _browserName }, info) => { await shell.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell.trace.finish(info, page); });
test.afterAll(async () => {
  if (shell) { await shell.app.close().catch(() => undefined); rmSync(shell.userDataRoot, { recursive: true, force: true }); }
  if (fixture) { fixture.closeAllConnections(); await new Promise<void>((resolve) => fixture.close(() => resolve())); }
});

test('live page zoom reaches preload and actual native guest geometry __F0985', async () => {
  await page.evaluate(() => {
    const target = window as BridgeWindow;
    target.zoomEvents = [];
    target.collimateDesktop.onZoomChanged((factor) => target.zoomEvents!.push(factor));
  });
  await shell.app.evaluate(({ BrowserWindow }) => {
    const host = BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()));
    if (!host) throw new Error('missing host');
    host.webContents.setZoomFactor(1.25);
  });
  await expect.poll(() => page.evaluate(() => (window as BridgeWindow).zoomEvents)).toContain(1.25);
  const factor = await page.evaluate(() => (window as BridgeWindow).collimateDesktop.getZoomFactor());
  expect(factor).toBe(1.25);
  await page.evaluate(({ guestId, factor }) => {
    const guest = (window as BridgeWindow).collimateDesktop.guest;
    guest.layoutGuests({ zoomFactor: factor, guests: [{ guestId, x: 40, y: 60, w: 320, h: 200, radius: 8 }] });
    return guest.setGuestVisible(guestId, true);
  }, { guestId, factor });
  await expect.poll(() => shell.app.evaluate(({ BrowserWindow }) => {
    const host = BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()));
    return host?.contentView.children[0]?.getBounds();
  })).toEqual({ x: 50, y: 75, width: 400, height: 250 });
  await shell.app.evaluate(({ BrowserWindow }) => {
    BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()))!.webContents.setZoomFactor(1);
  });
  await expect.poll(() => page.evaluate(() => (window as BridgeWindow).collimateDesktop.getZoomFactor())).toBe(1);
});

test('private JPEG85 placeholders preserve exact PNG screenshots and raw encoding timings __F1038', async ({ browserName: _browserName }, info) => {
  const dpr = await page.evaluate(() => devicePixelRatio);
  expect(dpr).toBe(2);
  const records = [];
  for (const [width, height] of [[640, 480], [1000, 650]]) {
    await page.evaluate(({ guestId, width, height }) => {
      const guest = (window as BridgeWindow).collimateDesktop.guest;
      guest.layoutGuests({ zoomFactor: 1, guests: [{ guestId, x: 0, y: 0, w: width!, h: height!, radius: 0 }] });
      return guest.setGuestVisible(guestId, true);
    }, { guestId, width, height });
    const record = await shell.app.evaluate(async ({ webContents }, fixtureUrl) => {
      const guest = webContents.getAllWebContents().find((contents: { getURL(): string }) => contents.getURL().startsWith(fixtureUrl));
      if (!guest) throw new Error('missing guest');
      const captureMs = [];
      const encodeMs = [];
      let png = Buffer.alloc(0);
      for (let index = 0; index < 10; index++) {
        const captureStart = performance.now();
        const image = await guest.capturePage();
        captureMs.push(performance.now() - captureStart);
        // Measure the first encoding of every fresh capture, never a cached
        // second toPNG() on the same NativeImage.
        const start = performance.now(); png = image.toPNG(); encodeMs.push(performance.now() - start);
      }
      const jpegEncodeMs = [];
      let jpegBytes = 0;
      for (let index = 0; index < 5; index++) {
        const image = await guest.capturePage();
        const start = performance.now(); const jpeg = image.toJPEG(85); jpegEncodeMs.push(performance.now() - start);
        jpegBytes = jpeg.length;
      }
      return { captureMs, encodeMs, jpegEncodeMs, jpegBytes, bytes: png.length, pixels: { width: png.readUInt32BE(16), height: png.readUInt32BE(20) }, signature: [...png.subarray(0, 8)] };
    }, fixtureUrl);
    expect(record.signature).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    expect(record.pixels).toEqual({ width: width! * 2, height: height! * 2 });
    // Hold one actual captured frame so byte equality proves PNG exactness.
    const expected = await shell.app.evaluate(async ({ webContents }, fixtureUrl) => {
      const guest = webContents.getAllWebContents().find((contents: { getURL(): string }) => contents.getURL().startsWith(fixtureUrl));
      if (!guest) throw new Error('missing guest');
      const original = guest.capturePage;
      const image = await original.call(guest);
      const tracked = globalThis as typeof globalThis & { restorePolicyCapture?: () => void };
      tracked.restorePolicyCapture = () => { guest.capturePage = original; };
      guest.capturePage = async () => image;
      return { png: [...image.toPNG()], jpeg: [...image.toJPEG(85)] };
    }, fixtureUrl);
    let paths;
    try {
      paths = await page.evaluate(async (guestId) => {
        const guest = (window as BridgeWindow).collimateDesktop.guest;
        return { placeholder: [...new Uint8Array(await guest.capturePlaceholder(guestId))], screenshot: [...new Uint8Array(await guest.capturePng(guestId))] };
      }, guestId);
    } finally {
      await shell.app.evaluate(() => {
        (globalThis as typeof globalThis & { restorePolicyCapture?: () => void }).restorePolicyCapture?.();
      });
    }
    expect(paths.placeholder.slice(0, 3)).toEqual([255, 216, 255]);
    expect(paths.placeholder).toEqual(expected.jpeg);
    expect(paths.screenshot.slice(0, 8)).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    expect(paths.screenshot).toEqual(expected.png);
    records.push({ width, height, dpr, ...record, placeholderFormat: 'jpeg85', screenshotFormat: 'exact-png' });
  }
  const recordPath = info.outputPath('actual-2x-PNG-encoding-F1038.json');
  writeFileSync(recordPath, JSON.stringify(records, null, 2));
  await info.attach('actual-2x-PNG-encoding-F1038', { path: recordPath, contentType: 'application/json' });
});

test('a real main-frame HTTP redirect cannot load the foreign origin __F0995', async () => {
  const origin = new URL(page.url()).origin;
  const target = `${fixtureUrl}/foreign`;
  await shell.app.evaluate(({ shell }) => {
    const tracked = globalThis as typeof globalThis & { policyOpened?: string[] };
    tracked.policyOpened = [];
    shell.openExternal = async (url: string) => { tracked.policyOpened!.push(url); };
  });
  // A real Chromium 302 is supplied at the already-bound app origin; the
  // destination is an actual separate loopback server with a request counter.
  await page.route(`${origin}/__policy_redirect`, (route) => route.fulfill({ status: 302, headers: { location: target } }));
  await page.goto(`${origin}/__policy_redirect`).catch(() => undefined);
  await expect.poll(() => shell.app.evaluate(() => (globalThis as typeof globalThis & { policyOpened?: string[] }).policyOpened), { timeout: 3_000 }).toContain(target);
  expect(foreignRequests).toBe(0);
  expect(new URL(page.url()).origin).toBe(origin);
});
