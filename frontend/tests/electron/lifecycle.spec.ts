import { existsSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type ElectronApplication } from '@playwright/test';
import {
  killRecordedServer, launchShell, pidAlive, serverIsUp, serverRecord, workbenchWindow, type LaunchedShell,
} from './desktop';

test.describe.configure({ mode: 'serial' });
let shell: LaunchedShell | undefined;
test.afterEach(async ({ browserName: _browserName }, info) => {
  if (!shell) return;
  if (info.status !== info.expectedStatus) await info.attach('actual shell log', {
    body: readFileSync(path.join(shell.runtimeDir, 'logs', 'server.log'), 'utf8'), contentType: 'text/plain' });
  await shell.trace.finish(info, shell.app.windows().find((page) => !page.isClosed()));
  const socketTemp = await shell.app.evaluate(() => process.env.TMPDIR).catch(() => undefined);
  try {
    await shell.app.close().catch(() => undefined);
    if (socketTemp) expect(existsSync(socketTemp), 'owned socket temporary directory removed after close').toBe(false);
  } finally {
    rmSync(shell.userDataRoot, { recursive: true, force: true });
    shell = undefined;
  }
});

test('boot connect remains usable while the private shell probe is running __F1042_F1918_P0F32', async ({ browserName: _browserName }, info) => {
  shell = await launchShell({ connect: { url: 'http://127.0.0.1:1', token: '' }, shellFixture: { delaySeconds: 3 } });
  await shell.trace.begin(info);
  const socketTemp = await shell.app.evaluate(() => process.env.TMPDIR!);
  expect(socketTemp).toMatch(/^\/tmp\/collimate-e2e-[A-Za-z0-9]+$/);
  expect(statSync(socketTemp).mode & 0o777).toBe(0o700);
  expect(socketTemp.startsWith(shell.userDataRoot)).toBe(false);
  await expect.poll(() => existsSync(path.join(shell!.userDataRoot, 'shell-probe-started'))).toBe(true);
  const splash = shell.app.windows().find((page) => page.url().startsWith('file:'))!;
  await expect(splash.locator('#view-connect')).toBeVisible({ timeout: 1_500 });
  expect(existsSync(path.join(shell.userDataRoot, 'shell-probe-completed'))).toBe(false);
  await expect(splash.locator('#connect-url')).toBeEnabled();
});

test('boot connect offers Quit and closes the actual shell __F1002', async ({ browserName: _browserName }, info) => {
  shell = await launchShell({ connect: { url: 'http://127.0.0.1:1', token: '' } });
  await shell.trace.begin(info);
  const splash = await shell.app.firstWindow();
  await expect(splash.locator('#view-connect')).toBeVisible();
  await expect(splash.locator('#connect-quit')).toBeVisible({ timeout: 1_500 });
  // The local server is offered as what it is.
  await expect(splash.locator('#connect-use-local')).toHaveText('Start local server');
  await shell.trace.beforeClose(info, splash);
  const closed = shell.app.waitForEvent('close');
  await splash.locator('#connect-quit').click();
  await closed;
});

test('a real renderer crash shows the existing retry and quit failure splash __F0993_F1008', async ({ browserName: _browserName }, info) => {
  shell = await launchShell();
  await shell.trace.begin(info);
  await workbenchWindow(shell.app);
  const crashDetails = await shell.app.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows().find((win: { webContents: { getURL(): string; forcefullyCrashRenderer(): void; once(event: string, handler: (...args: unknown[]) => void): void } }) => win.webContents.getURL().startsWith('http:'));
    if (!window) throw new Error('actual workbench window is absent');
    return new Promise((resolve) => {
      window.webContents.once('render-process-gone', (_event: unknown, details: unknown) => resolve(details));
      window.webContents.forcefullyCrashRenderer();
    });
  });
  expect(crashDetails).toMatchObject({ reason: 'crashed' });
  await expect.poll(() => shell!.app.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows().some((win: { webContents: { getURL(): string } }) => win.webContents.getURL().startsWith('http:'))),
  { timeout: 5_000 }).toBe(false);
  await expect.poll(() => shell!.app.windows().some((page) => page.url().startsWith('file:'))).toBe(true);
  const splash = shell.app.windows().find((page) => page.url().startsWith('file:'))!;
  await expect(splash.locator('#view-failure')).toBeVisible();
  await expect(splash.locator('#retry')).toBeVisible();
  await expect(splash.locator('#quit')).toBeVisible();
  expect(readFileSync(path.join(shell.runtimeDir, 'logs', 'server.log'), 'utf8')).toMatch(/renderer.*crash/i);
});

