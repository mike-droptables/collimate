// Stable shortcut identifiers. Every handler, tooltip, and
// Settings row references a shortcut by its id; the physical key
// combo is resolved at run time through the store so user
// overrides carry through automatically.
//
// IDs are namespaced by context (the bucket the shortcut lives
// in). Keep them human-readable — they land in export files and
// in Settings row labels' debug output.

export const SHORTCUT_IDS = {
    // ── Global (main scene, always available) ──────────────────
    /** Toggle the Commands pane (Mod+K; also from inside the pane). The
     *  id predates the pane and is kept so per-channel overrides carry. */
    GLOBAL_OPEN_PALETTE: 'global.openPalette',
    /** Open the Commands pane with a bare key (`/`); never from a text
     *  field or a renderer. */
    GLOBAL_OPEN_COMMANDS: 'global.openCommands',
    GLOBAL_OPEN_SETTINGS: 'global.openSettings',
    /** Open the Files panel on the selected cell (Mod+P): search or browse
     *  its files, create one, and open, inform on, duplicate, rename or
     *  delete one. */
    GLOBAL_FIND_FILE: 'global.findFile',
    GLOBAL_TOGGLE_ZOOM: 'global.toggleZoom',
    GLOBAL_BACK: 'global.back',

    // Cell-width / fullscreen — main scene.
    SCENE_WIDTH_DEC: 'scene.widthDec',
    SCENE_WIDTH_INC: 'scene.widthInc',
    SCENE_WIDTH_RESET: 'scene.widthReset',
    SCENE_FULLSCREEN: 'scene.fullscreen',
    // Fan the focused stack out (every cell side by side, in place in the
    // main row) or collapse it. One fanned stack at a time.
    SCENE_FAN_OUT: 'scene.fanOut',

    // Stack-scope actions (main scene; a fan column resolves to its stack + cell).
    STACK_RUN: 'stack.run',
    STACK_DUPLICATE: 'stack.duplicate',
    STACK_DUPLICATE_RUN: 'stack.duplicateRun',
    STACK_DUPLICATE_STACK: 'stack.duplicateStack',
    STACK_NEW_CELL: 'stack.newCell',
    STACK_NEW_CELL_RUN: 'stack.newCellRun',
    STACK_CENTER: 'stack.center',
    STACK_ALIGN_LEFT: 'stack.alignLeft',
    STACK_ALIGN_RIGHT: 'stack.alignRight',
    STACK_REMOVE: 'stack.remove',
    STACK_ROTATE_PREV: 'stack.rotatePrev',
    STACK_ROTATE_NEXT: 'stack.rotateNext',

    // Cell-scope actions (operate on the selected cell within a stack).
    CELL_REMOVE: 'cell.remove',
    /** Toggle the focused cell's title editor. Press once to enter
     *  edit mode, press again to commit and leave. */
    CELL_EDIT_TITLE: 'cell.editTitle',

    // Spatial stack nav.
    NAV_STACK_LEFT: 'nav.stackLeft',
    NAV_STACK_RIGHT: 'nav.stackRight',
    NAV_CELL_MOVE_LEFT: 'nav.cellMoveLeft',
    NAV_CELL_MOVE_RIGHT: 'nav.cellMoveRight',

    // ── Lists (the Commands pane and the cell panels) ──────────
    LIST_RUN_HIGHLIGHTED: 'list.runHighlighted',
    LIST_CLOSE: 'list.close',
    LIST_NEXT: 'list.next',
    LIST_PREV: 'list.prev',

    // ── File editor overlay ─────────────────────────────────────
    EDITOR_SAVE: 'editor.save',
    EDITOR_CLOSE: 'editor.close',
} as const;
