/**
 * ThemeProvider: the server's theme library and default theme (fetched,
 * re-read on app_settings_changed, saved with a debounced PUT that an echo
 * or a stale read never reverts), the channel's override (adopted from the
 * channel doc, saved to the channel it was adopted from, flushed across a
 * switch), the resolution between them (the channels page shows the
 * default), the boot cache, and the skin applied to <html>.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, act } from '@testing-library/react';

vi.mock('@/lib/api', () => ({
  putChannelSettings: vi.fn(() => Promise.resolve({})),
  fetchChannelSettings: vi.fn(),
  fetchAppSettings: vi.fn(() => new Promise(() => {})),
  putAppSettings: vi.fn(() => Promise.resolve({})),
}));

import {
  fetchAppSettings, putAppSettings, putChannelSettings, type AppSettings, type ChannelSettings, type ThemeLibrary,
} from '@/lib/api';
import { ThemeProvider, useDefaultThemeSurface, useThemeContext } from './ThemeContext';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { SCENE_CSS_VARS, THEME_CSS_VARS, getTheme, sceneCssVars, sceneGround, themeCssVars, DEFAULT_THEME_ID } from '@/lib/themes';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { isDesktop } from '@/lib/desktop';
import { stackFor } from '@/lib/fonts';
import { readBootCache, writeBootCache } from '@/lib/settingsCache';
import { reloadThemeLibrary } from '@/lib/themeLibraryEvents';
import { newUserTheme, type UserTheme } from '@/lib/userThemes';

type Ctx = ReturnType<typeof useThemeContext>;

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';

function Probe({ onCtx }: { onCtx: (ctx: Ctx) => void }) {
  onCtx(useThemeContext());
  return null;
}

function Page() {
  useDefaultThemeSurface();
  return null;
}

function doc(override: string | null = null): ChannelSettings {
  return { theme: { override } } as unknown as ChannelSettings;
}

function appSettings(theme: Partial<ThemeLibrary>): AppSettings {
  return { version: 1, kind: 'app-settings', theme: { defaultThemeId: null, userThemes: [], ...theme } };
}

const fetchMock = vi.mocked(fetchAppSettings);
const putAppMock = vi.mocked(putAppSettings);
const putChannelMock = vi.mocked(putChannelSettings);

/** Let resolved fetches land. */
const settle = () => act(async () => {});

function mountProvider(extra?: React.ReactNode) {
  let ctx!: Ctx;
  const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} />{extra}</ThemeProvider>);
  return { view, ctx: () => ctx };
}

function reset() {
  fetchMock.mockReset().mockImplementation(() => new Promise(() => {}));
  putAppMock.mockReset().mockResolvedValue(appSettings({}));
  putChannelMock.mockReset().mockResolvedValue({} as ChannelSettings);
  useChannelSettingsStore.setState({ doc: null, loadedFor: null });
  localStorage.clear();
  document.documentElement.removeAttribute('style');
}

