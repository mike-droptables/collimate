import { createServer, type Server } from 'node:http';
import { readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { expect, request as playwrightRequest, test, type Locator, type Page } from '@playwright/test';
import { createSeededChannel } from '../seedChannel';
import { launchShell, missingPrerequisite, workbenchWindow, type LaunchedShell } from './desktop';

/**
 * The web runner's device emulation (plan row GN-04) and its queued start
 * (GN-03), inside the real shell.
 *
 * The browser suites only see the iframe fallback: no native guest, no user
 * agent, no emulation. Here the shipped web generator runs as a real daemon
 * behind the real server the shell started, the webview renderer mounts a
 * real WebContentsView guest, and the toolbar's Desktop / Phone / Tablet
 * toggle goes through the `device` action, the daemon's rewrite of the tab
 * file and the shell's `collimate:guest-set-device`. Every value asserted
 * is read out of the guest itself (its navigator.userAgent and innerWidth,
 * through `executeJavaScript` on the view's webContents) and out of the
 * view's own bounds, never off a screenshot.
 */

const blocked = missingPrerequisite();
test.skip(blocked !== null, `desktop e2e prerequisites are missing: ${blocked ?? ''}`);
test.describe.configure({ mode: 'serial' });

const RUNNER_ID = 'web-dev';

let shell: LaunchedShell | undefined;
let page: Page;
let serverUrl = '';
let fixture: Server | undefined;
let fixtureUrl = '';
/** Every user agent the fixture page was requested with, in order. */
const fixtureAgents: string[] = [];
let channel = '';
let cellId = '';
let stackId = '';
let runId = '';
let pointer = '';
let cardPath = '';

/** A decoded JSON answer; the assertions name the fields they read. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;

/** One call to the server the shell started (an open loopback server). */
async function api(method: string, route: string, body?: unknown): Promise<{ status: number; json: Json; text: string }> {
  const response = await fetch(`${serverUrl}${route}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let json: Json = null;
  try { json = JSON.parse(text); } catch { /* not JSON */ }
  return { status: response.status, json, text };
}

async function ok(method: string, route: string, body?: unknown): Promise<Json> {
  const answer = await api(method, route, body);
  expect(answer.status, `${method} ${route}: ${answer.text}`).toBeLessThan(300);
  return answer.json;
}

const cellRoute = () => `/api/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}`;

/** The cell's file paths, body-free. */
async function cellPaths(): Promise<string[]> {
  return (await ok('GET', `${cellRoute()}/file-paths`)).paths as string[];
}

/** The tab file as the daemon last wrote it, or null while it is absent or not JSON. */
async function tabArtifact(): Promise<Record<string, Json> | null> {
  const answer = await api('GET', `${cellRoute()}/files/${pointer.split('/').map(encodeURIComponent).join('/')}`);
  if (answer.status !== 200) return null;
  return answer.json && typeof answer.json === 'object' ? answer.json : null;
}

interface GuestReading {
  url: string;
  userAgent: string;
  innerWidth: number;
  printed: string;
  bounds: { x: number; y: number; width: number; height: number };
}

/**
 * The guest showing the fixture page, read in the main process: the view's
 * bounds, and the page's own user agent and width through its webContents.
 */
async function readGuest(): Promise<GuestReading | null> {
  return shell!.app.evaluate(async ({ BrowserWindow }, prefix) => {
    for (const window of BrowserWindow.getAllWindows()) {
      for (const view of window.contentView.children) {
        const contents = (view as unknown as { webContents?: {
          isDestroyed(): boolean; getURL(): string; isLoading(): boolean; executeJavaScript(code: string): Promise<unknown>;
        } }).webContents;
        if (!contents || contents.isDestroyed() || !contents.getURL().startsWith(prefix)) continue;
        if (contents.isLoading()) return null;
        const values = await contents.executeJavaScript(
          '({ url: location.href, userAgent: navigator.userAgent, innerWidth: window.innerWidth,'
          + ' printed: document.body ? document.body.innerText : "" })',
        ) as Omit<GuestReading, 'bounds'>;
        return { ...values, bounds: view.getBounds() };
      }
    }
    return null;
  }, fixtureUrl);
}

/** The shell's own user agent: what a desktop guest presents. */
async function shellUserAgent(): Promise<string> {
  return shell!.app.evaluate(({ app }) => app.userAgentFallback);
}

const cell = (): Locator => page.locator(`[data-cell-id="${cellId}"]`);
const tabOf = (file: string): Locator => cell().locator(`[role="tab"][title^="${file}"]`);
const deviceButton = (label: string): Locator =>
  cell().locator('[role="group"][aria-label="Device"] button', { hasText: label });

/**
 * Bring the cell on screen and select it. The focus overlay of an unfocused
 * cell eats the first click; both clicks land a third of the way down, clear
 * of the empty tab's centred "Open file" button.
 */
async function selectCell(): Promise<void> {
  const target = cell().first();
  await expect(target).toBeVisible({ timeout: 30_000 });
  const viewport = await page.evaluate(() => window.innerWidth);
  for (let step = 0; step < 14; step++) {
    const box = await target.boundingBox();
    if (box && box.x >= 0 && box.x + box.width <= viewport) break;
    await page.keyboard.press('Control+ArrowRight');
    await page.waitForTimeout(600);
  }
  const box = (await target.boundingBox())!;
  const spot = { x: box.width / 2, y: box.height * 0.3 };
  await target.click({ position: spot });
  await page.waitForTimeout(800);
  await target.click({ position: spot });
  await page.waitForTimeout(800);
}

/** Open `file` from the Files pane (each folder on the way down, then its row). */
async function openFromFilesPane(file: string): Promise<void> {
  const show = cell().getByRole('button', { name: 'Show files', exact: true });
  if (await show.count()) await show.first().click();
  const row = (rowPath: string, kind: string) =>
    cell().locator(`[data-filenode-kind="${kind}"][data-filenode-path="${rowPath}"]`);
  const parts = file.split('/');
  for (let depth = 1; depth < parts.length; depth++) {
    const child = parts.slice(0, depth + 1).join('/');
    const kind = depth === parts.length - 1 ? 'file' : 'folder';
    if (!(await row(child, kind).count())) {
      await row(parts.slice(0, depth).join('/'), 'folder').first().click({ timeout: 10_000 });
      await page.waitForTimeout(600);
    }
  }
  await row(file, 'file').first().click({ timeout: 15_000 });
}

test.beforeAll(async ({ browserName: _browserName }, info) => {
  test.setTimeout(360_000);
  fixture = createServer((request, response) => {
    fixtureAgents.push(String(request.headers['user-agent'] ?? ''));
    response.writeHead(200, { 'Content-Type': 'text/html', 'Cache-Control': 'no-store' });
    // A mobile page declares its viewport: without the meta, mobile emulation
    // lays the page out at Chrome's 980 px default (or wider content), as a
    // real phone does, and innerWidth is not the device's width.
    response.end('<!doctype html><meta charset="utf-8"><title>Device probe</title>'
      + '<meta name="viewport" content="width=device-width, initial-scale=1">'
      + '<style>body{margin:0;font:16px sans-serif}#probe{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
      + '<body><div id="probe"></div><script>'
      + 'const show=()=>{document.getElementById("probe").textContent='
      + '"UA="+navigator.userAgent+"\\nWIDTH="+window.innerWidth;};show();addEventListener("resize",show);'
      + '</script>');
  });
  await new Promise<void>((resolve) => fixture!.listen(0, '127.0.0.1', resolve));
  const address = fixture.address();
  if (!address || typeof address === 'string') throw new Error('fixture failed to bind');
  fixtureUrl = `http://127.0.0.1:${address.port}/device-probe`;

  shell = await launchShell();
  try {
    page = await workbenchWindow(shell.app);
    serverUrl = new URL(page.url()).origin;
    // Room for the tablet's 800 px letterbox beside the files pane.
    await shell.app.evaluate(({ BrowserWindow }) => {
      const host = BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()));
      if (!host) throw new Error('missing workbench window');
      host.setBounds({ x: 40, y: 40, width: 1800, height: 1100 });
    });

    // A channel (created active) with one cell, seeded through the API.
    const seeding = await playwrightRequest.newContext({ baseURL: serverUrl });
    try {
      channel = await createSeededChannel(seeding, `Device e2e ${Date.now()}`);
    } finally {
      await seeding.dispose();
    }
    const upload = await fetch(`${serverUrl}/api/blob/stream`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
      body: Buffer.from('# Device notes\n', 'utf8'),
    });
    expect(upload.ok, await upload.clone().text()).toBe(true);
    const { blob_hash } = await upload.json() as { blob_hash: string };
    const imported = await ok('POST', `/api/channels/${encodeURIComponent(channel)}/import`, {
      insert_index: null,
      stacks: [{ name: 'Device', cells: [{ name: 'Device cell', files: [{ path: 'notes.md', blob_hash }], dirs: [] }] }],
    });
    cellId = imported.cell_ids[0] as string;
    stackId = imported.stack_ids[0] as string;

    // A web runner saved from the shipped generator's Default preset.
    const draft = await ok('GET', '/api/runner_generators/web/configs/default');
    await ok('POST', '/api/runners', {
      runner: { ...draft.runner, generator: 'web', id: RUNNER_ID, name: 'Web dev' },
      files: draft.files,
    });

    await page.reload({ waitUntil: 'domcontentloaded' });
    await selectCell();
    // Full width for the stack (the head's double-click), so the page box
    // has room for the tablet's 800 px letterbox.
    // Between the head's back arrow and its name: the arrows keep their own
    // double-clicks.
    const head = page.locator(`[data-stack-id="${stackId}"] [data-stack-head]`).first();
    const headBox = (await head.boundingBox())!;
    await head.dblclick({ position: { x: Math.round(headBox.width * 0.25), y: Math.round(headBox.height / 2) } });
    await expect.poll(async () => (await cell().first().boundingBox())?.width ?? 0).toBeGreaterThan(1200);
    await selectCell();
  } catch (error) {
    await shell.trace.finish(info, page ?? shell.app.windows()[0], true);
    throw error;
  }
});

