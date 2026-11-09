import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Frame, type Page } from '@playwright/test';
import { createSeededChannel } from '../seedChannel';
import { importCell } from './persistenceFixtures';

// The keyboard follows the selected cell (frontend/src/lib/selectionFocus.ts):
// typing in a cell's .md, then moving the selection with the canvas shortcut
// from inside that editor, must edit the newly selected cell's file and never
// the previous one. Live, the second text used to land in the first .md (its
// pooled iframe kept the keys).

/** Show `path` in the cell's one tab, the Files pane collapsed so a press on
 *  the body lands on the renderer. */
async function showFile(request: APIRequestContext, channel: string, cellId: string, path: string): Promise<void> {
  const leaf = randomUUID(), tab = randomUUID();
  const response = await request.put(`/api/channels/${channel}/cell_display/${cellId}`, { data: {
    version: 1, root: { type: 'leaf', id: leaf, tabs: [{ id: tab, kind: 'file', path }], activeTabId: tab }, activeLeafId: leaf,
    filesPane: { collapsed: true, side: 'left', width: null, height: null, viewMode: 'tree', panelFilters: { runsOnly: false, sortByRecency: false } },
    runsPane: { open: false, side: 'right', width: null, height: null },
    pinnedTabCompact: false, history: null,
  } });
  expect(response.ok(), await response.text()).toBe(true);
}

async function fileText(request: APIRequestContext, channel: string, cellId: string, path: string): Promise<string> {
  const response = await request.get(`/api/channels/${channel}/cells/${cellId}/files/${path}`);
  expect(response.ok(), await response.text()).toBe(true);
  return response.text();
}

/** The milkdown frame positioned over the cell's card. */
async function editorFrameOf(page: Page, cellId: string): Promise<Frame | null> {
  const card = await page.locator(`[data-cell-id="${cellId}"] [data-stack-card]`).boundingBox();
  if (!card) return null;
  for (const frame of page.frames()) {
    if (frame === page.mainFrame() || !(await frame.locator('.ProseMirror').count())) continue;
    const box = await (await frame.frameElement()).boundingBox();
    if (!box) continue;
    const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
    if (cx >= card.x && cx <= card.x + card.width && cy >= card.y && cy <= card.y + card.height) return frame;
  }
  return null;
}

test('typing after a selection shortcut lands in the newly selected cell', async ({ page, request }) => {
  const channel = await createSeededChannel(request, `Selection focus ${Date.now()}`);
  const a = await importCell(request, channel, 'Focus A', [{ name: 'a.md', content: '# Focus A\n\nalpha body\n' }]);
  const b = await importCell(request, channel, 'Focus B', [{ name: 'b.md', content: '# Focus B\n\nbeta body\n' }]);
  const c = await importCell(request, channel, 'Focus C', [{ name: 'c.txt', content: 'gamma body\n' }]);
  await showFile(request, channel, a.cell_id, 'a.md');
  await showFile(request, channel, b.cell_id, 'b.md');
  await showFile(request, channel, c.cell_id, 'c.txt');

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator(`[data-cell-id="${a.cell_id}"] [data-stack-card]`)).toBeVisible();
  await expect.poll(async () => !!(await editorFrameOf(page, a.cell_id))).toBe(true);
  await expect.poll(async () => !!(await editorFrameOf(page, b.cell_id))).toBe(true);
  const frameA = (await editorFrameOf(page, a.cell_id))!;
  const frameB = (await editorFrameOf(page, b.cell_id))!;

  // The caret into A's body text: the first press selects the cell (its
  // scrim takes it and hands the editor the keys), a second one lands
  // natively once the frame takes pointer events.
  const line = await frameA.locator('.ProseMirror p').first().boundingBox();
  expect(line).not.toBeNull();
  await expect.poll(async () => {
    await page.mouse.click(line!.x + line!.width - 4, line!.y + line!.height / 2);
    await page.waitForTimeout(300);
    return frameA.evaluate(() => document.hasFocus());
  }).toBe(true);
  await page.keyboard.press('End');
  await page.keyboard.type('FIRSTA');

  // Ctrl+→ from inside A's frame (the frame forwards it) selects B; the
  // keys must follow.
  await page.keyboard.press('Control+ArrowRight');
  await expect.poll(() => frameB.evaluate(() => document.hasFocus())).toBe(true);
  expect(await frameA.evaluate(() => document.hasFocus())).toBe(false);
  await page.keyboard.type('SECONDB');

  await expect.poll(() => fileText(request, channel, b.cell_id, 'b.md'), { timeout: 15_000 }).toContain('SECONDB');
  const aText = await fileText(request, channel, a.cell_id, 'a.md');
  expect(aText).toContain('FIRSTA');
  expect(aText).not.toContain('SECONDB');

  // On to a native renderer (.txt in a shadow root): the keys follow again.
  await page.keyboard.press('Control+ArrowRight');
  await expect.poll(() => frameB.evaluate(() => document.hasFocus())).toBe(false);
  await page.waitForTimeout(300);
  await page.keyboard.type('THIRDC');
  await expect.poll(() => fileText(request, channel, c.cell_id, 'c.txt'), { timeout: 15_000 }).toContain('THIRDC');
  expect(await fileText(request, channel, b.cell_id, 'b.md')).not.toContain('THIRDC');
});
