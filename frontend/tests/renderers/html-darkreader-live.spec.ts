import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { expect, test, type Frame, type Page } from '@playwright/test';
import { THEMES } from '../../src/lib/themes';
import { fixtureFiles } from './fixtures';
import { ready } from './readiness';

/** The page renderer's DarkReader on the skin (R04 W2b), through the real app.
 *
 * The html renderer opens with its style theme set to Darkened (the channel's
 * `renderer_styles.html`, PUT with the skin) in `night`. Its srcDoc must be built from the host's skin record, never the
 * iframe placeholder: the bootstrap enables DarkReader on night's `--sheet`
 * and `--ink`, not on `{}` (the library's own greys). The skin then changes to
 * `night-cool` through Settings → Themes; the renderer posts the new colours
 * into the loaded page, whose bootstrap re-enables DarkReader on them, so the
 * page repaints in night-cool with no reload of the frame (the same document,
 * its marker intact).
 *
 * Inside a frame DarkReader leaves the root and body grounds transparent; what
 * shows behind the page is the renderer's frame backing, and what DarkReader
 * paints over an authored ground is its neutral background. Both are checked.
 */
const START = 'night';
const NEXT = 'night-cool';
const skin = (id: string) => THEMES.find(t => t.id === id)!;
const hex = (c: unknown) => String(c).trim().toLowerCase();

/** The renderer's sandbox frame, and the user's page inside it. */
function sandboxFrame(page: Page): Frame | undefined {
  return page.frames().find(frame => frame.url().includes('/sandbox.html?renderer=html'));
}
function pageFrame(page: Page): Frame | undefined {
  return page.frames().find(frame => frame.url() === 'about:srcdoc'
    && !!frame.parentFrame()?.url().includes('/sandbox.html?renderer=html'));
}

/** What the darkened page shows, against the skin's colours resolved the same
 * way: the body's text, DarkReader's neutral ground and text, and the live
 * marker a reload or srcDoc swap would wipe. */
async function pagePaint(frame: Frame, id: string) {
  const { sheet, ink } = skin(id).vars;
  return frame.evaluate(({ sheet, ink }) => {
    const probe = document.createElement('i');
    document.documentElement.appendChild(probe);
    probe.style.color = ink;
    const inkRgb = getComputedStyle(probe).color;
    probe.remove();
    const root = getComputedStyle(document.documentElement);
    return {
      darkened: !!document.querySelector('style.darkreader'),
      text: getComputedStyle(document.body).color,
      inkRgb,
      neutralGround: root.getPropertyValue('--darkreader-neutral-background').trim().toLowerCase(),
      neutralText: root.getPropertyValue('--darkreader-neutral-text').trim().toLowerCase(),
      sheet, ink,
      live: (window as Window & { __w2bLive?: boolean }).__w2bLive === true,
    };
  }, { sheet: hex(sheet), ink: hex(ink) });
}

/** The renderer's side: the backing behind the page, the srcDoc it built and
 * whether the style control is mounted. */
async function rendererSide(frame: Frame, id: string) {
  return frame.evaluate((sheet) => {
    const iframe = document.querySelector('iframe[title="page"]') as HTMLIFrameElement | null;
    const probe = document.createElement('i');
    document.body.appendChild(probe);
    probe.style.backgroundColor = sheet;
    const sheetRgb = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return {
      srcdoc: iframe?.getAttribute('srcdoc') ?? '',
      backing: iframe?.parentElement ? getComputedStyle(iframe.parentElement).backgroundColor : '',
      sheetRgb,
      styled: !!document.querySelector('[data-collimate-style-control]'),
    };
  }, hex(skin(id).vars.sheet));
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

test('a darkened page opens on the skin and repaints onto the next skin without a reload (R04 W2b)', async ({ page, request }) => {
  test.setTimeout(240_000);
  expect(hex(skin(START).vars.sheet)).not.toBe(hex(skin(NEXT).vars.sheet));
  expect(hex(skin(START).vars.ink)).not.toBe(hex(skin(NEXT).vars.ink));
  const files = await fixtureFiles('html', request);
  const channel = await createSeededChannel(request, `W2b html darkreader ${Date.now()}`);

  const { cell_id } = await importCell(request, channel, 'W2b html darkreader', files);
  const put = await request.put(`/api/channels/${encodeURIComponent(channel)}/settings`, {
    data: {
      theme: { activeThemeId: START, userThemes: [] },
      // The page's look is its per-channel renderer style: Darkened.
      renderer_styles: { html: { theme: 'darkened', font: null, textSize: null } },
    },
  });
  expect(put.ok(), await put.text()).toBeTruthy();
  const filePath = files[0].name;
  await displayFile(request, channel, cell_id, filePath, 'html');
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', START);
  await expect.poll(() => ready(page, 'html'), { message: 'html: mounted' }).toBe(true);
  await expect.poll(async () => {
    const frame = pageFrame(page);
    const seen = frame ? await pagePaint(frame, START).catch(() => null) : null;
    return !!seen && seen.darkened && seen.text === seen.inkRgb;
  }, { message: `darkened onto ${START}` }).toBe(true);

  // The srcDoc was built from the host's record: the darkened style did not
  // skip the wait, so the first enable is already on the skin, never on {}.
  const sandbox = sandboxFrame(page)!;
  const built = await rendererSide(sandbox, START);
  expect(built.styled, 'the style control is mounted').toBe(true);
  const night = { sheet: hex(skin(START).vars.sheet), ink: hex(skin(START).vars.ink) };
  // The theme is enable's first argument; the document view's fixes follow it.
  expect(built.srcdoc).toContain(
    `DarkReader.enable({"darkSchemeBackgroundColor":"${night.sheet}ff","darkSchemeTextColor":"${night.ink}ff"}`);
  expect(built.srcdoc).not.toContain('DarkReader.enable({});');
  expect(built.backing).toBe(built.sheetRgb);

  const frame = pageFrame(page)!;
  const first = await pagePaint(frame, START);
  expect(first.neutralGround).toBe(first.sheet);
  expect(first.neutralText).toBe(first.ink);
  // A marker the page's document would lose on a srcDoc swap or a reload.
  await frame.evaluate(() => { (window as Window & { __w2bLive?: boolean }).__w2bLive = true; });

  await chooseSkin(page, NEXT);
  await expect(page.locator('html')).toHaveAttribute('data-theme', NEXT);
  await expect.poll(async () => {
    const seen = await pagePaint(frame, NEXT).catch(() => null);
    return !!seen && seen.text === seen.inkRgb && seen.neutralGround === seen.sheet;
  }, { message: `repainted in ${NEXT}` }).toBe(true);
  const after = await pagePaint(frame, NEXT);
  expect(after.live, 'the same document: no reload, no srcDoc swap').toBe(true);
  expect(after.darkened).toBe(true);
  expect(after.neutralGround).toBe(after.sheet);
  expect(after.neutralText).toBe(after.ink);
  expect(pageFrame(page), 'the same frame').toBe(frame);
  const moved = await rendererSide(sandbox, NEXT);
  expect(moved.backing, `the frame backing is ${NEXT}'s sheet`).toBe(moved.sheetRgb);
  // The skin change did not rebuild the document: the srcDoc keeps night's
  // baked colours, and the repaint came from the posted message.
  expect(moved.srcdoc).toBe(built.srcdoc);
  expect(errors, 'uncaught module/mount failures').toEqual([]);
});
