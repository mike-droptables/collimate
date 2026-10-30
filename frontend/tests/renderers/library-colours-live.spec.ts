import { displayFile, importCell } from './persistenceFixtures';
import { createSeededChannel } from '../seedChannel';
import { expect, test, type APIRequestContext, type Frame, type Page } from '@playwright/test';
import { BUILTIN_THEME_IDS, THEME_CSS_VARS, THEMES } from '../../src/lib/themes';
import { fixtureFiles, marker } from './fixtures';
import { ready } from './readiness';

/** The libraries draw in the skin's colours, through the real app (W2b).
 *
 * The skin baselines compare whole cells against a pixel budget that a few
 * recoloured glyphs fit inside, so they cannot tell Monaco's own keyword blue
 * from the skin's teal. These cases read the colours the libraries actually
 * paint, in every built-in skin:
 *
 * - Monaco (R05 W2b): a keyword, a number and a comment are drawn in the
 *   `syntaxPalette` colours the served runtime derives from the skin record.
 * - Milkdown (R02 W2b): a fenced code block's tokens are drawn in the same
 *   palette (CodeMirror's highlight, not Crepe's One Dark).
 * - Excalidraw (R05 W2b): the drawing's canvas is transparent over the
 *   frame's `--canvas`, and its islands are the skin's `--sheet-2`.
 *
 * The expected palette is computed in the renderer's own frame by the served
 * `/api/sdk/react.js` from the skin's tokens, so a nudged role is expected at
 * its nudged value.
 */
const skin = (id: string) => THEMES.find(t => t.id === id)!;

/** The skin as the record a renderer receives: tokens keyed by CSS name. */
function record(id: string) {
  const def = skin(id);
  const vars: Record<string, string> = {};
  for (const [key, cssName] of Object.entries(THEME_CSS_VARS)) {
    vars[cssName] = String((def.vars as unknown as Record<string, unknown>)[key]);
  }
  return { themeId: id, scheme: def.scheme, known: true, vars, fonts: { ui: '', mono: '', prose: '' } };
}

const rgb = (hex: string) => {
  const n = parseInt(hex.slice(1, 7), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
};

async function openCell(request: APIRequestContext, id: string, files: { name: string }[]) {
  const channel = await createSeededChannel(request, `W2b library colours ${id} ${Date.now()}`);

  const { cell_id } = await importCell(request, channel, `W2b ${id}`, files);
  const file = files[0].name;
  await displayFile(request, channel, cell_id, file, id);
  return channel;
}

/** Switch the channel's skin, (re)load, and wait for the renderer to mount
 * with the skin's `--sheet` in the frame that holds `selector`. */
async function inSkin(page: Page, request: APIRequestContext, channel: string, id: string, skinId: string,
  first: boolean, selector: string): Promise<Frame> {
  const put = await request.put(`/api/channels/${encodeURIComponent(channel)}/settings`, {
    data: { theme: { activeThemeId: skinId, userThemes: [] } },
  });
  expect(put.ok(), await put.text()).toBeTruthy();
  if (first) await page.goto('/', { waitUntil: 'domcontentloaded' });
  else await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page.locator('html')).toHaveAttribute('data-theme', skinId);
  await expect.poll(() => ready(page, id), { message: `${id}: mounted in ${skinId}` }).toBe(true);
  const sheet = String(skin(skinId).vars.sheet).trim().toLowerCase();
  let found: Frame | undefined;
  await expect.poll(async () => {
    for (const frame of page.frames()) {
      const seen = await frame.evaluate((sel) => document.querySelector(sel)
        ? getComputedStyle(document.documentElement).getPropertyValue('--sheet').trim().toLowerCase()
        : null, selector).catch(() => null);
      if (seen === sheet) { found = frame; return true; }
    }
    return false;
  }, { message: `${id}: ${skinId} reached the frame holding ${selector}` }).toBe(true);
  return found!;
}

/** The palette the served runtime derives from this skin, in that frame. */
async function servedPalette(frame: Frame, skinId: string): Promise<Record<string, string>> {
  const palette = await frame.evaluate(async (rec) => {
    const runtime = await import(new URL('/api/sdk/react.js', location.href).href);
    return runtime.syntaxPalette(rec);
  }, record(skinId));
  expect(palette, `${skinId}: the served syntaxPalette`).not.toBeNull();
  return palette as Record<string, string>;
}

/** The colour each listed text is drawn in, from the token spans under `root`. */
async function tokenColours(frame: Frame, root: string, texts: string[]) {
  return frame.evaluate(({ root, texts }) => {
    const out: Record<string, string | null> = {};
    const spans = Array.from(document.querySelectorAll(`${root} span`)) as HTMLElement[];
    for (const text of texts) {
      // Monaco draws spaces as no-break spaces.
      const span = spans.find(s => s.children.length === 0 && s.textContent?.replace(/\u00a0/g, ' ').trim() === text);
      out[text] = span ? getComputedStyle(span).color : null;
    }
    return out;
  }, { root, texts });
}