test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => {
  if (!shell) return;
  if (info.status !== info.expectedStatus) {
    try {
      await info.attach('server log', {
        body: readFileSync(path.join(shell.runtimeDir, 'logs', 'server.log'), 'utf8'), contentType: 'text/plain',
      });
    } catch { /* no log yet */ }
    if (channel && runId) {
      const run = await api('GET', `/api/channels/${encodeURIComponent(channel)}/runs/${runId}`).catch(() => null);
      if (run) await info.attach('run record', { body: run.text, contentType: 'application/json' });
    }
  }
  await shell.trace.finish(info, page);
});

test.afterAll(async () => {
  if (shell) {
    if (channel && runId) await api('DELETE', `/api/channels/${encodeURIComponent(channel)}/runs/${runId}`).catch(() => undefined);
    await shell.app.close().catch(() => undefined);
    rmSync(shell.userDataRoot, { recursive: true, force: true });
    shell = undefined;
  }
  if (fixture) {
    fixture.closeAllConnections();
    await new Promise<void>((resolve) => fixture!.close(() => resolve()));
    fixture = undefined;
  }
});

test('GN-03 a web run is accepted queued, naming its tab, with the tab pointer and run card already in the cell', async () => {
  const answer = await ok('POST', `/api/channels/${encodeURIComponent(channel)}/runs`, { runner: RUNNER_ID, cell_id: cellId });
  runId = answer.run_id as string;
  const label = answer.label as string;
  expect(answer.status).toBe('queued');
  expect(typeof runId).toBe('string');
  expect(label).toBeTruthy();
  pointer = `_runs/${label}/tab-1.webview`;
  cardPath = `_runs/${label}/${label}.run`;
  expect(answer.opens).toEqual([pointer]);

  // The optimistic start: the accept committed both before answering.
  const paths = await cellPaths();
  expect(paths).toContain(pointer);
  expect(paths).toContain(cardPath);
  // And the surface is claimed for this run from the accept on.
  const surface = await api('GET', `${cellRoute()}/surfaces/${pointer}`);
  expect(surface.status, surface.text).toBe(200);
  expect(surface.json.run_id).toBe(runId);
  expect(['starting', 'live']).toContain(surface.json.state);
});

