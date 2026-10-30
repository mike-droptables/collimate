import { test, expect, type Page } from '@playwright/test';
import { gotoPlayground, listRenderers, type RendererSummary } from '../helpers/playground';
import { FIXTURES, EXPECTED_VISUAL_COUNT } from './fixtures';
import { visualCoverage, completedCoverage } from './coverage.mjs';

// Curated static output coverage, not an all-renderer visual claim. Actual
// compiled sources and SDK runtime are served by the assembled site's bridge.
async function mountRendererInHost(page: Page, renderer: RendererSummary, fixture: typeof FIXTURES[string]) {
  await page.evaluate(async ({ renderer, fixture }) => {
    const host = document.createElement('div');
    host.id = '__visual-host__';
    Object.assign(host.style, {
      position: 'fixed', top: '0', left: '0', width: '720px', height: '480px',
      background: '#1a1a1a', color: '#eeeeee', zIndex: '2147483647', overflow: 'hidden',
      fontFamily: 'monospace', colorScheme: 'dark',
    });
    document.body.appendChild(host);
    const state = window as unknown as { __visualCleanup: () => void };
    state.__visualCleanup = () => host.remove();
    const shadow = host.attachShadow({ mode: 'open' });
    const container = document.createElement('div');
    container.style.cssText = 'width:100%;height:100%;';
    shadow.appendChild(container);
    const res = await fetch(`../api/renderers/${encodeURIComponent(renderer.id)}/source?v=${renderer.content_hash}`);
    if (!res.ok || res.headers.get('X-Entrypoint') !== 'module.js'
      || res.headers.get('ETag') !== `"${renderer.content_hash}"`) {
      throw new Error(`Compiled renderer identity mismatch: ${renderer.id} (${res.status})`);
    }
    const base = new URL('../', window.location.href).href.replace(/\/$/, '');
    const source = (await res.text()).replace(/['"]\/api\/sdk\/(.*?)['"]/g, `"${base}/api/sdk/$1"`);
    const url = URL.createObjectURL(new Blob([source], { type: 'application/javascript' }));
    const noopSubscription = () => () => {};
    const api = {
      getContent: async () => fixture.content,
      getDiffContent: async () => null, getDiffLabels: () => null,
      getFilename: () => fixture.filename, getCellId: () => 'visual-cell',
      getSiblingFile: async () => { throw new Error('No sibling in visual fixture'); },
      getFilePaths: async () => [fixture.filename],
      reportChange: () => { throw new Error('Read-only visual fixture'); },
      onFilesChange: noopSubscription, onSetContent: noopSubscription,
      onFlush: noopSubscription, onThemeChange: noopSubscription,
      getTheme: () => ({ scheme: 'dark' }), isReadOnly: () => true,
      injectCss: (css: string) => {
        const style = document.createElement('style');
        style.textContent = css;
        shadow.appendChild(style);
      },
      ready: () => host.setAttribute('data-ready', '1'),
    };
    try {
      const mod = await import(/* @vite-ignore */ url);
      const def = mod.default || mod;
      if (typeof def.mount !== 'function') throw new Error('Renderer missing mount()');
      const cleanup = await def.mount(container, api);
      state.__visualCleanup = () => {
        if (typeof cleanup === 'function') cleanup();
        else cleanup?.unmount?.();
        host.remove();
      };
    } finally { URL.revokeObjectURL(url); }
  }, { renderer, fixture });
}

test('P4F46 three current native fixtures match Linux baselines with actual coverage', async ({ page }, testInfo) => {
  await gotoPlayground(page);
  const catalog = await listRenderers(page);
  const coverage = visualCoverage(catalog, FIXTURES, EXPECTED_VISUAL_COUNT);
  // A source identity mismatch must fail before producing a baseline.
  const textRenderer = catalog.find(r => r.id === 'text')!;
  await expect(mountRendererInHost(page, { ...textRenderer, content_hash: 'invalid-visual-hash' }, FIXTURES.text))
    .rejects.toThrow(/Compiled renderer identity mismatch/);
  await page.evaluate(() => (window as unknown as { __visualCleanup: () => void }).__visualCleanup());
  await expect(page.locator('#__visual-host__')).toHaveCount(0);
  const completed: string[] = [];
  for (const id of coverage.covered) {
    const fixture = FIXTURES[id];
    try {
      await mountRendererInHost(page, catalog.find(r => r.id === id)!, fixture);
      const host = page.locator('#__visual-host__');
      await expect(host).toHaveAttribute('data-ready', '1');
      expect(await host.evaluate(el => el.shadowRoot !== null)).toBe(true);
      if (id === 'text') await expect(host.locator('textarea')).toHaveValue(fixture.content);
      else await expect(host.getByText(fixture.marker, { exact: true })).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      await expect(host).toHaveScreenshot(`${id}.png`);
      completed.push(id);
    } finally {
      await page.evaluate(() => (window as unknown as { __visualCleanup?: () => void }).__visualCleanup?.());
    }
  }
  completedCoverage(coverage, completed);
  await testInfo.attach('P4F46-coverage', {
    body: JSON.stringify({ ...coverage, completed }, null, 2), contentType: 'application/json',
  });
});
