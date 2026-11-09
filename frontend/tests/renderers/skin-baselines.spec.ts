import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { BUILTIN_THEME_IDS, THEME_CSS_VARS, THEMES, type BuiltinThemeId } from '../../src/lib/themes';
import { fixtureFiles } from './fixtures';
import { ready } from './readiness';

/** Visual baselines of every shipped renderer in the three founding skins,
 * and of a representative few in every other built-in skin.
 *
 * The roster is the SDK's: the rollout helper's roster route runs the SDK's
 * own definition discovery over content/renderers and
 * builtins/. It is read when this
 * file is collected, so an unreachable helper fails the run rather than
 * collecting nothing. Each renderer mounts in the real app through the same
 * persisted file-tab projection as the rollout gate; the channel's skin is
 * switched through PUT /api/channels/{channel}/settings. A missing baseline
 * is written from a settled capture and then compared in the same run; later
 * runs settle the same way and compare against the stored file.
 */
const origin = process.env.COLLIMATE_RENDERER_TEST_URL ?? 'http://127.0.0.1:5113';
const FETCH = "fetch(process.argv[1]).then(async r => { if (!r.ok) throw new Error('roster HTTP ' + r.status); process.stdout.write(await r.text()); })";
const roster: { id: string; embed: string }[] = JSON.parse(execFileSync(
  process.execPath, ['-e', FETCH, `${origin}/__renderer_rollout/roster`], { encoding: 'utf8', timeout: 60_000 }));

/** Every renderer is captured in these. A renderer reads only the skin's
 * scheme and tokens, and these three span both schemes, a warm and a cool
 * dark, so they cover what each renderer does with a skin. */
const ROSTER_SKINS: readonly BuiltinThemeId[] = ['night', 'night-cool', 'paper'];
/** Captured in every built-in skin: code with the syntax palette, prose,
 * the terminal's code ground and a ruled table, the surfaces where a skin's
 * accents and lines show most. Five more skins over the whole roster would
 * add a hundred baselines for what these already see. */
const SAMPLED_RENDERERS: ReadonlySet<string> = new Set(['monaco', 'milkdown', 'terminal', 'table']);
const skinsFor = (id: string): readonly BuiltinThemeId[] => (SAMPLED_RENDERERS.has(id) ? BUILTIN_THEME_IDS : ROSTER_SKINS);
const baselineName = (id: string, skin: string) => `${id}-${skin}.png`;
/** Per-pixel colour distance below one skin step: night and night-cool
 * surfaces differ by about 0.021 on pixelmatch's scale, so Playwright's 0.2
 * default would pass a swapped skin. The pixel budget (about 450 pixels of a
 * cell) admits glyph anti-aliasing noise, not a recoloured control. */
const tolerance = { threshold: 0.01, maxDiffPixelRatio: 0.001 };

const shotOptions = { animations: 'disabled' as const, caret: 'hide' as const };

/** The record the renderer was handed is the skin's: the page root carries
 * every token, and every frame that defines --sheet (renderer sandboxes and
 * the sealed preview mirror it verbatim) carries the same value; an iframe
 * renderer must have such a frame. Guest pages and authored documents define
 * none and are not asked. */
async function skinReached(page: Page, skin: string, iframe: boolean): Promise<boolean> {
  const expected = THEMES.find(t => t.id === skin)!.vars;
  const sheet = String(expected.sheet).trim().toLowerCase();
  let mirrored = false;
  for (const frame of page.frames()) {
    try {
      const actual = await frame.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--sheet').trim().toLowerCase());
      if (frame === page.mainFrame() ? actual !== sheet : actual !== '' && actual !== sheet) return false;
      if (frame !== page.mainFrame() && actual === sheet) mirrored = true;
    } catch {
      return false;
    }
  }
  if (iframe && !mirrored) return false;
  const root = await page.evaluate((names) => names.map(n => getComputedStyle(document.documentElement).getPropertyValue(n).trim()),
    Object.values(THEME_CSS_VARS));
  return root.every(v => v !== '');
}

/** No renderer keeps focus while it is captured: a focused terminal or editor
 * blinks its cursor. Unfocused, each paints its static inactive state. */