test('GN-04 the tab shows the Desktop / Phone / Tablet toggle once the daemon has written its v4 artifact', async () => {
  test.setTimeout(300_000);
  // The daemon resolves its script environment with uv, then writes the tab.
  await expect.poll(async () => (await tabArtifact())?.v, {
    timeout: 240_000, intervals: [1_000, 2_000, 3_000],
    message: 'the web daemon to write tab-1.webview as artifact v4',
  }).toBe(4);
  expect(await tabArtifact()).toMatchObject({ v: 4, tab: 'tab-1', device: 'desktop', viewport: null,
    native: { partition: 'persist:collimate-default', device: 'desktop' } });

  // The client revealed the run's tab on the selected cell; else open it.
  const tab = tabOf(pointer);
  let revealed = true;
  try {
    await expect(tab.first()).toBeVisible({ timeout: 15_000 });
  } catch {
    revealed = false;
    await openFromFilesPane(pointer);
  }
  test.info().annotations.push({ type: 'tab', description: revealed ? 'revealed by the client' : 'opened from the Files pane' });
  console.log(`[device] tab-1.webview ${revealed ? 'revealed by the client' : 'opened from the Files pane'}`);
  await expect(tab.first()).toHaveAttribute('aria-selected', 'true', { timeout: 20_000 });

  const group = cell().locator('[role="group"][aria-label="Device"]');
  await expect(group).toBeVisible({ timeout: 30_000 });
  await expect(group.locator('button')).toHaveText(['Desktop', 'Phone', 'Tablet']);
  await expect(deviceButton('Desktop')).toHaveAttribute('aria-pressed', 'true');
  await expect(deviceButton('Phone')).toBeEnabled();
});

