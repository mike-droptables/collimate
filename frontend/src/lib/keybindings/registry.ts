import { SHORTCUT_CONTEXT_ORDER, ShortcutEntry } from './types';
import { SHORTCUT_IDS } from './ids';

// Default shortcut registry. Adding a shortcut means:
//
//   1. a new const in ids.ts
//   2. a new entry here
//   3. a useShortcut() call in whichever component owns the action
//
// The registry is intentionally static — loaded once at module
// import, never mutated. User overrides live in the store and
// layer on top at resolve time.
//
// ─── Who gets a key: the modifier rule ─────────────────────────
// A shortcut whose combo carries Mod, Ctrl, Meta or Alt wins
// everywhere, inside a renderer and its text fields included, and
// consumes the key: the renderer never sees it (forwardedCombos.ts
// for frames and guests, the window capture listeners for native
// renderers). A bare key (`/`, Esc, the list keys, typing) stays with
// whatever has focus; the host acts on it only where nothing else
// took it.
//
// ─── Scope note: form-local Enter/Esc ──────────────────────────
// We deliberately do NOT register shortcuts for "submit this
// input" / "cancel this rename" / "dismiss this prompt bubble"
// type actions (EditableLabel, FileTreeNode rename, channel
// dialog, API key prompt bubble, …). Every form has its own
// submit shape and the value of remapping "press Enter to commit
// this one text field" is low while the surface area of getting
// it wrong (accidentally intercepting Enter in a chat box, or
// breaking IME composition) is high. Form-local Enter/Esc keeps
// its raw e.key handler in place — by design.
//
// Cross-cutting Enter/Esc *IS* registered: LIST_RUN_HIGHLIGHTED,
// LIST_CLOSE, GLOBAL_BACK. Those are app-level semantics, not form-level.

