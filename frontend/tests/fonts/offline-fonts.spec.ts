import { createReadStream, existsSync, statSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import path from 'node:path';
import { expect, test } from '@playwright/test';

// The three default faces load from the app's own /fonts/ with the network
// off: the built app is served locally and every request to another origin is
// refused, so a face that is not bundled cannot load.

const DIST = path.resolve(__dirname, '../../dist');
const TYPES: Record<string, string> = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
};
const DEFAULT_FACES = ['15px Lora', '15px "Hanken Grotesk"', '15px "JetBrains Mono"'];

let server: Server;
let origin = '';

test.beforeAll(async () => {
  if (!existsSync(path.join(DIST, 'fonts', 'fonts.css'))) throw new Error(`no built frontend at ${DIST}: run npm run build`);
  server = createServer((req, res) => {
    const pathname = decodeURIComponent(new URL(req.url ?? '/', 'http://local').pathname);
    let file = path.join(DIST, path.normalize(pathname));
    if (!file.startsWith(DIST) || !existsSync(file) || statSync(file).isDirectory()) file = path.join(DIST, 'index.html');
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] ?? 'application/octet-stream' });
    createReadStream(file).pipe(res);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

test.afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

test('the three default faces load with the network off (S03)', async ({ context, page }) => {
  const refused: string[] = [];
  const fontFiles: string[] = [];
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      refused.push(url.href);
      return route.abort('internetdisconnected');
    }
    if (url.pathname.endsWith('.woff2')) fontFiles.push(url.pathname);
    return route.continue();
  });

  await page.goto(`${origin}/`);
  const faces = await page.evaluate(async (specs) => {
    const out: Record<string, { loaded: number; check: boolean }> = {};
    for (const spec of specs) {
      const loaded = await document.fonts.load(spec, 'Ag');
      out[spec] = { loaded: loaded.filter((face) => face.status === 'loaded').length, check: document.fonts.check(spec, 'Ag') };
    }
    return out;
  }, DEFAULT_FACES);

  for (const spec of DEFAULT_FACES) {
    expect(faces[spec].loaded, spec).toBeGreaterThan(0);
    expect(faces[spec].check, spec).toBe(true);
  }
  expect(fontFiles.every((file) => file.startsWith('/fonts/'))).toBe(true);
  expect(fontFiles.some((file) => file.startsWith('/fonts/lora/'))).toBe(true);
  expect(fontFiles.some((file) => file.startsWith('/fonts/hanken-grotesk/'))).toBe(true);
  expect(fontFiles.some((file) => file.startsWith('/fonts/jetbrains-mono/'))).toBe(true);
  expect(refused.filter((url) => /font|woff/i.test(url))).toEqual([]);
});

test('the ebook-fonts core faces and Charis SIL load from /fonts/ with the network off, bold italic included (S03)', async ({ context, page }) => {
  await context.route('**/*', (route) => (new URL(route.request().url()).origin === origin
    ? route.continue()
    : route.abort('internetdisconnected')));
  await page.goto(`${origin}/`);
  const specs = ['15px "NV Jost"', '15px "NV Legible Next"', 'italic 700 15px "NV Garamond"', '700 15px Cartisse', 'italic 15px "NV Palatium"',
    'italic 700 15px "Charis SIL"'];
  const loaded = await page.evaluate(async (all) => {
    const out: Record<string, string[]> = {};
    for (const spec of all) out[spec] = (await document.fonts.load(spec, 'Ag')).map((face) => `${face.style} ${face.weight} ${face.status}`);
    return out;
  }, specs);
  expect(loaded).toEqual({
    '15px "NV Jost"': ['normal 400 loaded'],
    '15px "NV Legible Next"': ['normal 400 loaded'],
    'italic 700 15px "NV Garamond"': ['italic 700 loaded'],
    '700 15px Cartisse': ['normal 700 loaded'],
    'italic 15px "NV Palatium"': ['italic 400 loaded'],
    'italic 700 15px "Charis SIL"': ['italic 700 loaded'],
  });
});
