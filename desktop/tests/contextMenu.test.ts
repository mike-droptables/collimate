import type { BrowserWindow, MenuItemConstructorOptions } from 'electron';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildContextMenuTemplate,
  installContextMenu,
  type ContextMenuParamsLike,
} from '../src/main/contextMenu.js';

/** What Menu.buildFromTemplate was handed, and every popup's options. */
const menus = vi.hoisted(() => ({
  templates: [] as MenuItemConstructorOptions[][],
  popups: [] as unknown[],
}));

vi.mock('electron', () => ({
  Menu: {
    buildFromTemplate: (template: MenuItemConstructorOptions[]) => {
      menus.templates.push(template);
      return { popup: (options: unknown) => menus.popups.push(options) };
    },
  },
}));

/**
 * The native right-click menu. Electron draws none unless the shell builds
 * one, so Chromium's spelling squiggles had nothing behind them; what's
 * frozen here is which items a right-click gets (spelling first, then the
 * edit verbs as Chromium reports them, Copy alone over static text, and
 * no menu at all over nothing) and that each item acts on the webContents
 * that was right-clicked.
 */

const NO_FLAGS = { canCut: false, canCopy: false, canPaste: false, canSelectAll: false };

function params(overrides: Partial<ContextMenuParamsLike> = {}): ContextMenuParamsLike {
  return {
    misspelledWord: '',
    dictionarySuggestions: [],
    isEditable: false,
    selectionText: '',
    editFlags: NO_FLAGS,
    ...overrides,
  };
}

class FakeContents {
  replacements: string[] = [];
  dictionaryWords: string[] = [];
  readonly listeners = new Map<string, Array<(event: unknown, params: ContextMenuParamsLike) => void>>();
  readonly session = {
    addWordToSpellCheckerDictionary: (word: string): boolean => {
      this.dictionaryWords.push(word);
      return true;
    },
  };

  replaceMisspelling(text: string): void {
    this.replacements.push(text);
  }

  on(event: string, listener: (event: unknown, params: ContextMenuParamsLike) => void): this {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener]);
    return this;
  }

  off(event: string, listener: (event: unknown, params: ContextMenuParamsLike) => void): this {
    this.listeners.set(event, (this.listeners.get(event) ?? []).filter((entry) => entry !== listener));
    return this;
  }

  rightClick(value: ContextMenuParamsLike): void {
    for (const listener of [...(this.listeners.get('context-menu') ?? [])]) listener({}, value);
  }
}

class FakeWindow {
  readonly webContents = new FakeContents();
  closedListener: (() => void) | null = null;

  once(event: string, listener: () => void): this {
    if (event === 'closed') this.closedListener = listener;
    return this;
  }

  asWindow(): BrowserWindow {
    return this as unknown as BrowserWindow;
  }
}

/** A template's items as short tags: label, else role, else type. */
function shape(template: MenuItemConstructorOptions[]): string[] {
  return template.map((item) => item.label ?? item.role ?? item.type ?? '?');
}

function click(item: MenuItemConstructorOptions | undefined): void {
  item!.click!({} as never, undefined, {} as never);
}

beforeEach(() => {
  menus.templates.length = 0;
  menus.popups.length = 0;
});

