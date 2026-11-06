// DOM hooks of the Commands pane shared with the scene keyboard, the top
// bar and the cell editor: the attribute on the pane's root, the one on the
// button that toggles it, the "esc to cell" focus move, which cell panel the
// pane hosts, and the space the pane takes from the canvas.

import { COMMAND_PANE_WIDTH, type CommandPaneState } from '@/stores/collimate/commandPaneSlice';
import type { CollimateState } from '@/stores/useCollimateStore';

/** On the pane's root. The scene keyboard reads keys typed in the pane by it. */
export const COMMAND_PANE_ATTR = 'data-command-pane';

/** On the control that toggles the pane (the top bar's Commands button): a
 *  click on it is not a click outside the pane. */
export const COMMANDS_TOGGLE_ATTR = 'data-commands-toggle';

/** The keystroke was typed inside the Commands pane. */
export function inCommandPane(e: Event): boolean {
    const target = (e.composedPath?.()[0] ?? e.target) as Node | null;
    return target instanceof Element && target.closest(`[${COMMAND_PANE_ATTR}]`) !== null;
}

/** On the pane's query (the commands view's search field). */
export const PANE_QUERY_ATTR = 'data-pane-query';

/** On the pane's panel slot, the element a hosted cell panel renders into. */
export const PANE_PANEL_SLOT_ATTR = 'data-pane-panel-slot';

/** On the element a view puts the keyboard on when it shows (the query, a
 *  section's back button, the settings root's row). */
export const PANE_ENTRY_ATTR = 'data-pane-entry';

/** The pane hosts this cell's panel: the pane is open on its commands
 *  view (the settings view shows over a picker, which waits in its cell)
 *  with its panel slot mounted, the cell is the selected one, and its
 *  session is a picker (`files` or `runners`) its editor shows (not over
 *  version history or a cell diff, where the editor shows no panel).
 *  Every other panel (and every other cell's) stays in its cell. */
export function paneHostsPanel(state: CollimateState, cellId: string): boolean {
    const pane = state.commandPane;
    if (!pane.open || pane.view.kind !== 'commands' || state.commandPanePanelSlot === null) return false;
    if (state.selectedCellId !== cellId) return false;
    if (state.cellHistory[cellId] || state.cellDiffs[cellId]) return false;
    const kind = state.pickSessions[cellId]?.kind;
    return kind === 'files' || kind === 'runners';
}

/** Put the keyboard on a cell's card (the pane's Esc: "to cell"). The card
 *  is not in the tab order, so it takes a programmatic tabindex; without a
 *  mounted card the active element just lets go, which leaves the scene's
 *  shortcuts on the selected cell all the same. */
export function focusCellCard(cellId: string | null): void {
    const card = cellId
        ? document.querySelector<HTMLElement>(`[data-cell-id="${CSS.escape(cellId)}"] [data-stack-card]`)
        : null;
    if (card) {
        if (!card.hasAttribute('tabindex')) card.tabIndex = -1;
        card.focus({ preventScroll: true, focusVisible: false } as FocusOptions);
        if (document.activeElement === card) return;
    }
    (document.activeElement as HTMLElement | null)?.blur?.();
}

/** Put the keyboard on a cell's status caret (the status pane's Esc hands
 *  it back there), else on its card. */
export function focusRunsPaneToggle(cellId: string): void {
    const caret = document.querySelector<HTMLElement>(
        `[data-cell-id="${CSS.escape(cellId)}"] [data-testid="runs-pane-toggle"]`,
    );
    if (caret) {
        caret.focus({ preventScroll: true });
        if (document.activeElement === caret) return;
    }
    focusCellCard(cellId);
}

/** Screen space a surface docked over the canvas takes from either side:
 *  the visible band starts past it. */
export interface ViewInset { left: number; right: number }

export const NO_INSET: ViewInset = { left: 0, right: 0 };
const LEFT_INSET: ViewInset = { left: COMMAND_PANE_WIDTH, right: 0 };
const RIGHT_INSET: ViewInset = { left: 0, right: COMMAND_PANE_WIDTH };

/** The space the Commands pane takes from the canvas: its width on the side
 *  it docks while it is open, none while it is shut. The one source for
 *  everything laid out against the free region the pane leaves (the
 *  camera's band, the full stack width, the overview's board region and
 *  its right-anchored controls, the shelf's reflow). The overview's zoom
 *  and camera are never changed by it: a left pane slides the content
 *  right with the region's edge, a right pane leaves it where it is.
 *  Stable objects, so a store selector can return it. */
export function commandPaneInsetOf(pane: Pick<CommandPaneState, 'open' | 'side'>): ViewInset {
    if (!pane.open) return NO_INSET;
    return pane.side === 'left' ? LEFT_INSET : RIGHT_INSET;
}

/** How long the free region's edges, and what rides them, take to slide as
 *  the pane opens, closes or docks across (none with reduced motion). */
export const COMMAND_PANE_INSET_MS = 160;
/** The slide's curve: CSS `ease`, the same for the board's camera-tied
 *  motion and the overlays' CSS transitions. */
export const COMMAND_PANE_INSET_EASE = [0.25, 0.1, 0.25, 1] as const;

/** The CSS transition of an element whose `props` keep it clear of the
 *  pane: those over the pane's slide, after the element's `own`
 *  transitions (an inline transition replaces its classes'). None with
 *  reduced motion. */
export function paneInsetTransition(
    props: readonly string[], reduceMotion: boolean | null, own: readonly string[] = [],
): string | undefined {
    if (reduceMotion) return undefined;
    return [...own, ...props.map(p => `${p} ${COMMAND_PANE_INSET_MS}ms ease`)].join(', ');
}