describe('ThemeProvider library (server-wide)', () => {
  const dusk: UserTheme = { ...newUserTheme('night', 'Dusk'), id: 'user.dusk', createdAt: 1 };
  beforeEach(() => {
    vi.useFakeTimers();
    reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('adopts the library and the default from the server, never echoing a PUT back', async () => {
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'user.dusk', userThemes: [dusk] }));
    const { view, ctx } = mountProvider();
    await settle();
    expect(ctx().userThemes).toEqual([dusk]);
    expect(ctx().defaultThemeId).toBe('user.dusk');
    expect(ctx().themeId).toBe('user.dusk');
    expect(document.documentElement.getAttribute('data-theme')).toBe('user.dusk');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putAppMock).not.toHaveBeenCalled();
    expect(readBootCache().themeLibrary).toEqual({ defaultThemeId: 'user.dusk', userThemes: [dusk] });
    view.unmount();
  });

  it('saves the default and library edits with one debounced, section-wholesale PUT, never to the channel', () => {
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }); });
    act(() => { ctx().setDefaultThemeId('paper'); });
    let id = '';
    act(() => { id = ctx().createUserTheme('default'); });
    act(() => { vi.advanceTimersByTime(100); });
    expect(putAppMock).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(400); });
    expect(putAppMock).toHaveBeenCalledOnce();
    const section = putAppMock.mock.calls[0][0].theme!;
    expect(section.defaultThemeId).toBe(id);
    expect(section.userThemes).toEqual([expect.objectContaining({ id, baseThemeId: 'paper' })]);
    expect(putChannelMock).not.toHaveBeenCalled();
    // The boot cache holds what was saved, so a reload paints it first.
    expect(readBootCache().themeLibrary).toEqual({
      defaultThemeId: id, userThemes: [expect.objectContaining({ id, baseThemeId: 'paper' })],
    });
    view.unmount();
  });

  it("re-reads on app_settings_changed, but not while this window's save is pending or in flight", async () => {
    const { view, ctx } = mountProvider();
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'night' }));
    // A save is armed: a read now would revert it.
    act(() => { ctx().setDefaultThemeId('paper'); });
    act(() => { reloadThemeLibrary(); });
    await settle();
    expect(ctx().defaultThemeId).toBe('paper');
    // The save is in flight: still not.
    let land: (doc: AppSettings) => void = () => {};
    putAppMock.mockImplementationOnce(() => new Promise(resolve => { land = resolve; }));
    act(() => { vi.advanceTimersByTime(400); });
    act(() => { reloadThemeLibrary(); });
    await settle();
    expect(ctx().defaultThemeId).toBe('paper');
    // Landed, and another window's change arrives: adopted.
    await act(async () => { land(appSettings({ defaultThemeId: 'paper' })); });
    act(() => { reloadThemeLibrary(); });
    await settle();
    expect(ctx().defaultThemeId).toBe('night');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putAppMock).toHaveBeenCalledOnce();
    view.unmount();
  });

  it('drops a read that began before a save started', async () => {
    const { view, ctx } = mountProvider();
    let answer: (doc: AppSettings) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise(resolve => { answer = resolve; }));
    act(() => { reloadThemeLibrary(); });
    act(() => { ctx().setDefaultThemeId('paper'); });
    act(() => { vi.advanceTimersByTime(400); });
    await settle();
    await act(async () => { answer(appSettings({ defaultThemeId: 'night' })); });
    expect(ctx().defaultThemeId).toBe('paper');
    view.unmount();
  });

  it('keeps the boot paint when the server cannot be read', async () => {
    writeBootCache({ themeLibrary: { defaultThemeId: 'paper', userThemes: [] } });
    fetchMock.mockRejectedValue(new Error('offline'));
    const { view, ctx } = mountProvider();
    await settle();
    expect(ctx().themeId).toBe('paper');
    view.unmount();
  });
});

describe('ThemeProvider override (per channel)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('adopts the override from the channel doc and saves only {theme: {override}} to that channel', () => {
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc('night'), loadedFor: A }); });
    expect(ctx().overrideThemeId).toBe('night');
    expect(ctx().themeId).toBe('night');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).not.toHaveBeenCalled();   // adopting never echoes
    act(() => { ctx().setOverrideThemeId('paper'); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).toHaveBeenCalledExactlyOnceWith(A, { theme: { override: 'paper' } });
    act(() => { ctx().setOverrideThemeId(null); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).toHaveBeenLastCalledWith(A, { theme: { override: null } });
    expect(putAppMock).not.toHaveBeenCalled();
    view.unmount();
  });

  it('flushes a pending override to its own channel when another channel is adopted', () => {
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }); });
    act(() => { vi.advanceTimersByTime(1000); });
    act(() => { ctx().setOverrideThemeId('paper'); });
    // Switch before the 400 ms debounce fires.
    act(() => { vi.advanceTimersByTime(100); });
    act(() => { useChannelSettingsStore.setState({ doc: doc('grid'), loadedFor: B }); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock.mock.calls).toEqual([[A, { theme: { override: 'paper' } }]]);
    expect(ctx().overrideThemeId).toBe('grid');
    view.unmount();
  });

  it('a patch of another section (Set current) keeps an override chosen since the load (F0903)', () => {
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }); });
    act(() => { ctx().setOverrideThemeId('paper'); });
    act(() => { vi.advanceTimersByTime(1000); });   // saved to A
    putChannelMock.mockClear();
    // "Set current" in the width popup: the layout section is merged into
    // the load-time snapshot, whose theme section predates the choice.
    act(() => { useChannelSettingsStore.getState().patchDoc(A, { layout: { defaultStackWidth: 700 } }); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth).toBe(700);
    expect(ctx().overrideThemeId).toBe('paper');
    expect(putChannelMock).not.toHaveBeenCalled();
    view.unmount();
  });

  it('with no channel loaded an override goes to the boot cache only', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setOverrideThemeId('paper'); });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).not.toHaveBeenCalled();
    expect(readBootCache().theme).toEqual({ override: 'paper' });
    view.unmount();
  });
});

