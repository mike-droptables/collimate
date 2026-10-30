/**
 * Theme persistence across a channel switch: an edit still inside the
 * persist debounce when another channel's settings doc is adopted is saved
 * to the channel it was made in, not dropped with the cancelled timer.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, act } from '@testing-library/react';

vi.mock('@/lib/api', () => ({
  putChannelSettings: vi.fn(() => Promise.resolve({})),
  fetchChannelSettings: vi.fn(),
}));

import { putChannelSettings, type ChannelSettings } from '@/lib/api';
import { ThemeProvider, useThemeContext } from './ThemeContext';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { THEME_CSS_VARS, getTheme, themeCssVars } from '@/lib/themes';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { isDesktop } from '@/lib/desktop';
import { stackFor } from '@/lib/fonts';
import { writeBootCacheSection } from '@/lib/settingsCache';

type Ctx = ReturnType<typeof useThemeContext>;

function Probe({ onCtx }: { onCtx: (ctx: Ctx) => void }) {
  onCtx(useThemeContext());
  return null;
}

function doc(): ChannelSettings {
  return { theme: { activeThemeId: null, userThemes: [] } } as unknown as ChannelSettings;
}

describe('ThemeProvider persistence', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(putChannelSettings).mockClear();
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('flushes a pending edit to its own channel when another channel is adopted', () => {
    let ctx: Ctx | null = null;
    render(<ThemeProvider><Probe onCtx={c => { ctx = c; }} /></ThemeProvider>);

    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }); });
    act(() => { vi.advanceTimersByTime(1000); });
    vi.mocked(putChannelSettings).mockClear();

    act(() => { ctx!.createUserTheme(); });
    // Switch before the 400 ms debounce fires.
    act(() => { vi.advanceTimersByTime(100); });
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: '22222222-2222-4222-8222-222222222222' }); });
    act(() => { vi.advanceTimersByTime(1000); });

    const calls = vi.mocked(putChannelSettings).mock.calls;
    expect(calls.map(c => c[0])).toEqual(['11111111-1111-4111-8111-111111111111']);
    expect(calls[0][1].theme?.userThemes).toHaveLength(1);
  });

  it("a patch of another section (Set current) keeps theme edits made since the load (F0903)", () => {
    let ctx: Ctx | null = null;
    render(<ThemeProvider><Probe onCtx={c => { ctx = c; }} /></ThemeProvider>);

    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }); });
    act(() => { vi.advanceTimersByTime(1000); });

    let created = '';
    act(() => { created = ctx!.createUserTheme(); });
    act(() => { vi.advanceTimersByTime(1000); });   // the edit is saved to A
    vi.mocked(putChannelSettings).mockClear();

    // "Set current" in the width popup: the layout section is merged into
    // the load-time snapshot, whose theme section predates the edit.
    act(() => {
      useChannelSettingsStore.getState().patchDoc('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 700 } });
    });
    act(() => { vi.advanceTimersByTime(1000); });

    expect(useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth).toBe(700);
    expect(ctx!.themeId).toBe(created);
    expect(ctx!.userThemes).toHaveLength(1);
    // Nothing re-adopted, so nothing re-saved either.
    expect(putChannelSettings).not.toHaveBeenCalled();

    // Any new doc identity carrying the same theme section adopts nothing.
    act(() => {
      const prev = useChannelSettingsStore.getState().doc!;
      useChannelSettingsStore.setState({ doc: { ...prev } });
    });
    expect(ctx!.userThemes).toHaveLength(1);
  });
});

it('dispatches the record once per change of id, scheme or token, never twice for the same record__F0966 (R01)', () => {
  let ctx!: Ctx;
  const events: CustomEvent[] = [];
  const listener = (event: Event) => events.push(event as CustomEvent);
  window.addEventListener('collimate:theme-changed', listener);
  const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
  const initial = events.length;
  const before = events.at(-1)!.detail;
  act(() => { ctx.createUserTheme(); });
  // a copy of the active skin changes only the id: the frames need it (their data-theme mirrors it)
  expect(events).toHaveLength(initial + 1);
  expect(events.at(-1)!.detail.themeId).not.toBe(before.themeId);
  expect(events.at(-1)!.detail.vars).toEqual(before.vars);
  act(() => { ctx.setThemeId(ctx.themeId); });
  expect(events).toHaveLength(initial + 1);
  act(() => { ctx.setUserThemeVar(ctx.themeId, 'ink', '#123456'); });
  expect(events).toHaveLength(initial + 2);
  view.unmount();
  window.removeEventListener('collimate:theme-changed', listener);
});

describe('ThemeProvider applies the skin', () => {
  beforeEach(() => {
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    localStorage.clear();  // the boot cache, written by the tests above
    document.documentElement.removeAttribute('style');
  });
  afterEach(() => {
    uninstallDesktopShell();
  });

  it('writes every token on <html> and dispatches the full record (S01)', () => {
    const events: CustomEvent[] = [];
    const listener = (event: Event) => events.push(event as CustomEvent);
    window.addEventListener('collimate:theme-changed', listener);
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    try {
      const root = document.documentElement;
      const night = getTheme('night').vars;
      expect(root.getAttribute('data-theme')).toBe('night');
      expect(root.getAttribute('data-scheme')).toBe('dark');
      expect(root.style.getPropertyValue('color-scheme')).toBe('dark');
      for (const [key, name] of Object.entries(THEME_CSS_VARS)) {
        expect(root.style.getPropertyValue(name), name).toBe(night[key as keyof typeof night]);
      }
      expect(events.at(-1)!.detail).toEqual({
        themeId: 'night', scheme: 'dark', known: true, vars: themeCssVars(night),
        fonts: { ui: night.fontUi, mono: night.fontMono, prose: night.fontProse },
      });

      act(() => { ctx.setThemeId('paper'); });
      expect(root.getAttribute('data-theme')).toBe('paper');
      expect(root.getAttribute('data-scheme')).toBe('light');
      expect(root.style.getPropertyValue('--sheet-2')).toBe(getTheme('paper').vars.sheet2);
      expect(events.at(-1)!.detail.vars['--sheet-2']).toBe(getTheme('paper').vars.sheet2);
      expect(events.at(-1)!.detail.themeId).toBe('paper');
    } finally {
      view.unmount();
      window.removeEventListener('collimate:theme-changed', listener);
    }
  });

  it('repaints the desktop window in the skin on every change (S05)', () => {
    const { shell } = installDesktopShell();
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    const night = getTheme('night').vars;
    expect(shell.setWindowTheme).toHaveBeenLastCalledWith({ background: night.canvas, overlay: night.sheet, symbol: night.ink });
    act(() => { ctx.setThemeId('paper'); });
    const paper = getTheme('paper').vars;
    expect(shell.setWindowTheme).toHaveBeenLastCalledWith({ background: paper.canvas, overlay: paper.sheet, symbol: paper.ink });
    view.unmount();
  });

  it('applies the skin in an older desktop shell without setWindowTheme (S05)', () => {
    const { shell } = installDesktopShell();
    delete (shell as { setWindowTheme?: unknown }).setWindowTheme;
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe('night');
    expect(() => act(() => { ctx.setThemeId('paper'); })).not.toThrow();
    expect(document.documentElement.getAttribute('data-theme')).toBe('paper');
    view.unmount();
  });

  it('applies the skin without a desktop shell (S05)', () => {
    expect(isDesktop()).toBe(false);
    const view = render(<ThemeProvider><Probe onCtx={() => {}} /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe('night');
    view.unmount();
  });
});

describe('ThemeProvider user skins', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(putChannelSettings).mockClear();
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    localStorage.clear();
    document.documentElement.removeAttribute('style');
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function mount() {
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }); });
    act(() => { vi.advanceTimersByTime(1000); });
    vi.mocked(putChannelSettings).mockClear();
    return { view, ctx: () => ctx };
  }

  it('creates from a built-in with no overrides, and from a user skin as a copy (S02)', () => {
    const { view, ctx } = mount();
    act(() => { ctx().setThemeId('paper'); });
    let first = '';
    act(() => { first = ctx().createUserTheme(); });
    expect(ctx().themeId).toBe(first);
    expect(ctx().userThemes[0]).toMatchObject({ label: 'My theme', baseThemeId: 'paper', scheme: 'light', vars: {} });
    act(() => { ctx().setUserThemeVar(first, 'ink', '#202020'); });
    let second = '';
    act(() => { second = ctx().createUserTheme(); });
    expect(ctx().themeId).toBe(second);
    expect(ctx().userThemes[1]).toMatchObject({ label: 'My theme 2', baseThemeId: 'paper', vars: { ink: '#202020' } });
    view.unmount();
  });

  it('rebases, overrides, resets and sets a font role, persisting the section in the skin keys (S02)', () => {
    const { view, ctx } = mount();
    let id = '';
    act(() => { id = ctx().createUserTheme(); });
    act(() => { ctx().setUserThemeVar(id, 'sheet', '#334455'); });
    act(() => { ctx().setUserThemeBase(id, 'paper'); });
    const root = document.documentElement;
    expect(root.getAttribute('data-scheme')).toBe('light');
    expect(root.style.getPropertyValue('--sheet')).toBe('#334455');
    expect(root.style.getPropertyValue('--canvas')).toBe(getTheme('paper').vars.canvas);
    act(() => { ctx().setUserThemeScheme(id, 'dark'); });
    expect(root.getAttribute('data-scheme')).toBe('dark');
    act(() => { ctx().setUserThemeFont(id, 'prose', 'Lora'); });
    expect(root.style.getPropertyValue('--font-prose')).toBe(stackFor('Lora'));
    act(() => { ctx().setUserThemeFont(id, 'prose', 'Not a catalogue face'); });
    expect(root.style.getPropertyValue('--font-prose')).toBe(stackFor('Lora'));

    act(() => { vi.advanceTimersByTime(1000); });
    const section = vi.mocked(putChannelSettings).mock.calls.at(-1)![1].theme!;
    expect(section.activeThemeId).toBe(id);
    expect(section.userThemes).toEqual([{
      id, label: 'My theme', scheme: 'dark', baseThemeId: 'paper', createdAt: expect.any(Number),
      vars: { sheet: '#334455', fontProse: stackFor('Lora') },
    }]);

    act(() => { ctx().resetUserThemeVar(id, 'sheet'); });
    expect(root.style.getPropertyValue('--sheet')).toBe(getTheme('paper').vars.sheet);
    expect(ctx().userThemes[0].vars).toEqual({ fontProse: stackFor('Lora') });
    view.unmount();
  });

  // A user skin stored with a base that is no longer a built-in and keys
  // outside the skin vocabulary.
  const strayStored = {
    id: 'user.stray', label: 'Stray', baseThemeId: 'gone-light', scheme: 'light', createdAt: 1,
    vars: { surface: '#000', backdrop: 'data:image/png;base64,AAAA', ink: '#111111', radius: 8 },
  };

  it('normalises a stored user skin it adopts: built-in base, its scheme, skin keys only (S02)', () => {
    const { view, ctx } = mount();
    const stored = { theme: { activeThemeId: 'user.stray', userThemes: [strayStored] } } as unknown as ChannelSettings;
    act(() => { useChannelSettingsStore.setState({ doc: stored, loadedFor: '11111111-1111-4111-8111-111111111111' }); });
    const root = document.documentElement;
    expect(root.getAttribute('data-scheme')).toBe('light');
    expect(root.style.getPropertyValue('color-scheme')).toBe('light');
    expect(root.style.getPropertyValue('--canvas')).toBe(getTheme('paper').vars.canvas);
    expect(root.style.getPropertyValue('--ink')).toBe('#111111');
    expect(ctx().userThemes[0]).toEqual({
      id: 'user.stray', label: 'Stray', baseThemeId: 'paper', scheme: 'light', createdAt: 1, vars: { ink: '#111111' },
    });

    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelSettings).not.toHaveBeenCalled();   // adopting never echoes
    act(() => { ctx().setUserThemeVar('user.stray', 'sheet', '#fafafa'); });
    act(() => { vi.advanceTimersByTime(1000); });
    const section = vi.mocked(putChannelSettings).mock.calls.at(-1)![1].theme!;
    expect(section.userThemes[0].vars).toEqual({ ink: '#111111', sheet: '#fafafa' });
    expect(section.userThemes[0].baseThemeId).toBe('paper');
    view.unmount();
  });

  it('normalises the user skins in the boot cache before the first paint (S02)', () => {
    writeBootCacheSection('theme', { activeThemeId: 'user.stray', userThemes: [strayStored] } as never);
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    const root = document.documentElement;
    expect(ctx.themeId).toBe('user.stray');
    expect(root.getAttribute('data-scheme')).toBe('light');
    expect(root.style.getPropertyValue('--canvas')).toBe(getTheme('paper').vars.canvas);
    expect(ctx.userThemes[0].vars).toEqual({ ink: '#111111' });
    view.unmount();
  });

  it('deleting the active user skin returns to its base (S02)', () => {
    const { view, ctx } = mount();
    act(() => { ctx().setThemeId('night-cool'); });
    let id = '';
    act(() => { id = ctx().createUserTheme(); });
    act(() => { ctx().renameUserTheme(id, 'Dusk'); });
    expect(ctx().userThemes[0].label).toBe('Dusk');
    act(() => { ctx().deleteUserTheme(id); });
    expect(ctx().themeId).toBe('night-cool');
    expect(ctx().userThemes).toEqual([]);
    view.unmount();
  });
});
