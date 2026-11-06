import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { expect, test, type Frame, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { THEMES } from '../../src/lib/themes';
import { fixtureFiles } from './fixtures';
import { ready } from './readiness';

/** The iframe host's half of the R01 theme round trip, through the real app.
 *
 * Each shipped iframe renderer mounts in the built workbench. The skin is then
 * changed twice in the running page through Settings → Themes, with no reload,
 * so the record travels the path a user's change takes: ThemeContext
 * dispatches collimate:theme-changed, SandboxIframe pushes SET_THEME through
 * pushSandboxTheme, and sandbox.html mirrors it onto its :root. After each
 * change every renderer sandbox carries the new skin's id, scheme and tokens
 * verbatim by CSS name, and the renderer repaints its ground in the new skin
 * (the native host's half is theme-roundtrip-native.browser.test.tsx).
 */
const origin = process.env.COLLIMATE_RENDERER_TEST_URL ?? 'http://127.0.0.1:5113';
const FETCH = "fetch(process.argv[1]).then(async r => { if (!r.ok) throw new Error('roster HTTP ' + r.status); process.stdout.write(await r.text()); })";
const roster: { id: string; embed: string }[] = JSON.parse(execFileSync(
  process.execPath, ['-e', FETCH, `${origin}/__renderer_rollout/roster`], { encoding: 'utf8', timeout: 60_000 }));
const iframeRenderers = roster.filter(r => r.embed === 'iframe').map(r => r.id);

const START = 'night';
const CHANGES = ['paper', 'night-cool'] as const;
const skin = (id: string) => THEMES.find(t => t.id === id)!;
/** The grounds a renderer may paint: the skin's canvas and sheet tiers. */
const grounds = (id: string) => {
  const v = skin(id).vars;
  return [v.canvas, v.sheet, v.sheet2, v.sheet3].map(c => String(c).trim().toLowerCase());
};

function sandboxFrames(page: Page): Frame[] {
  return page.frames().filter(frame => frame.url().includes('/sandbox.html'));
}

/** What one sandbox frame shows of the skin: the attributes and the token
 * sandbox.html wrote, and every background colour painted in its document
 * that matches one of the given grounds (compared as computed rgb). */
async function frameSkin(frame: Frame, candidates: string[]) {
  return frame.evaluate((colours) => {
    const root = document.documentElement;
    const probe = document.createElement('i');
    document.body.appendChild(probe);
    const rgb = colours.map((c) => { probe.style.backgroundColor = c; return getComputedStyle(probe).backgroundColor; });
    probe.remove();
    const painted = new Set<string>();
    for (const el of Array.from(document.querySelectorAll('body, body *'))) {
      const bg = getComputedStyle(el).backgroundColor;
      const at = rgb.indexOf(bg);
      if (at >= 0) painted.add(colours[at]);
    }
    return {
      themeId: root.getAttribute('data-theme'),
      scheme: root.getAttribute('data-scheme'),
      sheet2: getComputedStyle(root).getPropertyValue('--sheet-2').trim().toLowerCase(),
      painted: [...painted],
    };
  }, candidates);
}

/** Every renderer sandbox carries the skin, and at least one paints its ground. */
async function sandboxesShow(page: Page, id: string): Promise<boolean> {
  const frames = sandboxFrames(page);
  if (!frames.length) return false;
  const expected = skin(id);
  let painted = false;
  for (const frame of frames) {
    const seen = await frameSkin(frame, grounds(id)).catch(() => null);
    if (!seen) return false;
    if (seen.themeId !== id || seen.scheme !== expected.scheme) return false;
    if (seen.sheet2 !== String(expected.vars.sheet2).trim().toLowerCase()) return false;
    if (seen.painted.length) painted = true;
  }
  return painted;
}

async function chooseSkin(page: Page, id: string) {
  const themes = page.locator('[data-command-pane][data-pane-view="settings"] [data-settings-section="theme"]');
  if (!await themes.isVisible()) {
    // Settings live in the Commands pane: its Settings group's Themes row
    // opens the Themes section there.
    await page.getByRole('button', { name: 'Commands', exact: true }).click();
    await page.getByRole('option').filter({ hasText: 'Themes' }).click();
    await expect(themes).toBeVisible();
  }
  const card = themes.getByRole('button', { name: skin(id).label, exact: true });
  await card.click();
  await expect(card).toHaveAttribute('aria-pressed', 'true');
}

test('the R01 test covers every shipped iframe renderer (R01)', () => {
  expect(iframeRenderers.length, 'iframe renderers in the SDK roster').toBeGreaterThan(0);
  // The skins it switches between share no ground, so a renderer that kept
  // the previous skin cannot pass for one that repainted.
  for (const [a, b] of [[START, CHANGES[0]], [CHANGES[0], CHANGES[1]]]) {
    expect(grounds(a).filter(c => grounds(b).includes(c)), `${a} vs ${b}`).toEqual([]);
  }
});

for (const id of iframeRenderers) {
  test(`a live skin change reaches and repaints the iframe ${id} renderer, twice, without a reload (R01)`, async ({ page, request }) => {
    test.setTimeout(240_000);
    const files = await fixtureFiles(id, request);
    const channel = await createSeededChannel(request, `W2 roundtrip ${id} ${Date.now()}`);

    const { cell_id } = await importCell(request, channel, `Roundtrip ${id}`, files);
    const put = await request.put(`/api/channels/${encodeURIComponent(channel)}/settings`, {
      data: { theme: { activeThemeId: START, userThemes: [] } },
    });
    expect(put.ok(), await put.text()).toBeTruthy();
    const filePath = files[0].name;
    await displayFile(request, channel, cell_id, filePath, id);
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('html')).toHaveAttribute('data-theme', START);
    await expect(page.locator(`[data-cell-id="${cell_id}"]`)).toBeVisible();
    await expect.poll(() => ready(page, id), { message: `${id}: mounted` }).toBe(true);
    await expect.poll(() => sandboxesShow(page, START), { message: `${id}: ${START} at mount` }).toBe(true);
    // A marker that a reload would wipe: the changes below must arrive live.
    await page.evaluate(() => { (window as Window & { __r01Live?: boolean }).__r01Live = true; });

    for (const next of CHANGES) {
      await chooseSkin(page, next);
      await expect(page.locator('html')).toHaveAttribute('data-theme', next);
      await expect.poll(() => sandboxesShow(page, next), { message: `${id}: repainted in ${next}` }).toBe(true);
    }
    expect(await page.evaluate(() => (window as Window & { __r01Live?: boolean }).__r01Live === true), 'no reload between changes').toBe(true);
    expect(errors, 'uncaught module/mount failures').toEqual([]);
  });
}
