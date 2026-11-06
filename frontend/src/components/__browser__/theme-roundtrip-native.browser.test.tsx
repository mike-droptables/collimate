import { expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { NativeRenderer } from '../NativeRenderer';
import { readThemeSnapshot } from '@/lib/themeSnapshot';
import { FIXTURE_PROBE_ID, type ProbeState } from '@/test/rendererFixtures';

// The shadow-DOM sibling of theme-roundtrip.browser.test.tsx: a native
// renderer's api carries the same record the iframe gets (every token by CSS
// name, known, the fonts), and var(--sheet-2) inside its shadow root repaints
// when the skin changes, with no re-injection.
it('a native renderer sees every skin token verbatim and repaints on a skin change (R01)', async () => {
  const root = document.documentElement;
  const oldStyle = root.getAttribute('style');
  const oldScheme = root.getAttribute('data-scheme');
  const oldTheme = root.getAttribute('data-theme');
  const p: ProbeState = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
  (window as unknown as { __probe: ProbeState }).__probe = p;
  const first = { sheet2: 'rgb(17, 34, 51)', prose: '"Literata", Georgia, serif' };
  const second = { sheet2: 'rgb(200, 190, 180)', prose: '"Alegreya", Georgia, serif' };
  const skin = (s: typeof first, id: string, scheme: 'dark' | 'light') => {
    root.style.setProperty('--sheet-2', s.sheet2);
    root.style.setProperty('--font-prose', s.prose);
    root.setAttribute('data-scheme', scheme);
    root.setAttribute('data-theme', id);
  };
  skin(first, 'roundtrip-native', 'dark');
  const view = await render(
    <div style={{ width: 300, height: 120 }}>
      <NativeRenderer rendererId={FIXTURE_PROBE_ID} rendererHash="R01-theme"
        fileName="theme.txt" fileContent="theme" isActiveFocus={true} />
    </div>,
  );
  try {
    await vi.waitFor(() => expect(p.mounts).toBe(1));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const api = p.apis[0] as any;
    const theme = api.getTheme();
    expect(theme).toEqual(readThemeSnapshot());
    expect(theme).toMatchObject({
      themeId: 'roundtrip-native', scheme: 'dark', known: true,
      fonts: { prose: first.prose },
    });
    expect(theme.vars['--sheet-2']).toBe(first.sheet2);
    expect(theme.vars['--font-prose']).toBe(first.prose);

    const host = Array.from(view.container.querySelectorAll('*'))
      .find((el) => el.shadowRoot?.getElementById('mount'));
    const mount = host!.shadowRoot!.getElementById('mount')!;
    const swatch = document.createElement('div');
    swatch.style.background = 'var(--sheet-2)';
    swatch.style.fontFamily = 'var(--font-prose)';
    mount.append(swatch);
    expect(getComputedStyle(swatch).backgroundColor).toBe(first.sheet2);

    const seen: Array<{ themeId: string; sheet2: string; known: boolean }> = [];
    const off = api.onThemeChange((t: { themeId: string; known: boolean; vars: Record<string, string> }) =>
      seen.push({ themeId: t.themeId, sheet2: t.vars['--sheet-2'], known: t.known }));
    skin(second, 'roundtrip-native-2', 'light');
    window.dispatchEvent(new CustomEvent('collimate:theme-changed'));
    off();
    expect(seen).toEqual([
      { themeId: 'roundtrip-native', sheet2: first.sheet2, known: true },
      { themeId: 'roundtrip-native-2', sheet2: second.sheet2, known: true },
    ]);
    expect(api.getTheme()).toMatchObject({ scheme: 'light', fonts: { prose: second.prose } });
    expect(getComputedStyle(swatch).backgroundColor).toBe(second.sheet2);
    expect(getComputedStyle(swatch).fontFamily).toBe('Alegreya, Georgia, serif');
  } finally {
    await view.unmount();
    const previous: [string, string | null][] = [
      ['style', oldStyle], ['data-scheme', oldScheme], ['data-theme', oldTheme],
    ];
    for (const [key, value] of previous) {
      if (value === null) root.removeAttribute(key);
      else root.setAttribute(key, value);
    }
  }
}, 30000);
