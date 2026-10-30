import { existsSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { launchShell, missingPrerequisite, workbenchWindow, type LaunchedShell } from './desktop';

/**
 * Plan 1011 transfer pickers (TR-02 / TR-11), the interface size (RS-05)
 * and the desktop-only Servers section, inside the real shell.
 *
 * Headless Chromium covers the panels themselves; what only this suite can
 * show is that Electron's window really exposes the File System Access
 * pickers the folder export relies on, that the Themes section's Interface size
 * row drives the shell's actual page zoom and settings.json, and that the
 * shell re-applies that zoom on every load.
 *
 * No native picker is ever opened: a native dialog cannot be driven.
 */

const blocked = missingPrerequisite();
test.skip(blocked !== null, `desktop e2e prerequisites are missing: ${blocked ?? ''}`);
test.describe.configure({ mode: 'serial' });

let shell: LaunchedShell | null = null;
let page: Page;

test.beforeAll(async ({ browserName: _browserName }, info) => {
  shell = await launchShell();
  try { page = await workbenchWindow(shell.app); }
  catch (error) { await shell.trace.finish(info, shell.app.windows()[0], true); throw error; }
});
test.beforeEach(async ({ browserName: _browserName }, info) => { await shell?.trace.begin(info); });
test.afterEach(async ({ browserName: _browserName }, info) => { await shell?.trace.finish(info, page); });
test.afterAll(async () => {
  if (!shell) return;
  await shell.app.close().catch(() => undefined);
  rmSync(shell.userDataRoot, { recursive: true, force: true });
  shell = null;
});

/** The window's page zoom, read in the main process. */
const zoomFactor = (): Promise<number> => shell!.app.evaluate(({ BrowserWindow }) => {
  const host = BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()));
  if (!host) throw new Error('missing workbench window');
  return host.webContents.getZoomFactor();
});

/** Force the page zoom from main without touching settings.json. */
const forceZoom = (factor: number): Promise<void> => shell!.app.evaluate(({ BrowserWindow }, factor) => {
  const host = BrowserWindow.getAllWindows().find((window: { webContents: { getURL(): string } }) => /^http:/.test(window.webContents.getURL()));
  if (!host) throw new Error('missing workbench window');
  host.webContents.setZoomFactor(factor);
}, factor);

const settingsFile = (): string => path.join(shell!.runtimeDir, 'settings.json');
const savedScale = (): number | null => {
  if (!existsSync(settingsFile())) return null;
  try {
    const value = (JSON.parse(readFileSync(settingsFile(), 'utf8')) as { interfaceScale?: unknown }).interfaceScale;
    return typeof value === 'number' ? value : null;
  } catch {
    return null;
  }
};

/** The Commands pane on its settings view. */
const settingsPane = () => page.locator('[data-command-pane][data-pane-view="settings"]');

/** Mod+, opens the Commands pane on the settings root. */
async function openSettings(): Promise<void> {
  await page.locator('body').click({ position: { x: 2, y: 2 } }).catch(() => undefined);
  await page.keyboard.press('Control+Comma');
  await expect(settingsPane()).toBeVisible();
}

const settingsSection = (label: string) => settingsPane().getByRole('button', { name: label, exact: true });

test('TR-02/TR-11 the shell window exposes the File System Access pickers', async () => {
  const kinds = await page.evaluate(() => {
    const w = window as unknown as Record<string, unknown>;
    return {
      showDirectoryPicker: typeof w.showDirectoryPicker,
      showSaveFilePicker: typeof w.showSaveFilePicker,
      showOpenFilePicker: typeof w.showOpenFilePicker,
      isSecureContext: window.isSecureContext,
    };
  });
  expect(kinds).toEqual({
    showDirectoryPicker: 'function',
    showSaveFilePicker: 'function',
    showOpenFilePicker: 'function',
    isSecureContext: true,
  });
});

test('TR-02/TR-11 folder export is offered in the shell, with no other-browser note', async () => {
  // Seed through the window's own origin: the shell binds the server's
  // credential to the window session, so same-origin fetches are authorised.
  const seeded = await page.evaluate(async () => {
    const ok = async (response: Response) => {
      if (!response.ok) throw new Error(`${response.url} → ${response.status} ${await response.text()}`);
      return response.json();
    };
    const channel = await ok(await fetch('/api/channels?name=transfer-scale', { method: 'POST' }));
    const blob = await ok(await fetch('/api/blob/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
      body: new TextEncoder().encode('# Transfer\n\nSeeded for the transfer panel.\n'),
    }));
    const imported = await ok(await fetch(`/api/channels/${encodeURIComponent(channel.id)}/import`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ insert_index: null, stacks: [{ name: 'Transfer', cells: [{ name: 'Transfer cell', files: [{ path: 'notes.md', blob_hash: blob.blob_hash }], dirs: [] }] }] }),
    }));
    await ok(await fetch(`/api/channels/${encodeURIComponent(channel.id)}/select`, { method: 'POST' }));
    return { channel: channel.id as string, cell: imported.cell_ids[0] as string };
  });
  expect(seeded.cell).toBeTruthy();
  await page.reload({ waitUntil: 'domcontentloaded' });

  const transferLink = page.getByRole('button', { name: 'Import / export' }).first();
  // The pane may start hidden; its toggle then leads the tab strip.
  await expect(page.getByRole('button', { name: 'Show files' }).or(transferLink).first()).toBeVisible({ timeout: 60_000 });
  if (!(await transferLink.isVisible())) await page.getByRole('button', { name: 'Show files' }).first().click();
  await expect(transferLink).toBeVisible();
  await expect(transferLink).toBeEnabled();
  await transferLink.click();

  const panel = page.locator('[role="dialog"][data-cell-panel="transfer"]');
  await expect(panel).toBeVisible();
  await panel.getByRole('group', { name: 'Mode' }).getByRole('button', { name: 'Export' }).click();

  await expect(panel.getByTestId('cell-panel-primary')).toBeVisible();
  // TR-02/TR-11: no "folder export needs another browser" note in the shell.
  await expect(panel.getByText(/folder export needs/i)).toHaveCount(0);
  await expect(page.getByText(/folder export needs/i)).toHaveCount(0);
  // The shell starts on the folder format (the pickers exist), so the
  // primary exports to a folder and the secondary switches to zip.
  await expect(panel.getByTestId('cell-panel-primary')).toContainText('Export to folder');
  const secondary = panel.getByTestId('cell-panel-secondary');
  await expect(secondary).toHaveAttribute('aria-label', 'Switch to zip');
  await expect(secondary).toBeEnabled();
  // Over to zip and back: "Switch to folder" is offered and enabled.
  await secondary.click();
  await expect(secondary).toHaveAttribute('aria-label', 'Switch to folder');
  await expect(secondary).toBeEnabled();
  await expect(panel.getByTestId('cell-panel-primary')).toContainText('Export as zip');
  await secondary.click();
  await expect(panel.getByTestId('cell-panel-primary')).toContainText('Export to folder');
  await page.keyboard.press('Escape');
});

