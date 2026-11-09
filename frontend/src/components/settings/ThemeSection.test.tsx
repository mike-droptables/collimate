/**
 * The Themes section (S02, CP-6): theme cards over the built-ins and the
 * server's theme library, and the editor for the library theme on show, in
 * the Commands pane's column (renderInPaneColumn, PANE_COLUMN_PX wide).
 * 'default' mode (the channels page's panel) sets the server's default;
 * 'override' mode (the Commands pane) sets this channel's override, "Use the
 * default" first.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, fireEvent, within, cleanup, act } from '@testing-library/react';

vi.mock('@/lib/api', () => ({
  putChannelSettings: vi.fn(() => Promise.resolve({})),
  fetchChannelSettings: vi.fn(),
  fetchAppSettings: vi.fn(() => new Promise(() => {})),
  putAppSettings: vi.fn(() => Promise.resolve({})),
}));
vi.mock('@/lib/api/appSettings', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/appSettings')>()),
  uploadThemeImage: vi.fn(),
}));

import { putAppSettings, putChannelSettings, type ChannelSettings } from '@/lib/api';
import { uploadThemeImage } from '@/lib/api/appSettings';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { ChannelReadOnlyContext } from '@/contexts/ChannelReadOnlyContext';
import { writeBootCache } from '@/lib/settingsCache';
import { newUserTheme } from '@/lib/userThemes';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { SCENE_CSS_VARS, THEMES, getTheme, softVariant, THEME_VAR_KEYS, type ThemeVarKey } from '@/lib/themes';
import { stackFor } from '@/lib/fonts';
import { ThemeSection } from './ThemeSection';
import { expectButtonRowsWrap, expectFitsPaneColumn, expectStackedFields, renderInPaneColumn } from './paneColumn.testkit';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const root = () => document.documentElement;
const cssVar = (name: string) => root().style.getPropertyValue(name);

const CHANNEL = '11111111-1111-4111-8111-111111111111';

/** The section in `mode`, over a server whose default is night (the boot
 *  cache's library; the server read never lands) and a channel whose
 *  override is `override`. */
function mount(mode: 'default' | 'override' = 'default', { override = null as string | null, archived = false } = {}) {
  writeBootCache({ themeLibrary: { defaultThemeId: 'night', userThemes: [] } });
  const { body } = renderInPaneColumn(
    <ChannelReadOnlyContext.Provider value={archived}>
      <ThemeProvider><ThemeSection mode={mode} /></ThemeProvider>
    </ChannelReadOnlyContext.Provider>,
  );
  act(() => {
    useChannelSettingsStore.setState({
      doc: { theme: { override } } as unknown as ChannelSettings,
      loadedFor: CHANNEL,
    });
  });
  return body;
}

/** The library section of the last save. */
const savedLibrary = () => vi.mocked(putAppSettings).mock.calls.at(-1)![0].theme!;

function cardGrid() {
  return within(screen.getByRole('region', { name: 'Themes' }));
}

function editor() {
  return within(screen.getByRole('region', { name: 'Theme editor' }));
}

function createTheme() {
  fireEvent.click(cardGrid().getByRole('button', { name: /new theme/i }));
}