async function releaseFocus(page: Page) {
  for (const frame of page.frames()) {
    await frame.evaluate(() => {
      let active = document.activeElement;
      while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
      if (active instanceof HTMLElement) active.blur();
    }).catch(() => undefined);
  }
}

async function fontsSettled(page: Page) {
  for (const frame of page.frames()) {
    await frame.evaluate(() => document.fonts.ready.then(() => undefined)).catch(() => undefined);
  }
}

/** Three identical captures half a second apart. Editors hide overlay
 * scrollbars and settle layout on timers after mount; one short pair of equal
 * frames can still precede that change. */
async function settledCapture(page: Page, target: Locator): Promise<Buffer> {
  const recent: Buffer[] = [];
  for (let attempt = 0; attempt < 40; attempt++) {
    recent.push(await target.screenshot(shotOptions));
    if (recent.length > 3) recent.shift();
    if (recent.length === 3 && recent.every(shot => shot.equals(recent[0]))) return recent[0];
    await page.waitForTimeout(500);
  }
  throw new Error('the renderer never painted three identical frames');
}

test('the SDK roster is non-empty and every shipped renderer has a visual fixture (R05)', async ({ request }) => {
  expect(roster.length, 'SDK renderer discovery').toBeGreaterThan(0);
  expect(new Set(roster.map(r => r.id)).size, 'unique renderer ids').toBe(roster.length);
  // A renamed renderer must not drop out of the every-skin sample unseen.
  for (const id of SAMPLED_RENDERERS) expect(roster.map(r => r.id), `sampled ${id}`).toContain(id);
  for (const { id } of roster) {
    const files = await fixtureFiles(id, request);
    expect(files.length, `${id} fixture`).toBeGreaterThan(0);
  }
});

for (const { id, embed } of roster) {
  const skins = skinsFor(id);
  test(`shipped ${id} matches its baseline in ${skins.join(', ')} (R05)`, async ({ page, request }, testInfo) => {
    test.setTimeout(360_000);
    const files = await fixtureFiles(id, request);
    const channel = await createSeededChannel(request, `W2 visual ${id} ${Date.now()}`);

    const { cell_id } = await importCell(request, channel, `Visual ${id}`, files);
    const filePath = files[0].name;
    await displayFile(request, channel, cell_id, filePath, id);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    const cell = page.locator(`[data-cell-id="${cell_id}"]`);

    for (const [index, skin] of skins.entries()) {
      const put = await request.put(`/api/channels/${encodeURIComponent(channel)}/settings`, {
        data: { theme: { override: skin } },
      });
      expect(put.ok(), await put.text()).toBeTruthy();
      expect((await put.json()).theme.override).toBe(skin);
      if (index === 0) await page.goto('/', { waitUntil: 'domcontentloaded' });
      else await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('html')).toHaveAttribute('data-theme', skin);
      await expect(cell).toBeVisible();
      await expect.poll(() => ready(page, id), { message: `${id}: mounted in ${skin}` }).toBe(true);
      await expect.poll(() => skinReached(page, skin, embed === 'iframe'), { message: `${id}: ${skin} tokens reached every renderer frame` }).toBe(true);
      await releaseFocus(page);
      await fontsSettled(page);
      const name = baselineName(id, skin);
      const stored = testInfo.snapshotPath(name, { kind: 'screenshot' });
      const settled = await settledCapture(page, cell);
      if (!existsSync(stored)) {
        mkdirSync(path.dirname(stored), { recursive: true });
        writeFileSync(stored, settled);
        testInfo.annotations.push({ type: 'baseline-written', description: name });
      }
      await expect(cell, `${id} in ${skin}`).toHaveScreenshot(name, { ...shotOptions, ...tolerance });
    }
    expect(errors, 'uncaught module/mount failures').toEqual([]);
  });
}

test('every renderer has a baseline in each of its skins and none is orphaned (R05)', async () => {
  const testInfo = test.info();
  const expected = roster.flatMap(({ id }) => skinsFor(id).map(skin => testInfo.snapshotPath(baselineName(id, skin), { kind: 'screenshot' })));
  const folder = path.dirname(expected[0]);
  expect(expected.every(file => path.dirname(file) === folder)).toBe(true);
  const stored = readdirSync(folder).filter(name => name.endsWith('.png')).map(name => path.join(folder, name)).sort();
  expect(stored).toEqual([...expected].sort());
});
