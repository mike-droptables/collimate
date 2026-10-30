/**
 * The Themes section (S02, CP-6): theme cards over the built-ins and the
 * channel's user themes, and the editor for the active user theme, in the
 * Commands pane's 320px column.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, fireEvent, within, cleanup, act } from '@testing-library/react';

vi.mock('@/lib/api', () => ({
  putChannelSettings: vi.fn(() => Promise.resolve({})),
  fetchChannelSettings: vi.fn(),
}));

import { putChannelSettings, type ChannelSettings } from '@/lib/api';
import { ThemeProvider } from '@/contexts/ThemeContext';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { getTheme, softVariant, THEME_VAR_KEYS, type ThemeVarKey } from '@/lib/themes';
import { stackFor } from '@/lib/fonts';
import { ThemeSection } from './ThemeSection';
import { expectButtonRowsWrap, expectFitsPaneColumn, expectStackedFields, renderInPaneColumn } from './paneColumn.testkit';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const root = () => document.documentElement;
const cssVar = (name: string) => root().style.getPropertyValue(name);

function mount() {
  const { body } = renderInPaneColumn(<ThemeProvider><ThemeSection /></ThemeProvider>);
  act(() => {
    useChannelSettingsStore.setState({
      doc: { theme: { activeThemeId: 'night', userThemes: [] } } as unknown as ChannelSettings,
      loadedFor: '11111111-1111-4111-8111-111111111111',
    });
  });
  return body;
}

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
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    localStorage.clear();
    root().removeAttribute('style');
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
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

  it('lists night, night-cool and paper then "+"; built-ins are read-only (S02)', () => {
    mount();
    const buttons = cardGrid().getAllByRole('button');
    expect(buttons.map(b => b.getAttribute('aria-label') ?? b.textContent)).toEqual([
      'Night', 'Night · cool', 'Paper', 'New theme',
    ]);
    expect(cardGrid().getByRole('button', { name: 'Night' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('region', { name: 'Theme editor' })).toBeNull();
    expect(screen.getByText(/built-in themes are read-only/i)).toBeInTheDocument();

    fireEvent.click(cardGrid().getByRole('button', { name: 'Paper' }));
    expect(root().getAttribute('data-scheme')).toBe('light');
    expect(cardGrid().getByRole('button', { name: 'Paper' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('region', { name: 'Theme editor' })).toBeNull();
  });

  it('"+" creates a user theme from the active one, selects it and opens the editor (S02)', () => {
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

  it('groups the token pickers and has no background, glow, grid or vignette controls (S02)', () => {
    mount();
    createTheme();
    const legends = screen.getAllByRole('group').map(g => g.querySelector('legend')?.textContent).filter(Boolean);
    expect(legends).toEqual(['Surfaces', 'Lines', 'Inks', 'Accents', 'Code', 'Radius', 'Fonts']);
    for (const label of ['Canvas', 'Sheet', 'Sheet 2', 'Sheet 3', 'Teal sheet', 'Line', 'Line 2', 'Ink', 'Ink 2',
      'Ink 3', 'Prose ink', 'Brass', 'Brass soft', 'Teal', 'Teal soft', 'Red', 'Red soft', 'Green', 'Code ground']) {
      expect(editor().getByLabelText(label, { selector: 'input[type="text"]' })).toBeInTheDocument();
    }
    expect(screen.queryByText(/background|glow|vignette|shadow|lift/i)).toBeNull();
    expect(screen.queryByLabelText(/grid/i)).toBeNull();
  });

  it('has a labelled colour picker for every skin colour token (S02)', () => {
    mount();
    createTheme();
    const notColours = new Set<ThemeVarKey>(['shadow', 'lift', 'fontUi', 'fontMono', 'fontProse', 'radius']);
    const colourKeys = THEME_VAR_KEYS.filter(k => !notColours.has(k));
    const fields = editor().getAllByRole('textbox')
      .filter((el): el is HTMLInputElement => el instanceof HTMLInputElement && el.name !== '');
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
    const section = vi.mocked(putChannelSettings).mock.calls.at(-1)![1].theme!;
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

  describe('Interface size (RS-05)', () => {
    afterEach(() => uninstallDesktopShell());

    const sizeRow = () => screen.queryByRole('group', { name: 'Interface size' });

    it('is hidden in a browser, where the browser zoom does the job (RS-05)', () => {
      mount();
      expect(sizeRow()).toBeNull();
      expect(screen.queryByText('This device')).toBeNull();
    });

    it('in the desktop shell shows relative sizes for this device and follows page zoom (RS-05)', () => {
      let scale = 1.1;
      const setInterfaceScale = vi.fn((f: number) => { scale = f; });
      const stub = installDesktopShell({ getInterfaceScale: () => scale, setInterfaceScale });
      stub.setZoomFactor(1.1);
      const body = mount();
      expectFitsPaneColumn(body);
      expect(sizeRow()).toHaveClass('flex-wrap');
      const row = within(sizeRow()!);
      expect(screen.getByText('This device')).toBeInTheDocument();
      expect(row.getAllByRole('button').map(b => b.textContent)).toEqual(['70%', '80%', '90%', '100%', '110%', '125%', '150%']);
      expect(row.getByRole('button', { name: '110%' })).toHaveAttribute('aria-pressed', 'true');
      fireEvent.click(row.getByRole('button', { name: '125%' }));
      expect(setInterfaceScale).toHaveBeenCalledExactlyOnceWith(1.25);
      expect(row.getByRole('button', { name: '125%' })).toHaveAttribute('aria-pressed', 'true');
      expect(row.getByRole('button', { name: '110%' })).toHaveAttribute('aria-pressed', 'false');
      // A change made elsewhere (the View menu) shows through the zoom signal.
      act(() => stub.setZoomFactor(0.9));
      expect(row.getByRole('button', { name: '90%' })).toHaveAttribute('aria-pressed', 'true');
      // The scale is per device: nothing reaches the channel settings.
      expect(putChannelSettings).not.toHaveBeenCalled();
    });
  });
});