export const SHORTCUT_REGISTRY: Record<string, ShortcutEntry> = {
    [SHORTCUT_IDS.GLOBAL_OPEN_PALETTE]: {
        id: SHORTCUT_IDS.GLOBAL_OPEN_PALETTE,
        label: 'Toggle the Commands pane',
        description: 'Open or close the Commands pane: commands for the selected cell, its stack and the channel, and a jump to any cell on the canvas. Works from inside the pane too.',
        context: 'global',
        default: 'Mod+K',
    },
    [SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS]: {
        id: SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS,
        label: 'Open the Commands pane',
        description: 'Open the Commands pane (focus its query when it is open). A bare key: never fires while typing, never forwarded from a renderer.',
        context: 'global',
        default: '/',
    },
    [SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS]: {
        id: SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS,
        label: 'Open settings',
        description: 'Open Settings in the Commands pane.',
        context: 'global',
        // The Mac-standard "settings" shortcut. Explicit comma as
        // the key token — parser treats it as literal.
        default: 'Mod+,',
    },
    [SHORTCUT_IDS.GLOBAL_FIND_FILE]: {
        id: SHORTCUT_IDS.GLOBAL_FIND_FILE,
        label: 'Find file',
        description: 'Open the Files panel on the selected cell: search its files by name or glob, or browse its folders and create a file or folder in one; then open a file, open it with another renderer, see its facts, handler and recent changes, add a definition to Capabilities, duplicate, rename or delete it.',
        context: 'cell',
        default: 'Mod+P',
    },
    [SHORTCUT_IDS.EDITOR_SAVE]: {
        id: SHORTCUT_IDS.EDITOR_SAVE,
        label: 'Save file',
        description: 'Flush any pending auto-save and commit immediately.',
        context: 'editor',
        default: 'Mod+s',
    },

    // ── Global / scene extras ──────────────────────────────────
    [SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM]: {
        id: SHORTCUT_IDS.GLOBAL_TOGGLE_ZOOM,
        label: 'Toggle zoom',
        description: 'Toggle the zoomed-out stack overview.',
        context: 'global',
        default: 'Alt+a',
    },
    [SHORTCUT_IDS.GLOBAL_BACK]: {
        id: SHORTCUT_IDS.GLOBAL_BACK,
        label: 'Back / cancel',
        description: 'Cascade Esc, the first that applies: cancel a pending cell move, leave the selected cell\'s cell diff, release its renderer (the keyboard goes to the cell; a native renderer keeps a single Esc, two within 600ms release it), collapse the full-window view, step out of its version history (the scrubbed version first, then history), close the Commands pane, close the selected cell\'s panel, collapse a fanned-out stack, exit zoom, or reset focus. The semantic "go back one level" key; it never closes a tab.',
        context: 'global',
        default: 'Escape',
    },
    [SHORTCUT_IDS.SCENE_WIDTH_DEC]: {
        id: SHORTCUT_IDS.SCENE_WIDTH_DEC,
        label: 'Shrink cell width',
        description: 'Decrease the minimum cell width by 50px (100px on repeat).',
        context: 'global',
        default: 'Alt+-',
    },
    [SHORTCUT_IDS.SCENE_WIDTH_INC]: {
        id: SHORTCUT_IDS.SCENE_WIDTH_INC,
        label: 'Grow cell width',
        description: 'Increase the minimum cell width by 50px (100px on repeat).',
        context: 'global',
        default: 'Alt+=',
    },
    [SHORTCUT_IDS.SCENE_WIDTH_RESET]: {
        id: SHORTCUT_IDS.SCENE_WIDTH_RESET,
        label: 'Reset cell width',
        description: 'Reset the stack width to the channel\'s default (500px until one is saved; a maximise saves the width before it).',
        context: 'global',
        default: 'Alt+0',
    },
    [SHORTCUT_IDS.SCENE_FULLSCREEN]: {
        id: SHORTCUT_IDS.SCENE_FULLSCREEN,
        label: 'Fullscreen cell',
        description: 'Toggle the focused stack between full width and the channel\'s default width; maximising saves the width before it as that default.',
        context: 'global',
        default: 'Alt+f',
    },
    [SHORTCUT_IDS.SCENE_FAN_OUT]: {
        id: SHORTCUT_IDS.SCENE_FAN_OUT,
        label: 'Fan out stack',
        description: 'Show every cell of the focused stack side by side; press again (or Esc) to collapse. Only one stack is fanned out at a time, never Capabilities.',
        context: 'global',
        default: 'Alt+v',
    },
    [SHORTCUT_IDS.STACK_RUN]: {
        id: SHORTCUT_IDS.STACK_RUN,
        label: 'Run (focused stack)',
        description: 'Open the runners panel in the focused stack\'s front cell — also while runs are live there; not on Capabilities, where nothing runs.',
        context: 'cell',
        default: 'Alt+r',
    },
    [SHORTCUT_IDS.STACK_DUPLICATE]: {
        id: SHORTCUT_IDS.STACK_DUPLICATE,
        label: 'Duplicate cell to new stack',
        description: 'Duplicate the focused cell into a new stack beside it (live runs\' files stay behind).',
        context: 'cell',
        default: 'Alt+d',
    },
    [SHORTCUT_IDS.STACK_DUPLICATE_RUN]: {
        id: SHORTCUT_IDS.STACK_DUPLICATE_RUN,
        label: 'Duplicate cell + run options',
        description: 'Duplicate the focused cell into a new stack and open the runners panel on the copy.',
        context: 'cell',
        default: 'Alt+Shift+d',
    },
    [SHORTCUT_IDS.STACK_DUPLICATE_STACK]: {
        id: SHORTCUT_IDS.STACK_DUPLICATE_STACK,
        label: 'Duplicate stack',
        description: 'Copy the whole focused stack into a fresh stack to the right (not Capabilities: duplicate one of its cells).',
        context: 'cell',
        default: 'Alt+Shift+s',
    },
    [SHORTCUT_IDS.STACK_NEW_CELL]: {
        id: SHORTCUT_IDS.STACK_NEW_CELL,
        label: 'New cell',
        description: 'Create a new empty cell in its own stack, to the right of the current selection (appended on an empty channel).',
        // 'global', not 'cell': the empty-channel case has no selection to
        // scope to, and this is the only way to get the first cell.
        context: 'global',
        default: 'Alt+n',
    },
    [SHORTCUT_IDS.STACK_NEW_CELL_RUN]: {
        id: SHORTCUT_IDS.STACK_NEW_CELL_RUN,
        label: 'New cell + run options',
        description: 'Create a new empty cell and open the runners panel on it.',
        context: 'global',
        default: 'Alt+Shift+n',
    },
    [SHORTCUT_IDS.STACK_CENTER]: {
        id: SHORTCUT_IDS.STACK_CENTER,
        label: 'Center focused stack',
        description: 'Pan the canvas so the focused stack sits at the centerline.',
        context: 'cell',
        default: 'Alt+c',
    },
    [SHORTCUT_IDS.STACK_ALIGN_LEFT]: {
        id: SHORTCUT_IDS.STACK_ALIGN_LEFT,
        label: 'Align focused stack left',
        description: 'Pan the canvas so the focused stack\'s left edge sits at the left screen edge.',
        context: 'cell',
        default: 'Alt+[',
    },
    [SHORTCUT_IDS.STACK_ALIGN_RIGHT]: {
        id: SHORTCUT_IDS.STACK_ALIGN_RIGHT,
        label: 'Align focused stack right',
        description: 'Pan the canvas so the focused stack\'s right edge sits at the right screen edge.',
        context: 'cell',
        default: 'Alt+]',
    },
    [SHORTCUT_IDS.STACK_REMOVE]: {
        id: SHORTCUT_IDS.STACK_REMOVE,
        label: 'Remove focused stack',
        description: 'Remove a stack, or hide Capabilities to the top bar. A stack is blocked while a run is live in any of its cells.',
        context: 'cell',
        default: 'Alt+Shift+Backspace',
    },
    [SHORTCUT_IDS.CELL_REMOVE]: {
        id: SHORTCUT_IDS.CELL_REMOVE,
        label: 'Remove focused cell',
        description: 'Remove the cell; on Capabilities, hide the stack. The stack stays; only one cell is dropped. Blocked while a run is live in the cell.',
        context: 'cell',
        default: 'Alt+Backspace',
    },
    [SHORTCUT_IDS.CELL_EDIT_TITLE]: {
        id: SHORTCUT_IDS.CELL_EDIT_TITLE,
        label: 'Edit cell title',
        description: 'Toggle the focused cell\'s title editor. Press once to enter edit mode; press again to commit and leave.',
        context: 'cell',
        default: 'Alt+t',
    },
    [SHORTCUT_IDS.STACK_ROTATE_PREV]: {
        id: SHORTCUT_IDS.STACK_ROTATE_PREV,
        label: 'Show previous cell in stack',
        description: 'Bring the previous cell of the focused stack to the front. Stops at the first cell.',
        context: 'cell',
        default: 'Alt+ArrowUp',
    },
    [SHORTCUT_IDS.STACK_ROTATE_NEXT]: {
        id: SHORTCUT_IDS.STACK_ROTATE_NEXT,
        label: 'Show next cell in stack',
        description: 'Bring the next cell of the focused stack to the front. Stops at the last cell.',
        context: 'cell',
        default: 'Alt+ArrowDown',
    },
    [SHORTCUT_IDS.NAV_STACK_LEFT]: {
        id: SHORTCUT_IDS.NAV_STACK_LEFT,
        label: 'Select the cell to the left',
        description: 'Select the cell on the canvas to the left (a fan column counts as a cell), also in the overview and from the Commands pane.',
        context: 'global',
        default: 'Ctrl+ArrowLeft',
    },
    [SHORTCUT_IDS.NAV_STACK_RIGHT]: {
        id: SHORTCUT_IDS.NAV_STACK_RIGHT,
        label: 'Select the cell to the right',
        description: 'Select the cell on the canvas to the right (a fan column counts as a cell), also in the overview and from the Commands pane.',
        context: 'global',
        default: 'Ctrl+ArrowRight',
    },
    [SHORTCUT_IDS.NAV_CELL_MOVE_LEFT]: {
        id: SHORTCUT_IDS.NAV_CELL_MOVE_LEFT,
        label: 'Move focused cell left',
        description: 'Start/continue moving the focused cell one stack to the left. Release a required modifier to commit.',
        context: 'global',
        default: 'Ctrl+Shift+ArrowLeft',
    },
    [SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT]: {
        id: SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT,
        label: 'Move focused cell right',
        description: 'Start/continue moving the focused cell one stack to the right. Release a required modifier to commit.',
        context: 'global',
        default: 'Ctrl+Shift+ArrowRight',
    },

    // ── Lists ─────────────────────────────────────────────────
    // The highlighted-row keys of the Commands pane and the cell panels
    // (files, runners, handlers). Each list handles them only while it
    // holds the keyboard.
    [SHORTCUT_IDS.LIST_RUN_HIGHLIGHTED]: {
        id: SHORTCUT_IDS.LIST_RUN_HIGHLIGHTED,
        label: 'Run the highlighted row',
        description: 'Run the highlighted command in the Commands pane, or the primary action on the selected row of a cell panel.',
        context: 'list',
        default: 'Enter',
    },
    [SHORTCUT_IDS.LIST_CLOSE]: {
        id: SHORTCUT_IDS.LIST_CLOSE,
        label: 'Close the list',
        description: 'Close the Commands pane (the keyboard returns to the selected cell), or a cell panel (its confirm or step first).',
        context: 'list',
        default: 'Escape',
    },
    [SHORTCUT_IDS.LIST_NEXT]: {
        id: SHORTCUT_IDS.LIST_NEXT,
        label: 'Highlight the next row',
        description: 'Move the highlight down one row (the Commands pane wraps to the top).',
        context: 'list',
        default: 'ArrowDown',
    },
    [SHORTCUT_IDS.LIST_PREV]: {
        id: SHORTCUT_IDS.LIST_PREV,
        label: 'Highlight the previous row',
        description: 'Move the highlight up one row (the Commands pane wraps to the bottom).',
        context: 'list',
        default: 'ArrowUp',
    },
};

/** All registered shortcut entries, in a stable order grouped by context. */
export function allShortcuts(): ShortcutEntry[] {
    return Object.values(SHORTCUT_REGISTRY).sort((a, b) => {
        const ca = SHORTCUT_CONTEXT_ORDER.indexOf(a.context);
        const cb = SHORTCUT_CONTEXT_ORDER.indexOf(b.context);
        if (ca !== cb) return ca - cb;
        return a.label.localeCompare(b.label);
    });
}