describe('ThemeProvider resolution', () => {
  const dusk: UserTheme = { ...newUserTheme('night', 'Dusk'), id: 'user.dusk', createdAt: 1 };
  beforeEach(() => {
    vi.useFakeTimers();
    reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the override over the default, and the default while it is null', async () => {
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'paper' }));
    const { view, ctx } = mountProvider();
    await settle();
    act(() => { useChannelSettingsStore.setState({ doc: doc('night'), loadedFor: A }); });
    expect(ctx().themeId).toBe('night');
    act(() => { ctx().setOverrideThemeId(null); });
    expect(ctx().themeId).toBe('paper');
    view.unmount();
  });

  it('an override naming a theme not in the library follows the default, and resolves once the library has it', async () => {
    let answer: (doc: AppSettings) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise(resolve => { answer = resolve; }));
    writeBootCache({ themeLibrary: { defaultThemeId: 'paper', userThemes: [] } });
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc('user.dusk'), loadedFor: A }); });
    expect(ctx().overrideThemeId).toBeNull();
    expect(ctx().themeId).toBe('paper');
    await act(async () => { answer(appSettings({ defaultThemeId: 'paper', userThemes: [dusk] })); });
    expect(ctx().overrideThemeId).toBe('user.dusk');
    expect(ctx().themeId).toBe('user.dusk');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).not.toHaveBeenCalled();
    view.unmount();
  });

  it('a default naming a theme not in the library is the built-in default', async () => {
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'user.gone' }));
    const { view, ctx } = mountProvider();
    await settle();
    expect(ctx().defaultThemeId).toBe(DEFAULT_THEME_ID);
    expect(ctx().themeId).toBe(DEFAULT_THEME_ID);
    view.unmount();
  });

  it('the channels page (useDefaultThemeSurface) shows the default while mounted and re-reads the library, swapping cleanly both ways', async () => {
    // The one-frame swap guard stays set while no frame runs.
    const frame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 0);
    const root = document.documentElement;
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'paper' }));
    let page = false;
    let ctx!: Ctx;
    const tree = () => (
      <ThemeProvider>
        <Probe onCtx={value => { ctx = value; }} />
        {page && <Page />}
      </ThemeProvider>
    );
    const view = render(tree());
    await settle();
    act(() => { useChannelSettingsStore.setState({ doc: doc('night'), loadedFor: A }); });
    expect(ctx.themeId).toBe('night');
    const reads = fetchMock.mock.calls.length;
    root.removeAttribute('data-theme-swapping');
    page = true;
    view.rerender(tree());
    expect(root.hasAttribute('data-theme-swapping')).toBe(true);
    await settle();
    expect(fetchMock.mock.calls.length).toBe(reads + 1);
    expect(ctx.themeId).toBe('paper');
    expect(document.documentElement.getAttribute('data-theme')).toBe('paper');
    // The override is still the channel's, shown again once the page closes.
    expect(ctx.overrideThemeId).toBe('night');
    root.removeAttribute('data-theme-swapping');
    page = false;
    view.rerender(tree());
    expect(root.hasAttribute('data-theme-swapping')).toBe(true);
    expect(ctx.themeId).toBe('night');
    view.unmount();
    frame.mockRestore();
    root.removeAttribute('data-theme-swapping');
  });

  it('useDefaultThemeSurface outside a provider changes nothing', () => {
    expect(() => render(<Page />).unmount()).not.toThrow();
  });
});