test('RS-05 Interface size drives the shell zoom, persists to settings.json and is re-applied on load', async () => {
  expect(await zoomFactor()).toBe(1);
  await openSettings();
  await settingsSection('Themes').click();
  const row = page.locator('[data-interface-size]');
  await expect(row.getByText('Interface size')).toBeVisible();
  const options = row.getByRole('group').getByRole('button');
  await expect(options).toHaveText(['70%', '80%', '90%', '100%', '110%', '125%', '150%']);
  await expect(row.getByRole('button', { name: '100%' })).toHaveAttribute('aria-pressed', 'true');

  await row.getByRole('button', { name: '125%' }).click();
  await expect.poll(zoomFactor, { timeout: 5_000 }).toBe(1.25);
  await expect.poll(savedScale, { timeout: 5_000 }).toBe(1.25);
  expect(readFileSync(settingsFile(), 'utf8')).toMatch(/"interfaceScale":\s*1\.25/);
  await expect(row.getByRole('button', { name: '125%' })).toHaveAttribute('aria-pressed', 'true');

  // A reload keeps it.
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(zoomFactor, { timeout: 5_000 }).toBe(1.25);

  // And it is the shell re-applying the saved scale on load, not only
  // Chromium's per-origin zoom memory: knock the zoom down from main
  // without touching settings.json, reload, and it comes back.
  await forceZoom(1);
  await expect.poll(zoomFactor).toBe(1);
  expect(savedScale()).toBe(1.25);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(zoomFactor, { timeout: 5_000 }).toBe(1.25);
  expect(await page.evaluate(() => (window as unknown as { collimateDesktop: { getZoomFactor(): number } }).collimateDesktop.getZoomFactor())).toBe(1.25);

  // Back to 100%.
  await openSettings();
  await settingsSection('Themes').click();
  await expect(row.getByRole('button', { name: '125%' })).toHaveAttribute('aria-pressed', 'true');
  await row.getByRole('button', { name: '100%' }).click();
  await expect.poll(zoomFactor, { timeout: 5_000 }).toBe(1);
  await expect.poll(savedScale, { timeout: 5_000 }).toBe(1);
  await expect(row.getByRole('button', { name: '100%' })).toHaveAttribute('aria-pressed', 'true');
});

test('multi-server: the Servers section exists in the shell and lists the local server running', async () => {
  await openSettings();
  const row = settingsSection('Servers');
  await expect(row).toBeVisible();
  await row.click();
  const local = page.locator('li[data-server-id="local"]');
  await expect(local).toBeVisible();
  await expect(local).toContainText('Local');
  await expect(local).toContainText('This window');
  await expect(local).toContainText('Running', { timeout: 20_000 });
  const origin = new URL(page.url()).origin;
  await expect(local).toContainText(origin);
  // The keep-alive option lives in the running local server's own card, as a
  // switch beside Stop, and nowhere else in the section.
  const keep = local.getByRole('switch', { name: 'Keep local server running when the app quits' });
  await expect(keep).toBeVisible();
  await expect(keep).toHaveAttribute('aria-checked', 'false');
  await expect(local.getByRole('button', { name: 'Stop' })).toBeVisible();
  await expect(settingsPane().getByRole('switch')).toHaveCount(1);
  await keep.click();
  await expect(keep).toHaveAttribute('aria-checked', 'true');
  await expect.poll(() => {
    try { return JSON.parse(readFileSync(settingsFile(), 'utf8')).keepLocalRunning ?? null; } catch { return null; }
  }, { timeout: 5_000 }).toBe(true);
  await keep.click();
  await expect(keep).toHaveAttribute('aria-checked', 'false');
  // Esc steps back: the section, the settings root, the commands, closed.
  for (let i = 0; i < 3; i += 1) await page.keyboard.press('Escape');
  await expect(page.locator('[data-command-pane]')).toHaveCount(0);
});