let desktopWidth = 0;

test('GN-04 the tab navigates its native guest to a local page, presenting as the shell\'s desktop browser', async () => {
  // A tab with no page shows the Enter page form in its canvas.
  const field = cell().locator('input[aria-label="Page"]');
  await expect(field).toBeVisible({ timeout: 30_000 });
  await field.fill(fixtureUrl);
  await field.press('Enter');
  await expect.poll(async () => (await readGuest())?.url ?? null, { timeout: 30_000 }).toBe(fixtureUrl);
  const desktop = (await readGuest())!;
  expect(desktop.userAgent).toBe(await shellUserAgent());
  expect(desktop.userAgent).not.toContain('Mobile');
  expect(desktop.innerWidth).toBe(desktop.bounds.width);
  // The full page box, wider than a tablet's letterbox.
  expect(desktop.innerWidth).toBeGreaterThan(800);
  desktopWidth = desktop.innerWidth;
  // The page itself saw it.
  expect(desktop.printed).toContain(`WIDTH=${desktop.innerWidth}`);
});

test('GN-04 Phone reloads the guest as Android Chrome Mobile letterboxed at 412 px and rewrites the tab as a phone', async () => {
  const before = fixtureAgents.length;
  await deviceButton('Phone').click();
  await expect.poll(async () => (await tabArtifact())?.device, { timeout: 60_000 }).toBe('phone');
  expect(await tabArtifact()).toMatchObject({ device: 'phone', viewport: { width: 412 }, native: { device: 'phone' } });
  await expect(deviceButton('Phone')).toHaveAttribute('aria-pressed', 'true');

  await expect.poll(async () => {
    const guest = await readGuest();
    return guest && { mobile: guest.userAgent.includes('Mobile'), android: guest.userAgent.includes('Android'),
      width: guest.innerWidth, bounds: guest.bounds.width };
  }, { timeout: 30_000 }).toEqual({ mobile: true, android: true, width: 412, bounds: 412 });
  const phone = (await readGuest())!;
  expect(phone.url).toBe(fixtureUrl);
  expect(phone.printed).toContain('Mobile');
  expect(phone.printed).toContain('WIDTH=412');
  // The switch reloaded the page: the server saw it asked for again as a phone.
  const reloads = fixtureAgents.slice(before);
  expect(reloads.some((agent) => agent.includes('Mobile') && agent.includes('Android'))).toBe(true);
});

test('GN-04 Tablet presents an 800 px Android tablet without Mobile', async () => {
  await deviceButton('Tablet').click();
  await expect.poll(async () => (await tabArtifact())?.device, { timeout: 60_000 }).toBe('tablet');
  expect(await tabArtifact()).toMatchObject({ device: 'tablet', viewport: { width: 800 }, native: { device: 'tablet' } });
  await expect.poll(async () => {
    const guest = await readGuest();
    return guest && { mobile: guest.userAgent.includes('Mobile'), android: guest.userAgent.includes('Android'),
      width: guest.innerWidth, bounds: guest.bounds.width };
  }, { timeout: 30_000 }).toEqual({ mobile: false, android: true, width: 800, bounds: 800 });
  expect((await readGuest())!.printed).toContain('WIDTH=800');
});

test('GN-04 Desktop returns the guest to the shell\'s own user agent at the full width', async () => {
  await deviceButton('Desktop').click();
  await expect.poll(async () => (await tabArtifact())?.device, { timeout: 60_000 }).toBe('desktop');
  expect(await tabArtifact()).toMatchObject({ device: 'desktop', viewport: null, native: { device: 'desktop' } });
  const agent = await shellUserAgent();
  await expect.poll(async () => {
    const guest = await readGuest();
    return guest && { userAgent: guest.userAgent, width: guest.innerWidth, bounds: guest.bounds.width };
  }, { timeout: 30_000 }).toEqual({ userAgent: agent, width: desktopWidth, bounds: desktopWidth });
  expect((await readGuest())!.printed).toContain(`WIDTH=${desktopWidth}`);
});

test('GN-03 stopping the run releases its surface', async () => {
  const stopped = await api('DELETE', `/api/channels/${encodeURIComponent(channel)}/runs/${runId}`);
  expect(stopped.status, stopped.text).toBeLessThan(300);
  await expect.poll(async () => (await api('GET', `${cellRoute()}/surfaces/${pointer}`)).status, {
    timeout: 60_000, message: 'the stopped run to release tab-1.webview',
  }).toBe(404);
  // The daemon deleted its tab on the way out.
  await expect.poll(async () => (await cellPaths()).includes(pointer), { timeout: 60_000 }).toBe(false);
  runId = '';
});