for (const event of ['uncaughtException', 'unhandledRejection'] as const) {
  test(`main ${event} logs diagnostics while the workbench remains usable __F1008`, async ({ browserName: _browserName }, info) => {
    shell = await launchShell();
    await shell.trace.begin(info);
    const workbench = await workbenchWindow(shell.app);
    await shell.app.evaluate((_electron, name) => {
      if (name === 'unhandledRejection') {
        void Promise.reject(new Error(`owned lifecycle ${name}`));
      } else {
        setImmediate(() => { throw new Error(`owned lifecycle ${name}`); });
      }
    }, event);
    await expect.poll(() => readFileSync(path.join(shell!.runtimeDir, 'logs', 'server.log'), 'utf8'))
      .toContain(`owned lifecycle ${event}`);
    expect(workbench.isClosed()).toBe(false);
    expect(await workbench.evaluate(() => document.readyState)).not.toBe('loading');
    expect(await workbench.evaluate(async () => (await fetch('/health')).ok)).toBe(true);
    const failures = shell.app.windows().filter((page) => page.url().startsWith('file:'));
    for (const page of failures) await expect(page.locator('#view-failure')).toBeHidden();
  });
}

/** Click File → Quit, Keep Server Running once the local server runs, and wait for the exit. */
async function quitKeepingServer(app: ElectronApplication): Promise<void> {
  await expect.poll(() => app.evaluate(({ Menu }) =>
    Menu.getApplicationMenu()?.getMenuItemById('quit-keep-server')?.enabled ?? null)).toBe(true);
  const closed = app.waitForEvent('close');
  await app.evaluate(({ Menu }) => {
    Menu.getApplicationMenu()?.getMenuItemById('quit-keep-server')?.click();
  });
  await closed;
}

test('Quit, Keep Server Running leaves the server up; the next launch adopts it, a version change restarts it, a plain quit stops it (MS-04)', async ({ browserName: _browserName }, info) => {
  test.setTimeout(480_000);
  let root: string | undefined;
  let runtimeDir: string | undefined;
  try {
    // Launch 1: start, then quit keeping the server.
    shell = await launchShell();
    root = shell.userDataRoot;
    runtimeDir = shell.runtimeDir;
    await shell.trace.begin(info);
    const first = await workbenchWindow(shell.app);
    const url = new URL(first.url()).origin;
    const record = serverRecord(runtimeDir)!;
    expect(record).toMatchObject({ url, version: expect.any(String), dataDir: runtimeDir });
    const pid = record['pid'] as number;
    await shell.trace.beforeClose(info, first);
    await quitKeepingServer(shell.app);
    shell = undefined;
    expect(pidAlive(pid), 'the kept server outlives the app').toBe(true);
    expect(await serverIsUp(url)).toBe(true);
    expect(serverRecord(runtimeDir)?.['pid']).toBe(pid);

    // Launch 2: adopts the same server — same pid, same origin, no new spawn.
    shell = await launchShell({ userDataRoot: root });
    await shell.trace.begin(info);
    const second = await workbenchWindow(shell.app, 60_000, url);
    expect(new URL(second.url()).origin).toBe(url);
    expect(serverRecord(runtimeDir)?.['pid']).toBe(pid);
    const adoptedLog = readFileSync(path.join(runtimeDir, 'logs', 'server.log'), 'utf8');
    expect(adoptedLog).toContain('adopted the server left running');
    expect(adoptedLog).not.toContain('exec: ');
    await shell.trace.beforeClose(info, second);
    await quitKeepingServer(shell.app);
    shell = undefined;
    expect(pidAlive(pid)).toBe(true);

    // A record from another shell version is not adopted: stopped, then replaced.
    writeFileSync(path.join(runtimeDir, 'server.json'), JSON.stringify({ ...serverRecord(runtimeDir), shellVersion: '0.0.0-older' }));
    shell = await launchShell({ userDataRoot: root });
    await shell.trace.begin(info);
    const third = await workbenchWindow(shell.app);
    await expect.poll(() => pidAlive(pid), { timeout: 30_000 }).toBe(false);
    const replaced = serverRecord(runtimeDir)!;
    expect(replaced['pid']).not.toBe(pid);
    expect(replaced['shellVersion']).not.toBe('0.0.0-older');
    expect(readFileSync(path.join(runtimeDir, 'logs', 'server.log'), 'utf8')).toContain("can't be adopted");
    // The stable local port: the new server took the same origin back.
    expect(new URL(third.url()).origin).toBe(url);

    // The default quit still stops the server and clears its record.
    const newPid = replaced['pid'] as number;
    await shell.trace.beforeClose(info, third);
    await shell.app.close();
    shell = undefined;
    await expect.poll(() => serverIsUp(url), { timeout: 60_000 }).toBe(false);
    expect(pidAlive(newPid)).toBe(false);
    expect(existsSync(path.join(runtimeDir, 'server.json'))).toBe(false);
  } finally {
    await shell?.app.close().catch(() => undefined);
    shell = undefined;
    if (runtimeDir) await killRecordedServer(runtimeDir);
    if (root) rmSync(root, { recursive: true, force: true });
  }
});
