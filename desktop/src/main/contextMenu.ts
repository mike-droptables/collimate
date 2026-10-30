import { Menu, type BaseWindow, type BrowserWindow, type MenuItemConstructorOptions } from 'electron';

/**
 * The native right-click menu: spelling suggestions and the edit verbs.
 *
 * Chromium underlines misspellings by itself (spellcheck stays on in the
 * window's webPreferences), but Electron draws no context menu at all
 * unless the app builds one from a webContents' 'context-menu' event —
 * without this a right-click on an underlined word does nothing. The page's
 * own menus are untouched: a page that cancels the DOM `contextmenu` event
 * (the file tree, the tab strip, the overview) never raises 'context-menu'.
 *
 * Used for the app window (installContextMenu) and for every guest view
 * (guestViews.ts forwardEvents), one builder for both.
 */

/** The slice of Electron's ContextMenuParams the menu reads. */
export interface ContextMenuParamsLike {
  /**
   * The word under a spelling marker at the cursor, or ''. Blink fills it
   * only when a marker is there, so it is non-empty only where spellcheck
   * ran; the sibling `spellcheckEnabled` param is always false in
   * Electron's event and so is not read.
   */
  misspelledWord: string;
  dictionarySuggestions: string[];
  isEditable: boolean;
  selectionText: string;
  editFlags: { canCut: boolean; canCopy: boolean; canPaste: boolean; canSelectAll: boolean };
}

/** The slice of a webContents the menu's items act on. */
export interface ContextMenuTargetLike {
  replaceMisspelling(text: string): void;
  session: { addWordToSpellCheckerDictionary(word: string): boolean };
}

/** Shows a built template as a native menu. */
export type ContextMenuPopup = (template: MenuItemConstructorOptions[]) => void;

/**
 * The menu for one right-click, or [] when there is nothing to offer (and
 * so no menu: a right-click on the canvas background stays the camera's).
 *
 * Sections, separator-joined: spelling (a misspelled word under the
 * cursor) — its suggestions, or a disabled "No suggestions", then "Add to
 * dictionary"; the edit verbs on an editable, each enabled as Chromium
 * reports it can; or, on static content, Copy over a selection.
 */
export function buildContextMenuTemplate(
  params: ContextMenuParamsLike,
  contents: ContextMenuTargetLike,
): MenuItemConstructorOptions[] {
  const sections: MenuItemConstructorOptions[][] = [];
  const word = params.misspelledWord;
  if (word) {
    sections.push(
      params.dictionarySuggestions.length
        ? params.dictionarySuggestions.map((suggestion) => ({
            label: suggestion,
            click: () => contents.replaceMisspelling(suggestion),
          }))
        : [{ label: 'No suggestions', enabled: false }],
      [{ label: 'Add to dictionary', click: () => contents.session.addWordToSpellCheckerDictionary(word) }],
    );
  }
  if (params.isEditable) {
    const flags = params.editFlags;
    sections.push([
      { role: 'cut', enabled: flags.canCut },
      { role: 'copy', enabled: flags.canCopy },
      { role: 'paste', enabled: flags.canPaste },
      { type: 'separator' },
      { role: 'selectAll', enabled: flags.canSelectAll },
    ]);
  } else if (params.selectionText.trim()) {
    sections.push([{ role: 'copy' }]);
  }
  return sections.flatMap((section, index) => (index === 0 ? section : [{ type: 'separator' }, ...section]));
}

/** A 'context-menu' listener for `contents`: builds its menu and pops it when non-empty. */
export function contextMenuListener(
  contents: ContextMenuTargetLike,
  popup: ContextMenuPopup,
): (event: unknown, params: ContextMenuParamsLike) => void {
  return (_event, params) => {
    const template = buildContextMenuTemplate(params, contents);
    if (template.length) popup(template);
  };
}

/**
 * Pops a template in `window` at the cursor. No x/y: Electron places a
 * popup at the cursor by default, which is where a right-click menu
 * belongs, so none of the title bar's zoom math applies here.
 */
export function popupContextMenuIn(window: BaseWindow): ContextMenuPopup {
  return (template) => Menu.buildFromTemplate(template).popup({ window });
}

export interface ContextMenuOptions {
  /** The app window, whose own webContents gets the menu. */
  window: BrowserWindow;
}

/**
 * Give the app window its right-click menu.
 *
 * Returns a disposer, and calls it itself when the window closes — the
 * same lifecycle as installTitleBarControl, since the shell rebuilds the
 * app window after a server crash.
 */
export function installContextMenu(options: ContextMenuOptions): () => void {
  const { window } = options;
  const contents = window.webContents;
  const listener = contextMenuListener(contents, popupContextMenuIn(window));
  let disposed = false;
  contents.on('context-menu', listener);

  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    contents.off('context-menu', listener);
  };

  window.once('closed', dispose);
  return dispose;
}
