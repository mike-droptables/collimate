import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { launchShell, missingPrerequisite, packagedExecutable, workbenchWindow, type LaunchedShell, removeTestRoot } from './desktop';

/**
 * Plan 1011 transfer pickers (TR-02 / TR-11), the interface size (RS-05),
 * the desktop-only Servers tab and This profile (plan 1104 C2; the channels
 * page's Settings tab), inside the real shell.
 *
 * Headless Chromium covers the panels themselves; what only this suite can
 * show is that Electron's window really exposes the File System Access
 * pickers the folder export relies on, that This profile's Interface size
 * row drives the shell's actual page zoom and settings.json, and that the
 * shell re-applies that zoom on every load; likewise that its Spellcheck
 * switch reaches settings.json through main and outlives a reload.
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
  removeTestRoot(shell.userDataRoot);
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

/** The channels page, and This profile inline on its Settings tab. */
const channelsPage = () => page.getByRole('dialog', { name: 'Channels' });
const profileSection = () => channelsPage().locator('[data-profile-section]');

/** The `#channels/settings` route opens the channels page on Settings. */
async function openProfile(): Promise<void> {
  await page.evaluate(() => { window.location.hash = '#channels/settings'; });
  await expect(channelsPage().getByRole('tab', { name: 'Settings' })).toHaveAttribute('aria-selected', 'true');
  await expect(profileSection()).toBeVisible();
}

/** Back to the channel: the page closes. */
async function closeChannelsPage(): Promise<void> {
  await channelsPage().getByRole('button', { name: 'Close channels page' }).click();
  await expect(channelsPage()).toHaveCount(0);
}

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
    const blob = await ok(await fetch(`/api/channels/${channel.id}/blob/stream`, {
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
  await openProfile();
  const row = profileSection().locator('[data-interface-size]');
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
  await openProfile();
  await expect(row.getByRole('button', { name: '125%' })).toHaveAttribute('aria-pressed', 'true');
  await row.getByRole('button', { name: '100%' }).click();
  await expect.poll(zoomFactor, { timeout: 5_000 }).toBe(1);
  await expect.poll(savedScale, { timeout: 5_000 }).toBe(1);
  await expect(row.getByRole('button', { name: '100%' })).toHaveAttribute('aria-pressed', 'true');
  await closeChannelsPage();
});

test('G7 This profile: Spellcheck persists to settings.json and survives a reload, the profile named in the sentence', async () => {
  const saved = (): unknown => {
    try { return JSON.parse(readFileSync(settingsFile(), 'utf8')).spellcheck ?? null; } catch { return null; }
  };
  await openProfile();
  // The shell names the profile it runs in (getProfileName): its
  // settings.json is the one the switch writes.
  const profileName = await page.evaluate(() => (window as unknown as { collimateDesktop: { getProfileName(): string } }).collimateDesktop.getProfileName());
  expect(['Collimate', 'Collimate Dev']).toContain(profileName);
  await expect(channelsPage().locator('[data-profile-sentence]')).toHaveText(`This app on this computer: the ${profileName} profile`);

  const spellcheck = profileSection().getByRole('switch', { name: 'Spellcheck' });
  await expect(spellcheck).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('html')).toHaveAttribute('spellcheck', 'true');
  await spellcheck.click();
  await expect(spellcheck).toHaveAttribute('aria-checked', 'false');
  await expect.poll(saved, { timeout: 5_000 }).toBe(false);
  await expect(page.locator('html')).toHaveAttribute('spellcheck', 'false');

  // A reload paints it from main's push over the argv bootstrap the window
  // opened with (which still says true).
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('spellcheck', 'false');
  await openProfile();
  await expect(profileSection().getByRole('switch', { name: 'Spellcheck' })).toHaveAttribute('aria-checked', 'false');

  // Back on.
  await profileSection().getByRole('switch', { name: 'Spellcheck' }).click();
  await expect.poll(saved, { timeout: 5_000 }).toBe(true);
  await expect(page.locator('html')).toHaveAttribute('spellcheck', 'true');
  await closeChannelsPage();
});

test('multi-server: the Servers tab of the channels page exists in the shell and lists the local server running', async () => {
  // Since plan 1105 P7 the Servers section lives on the New / manage channels
  // page (the `#channels/<tab>` route), no longer under Settings.
  await page.evaluate(() => { window.location.hash = '#channels/servers'; });
  const dialog = page.getByRole('dialog', { name: 'Channels' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('tab', { name: 'Servers' })).toHaveAttribute('aria-selected', 'true');
  const local = dialog.locator('li[data-server-id="local"]');
  await expect(local).toBeVisible();
  await expect(local).toContainText('Local');
  await expect(local).toContainText('This window');
  await expect(local).toContainText('Running', { timeout: 20_000 });
  const origin = new URL(page.url()).origin;
  await expect(local).toContainText(origin);
  // The keep-alive option lives in the running local server's own card, as a
  // switch beside Stop, and nowhere else on the tab.
  const keep = local.getByRole('switch', { name: 'Keep local server running when the app quits' });
  await expect(keep).toBeVisible();
  await expect(keep).toHaveAttribute('aria-checked', 'false');
  await expect(local.getByRole('button', { name: 'Stop' })).toBeVisible();
  await expect(dialog.getByRole('switch')).toHaveCount(1);
  await keep.click();
  await expect(keep).toHaveAttribute('aria-checked', 'true');
  await expect.poll(() => {
    try { return JSON.parse(readFileSync(settingsFile(), 'utf8')).keepLocalRunning ?? null; } catch { return null; }
  }, { timeout: 5_000 }).toBe(true);
  await keep.click();
  await expect(keep).toHaveAttribute('aria-checked', 'false');
  // Back to the channel: the page closes and the workbench is interactive again.
  await dialog.getByRole('button', { name: 'Close channels page' }).click();
  await expect(page.getByRole('dialog', { name: 'Channels' })).toHaveCount(0);
});

test('C2 This profile: the version and stamp line, the automatic-updates switch persists to settings.json, Check for updates reaches the updater', async () => {
  const saved = (): unknown => {
    try { return JSON.parse(readFileSync(settingsFile(), 'utf8')).automaticUpdates ?? null; } catch { return null; }
  };
  await openProfile();
  await expect(channelsPage().getByText(/^This app on this computer: the Collimate( Dev)? profile$/)).toBeVisible();
  const section = profileSection();
  await expect(section.getByText(/^Collimate \d/)).toBeVisible();
  // A dev run carries no build stamp; a package names its commit, build
  // time and the server environment it carries.
  await expect(section.locator('[data-build-stamp]')).toHaveText(packagedExecutable
    ? /^Build [0-9a-f]{10}( · uncommitted changes)? · built .+ · server environment [0-9a-f]{12}$/
    : 'No build stamp (a development run)');

  const automatic = section.getByRole('switch', { name: 'Automatic updates' });
  await expect(automatic).toHaveAttribute('aria-checked', 'true');
  await automatic.click();
  await expect(automatic).toHaveAttribute('aria-checked', 'false');
  await expect.poll(saved, { timeout: 5_000 }).toBe(false);
  await automatic.click();
  await expect(automatic).toHaveAttribute('aria-checked', 'true');
  await expect.poll(saved, { timeout: 5_000 }).toBe(true);

  // A dev run has no feed: the controller answers with its reason, no dialog.
  await section.getByRole('button', { name: 'Check for updates' }).click();
  await expect(section.getByRole('status')).toHaveText(
    'This install does not update itself: no update feed (this package carries no app-update.yml and COLLIMATE_UPDATE_FEED is unset).',
  );
  await closeChannelsPage();
});