describe('ThemeProvider deletes and creates', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('deleting the default returns the default to its base (S02)', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setDefaultThemeId('night-cool'); });
    let id = '';
    act(() => { id = ctx().createUserTheme('default'); });
    act(() => { ctx().renameUserTheme(id, 'Dusk'); });
    expect(ctx().userThemes[0].label).toBe('Dusk');
    expect(ctx().defaultThemeId).toBe(id);
    act(() => { ctx().deleteUserTheme(id); });
    expect(ctx().defaultThemeId).toBe('night-cool');
    expect(ctx().userThemes).toEqual([]);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putAppMock).toHaveBeenLastCalledWith({ theme: { defaultThemeId: 'night-cool', userThemes: [] } });
    view.unmount();
  });

  it("deleting this channel's override has the channel follow the default", () => {
    const { view, ctx } = mountProvider();
    act(() => { useChannelSettingsStore.setState({ doc: doc('grid'), loadedFor: A }); });
    let id = '';
    act(() => { id = ctx().createUserTheme('override'); });
    expect(ctx().overrideThemeId).toBe(id);
    expect(ctx().userThemes[0]).toMatchObject({ id, baseThemeId: 'grid' });
    expect(ctx().defaultThemeId).toBe(DEFAULT_THEME_ID);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).toHaveBeenLastCalledWith(A, { theme: { override: id } });
    expect(putAppMock).toHaveBeenLastCalledWith({ theme: { defaultThemeId: DEFAULT_THEME_ID, userThemes: [expect.objectContaining({ id })] } });
    act(() => { ctx().deleteUserTheme(id); });
    expect(ctx().overrideThemeId).toBeNull();
    expect(ctx().themeId).toBe(DEFAULT_THEME_ID);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putChannelMock).toHaveBeenLastCalledWith(A, { theme: { override: null } });
    view.unmount();
  });

  it('"+" in override mode copies the theme the channel shows: the default when it follows it', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setDefaultThemeId('paper'); });
    act(() => { useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }); });
    let id = '';
    act(() => { id = ctx().createUserTheme('override'); });
    expect(ctx().userThemes[0]).toMatchObject({ id, baseThemeId: 'paper' });
    expect(ctx().defaultThemeId).toBe('paper');
    expect(ctx().themeId).toBe(id);
    view.unmount();
  });
});

it('dispatches the record once per change of id, scheme or token, never twice for the same record__F0966 (R01)', () => {
  reset();
  let ctx!: Ctx;
  const events: CustomEvent[] = [];
  const listener = (event: Event) => events.push(event as CustomEvent);
  window.addEventListener('collimate:theme-changed', listener);
  const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
  const initial = events.length;
  const before = events.at(-1)!.detail;
  act(() => { ctx.createUserTheme('default'); });
  // a copy of the active skin changes only the id: the frames need it (their data-theme mirrors it)
  expect(events).toHaveLength(initial + 1);
  expect(events.at(-1)!.detail.themeId).not.toBe(before.themeId);
  expect(events.at(-1)!.detail.vars).toEqual(before.vars);
  act(() => { ctx.setDefaultThemeId(ctx.themeId); });
  expect(events).toHaveLength(initial + 1);
  act(() => { ctx.setUserThemeVar(ctx.themeId, 'ink', '#123456'); });
  expect(events).toHaveLength(initial + 2);
  // The scene is the host's own: renderers are not re-sent the record.
  act(() => { ctx.setUserThemeScene(ctx.themeId, 'pattern', 'grid'); });
  expect(events).toHaveLength(initial + 2);
  view.unmount();
  window.removeEventListener('collimate:theme-changed', listener);
});