describe('buildContextMenuTemplate', () => {
  it('offers the suggestions for a misspelled word, each replacing it in that webContents', () => {
    const contents = new FakeContents();
    const template = buildContextMenuTemplate(
      params({ misspelledWord: 'teh', dictionarySuggestions: ['the', 'tea'], isEditable: true }),
      contents,
    );
    expect(shape(template).slice(0, 5)).toEqual(['the', 'tea', 'separator', 'Add to dictionary', 'separator']);
    click(template[1]);
    expect(contents.replacements).toEqual(['tea']);
  });

  it('Add to dictionary adds the misspelled word to that webContents\' session', () => {
    const contents = new FakeContents();
    const template = buildContextMenuTemplate(
      params({ misspelledWord: 'collimate', dictionarySuggestions: ['collate'], isEditable: true }),
      contents,
    );
    click(template.find((item) => item.label === 'Add to dictionary'));
    expect(contents.dictionaryWords).toEqual(['collimate']);
    expect(contents.replacements).toEqual([]);
  });

  it('says so, disabled, when the dictionary has no suggestion', () => {
    const template = buildContextMenuTemplate(
      params({ misspelledWord: 'qzxv', isEditable: true }),
      new FakeContents(),
    );
    expect(template[0]).toEqual({ label: 'No suggestions', enabled: false });
    expect(shape(template).slice(1, 3)).toEqual(['separator', 'Add to dictionary']);
  });

  it('shows no spelling section when no misspelled word is under the cursor', () => {
    const template = buildContextMenuTemplate(
      params({ misspelledWord: '', dictionarySuggestions: ['the'], isEditable: true }),
      new FakeContents(),
    );
    expect(shape(template)).toEqual(['cut', 'copy', 'paste', 'separator', 'selectAll']);
  });

  it('gives an editable the edit roles, each enabled as Chromium reports it can', () => {
    const template = buildContextMenuTemplate(
      params({ isEditable: true, editFlags: { canCut: true, canCopy: true, canPaste: false, canSelectAll: true } }),
      new FakeContents(),
    );
    expect(template).toEqual([
      { role: 'cut', enabled: true },
      { role: 'copy', enabled: true },
      { role: 'paste', enabled: false },
      { type: 'separator' },
      { role: 'selectAll', enabled: true },
    ]);
  });

  it('gives a selection on static content just Copy', () => {
    const template = buildContextMenuTemplate(
      params({ selectionText: 'some words', editFlags: { ...NO_FLAGS, canCopy: true } }),
      new FakeContents(),
    );
    expect(template).toEqual([{ role: 'copy' }]);
  });

  it('has nothing to offer over static content with no selection', () => {
    expect(buildContextMenuTemplate(params(), new FakeContents())).toEqual([]);
    expect(buildContextMenuTemplate(params({ selectionText: '  \n' }), new FakeContents())).toEqual([]);
  });
});

describe('installContextMenu', () => {
  it('pops the menu at the cursor in the app window — no coordinates to scale', () => {
    const window = new FakeWindow();
    installContextMenu({ window: window.asWindow() });
    window.webContents.rightClick(params({ isEditable: true, editFlags: { ...NO_FLAGS, canPaste: true } }));
    expect(menus.templates).toHaveLength(1);
    expect(shape(menus.templates[0]!)).toEqual(['cut', 'copy', 'paste', 'separator', 'selectAll']);
    expect(menus.popups).toEqual([{ window }]);
  });

  it('builds no menu when there is nothing to offer', () => {
    const window = new FakeWindow();
    installContextMenu({ window: window.asWindow() });
    window.webContents.rightClick(params());
    expect(menus.templates).toEqual([]);
    expect(menus.popups).toEqual([]);
  });

  it('suggestions from the app window\'s menu act on the app window\'s webContents', () => {
    const window = new FakeWindow();
    installContextMenu({ window: window.asWindow() });
    window.webContents.rightClick(params({ misspelledWord: 'wrod', dictionarySuggestions: ['word'], isEditable: true }));
    click(menus.templates[0]![0]);
    expect(window.webContents.replacements).toEqual(['word']);
  });

  it('the disposer removes the listener, and is wired to the window closing', () => {
    const window = new FakeWindow();
    const dispose = installContextMenu({ window: window.asWindow() });
    expect(window.webContents.listeners.get('context-menu')).toHaveLength(1);
    expect(window.closedListener).toBe(dispose);
    dispose();
    dispose();
    expect(window.webContents.listeners.get('context-menu')).toEqual([]);
    window.webContents.rightClick(params({ isEditable: true }));
    expect(menus.templates).toEqual([]);
  });
});
