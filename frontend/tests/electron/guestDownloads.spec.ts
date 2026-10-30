import { createServer, type Server } from 'node:http';
import { existsSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import type { CollimateDesktop, GuestEvent } from '../../src/lib/desktop';
import { launchShell, workbenchWindow, type LaunchedShell } from './desktop';

type TargetWindow = Window & { collimateDesktop: CollimateDesktop; downloads?: GuestEvent[] };
test.describe.configure({ mode: 'serial' });
let shell: LaunchedShell;
let page: Page;
let fixture: Server;
let url: string;
let slowStarted = false;
let downloadDirectory: string;
const guestId = 'download-proof';

test.beforeAll(async ({ browserName: _browserName }, info) => {
  fixture = createServer((request, response) => {
    if (request.url === '/slow') { slowStarted = true; return; }
    if (request.url === '/file') {
      response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="actual.txt"' });
      response.end('actual silent download bytes');
      return;
    }
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end('<!doctype html><a href="/file" download>Download</a><p>Actual guest</p>');
  });
  await new Promise<void>((resolve) => fixture.listen(0, '127.0.0.1', resolve));
  const address = fixture.address();
  if (!address || typeof address === 'string') throw new Error('fixture bind failed');
  url = `http://127.0.0.1:${address.port}`;
  shell = await launchShell();
  try { page = await workbenchWindow(shell.app); }
  catch (error) { await shell.trace.finish(info, shell.app.windows()[0], true); throw error; }
  downloadDirectory = path.join(shell.userDataRoot, 'owned-downloads');
  await shell.app.evaluate(({ app, dialog }, directory) => {
    app.setPath('downloads', directory);
    (globalThis as typeof globalThis & { saveDialogCalls: number }).saveDialogCalls = 0;
    dialog.showSaveDialog = async () => {
      (globalThis as typeof globalThis & { saveDialogCalls: number }).saveDialogCalls++;
      return { canceled: true, filePath: undefined };
    };
  }, downloadDirectory);
  await page.evaluate(async ({ guestId, url }) => {
    const target = window as TargetWindow;
    target.downloads = [];
    target.collimateDesktop.onGuestEvent((event) => { if (event.kind === 'download') target.downloads!.push(event); });
    await target.collimateDesktop.guest.createGuest({ guestId, partition: 'persist:collimate-download-proof', url: 'about:blank' });
    await target.collimateDesktop.guest.navigate(guestId, url);
  }, { guestId, url });
});
test.beforeEach(async ({ browserName: _browserName }, info) => { await shell.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell.trace.finish(info, page); });
test.afterAll(async () => {
  if (shell) { await shell.app.close().catch(() => undefined); rmSync(shell.userDataRoot, { recursive: true, force: true }); }
  if (fixture) { fixture.closeAllConnections(); await new Promise<void>((resolve) => fixture.close(() => resolve())); }
});

test('silently saves actual guest download bytes and reports completion without SaveDialog __F0994', async () => {
  await page.evaluate((guestId) => (window as TargetWindow).collimateDesktop.guest.evaluate(guestId, "document.querySelector('a').click()"), guestId);
  await expect.poll(() => page.evaluate(() => (window as TargetWindow).downloads!.filter((event) => event.kind === 'download').map((event) => event.data.status))).toEqual(['started', 'completed']);
  const events = await page.evaluate(() => (window as TargetWindow).downloads!);
  const completed = events.find((event) => event.kind === 'download' && event.data.status === 'completed');
  if (!completed || completed.kind !== 'download') throw new Error('missing completed download');
  const saved = path.join(downloadDirectory, 'Collimate', completed.data.fileName);
  expect(existsSync(saved)).toBe(true);
  expect(readFileSync(saved, 'utf8')).toBe('actual silent download bytes');
  expect(completed.guestId).toBe(guestId);
  expect(path.basename(completed.data.fileName)).toBe(completed.data.fileName);
  expect(await shell.app.evaluate(() => (globalThis as typeof globalThis & { saveDialogCalls: number }).saveDialogCalls)).toBe(0);
});

test('awaited superseded navigation accepts real Electron abort and preserves later navigation __F0996', async () => {
  const pending = page.evaluate(async ({ guestId, url }) => {
    await (window as TargetWindow).collimateDesktop.guest.navigate(guestId, `${url}/slow`);
    return 'accepted';
  }, { guestId, url });
  await expect.poll(() => slowStarted).toBe(true);
  await page.evaluate(({ guestId, url }) => (window as TargetWindow).collimateDesktop.guest.navigate(guestId, url), { guestId, url });
  expect(await pending).toBe('accepted');
  expect(await page.evaluate((guestId) => (window as TargetWindow).collimateDesktop.guest.currentUrl(guestId), guestId)).toBe(`${url}/`);
});