describe('ThemeProvider applies the skin', () => {
  beforeEach(() => {
    reset();
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
      const skin = getTheme(DEFAULT_THEME_ID).vars;
      expect(root.getAttribute('data-theme')).toBe(DEFAULT_THEME_ID);
      expect(root.getAttribute('data-scheme')).toBe('dark');
      expect(root.style.getPropertyValue('color-scheme')).toBe('dark');
      for (const [key, name] of Object.entries(THEME_CSS_VARS)) {
        expect(root.style.getPropertyValue(name), name).toBe(skin[key as keyof typeof skin]);
      }
      expect(events.at(-1)!.detail).toEqual({
        themeId: DEFAULT_THEME_ID, scheme: 'dark', known: true, vars: themeCssVars(skin),
        fonts: { ui: skin.fontUi, mono: skin.fontMono, prose: skin.fontProse },
      });

      act(() => { ctx.setDefaultThemeId('paper'); });
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
    const skin = getTheme(DEFAULT_THEME_ID).vars;
    expect(shell.setWindowTheme).toHaveBeenLastCalledWith({ background: skin.canvas, overlay: skin.sheet, symbol: skin.ink });
    act(() => { ctx.setDefaultThemeId('paper'); });
    const paper = getTheme('paper').vars;
    expect(shell.setWindowTheme).toHaveBeenLastCalledWith({ background: paper.canvas, overlay: paper.sheet, symbol: paper.ink });
    view.unmount();
  });

  it('paints the window in the scene\'s ground, which a skin may set apart from its canvas', () => {
    const { shell } = installDesktopShell();
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    let id = '';
    act(() => { id = ctx.createUserTheme('default'); });
    const calls = vi.mocked(shell.setWindowTheme!).mock.calls.length;
    act(() => { ctx.setUserThemeScene(id, 'pattern', 'dots'); });
    expect(vi.mocked(shell.setWindowTheme!).mock.calls).toHaveLength(calls);  // same colours, no call
    act(() => { ctx.setUserThemeScene(id, 'base', '#405060'); });
    const skin = getTheme(DEFAULT_THEME_ID).vars;
    expect(shell.setWindowTheme).toHaveBeenLastCalledWith({ background: '#405060', overlay: skin.sheet, symbol: skin.ink });
    view.unmount();
  });

  it('writes the scene on <html> under its own names, beside the tokens', () => {
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    const root = document.documentElement;
    for (const id of ['night', 'night-clear'] as const) {
      act(() => { ctx.setDefaultThemeId(id); });
      const { scene, vars } = getTheme(id);
      for (const [name, value] of Object.entries(sceneCssVars(scene, vars))) {
        expect(root.style.getPropertyValue(name), `${id} ${name}`).toBe(value);
      }
      expect(ctx.scene).toEqual(scene);
    }
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.edge)).toBe('2px');
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.ground)).toBe(sceneGround(getTheme('night-clear').scene, getTheme('night-clear').vars));
    view.unmount();
  });

  it('applies the skin in an older desktop shell without setWindowTheme (S05)', () => {
    const { shell } = installDesktopShell();
    delete (shell as { setWindowTheme?: unknown }).setWindowTheme;
    let ctx!: Ctx;
    const view = render(<ThemeProvider><Probe onCtx={value => { ctx = value; }} /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe(DEFAULT_THEME_ID);
    expect(() => act(() => { ctx.setDefaultThemeId('paper'); })).not.toThrow();
    expect(document.documentElement.getAttribute('data-theme')).toBe('paper');
    view.unmount();
  });

  it('applies the skin without a desktop shell (S05)', () => {
    expect(isDesktop()).toBe(false);
    const view = render(<ThemeProvider><Probe onCtx={() => {}} /></ThemeProvider>);
    expect(document.documentElement.getAttribute('data-theme')).toBe(DEFAULT_THEME_ID);
    view.unmount();
  });
});

