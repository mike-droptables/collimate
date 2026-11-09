import { expect, it } from 'vitest';
import { readThemeSnapshot } from '@/lib/themeSnapshot';
import { FIXTURE_BASIC_ID } from '@/test/rendererFixtures';

it('forwards every skin token verbatim by CSS name through the real iframe CSS __F2008 (S01)', async () => {
  const root = document.documentElement;
  const oldStyle = root.getAttribute('style');
  const oldScheme = root.getAttribute('data-scheme');
  const oldTheme = root.getAttribute('data-theme');
  const sheet2 = 'rgb(17, 34, 51)';
  const prose = '"Literata", Georgia, serif';
  const iframe = document.createElement('iframe');
  try {
    root.style.setProperty('--sheet-2', sheet2);
    root.style.setProperty('--font-prose', prose);
    root.setAttribute('data-scheme', 'light');
    root.setAttribute('data-theme', 'roundtrip-proof');
    const snapshot = readThemeSnapshot();
    expect(snapshot.vars['--sheet-2']).toBe(sheet2);
    expect(snapshot.fonts.prose).toBe(prose);
    iframe.src = `/sandbox.html?renderer=${FIXTURE_BASIC_ID}`;
    const loaded = new Promise<void>((resolve, reject) => {
      iframe.onload = () => resolve();
      iframe.onerror = () => reject(new Error('sandbox frame failed to load'));
    });
    document.body.appendChild(iframe);
    await loaded;
    iframe.contentWindow!.postMessage({ type: 'SET_THEME', ...snapshot }, location.origin);
    const child = iframe.contentDocument!.documentElement;
    await expect.poll(() => child.getAttribute('data-scheme')).toBe('light');
    expect(child.getAttribute('data-theme')).toBe('roundtrip-proof');
    const css = iframe.contentWindow!.getComputedStyle(child);
    expect({
      sheet2: css.getPropertyValue('--sheet-2').trim(),
      prose: css.getPropertyValue('--font-prose').trim(),
      scheme: css.getPropertyValue('color-scheme').trim(),
    }).toEqual({ sheet2, prose, scheme: 'light' });
  } finally {
    iframe.remove();
    const previous: [string, string | null][] = [
      ['style', oldStyle], ['data-scheme', oldScheme], ['data-theme', oldTheme],
    ];
    for (const [key, value] of previous) {
      if (value === null) root.removeAttribute(key);
      else root.setAttribute(key, value);
    }
  }
}, 30000);
