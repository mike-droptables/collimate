import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Frame, type Page } from '@playwright/test';
import { createSeededChannel } from '../seedChannel';
import { importCell, putTextFiles } from './persistenceFixtures';

/** Picking an element of an html page into a message (the renderer
 * annotation API), through the real app.
 *
 * The composer's Add annotation asks the page's view for context
 * (frontend/src/lib/rendererAnnotation.ts → sandbox.html → the html
 * renderer's useAnnotationRequest → the document view's element picker). A
 * click on the page attaches that element, and the page's own handlers never
 * see the click; Esc in the page abandons the pick; a write to the file
 * mid-pick reloads the page, and the pick re-arms in the new document.
 */
const PAGE = `<!doctype html><html><body><section id="sec"><ul><li>One</li><li>Second item</li></ul></section>
<script>window.__pageClicks = 0; document.addEventListener('click', () => { window.__pageClicks++; });</script></body></html>`;
const CHIP = 'page.html › #sec > ul:nth-of-type(1) > li:nth-of-type(2)';

/** Show `path` in the cell's one tab, with the status pane open beside it. */
async function showWithRunsPane(request: APIRequestContext, channel: string, cellId: string, path: string): Promise<string> {
  const leaf = randomUUID(), tab = randomUUID();
  const response = await request.put(`/api/channels/${channel}/cell_display/${cellId}`, { data: {
    version: 1, root: { type: 'leaf', id: leaf, tabs: [{ id: tab, kind: 'file', path }], activeTabId: tab }, activeLeafId: leaf,
    filesPane: { collapsed: true, side: 'left', width: null, height: null, viewMode: 'tree', panelFilters: { runsOnly: false, sortByRecency: false } },
    runsPane: { open: true, side: 'right', width: null, height: null },
    pinnedTabCompact: false, history: null,
  } });
  expect(response.ok(), await response.text()).toBe(true);
  return tab;
}

/** The user's page inside the html renderer's sandbox frame. */
function pageFrame(page: Page): Frame | undefined {
  return page.frames().find(frame => frame.url() === 'about:srcdoc'
    && !!frame.parentFrame()?.url().includes('/sandbox.html?renderer=html'));
}

/** The picker is armed in the page: its layer is up and the cursor is the crosshair. */
const armed = (frame: Frame) => frame.evaluate(() => !!document.querySelector('collimate-pick[data-collimate-pick]')
  && getComputedStyle(document.body).cursor === 'crosshair').catch(() => false);
const pageClicks = (frame: Frame) => frame.evaluate(() => (window as Window & { __pageClicks?: number }).__pageClicks);

test('a pick attaches the clicked element of an html page to the message (renderer annotations)', async ({ page, request }) => {
  test.setTimeout(240_000);
  const channel = await createSeededChannel(request, `Html pick ${Date.now()}`);
  const { cell_id } = await importCell(request, channel, 'Html pick', [{ name: 'page.html', content: PAGE }]);
  const tab = await showWithRunsPane(request, channel, cell_id, 'page.html');
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const cell = page.locator(`[data-cell-id="${cell_id}"]`);
  await expect(cell.locator('[data-stack-card]')).toBeVisible();
  await expect.poll(async () => {
    const frame = pageFrame(page);
    return !!frame && await frame.locator('#sec li').count().catch(() => 0);
  }, { message: 'the html page is up' }).toBe(2);
  // The file is the one the composer picks from (its last file tab).
  await cell.locator(`[data-tab-id="${tab}"]`).click();

  await cell.locator('[data-runs-pane] [data-testid="status-all"]').click();
  const composer = cell.locator('[data-runs-pane] [data-testid="annotation-composer"]');
  await expect(composer).toBeVisible();
  const add = composer.locator('[data-testid="composer-add-annotation"]');
  const chips = composer.locator('[data-testid="composer-context-chip"]');
  const waiting = composer.locator('[data-testid="composer-waiting"]');

  // A click on the second item attaches it; the page never hears the click.
  await add.click();
  await expect(waiting).toBeVisible();
  const frame = pageFrame(page)!;
  await expect.poll(() => armed(frame), { message: 'the picker is armed' }).toBe(true);
  const second = frame.locator('#sec li').nth(1);
  await second.hover();
  await second.click();
  await expect(chips).toHaveCount(1);
  await expect(chips.first()).toHaveText(CHIP);
  await expect(waiting).toHaveCount(0);
  expect(await pageClicks(frame)).toBe(0);
  expect(await armed(frame)).toBe(false);

  // Esc in the page abandons the pick: no chip, the composer waits no more.
  await add.click();
  await expect(waiting).toBeVisible();
  await expect.poll(() => armed(frame)).toBe(true);
  await frame.evaluate(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })); });
  await expect(waiting).toHaveCount(0);
  await expect(chips).toHaveCount(1);
  expect(await armed(frame)).toBe(false);

  // A write to the file mid-pick reloads the page; the pick re-arms there.
  await add.click();
  await expect(waiting).toBeVisible();
  await expect.poll(() => armed(frame)).toBe(true);
  await putTextFiles(request, channel, cell_id, [{ name: 'page.html', content: PAGE.replace('<li>One</li>', '<li>First</li>') }]);
  await expect.poll(async () => {
    const reloaded = pageFrame(page);
    if (!reloaded) return false;
    const text = await reloaded.locator('#sec li').first().textContent().catch(() => '');
    return text === 'First' && await armed(reloaded);
  }, { message: 'the reloaded page is armed again' }).toBe(true);
  const reloaded = pageFrame(page)!;
  await reloaded.locator('#sec li').nth(1).click();
  await expect(chips).toHaveCount(2);
  await expect(chips.nth(1)).toHaveText(CHIP);
  expect(await pageClicks(reloaded)).toBe(0);
  expect(errors, 'uncaught module/mount failures').toEqual([]);
});
