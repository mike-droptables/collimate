/** The shared control vocabulary: every form, panel and manage view renders
 *  its buttons, fields, tabs and cards from these, on the skin tokens, so one
 *  surface never drifts from the next. The looks are the Night Workbench
 *  mock's (`qbtn`, `rp-go`, `rp-q`, `.tab`, `kbd.k`). */

/** The quiet bordered button's frame, shared by every button variant. */
const control = 'inline-flex items-center justify-center gap-1.5 h-[1.625rem] px-2.5 rounded-sm border border-line-2 text-[0.78125rem] whitespace-nowrap transition-colors disabled:opacity-45 disabled:cursor-default';

/** The quiet bordered button (the mock's `qbtn`). */
export const btn = `${control} text-ink-2 hover:bg-sheet-2 hover:text-ink`;
/** The panel's primary action (the mock's `rp-go`): a raised sheet with an
 *  inset edge; its icon takes brass. */
export const btnPrimary = `${control} shadow-[inset_0_0_0_0.5px_var(--line)] bg-sheet text-ink font-semibold hover:bg-sheet-3 [&_svg]:text-brass`;
/** A destructive action: red text, the soft red fill on hover. */
export const btnDanger = `${control} text-red hover:bg-red-soft`;
/** A dashed button for an action that needs attention. */
export const btnAttention = `${control} group border-dashed text-ink-2 hover:border-solid hover:bg-sheet-2 hover:text-ink`;
/** A small icon-only control (a trash can, a key icon), as the cell head's tools. */
export const iconBtn = 'flex flex-none items-center justify-center rounded-xs text-ink-3 transition-colors hover:bg-sheet-2 hover:text-ink';

/** The field (the mock's `rp-q`). */
const field = 'w-full rounded-md border border-line bg-sheet px-2.5 text-[0.8125rem] text-ink placeholder:text-ink-3 outline-none transition-colors focus:border-line-2 disabled:opacity-50';
export const formInput = `${field} h-[1.875rem]`;
export const formTextarea = `${field} py-1.5 resize-y leading-snug`;
export const formSelect = `${formInput} cursor-pointer`;
export const formLabel = 'text-[0.75rem] text-ink-2';
/** A section heading inside a panel or menu (the mock's `h4`). */
export const sectionLabel = 'text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-ink-3';
/** A panel head's title (the mock's `rp-head .t`, `manage h4`). */
export const panelLabel = 'text-[0.71875rem] font-semibold uppercase tracking-[0.08em] text-ink-2';

/** A segmented choice: one option on (the mock's skin switch), the rest off. */
export const segmentOn = 'h-[1.625rem] px-3 rounded-sm border border-ink bg-ink text-[0.78125rem] font-medium text-sheet transition-colors';
export const segmentOff = 'h-[1.625rem] px-3 rounded-sm border border-line-2 text-[0.78125rem] font-medium text-ink-2 transition-colors hover:bg-sheet-2 hover:text-ink';

/** A tab: an 18px line separated from its neighbour by a hairline, with a
 *  2px rule below — teal when open and faint ink on hover. */
const tab = 'relative px-2.5 leading-[1.125rem] rounded-xs text-[0.78125rem] whitespace-nowrap transition-colors before:absolute before:inset-y-0 before:right-0 before:w-px before:bg-line before:pointer-events-none last:before:hidden after:absolute after:inset-x-2.5 after:-bottom-1 after:h-0.5 after:rounded-[0.0625rem] after:transition-opacity';
export const tabOn = `${tab} text-ink after:bg-teal after:opacity-100`;
export const tabOff = `${tab} text-ink-2 hover:bg-sheet-2 hover:text-ink after:bg-ink-3 after:opacity-0 hover:after:opacity-45`;

/** A mode tab (the cell tab rule at 13px): an 18px line, the 2px rule under
 *  the active one, which takes the cell's accent (brass in the selected
 *  cell, teal elsewhere). The cell panel's modes, the status pane's tabs. */
export const modeTab = 'relative mt-px h-[1.125rem] flex-none px-2.5 rounded-xs text-[0.8125rem] leading-[1.125rem] whitespace-nowrap cursor-pointer disabled:cursor-default disabled:opacity-40 after:absolute after:inset-x-2.5 after:top-[1.375rem] after:h-0.5 after:rounded-[1px]';
export const modeTabOn = 'text-ink after:opacity-100';
export const modeTabOff = 'text-ink-2 hover:bg-sheet-2 hover:text-ink after:bg-ink-3 after:opacity-0 hover:after:opacity-45';

/** A docked pane's rail button (the mock's `.files .rail button`): an icon
 *  on the 18px line the tab strip's buttons sit on, ink-3 at rest. The
 *  Files pane's rail, the status pane's. */
export const railBtn = 'inline-flex items-center h-[1.125rem] px-[0.3125rem] rounded-xs transition-colors disabled:opacity-45 disabled:cursor-not-allowed';
export const railBtnOff = 'text-ink-3 enabled:hover:text-ink enabled:hover:bg-sheet-2';
/** A pane's Hide toggle on: the top bar's toggles' on-state (AppTopBar
 *  tbBtnOn), pressed while the pane shows — a toggle, not a selection. */
export const railToggleOn = 'aria-pressed:bg-sheet-2 aria-pressed:text-ink';

/** A 20px pill: a tag, a count, a mode. */
export const chip = 'inline-flex items-center gap-1 h-5 px-2 rounded-full border border-line-2 text-[0.6875rem] text-ink-2 whitespace-nowrap';
/** A keyboard hint (the mock's `kbd.k`). */
export const kbd = 'inline-block px-1 rounded-xs border border-line font-mono text-[0.6875rem] leading-4 text-ink-3';
export const card = 'rounded-lg border border-line bg-sheet';
/** A card's 36px head. */
export const cardHeader = 'flex items-center justify-between gap-2 h-9 px-3 border-b border-line';
/** A dialog plate: solid, so nothing beneath a modal shows through. */
export const dialog = 'rounded-lg border border-line-2 bg-sheet shadow-(--shadow)';
/** The inline “Delete “X”? [Delete] [Keep]” strip every list confirms in. */
export const confirmStrip = 'flex flex-wrap items-center gap-2 px-3 py-2 border-t border-line bg-red-soft text-[0.75rem] text-ink';