describe('ThemeProvider user skins', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** The library section of the last PUT. */
  const savedLibrary = () => putAppMock.mock.calls.at(-1)![0].theme!;

  it('creates from a built-in with no overrides, and from a user skin as a copy (S02)', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setDefaultThemeId('paper'); });
    let first = '';
    act(() => { first = ctx().createUserTheme('default'); });
    expect(ctx().themeId).toBe(first);
    expect(ctx().userThemes[0]).toMatchObject({ label: 'My theme', baseThemeId: 'paper', scheme: 'light', vars: {} });
    act(() => { ctx().setUserThemeVar(first, 'ink', '#202020'); });
    let second = '';
    act(() => { second = ctx().createUserTheme('default'); });
    expect(ctx().themeId).toBe(second);
    expect(ctx().userThemes[1]).toMatchObject({ label: 'My theme 2', baseThemeId: 'paper', vars: { ink: '#202020' } });
    view.unmount();
  });

  it('rebases, overrides, resets and sets a font role, saving the library in the skin keys (S02)', () => {
    const { view, ctx } = mountProvider();
    let id = '';
    act(() => { id = ctx().createUserTheme('default'); });
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
    const section = savedLibrary();
    expect(section.defaultThemeId).toBe(id);
    expect(section.userThemes).toEqual([{
      id, label: 'My theme', scheme: 'dark', baseThemeId: 'paper', createdAt: expect.any(Number),
      vars: { sheet: '#334455', fontProse: stackFor('Lora') }, scene: {},
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
    scene: { pattern: 'dots', pitch: 13, image: 'data:image/png;base64,AAAA' },
  };

  it('normalises a stored user skin it adopts from the server: built-in base, its scheme, skin keys only (S02)', async () => {
    fetchMock.mockResolvedValue(appSettings({ defaultThemeId: 'user.stray', userThemes: [strayStored] as never }));
    const { view, ctx } = mountProvider();
    await settle();
    const root = document.documentElement;
    expect(root.getAttribute('data-scheme')).toBe('light');
    expect(root.style.getPropertyValue('color-scheme')).toBe('light');
    expect(root.style.getPropertyValue('--canvas')).toBe(getTheme('paper').vars.canvas);
    expect(root.style.getPropertyValue('--ink')).toBe('#111111');
    expect(ctx().userThemes[0]).toEqual({
      id: 'user.stray', label: 'Stray', baseThemeId: 'paper', scheme: 'light', createdAt: 1, vars: { ink: '#111111' },
      scene: { pattern: 'dots' },
    });

    act(() => { vi.advanceTimersByTime(1000); });
    expect(putAppMock).not.toHaveBeenCalled();   // adopting never echoes
    act(() => { ctx().setUserThemeVar('user.stray', 'sheet', '#fafafa'); });
    act(() => { vi.advanceTimersByTime(1000); });
    const section = savedLibrary();
    expect(section.userThemes[0].vars).toEqual({ ink: '#111111', sheet: '#fafafa' });
    expect(section.userThemes[0].baseThemeId).toBe('paper');
    view.unmount();
  });

  it('seeds the library and the override from the boot cache, normalised, before the first paint (S02)', () => {
    writeBootCache({
      themeLibrary: { defaultThemeId: 'paper', userThemes: [strayStored] as never },
      theme: { override: 'user.stray' },
    });
    const { view, ctx } = mountProvider();
    const root = document.documentElement;
    expect(ctx().defaultThemeId).toBe('paper');
    expect(ctx().overrideThemeId).toBe('user.stray');
    expect(ctx().themeId).toBe('user.stray');
    expect(root.getAttribute('data-scheme')).toBe('light');
    expect(root.style.getPropertyValue('--canvas')).toBe(getTheme('paper').vars.canvas);
    expect(ctx().userThemes[0].vars).toEqual({ ink: '#111111' });
    act(() => { vi.advanceTimersByTime(1000); });
    expect(putAppMock).not.toHaveBeenCalled();
    view.unmount();
  });

  it('an older boot cache shape reads as the defaults', () => {
    writeBootCache({ theme: { activeThemeId: 'user.stray', userThemes: [strayStored] } as never });
    const { view, ctx } = mountProvider();
    expect(ctx().themeId).toBe(DEFAULT_THEME_ID);
    expect(ctx().userThemes).toEqual([]);
    expect(ctx().overrideThemeId).toBeNull();
    view.unmount();
  });

  it('overrides and resets scene fields, saving them in the library', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setDefaultThemeId('grid'); });
    let id = '';
    act(() => { id = ctx().createUserTheme('default'); });
    act(() => { ctx().setUserThemeScene(id, 'pattern', 'scanlines'); });
    act(() => { ctx().setUserThemeScene(id, 'glowWarm', null); });
    act(() => { ctx().setUserThemeScene(id, 'edge', 2); });
    expect(ctx().scene).toEqual({ ...getTheme('grid').scene, pattern: 'scanlines', glowWarm: null, edge: 2 });
    const root = document.documentElement;
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.glowWarm)).toBe('transparent');
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.edge)).toBe('2px');

    act(() => { vi.advanceTimersByTime(1000); });
    expect(savedLibrary().userThemes[0].scene).toEqual({ pattern: 'scanlines', glowWarm: null, edge: 2 });

    act(() => { ctx().resetUserThemeScene(id, 'glowWarm'); });
    expect(ctx().userThemes[0].scene).toEqual({ pattern: 'scanlines', edge: 2 });
    expect(ctx().scene.glowWarm).toEqual(getTheme('grid').scene.glowWarm);
    view.unmount();
  });

  it('writes the chrome fills the scene may reach behind: the canvas, or transparent while it does', () => {
    const { view, ctx } = mountProvider();
    act(() => { ctx().setDefaultThemeId('grid'); });
    const root = document.documentElement;
    const canvas = getTheme('grid').vars.canvas;
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.topBarGround)).toBe(canvas);
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.removedGround)).toBe(canvas);
    let id = '';
    act(() => { id = ctx().createUserTheme('default'); });
    act(() => { ctx().setUserThemeScene(id, 'behindTopBar', true); });
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.topBarGround)).toBe('transparent');
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.removedGround)).toBe(canvas);
    act(() => { ctx().setUserThemeScene(id, 'behindRemoved', true); });
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.removedGround)).toBe('transparent');
    act(() => { ctx().resetUserThemeScene(id, 'behindTopBar'); });
    expect(root.style.getPropertyValue(SCENE_CSS_VARS.topBarGround)).toBe(canvas);
    view.unmount();
  });
});