test('Monaco draws code in the skin palette in every built-in skin (R05 W2b)', async ({ page, request }) => {
  test.setTimeout(300_000);
  const files = await fixtureFiles('monaco', request);
  const channel = await openCell(request, 'monaco', files);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const [index, skinId] of BUILTIN_THEME_IDS.entries()) {
    const frame = await inSkin(page, request, channel, 'monaco', skinId, index === 0, '.monaco-editor .view-lines');
    const palette = await servedPalette(frame, skinId);
    const want = { const: rgb(palette.keyword), '1': rgb(palette.number), [`// ${marker('monaco')}`]: rgb(palette.comment) };
    await expect.poll(() => tokenColours(frame, '.monaco-editor .view-lines', Object.keys(want)),
      { message: `monaco in ${skinId}: keyword, number and comment colours` }).toEqual(want);
    // Not the built-in base's highlighting (vs-dark / vs keyword blue).
    expect(want.const).not.toBe('rgb(86, 156, 214)');
    expect(want.const).not.toBe('rgb(0, 0, 255)');
  }
  expect(errors, 'uncaught module/mount failures').toEqual([]);
});

test('Milkdown draws fenced code in the skin palette in every built-in skin (R02 W2b)', async ({ page, request }) => {
  test.setTimeout(300_000);
  const md = `# ${marker('milkdown')}\n\n\`\`\`javascript\nconst value = 1;\n// note\n\`\`\`\n`;
  const files = [{ name: 'sample.md', content: md }];
  expect((await fixtureFiles('milkdown', request))[0].name).toBe('sample.md');
  const channel = await openCell(request, 'milkdown', files);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const [index, skinId] of BUILTIN_THEME_IDS.entries()) {
    const frame = await inSkin(page, request, channel, 'milkdown', skinId, index === 0, '.ProseMirror');
    const palette = await servedPalette(frame, skinId);
    const want = { const: rgb(palette.keyword), '1': rgb(palette.number), '// note': rgb(palette.comment) };
    await expect.poll(() => tokenColours(frame, '.cm-content', Object.keys(want)),
      { message: `milkdown in ${skinId}: fenced keyword, number and comment colours`, timeout: 30_000 }).toEqual(want);
  }
  expect(errors, 'uncaught module/mount failures').toEqual([]);
});

test('Excalidraw shows the skin canvas through a transparent drawing, with islands on the sheet (R05 W2b)', async ({ page, request }) => {
  test.setTimeout(300_000);
  const files = await fixtureFiles('excalidraw', request);
  const channel = await openCell(request, 'excalidraw', files);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  for (const [index, skinId] of BUILTIN_THEME_IDS.entries()) {
    const frame = await inSkin(page, request, channel, 'excalidraw', skinId, index === 0, '.excalidraw');
    const paint = () => frame.evaluate(() => {
      const probe = document.createElement('i');
      document.body.appendChild(probe);
      const resolve = (value: string) => { probe.style.backgroundColor = value; return getComputedStyle(probe).backgroundColor; };
      const canvasToken = resolve('var(--canvas)');
      const sheet2 = resolve('var(--sheet-2)');
      probe.remove();
      const island = document.querySelector('.excalidraw .App-toolbar .Island, .excalidraw .Island') as HTMLElement | null;
      const canvases = Array.from(document.querySelectorAll('.excalidraw canvas')) as HTMLCanvasElement[];
      // The scene canvas is cleared, not filled: an empty spot is transparent.
      const alphas = canvases.map(c => {
        const ctx = c.getContext('2d');
        return ctx && c.width > 8 && c.height > 8 ? ctx.getImageData(c.width - 4, c.height - 4, 1, 1).data[3] : -1;
      });
      return {
        body: getComputedStyle(document.body).backgroundColor,
        canvasToken,
        island: island ? getComputedStyle(island).backgroundColor : null,
        sheet2,
        alphas,
      };
    });
    await expect.poll(async () => {
      const seen = await paint();
      return seen.island === seen.sheet2 && seen.body === seen.canvasToken
        && seen.alphas.length > 0 && seen.alphas.every(a => a === 0);
    }, { message: `excalidraw in ${skinId}: ground and islands on the skin` }).toBe(true);
    const seen = await paint();
    // Not the library's own dark ground or its violet-grey island.
    expect(seen.body).not.toBe('rgb(18, 18, 18)');
    expect(seen.canvasToken).toBe(rgb(String(skin(skinId).vars.canvas).toLowerCase()));
  }
  expect(errors, 'uncaught module/mount failures').toEqual([]);
});
