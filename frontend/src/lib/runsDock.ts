// The status pane's dock sides as the UI shows them: each side's icon, the
// header caret's chevron and the toggles' hint, and the pane's root
// attribute. The drag itself is hooks/useRunsDockDrag.

import { List, PanelBottom, PanelRight, type LucideIcon } from 'lucide-react';
import type { RunsPaneSide } from '@/stores/collimate/types';
import { nextRunsPaneSide } from '@/lib/cellLayout';

/** The attribute on the pane's root (components/cellEditor/RunsPane): Esc
 *  anywhere in the pane is the pane's, so the scene's Esc cascade yields to
 *  it (hooks/useKeyboardHandler). */
export const RUNS_PANE_ATTR = 'data-runs-pane';

/** The pane's icon per dock side: the Commands pane's Dock status rows. */
export const RUNS_SIDE_ICON: Record<RunsPaneSide, LucideIcon> = {
    right: PanelRight,
    bottom: PanelBottom,
};

/** The header caret's glyph: the List icon, whose lines run to the right
 *  of their bullets as drawn. */
export const RunsCaretIcon: LucideIcon = List;

/** The caret's orientation, a transform class on the glyph: as drawn while
 *  the right pane is closed, mirrored while it is open; turned to point up
 *  while the bottom pane is open and down while it is closed. */
export function runsCaretClass(side: RunsPaneSide, open: boolean): string | undefined {
    if (side === 'right') return open ? '-scale-x-100' : undefined;
    return open ? '-rotate-90' : 'rotate-90';
}

/** The toggles' tooltip tail: what a right-click and a drag do. */
export const runsDockHint = (side: RunsPaneSide): string =>
    `right-click: dock on the ${nextRunsPaneSide(side)} · drag to the right or bottom edge to dock`;