describe('ThemeSection', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(putChannelSettings).mockClear();
    vi.mocked(putAppSettings).mockClear();
    vi.mocked(uploadThemeImage).mockReset();
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    localStorage.clear();
    root().removeAttribute('style');
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('opening re-reads the server\'s library, in either mode, since this window may have missed another\'s edit', () => {
    for (const mode of ['default', 'override'] as const) {
      const heard = vi.fn();
      window.addEventListener('collimate:app-settings-changed', heard);
      mount(mode);
      expect(heard).toHaveBeenCalledOnce();
      window.removeEventListener('collimate:app-settings-changed', heard);
      cleanup();
    }
  });

  it('fits the pane\'s column: two cards a row, the editor\'s fields, tokens and fonts in one column (CP-6)', () => {
    const body = mount();
    expect(screen.queryByRole('heading', { level: 3 })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Theme' })).toBeNull();
    expect(cardGrid().getByRole('button', { name: 'Night' }).parentElement).toHaveClass('grid', 'grid-cols-2', 'gap-2');
    createTheme();
    expectFitsPaneColumn(body);
    expectButtonRowsWrap(body);
    // Name and base sit under their labels, full width; the token rows keep
    // their label beside the pickers, so the stacked check skips them.
    const tokenField = (field: Element) => field.getAttribute('name') !== null;
    expect(expectStackedFields(body, tokenField)).toBe(5); // name, base, three font roles
    expect(editor().getByLabelText('Name').parentElement!.parentElement).toHaveClass('flex', 'flex-col');
    // The token rows of a group stand in one column.
    for (const group of ['Surfaces', 'Lines', 'Inks', 'Accents', 'Code']) {
      const rows = editor().getByRole('group', { name: group }).querySelector('div.flex-col');
      expect(rows, group).not.toBeNull();
      expect(rows!.children.length, group).toBeGreaterThan(0);
    }
    // A font role: its label, the select under it, the sample under that.
    const prose = editor().getByLabelText('Prose');
    const row = prose.parentElement!;
    expect(row).toHaveClass('flex', 'flex-col');
    expect([...row.children].map(c => c.tagName)).toEqual(['LABEL', 'SELECT', 'SPAN']);
    expect(row.lastElementChild).toHaveTextContent('The quick brown fox');
    // The segment rows wrap.
    const segments = (name: string) => editor().getAllByRole('group', { name }).find(g => g.tagName === 'DIV');
    expect(segments('Radius')).toHaveClass('flex-wrap');
    expect(editor().getByRole('group', { name: 'Scheme' })).toHaveClass('flex-wrap');
  });

  it('lists every built-in then "+"; built-ins are read-only (S02)', () => {
    mount();
    const buttons = cardGrid().getAllByRole('button');
    expect(buttons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual([
      ...THEMES.map(t => t.label), 'New theme',
    ]);
    expect(cardGrid().getByRole('button', { name: 'Night' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('region', { name: 'Theme editor' })).toBeNull();
    expect(screen.getByText(/built-in themes are read-only/i)).toBeInTheDocument();

    fireEvent.click(cardGrid().getByRole('button', { name: 'Paper' }));
    expect(root().getAttribute('data-scheme')).toBe('light');
    expect(cardGrid().getByRole('button', { name: 'Paper' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('region', { name: 'Theme editor' })).toBeNull();
  });

  it('"+" creates a library theme from the selected one, selects it and opens the editor (S02)', () => {
    mount();
    fireEvent.click(cardGrid().getByRole('button', { name: 'Paper' }));
    createTheme();
    const card = cardGrid().getByRole('button', { name: 'My theme' });
    expect(card).toHaveAttribute('aria-pressed', 'true');
    expect(card).toHaveTextContent('Paper · light');
    expect(editor().getByLabelText('Base theme')).toHaveValue('paper');
    expect(editor().getByRole('button', { name: 'Light' })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.change(editor().getByLabelText('Name'), { target: { value: 'Dusk' } });
    expect(cardGrid().getByRole('button', { name: 'Dusk' })).toBeInTheDocument();
  });

  it('groups the token pickers, then the background, radius, selection edge and fonts (S02)', () => {
    mount();
    createTheme();
    const legends = screen.getAllByRole('group').map(g => g.querySelector('legend')?.textContent).filter(Boolean);
    expect(legends).toEqual(['Surfaces', 'Lines', 'Inks', 'Accents', 'Code', 'Background', 'Radius', 'Selection edge', 'Fonts']);
    for (const label of ['Canvas', 'Sheet', 'Sheet 2', 'Sheet 3', 'Teal sheet', 'Line', 'Line 2', 'Ink', 'Ink 2',
      'Ink 3', 'Prose ink', 'Brass', 'Brass soft', 'Teal', 'Teal soft', 'Red', 'Red soft', 'Green', 'Code ground']) {
      expect(editor().getByLabelText(label, { selector: 'input[type="text"]' })).toBeInTheDocument();
    }
    // Shadow and lift always come from the base.
    expect(screen.queryByText(/shadow|lift/i)).toBeNull();
  });

  it('the background group edits the scene field by field, and each resets to the base', () => {
    mount();
    fireEvent.click(cardGrid().getByRole('button', { name: getTheme('grid').label }));
    createTheme();
    const base = getTheme('grid').scene;
    const background = within(editor().getByRole('group', { name: 'Background' }));
    expect(background.getByRole('button', { name: 'Grid' })).toHaveAttribute('aria-pressed', 'true');
    expect(background.getByRole('button', { name: '16 / 80' })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(background.getByRole('button', { name: 'Dots' }));
    fireEvent.click(background.getByRole('button', { name: '24 / 120' }));
    fireEvent.change(background.getByLabelText('Strength'), { target: { value: '0.5' } });
    fireEvent.change(background.getByLabelText('Vignette'), { target: { value: '0' } });
    fireEvent.input(background.getByLabelText('Ground picker'), { target: { value: '#203040' } });
    const ink = background.getByLabelText('Pattern ink', { selector: 'input[type="text"]' });
    fireEvent.focus(ink);
    fireEvent.change(ink, { target: { value: 'red' } });  // a scene colour is #rrggbb only
    fireEvent.change(ink, { target: { value: '#a0b0c0' } });
    fireEvent.blur(ink);
    fireEvent.click(background.getByLabelText('Warm glow'));
    expect(background.getByLabelText('Warm glow strength')).toBeDisabled();
    fireEvent.change(background.getByLabelText('Cool glow strength'), { target: { value: '0.25' } });

    expect(cssVar(SCENE_CSS_VARS.ground)).toBe('#203040');
    expect(cssVar(SCENE_CSS_VARS.ink)).toBe('#a0b0c080');
    expect(cssVar(SCENE_CSS_VARS.glowWarm)).toBe('transparent');
    expect(cssVar(SCENE_CSS_VARS.vignette)).toBe('#00000000');

    act(() => { vi.advanceTimersByTime(1000); });
    const section = savedLibrary();
    expect(section.userThemes[0].scene).toEqual({
      pattern: 'dots', pitch: 24, strength: 0.5, vignette: 0, base: '#203040', ink: '#a0b0c0',
      glowWarm: null, glowCool: { ...base.glowCool, alpha: 0.25 },
    });

    fireEvent.click(background.getByRole('button', { name: 'Reset Ground' }));
    expect(cssVar(SCENE_CSS_VARS.ground)).toBe(base.base);
    fireEvent.click(background.getByRole('button', { name: 'Reset Warm glow' }));
    expect(background.getByLabelText('Warm glow')).toBeChecked();
    expect(background.queryByRole('button', { name: 'Reset Warm glow' })).toBeNull();
  });

  it('the two reach switches let the scene run on behind the top bar and the removed shelf, each resetting to the base', () => {
    mount();
    createTheme();  // on night, which reaches behind no chrome
    const background = within(editor().getByRole('group', { name: 'Background' }));
    const topBar = background.getByRole('switch', { name: 'Behind the top bar' });
    const removed = background.getByRole('switch', { name: 'Behind the removed shelf' });
    const canvas = getTheme('night').vars.canvas;
    expect(topBar).toHaveAttribute('aria-checked', 'false');
    expect(removed).toHaveAttribute('aria-checked', 'false');
    expect(cssVar(SCENE_CSS_VARS.topBarGround)).toBe(canvas);
    expect(cssVar(SCENE_CSS_VARS.removedGround)).toBe(canvas);

    fireEvent.click(topBar);
    expect(topBar).toHaveAttribute('aria-checked', 'true');
    expect(cssVar(SCENE_CSS_VARS.topBarGround)).toBe('transparent');
    expect(cssVar(SCENE_CSS_VARS.removedGround)).toBe(canvas);
    fireEvent.click(removed);
    expect(cssVar(SCENE_CSS_VARS.removedGround)).toBe('transparent');

    act(() => { vi.advanceTimersByTime(1000); });
    const section = savedLibrary();
    expect(section.userThemes[0].scene).toEqual({ behindTopBar: true, behindRemoved: true });

    fireEvent.click(background.getByRole('button', { name: 'Reset Behind the top bar' }));
    expect(topBar).toHaveAttribute('aria-checked', 'false');
    expect(cssVar(SCENE_CSS_VARS.topBarGround)).toBe(canvas);
    expect(background.queryByRole('button', { name: 'Reset Behind the top bar' })).toBeNull();
    expect(removed).toHaveAttribute('aria-checked', 'true');
  });

  it('a picture of the user\'s own covers the ground: an upload sets it, Remove clears it, Reset follows the base', async () => {
    const body = mount();
    createTheme();
    const background = within(editor().getByRole('group', { name: 'Background' }));
    const input = background.getByLabelText('Choose image') as HTMLInputElement;
    expect(input).toHaveAttribute('accept', 'image/*');
    expect(cssVar(SCENE_CSS_VARS.image)).toBe('none');
    expect(background.getByRole('button', { name: 'Choose…' })).toBeInTheDocument();

    // Not an image: refused before any upload.
    fireEvent.change(input, { target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] } });
    expect(background.getByRole('alert')).toHaveTextContent('Not an image');
    expect(uploadThemeImage).not.toHaveBeenCalled();

    const hash = 'e'.repeat(64);
    vi.mocked(uploadThemeImage).mockResolvedValueOnce({ blob_hash: hash, size: 3, encoding: 'binary' } as never);
    const picture = new File([new Uint8Array([1, 2, 3])], 'ground.png', { type: 'image/png' });
    await act(async () => { fireEvent.change(input, { target: { files: [picture] } }); });
    expect(uploadThemeImage).toHaveBeenCalledWith(picture);
    expect(cssVar(SCENE_CSS_VARS.image)).toMatch(new RegExp(`^url\\(".*/api/app-settings/images/${hash}"\\)$`));
    expect(background.queryByRole('alert')).toBeNull();
    expect((editor().getByRole('group', { name: 'Background' }).querySelector('[data-scene-image]') as HTMLImageElement).src)
      .toContain(`/api/app-settings/images/${hash}`);
    expect(background.getByRole('button', { name: 'Change…' })).toBeInTheDocument();
    // Change… and Remove share a wrapping group between the thumbnail and the
    // reset, so the pane's column wraps Remove rather than widening the card
    // (the browser test lays it out).
    const actions = background.getByRole('button', { name: 'Change…' }).parentElement!;
    expect(actions).toBe(background.getByRole('button', { name: 'Remove' }).parentElement);
    expect(actions).toHaveClass('flex', 'flex-wrap', 'min-w-0', 'flex-1');
    expectFitsPaneColumn(body);
    expectButtonRowsWrap(body);
    act(() => { vi.advanceTimersByTime(1000); });
    expect(savedLibrary().userThemes[0].scene).toEqual({ image: hash });
    // The active user skin's card previews it too.
    const card = cardGrid().getAllByRole('button', { pressed: true }).find(b => b.querySelector('[data-scene-preview]'))!;
    expect((card.querySelector('[data-scene-preview]') as HTMLElement).style.backgroundImage).toContain(`/api/app-settings/images/${hash}`);

    fireEvent.click(background.getByRole('button', { name: 'Remove' }));
    expect(cssVar(SCENE_CSS_VARS.image)).toBe('none');
    act(() => { vi.advanceTimersByTime(1000); });
    expect(savedLibrary().userThemes[0].scene).toEqual({ image: null });

    fireEvent.click(background.getByRole('button', { name: 'Reset Image' }));
    expect(background.queryByRole('button', { name: 'Reset Image' })).toBeNull();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(savedLibrary().userThemes[0].scene).toEqual({});
  });

  it('switching on a glow its base lacks starts from the pattern ink', () => {
    mount();
    createTheme();  // on night, which has no glows
    const background = within(editor().getByRole('group', { name: 'Background' }));
    expect(background.getByLabelText('Cool glow')).not.toBeChecked();
    fireEvent.click(background.getByLabelText('Cool glow'));
    expect(cssVar(SCENE_CSS_VARS.glowCool)).toBe(`${getTheme('night').scene.ink}33`);
  });

  it('the selection edge sets the selected card\'s edge width', () => {
    mount();
    createTheme();
    const edge = within(editor().getAllByRole('group', { name: 'Selection edge' }).find(g => g.tagName === 'DIV')!);
    expect(edge.getByRole('button', { name: '1px' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(edge.getByRole('button', { name: '2px' }));
    expect(cssVar(SCENE_CSS_VARS.edge)).toBe('2px');
    expect(edge.getByRole('button', { name: '2px' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('a skin card previews its scene: the ground and the pattern', () => {
    mount();
    const preview = (label: string) =>
      cardGrid().getByRole('button', { name: label }).querySelector('[data-scene-preview]') as HTMLElement;
    for (const theme of THEMES) expect(preview(theme.label).dataset.scenePreview, theme.id).toBe(theme.scene.pattern);
    expect(preview(getTheme('night').label).style.backgroundImage).toBe('');
    expect(preview(getTheme('grid').label).style.backgroundImage).toMatch(/linear-gradient/);
  });

  it('shows a built-in\'s interface face from another role in the Interface select', () => {
    mount();
    const mono = THEMES.find(t => t.vars.fontUi === t.vars.fontMono || /monospace$/.test(t.vars.fontUi));
    expect(mono, 'a built-in with a mono interface').toBeDefined();
    fireEvent.click(cardGrid().getByRole('button', { name: mono!.label }));
    createTheme();
    const ui = editor().getByLabelText('Interface') as HTMLSelectElement;
    expect(stackFor(ui.value)).toBe(mono!.vars.fontUi);
    expect(within(ui).queryByText('Choose a face')).toBeNull();
  });

  it('has a labelled colour picker for every skin colour token (S02)', () => {
    mount();
    createTheme();
    const notColours = new Set<ThemeVarKey>(['shadow', 'lift', 'fontUi', 'fontMono', 'fontProse', 'radius']);
    const colourKeys = THEME_VAR_KEYS.filter(k => !notColours.has(k));
    const fields = editor().getAllByRole('textbox')
      .filter((el): el is HTMLInputElement => el instanceof HTMLInputElement && el.name !== '' && !el.name.startsWith('scene.'));
    expect(fields.map(f => f.name).sort()).toEqual([...colourKeys].sort());
    for (const field of fields) {
      const label = field.labels?.[0]?.textContent ?? '';
      expect(label).not.toBe('');
      expect(editor().getByLabelText(`${label} picker`)).toHaveAttribute('type', 'color');
    }
  });

  it('a teal sheet override repaints Capabilities heads (S02)', () => {
    mount();
    createTheme();
    fireEvent.input(editor().getByLabelText('Teal sheet picker'), { target: { value: '#e8f0ec' } });
    expect(cssVar('--teal-sheet')).toBe('#e8f0ec');
  });

  it('editing a token repaints the app, persists it and resets to the base (S02)', () => {
    mount();
    createTheme();
    const sheet = editor().getByLabelText('Sheet', { selector: 'input[type="text"]' });
    fireEvent.focus(sheet);
    fireEvent.change(sheet, { target: { value: '#20304' } });  // not a colour yet
    expect(cssVar('--sheet')).toBe(getTheme('night').vars.sheet);
    fireEvent.change(sheet, { target: { value: '#203040' } });
    expect(cssVar('--sheet')).toBe('#203040');
    fireEvent.blur(sheet);

    fireEvent.input(editor().getByLabelText('Canvas picker'), { target: { value: '#101820' } });
    expect(cssVar('--canvas')).toBe('#101820');

    act(() => { vi.advanceTimersByTime(1000); });
    const section = savedLibrary();
    expect(section.userThemes[0].vars).toEqual({ sheet: '#203040', canvas: '#101820' });
    expect(section.userThemes[0]).toMatchObject({ baseThemeId: 'night', scheme: 'dark' });

    fireEvent.click(editor().getByRole('button', { name: 'Reset Sheet' }));
    expect(cssVar('--sheet')).toBe(getTheme('night').vars.sheet);
    expect(editor().queryByRole('button', { name: 'Reset Sheet' })).toBeNull();
  });

  it('a soft accent follows its accent at the base alpha until it is set (S02)', () => {
    mount();
    createTheme();
    const night = getTheme('night').vars;
    fireEvent.input(editor().getByLabelText('Brass picker'), { target: { value: '#204060' } });
    const brassSoft = editor().getByLabelText('Brass soft', { selector: 'input[type="text"]' });
    expect(brassSoft).toHaveValue(softVariant('#204060', night.brassSoft));
    expect(cssVar('--brass-soft')).toBe('rgba(32, 64, 96, 0.16)');
    expect(editor().getByTitle('Follows its accent')).toBeInTheDocument();

    fireEvent.input(editor().getByLabelText('Brass soft picker'), { target: { value: '#ff0000' } });
    expect(cssVar('--brass-soft')).toBe('rgba(255, 0, 0, 0.16)');
    fireEvent.focus(brassSoft);
    fireEvent.change(brassSoft, { target: { value: 'rgba(255, 0, 0, 0.3)' } });
    expect(cssVar('--brass-soft')).toBe('rgba(255, 0, 0, 0.3)');
    fireEvent.click(editor().getByRole('button', { name: 'Reset Brass soft' }));
    expect(cssVar('--brass-soft')).toBe('rgba(32, 64, 96, 0.16)');
  });

  it('base, scheme, radius and the three font roles (S02)', () => {
    mount();
    createTheme();
    fireEvent.change(editor().getByLabelText('Base theme'), { target: { value: 'paper' } });
    expect(root().getAttribute('data-scheme')).toBe('light');
    expect(cssVar('--canvas')).toBe(getTheme('paper').vars.canvas);
    fireEvent.click(editor().getByRole('button', { name: 'Dark' }));
    expect(root().getAttribute('data-scheme')).toBe('dark');

    fireEvent.click(editor().getByRole('button', { name: '12px' }));
    expect(cssVar('--radius')).toBe('12px');
    expect(editor().getByRole('button', { name: '12px' })).toHaveAttribute('aria-pressed', 'true');

    const prose = editor().getByLabelText('Prose');
    expect(prose).toHaveValue('Lora');
    expect(within(prose).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Alegreya', 'Source Serif 4', 'Literata', 'Lora', 'Crimson Pro',
        'Libron', 'Sourcerer', 'Cartisse', 'Charis SIL', 'Garamond', 'Bitter', 'Palatium']);
    fireEvent.change(prose, { target: { value: 'Literata' } });
    expect(cssVar('--font-prose')).toBe(stackFor('Literata'));
    fireEvent.change(editor().getByLabelText('Interface'), { target: { value: 'Inter' } });
    expect(cssVar('--font-ui')).toBe(stackFor('Inter'));
    fireEvent.change(editor().getByLabelText('Code', { selector: 'select' }), { target: { value: 'Fira Code' } });
    expect(cssVar('--font-mono')).toBe(stackFor('Fira Code'));
  });

  it('a new theme reads Lora for prose and JetBrains Mono for code as its defaults on every base (polish, RS-05)', () => {
    mount();
    createTheme();
    for (const base of ['night', 'night-cool', 'paper']) {
      fireEvent.change(editor().getByLabelText('Base theme'), { target: { value: base } });
      expect(editor().getByLabelText('Prose'), base).toHaveValue('Lora');
      expect(editor().getByLabelText('Code', { selector: 'select' }), base).toHaveValue('JetBrains Mono');
      expect(cssVar('--font-prose'), base).toBe(stackFor('Lora'));
      expect(cssVar('--font-mono'), base).toBe(stackFor('JetBrains Mono'));
    }
  });

  it('offers the ebook-fonts core families by role, labelled without the NV prefix (S03)', () => {
    mount();
    createTheme();
    const options = (label: string, selector = 'select') =>
      within(editor().getByLabelText(label, { selector })).getAllByRole('option')
        .map(o => [o.getAttribute('value'), o.textContent]);
    expect(options('Interface').slice(-2)).toEqual([['NV Jost', 'Jost'], ['NV Legible Next', 'Legible Next']]);
    expect(options('Prose').slice(-7)).toEqual([
      ['Libron', 'Libron'], ['Sourcerer', 'Sourcerer'], ['Cartisse', 'Cartisse'], ['Charis SIL', 'Charis SIL'],
      ['NV Garamond', 'Garamond'], ['NV Bitter', 'Bitter'], ['NV Palatium', 'Palatium'],
    ]);
    expect(options('Code').map(([value]) => value))
      .toEqual(['IBM Plex Mono', 'JetBrains Mono', 'Source Code Pro', 'Fira Code', 'Geist Mono']);

    fireEvent.change(editor().getByLabelText('Prose'), { target: { value: 'NV Garamond' } });
    expect(cssVar('--font-prose')).toBe('"NV Garamond", Georgia, "Times New Roman", serif');
    expect(editor().getByLabelText('Prose')).toHaveValue('NV Garamond');
    fireEvent.change(editor().getByLabelText('Interface'), { target: { value: 'NV Legible Next' } });
    expect(cssVar('--font-ui')).toBe(stackFor('NV Legible Next'));
  });

  it('deletes a user theme after the inline confirmation (S02)', () => {
    mount();
    fireEvent.click(cardGrid().getByRole('button', { name: 'Night · cool' }));
    createTheme();
    fireEvent.click(editor().getByRole('button', { name: 'Delete theme' }));
    fireEvent.click(editor().getByRole('button', { name: 'Keep' }));
    expect(cardGrid().getByRole('button', { name: 'My theme' })).toBeInTheDocument();

    fireEvent.click(editor().getByRole('button', { name: 'Delete theme' }));
    fireEvent.click(editor().getByRole('button', { name: 'Delete' }));
    expect(cardGrid().queryByRole('button', { name: 'My theme' })).toBeNull();
    expect(cardGrid().getByRole('button', { name: 'Night · cool' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('region', { name: 'Theme editor' })).toBeNull();
  });

  describe("'default' mode", () => {
    it('selects the server default, saves a choice to the library, never to the channel, and says who shares it', () => {
      mount('default', { override: 'grid' });
      expect(cardGrid().queryByRole('button', { name: 'Use the default' })).toBeNull();
      expect(cardGrid().getByRole('button', { name: 'Night' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByText(
        'This page and every channel without an override use the default. Themes are shared by every channel on this server.',
      )).toBeInTheDocument();
      fireEvent.click(cardGrid().getByRole('button', { name: 'Paper' }));
      expect(cardGrid().getByRole('button', { name: 'Paper' })).toHaveAttribute('aria-pressed', 'true');
      act(() => { vi.advanceTimersByTime(1000); });
      expect(putAppSettings).toHaveBeenLastCalledWith({ theme: { defaultThemeId: 'paper', userThemes: [] } });
      expect(putChannelSettings).not.toHaveBeenCalled();
    });

    it('"+" adds a library theme and makes it the default', () => {
      mount('default');
      createTheme();
      act(() => { vi.advanceTimersByTime(1000); });
      const section = savedLibrary();
      expect(section.userThemes).toHaveLength(1);
      expect(section.defaultThemeId).toBe(section.userThemes[0].id);
      expect(putChannelSettings).not.toHaveBeenCalled();
      expect(screen.queryByText(/changes it for every channel that uses it/)).toBeNull();
    });
  });

  describe("'override' mode", () => {
    it('shows "Use the default" first, selected while the channel follows the default, previewing it', () => {
      mount('override');
      const buttons = cardGrid().getAllByRole('button');
      expect(buttons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual([
        'Use the default', ...THEMES.map(t => t.label), 'New theme',
      ]);
      const useDefault = cardGrid().getByRole('button', { name: 'Use the default' });
      expect(useDefault).toHaveAttribute('aria-pressed', 'true');
      expect(useDefault).toHaveTextContent('Default · Night');
      expect(cardGrid().getByRole('button', { name: 'Night' })).toHaveAttribute('aria-pressed', 'false');
      expect(screen.queryByText(/This page and every channel without an override/)).toBeNull();
    });

    it("a card sets this channel's override and \"Use the default\" clears it, saved to the channel only", () => {
      mount('override');
      fireEvent.click(cardGrid().getByRole('button', { name: 'Paper' }));
      expect(cardGrid().getByRole('button', { name: 'Paper' })).toHaveAttribute('aria-pressed', 'true');
      expect(cardGrid().getByRole('button', { name: 'Use the default' })).toHaveAttribute('aria-pressed', 'false');
      expect(root().getAttribute('data-theme')).toBe('paper');
      act(() => { vi.advanceTimersByTime(1000); });
      expect(putChannelSettings).toHaveBeenLastCalledWith(CHANNEL, { theme: { override: 'paper' } });
      fireEvent.click(cardGrid().getByRole('button', { name: 'Use the default' }));
      expect(root().getAttribute('data-theme')).toBe('night');
      act(() => { vi.advanceTimersByTime(1000); });
      expect(putChannelSettings).toHaveBeenLastCalledWith(CHANNEL, { theme: { override: null } });
      expect(putAppSettings).not.toHaveBeenCalled();
    });

    it('"+" adds a library theme from the channel\'s theme and makes it the override, editing it shared', () => {
      mount('override', { override: 'paper' });
      createTheme();
      const card = cardGrid().getByRole('button', { name: 'My theme' });
      expect(card).toHaveAttribute('aria-pressed', 'true');
      expect(card).toHaveTextContent('Paper · light');
      expect(screen.getByText('Editing My theme changes it for every channel that uses it.')).toBeInTheDocument();
      act(() => { vi.advanceTimersByTime(1000); });
      const id = savedLibrary().userThemes[0].id;
      expect(savedLibrary().defaultThemeId).toBe('night');
      expect(putChannelSettings).toHaveBeenLastCalledWith(CHANNEL, { theme: { override: id } });
    });

    it('opens the editor for a library default the channel follows, with the shared-edit line', () => {
      const dusk = { ...newUserTheme('night', 'Dusk'), id: 'user.dusk' as const };
      writeBootCache({ themeLibrary: { defaultThemeId: 'user.dusk', userThemes: [dusk] } });
      renderInPaneColumn(<ThemeProvider><ThemeSection mode="override" /></ThemeProvider>);
      expect(cardGrid().getByRole('button', { name: 'Use the default' })).toHaveTextContent('Default · Dusk');
      expect(editor().getByLabelText('Name')).toHaveValue('Dusk');
      expect(screen.getByText('Editing Dusk changes it for every channel that uses it.')).toBeInTheDocument();
    });

    it('in an archived channel the cards are disabled and say why; the library stays editable', () => {
      const dusk = { ...newUserTheme('night', 'Dusk'), id: 'user.dusk' as const };
      writeBootCache({ themeLibrary: { defaultThemeId: 'user.dusk', userThemes: [dusk] } });
      renderInPaneColumn(
        <ChannelReadOnlyContext.Provider value>
          <ThemeProvider><ThemeSection mode="override" /></ThemeProvider>
        </ChannelReadOnlyContext.Provider>,
      );
      for (const button of cardGrid().getAllByRole('button')) expect(button).toBeDisabled();
      expect(screen.getByText('This channel is archived: its theme cannot change.')).toBeInTheDocument();
      fireEvent.change(editor().getByLabelText('Name'), { target: { value: 'Dawn' } });
      act(() => { vi.advanceTimersByTime(1000); });
      expect(savedLibrary().userThemes[0].label).toBe('Dawn');
    });

    const setDefault = () => screen.queryByTestId('theme-set-default');

    it('"Set default to this" shows only while the override names a theme other than the default', () => {
      mount('override');
      expect(setDefault()).toBeNull();
      cleanup();
      mount('override', { override: 'night' });
      expect(setDefault()).toBeNull();
      cleanup();
      mount('override', { override: 'paper' });
      expect(setDefault()).toHaveTextContent('Set default to this');
      expect(setDefault()).toBeEnabled();
      expect(screen.getByText('This channel and every channel without an override will use Paper.')).toBeInTheDocument();
    });

    it('"Set default to this" makes the override the server default and clears it, with no change of look', () => {
      mount('override', { override: 'paper' });
      act(() => { vi.advanceTimersByTime(1000); });
      vi.mocked(putChannelSettings).mockClear();
      expect(root().getAttribute('data-theme')).toBe('paper');
      const repainted = vi.fn();
      window.addEventListener('collimate:theme-changed', repainted);
      fireEvent.click(setDefault()!);
      window.removeEventListener('collimate:theme-changed', repainted);
      expect(repainted).not.toHaveBeenCalled();
      expect(root().getAttribute('data-theme')).toBe('paper');
      const useDefault = cardGrid().getByRole('button', { name: 'Use the default' });
      expect(useDefault).toHaveAttribute('aria-pressed', 'true');
      expect(useDefault).toHaveTextContent('Default · Paper');
      expect(cardGrid().getByRole('button', { name: 'Paper' })).toHaveAttribute('aria-pressed', 'false');
      expect(setDefault()).toBeNull();
      act(() => { vi.advanceTimersByTime(1000); });
      expect(putAppSettings).toHaveBeenCalledOnce();
      expect(putAppSettings).toHaveBeenLastCalledWith({ theme: { defaultThemeId: 'paper', userThemes: [] } });
      expect(putChannelSettings).toHaveBeenCalledOnce();
      expect(putChannelSettings).toHaveBeenLastCalledWith(CHANNEL, { theme: { override: null } });
    });

    it('"Set default to this" names a library theme by its label', () => {
      const dusk = { ...newUserTheme('night', 'Dusk'), id: 'user.dusk' as const };
      writeBootCache({ themeLibrary: { defaultThemeId: 'night', userThemes: [dusk] } });
      renderInPaneColumn(<ThemeProvider><ThemeSection mode="override" /></ThemeProvider>);
      act(() => {
        useChannelSettingsStore.setState({
          doc: { theme: { override: 'user.dusk' } } as unknown as ChannelSettings,
          loadedFor: CHANNEL,
        });
      });
      expect(screen.getByText('This channel and every channel without an override will use Dusk.')).toBeInTheDocument();
      fireEvent.click(setDefault()!);
      expect(cardGrid().getByRole('button', { name: 'Use the default' })).toHaveTextContent('Default · Dusk');
      act(() => { vi.advanceTimersByTime(1000); });
      expect(savedLibrary().defaultThemeId).toBe('user.dusk');
      expect(putChannelSettings).toHaveBeenLastCalledWith(CHANNEL, { theme: { override: null } });
    });

    it('"Set default to this" is disabled in an archived channel and absent in \'default\' mode', () => {
      mount('override', { override: 'paper', archived: true });
      expect(setDefault()).toBeDisabled();
      cleanup();
      mount('default', { override: 'paper' });
      expect(setDefault()).toBeNull();
    });

    it("the default mode's cards stay enabled on an archived channel's page", () => {
      mount('default', { archived: true });
      for (const button of cardGrid().getAllByRole('button')) expect(button).toBeEnabled();
    });
  });

  describe('Interface size (RS-05)', () => {
    afterEach(() => uninstallDesktopShell());

    it('is not here even in the desktop shell (RS-05)', () => {
      installDesktopShell({ getInterfaceScale: () => 1, setInterfaceScale: vi.fn() });
      mount();
      expect(screen.queryByRole('group', { name: 'Interface size' })).toBeNull();
      expect(document.querySelector('[data-interface-size]')).toBeNull();
    });
  });
});
