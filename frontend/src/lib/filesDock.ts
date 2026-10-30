// The Files pane's dock sides as the UI shows them: each side's icon and the
// Hide / Show toggles' hint. The drag itself is hooks/useFilesDockDrag.

import { PanelLeft, PanelTop, type LucideIcon } from 'lucide-react';
import type { FilesPaneSide } from '@/stores/collimate/types';
import { nextFilesPaneSide } from '@/lib/cellLayout';

/** The pane's icon per dock side: the Hide / Show toggles and the Commands
 *  pane's rows show it. */
export const FILES_SIDE_ICON: Record<FilesPaneSide, LucideIcon> = {
    left: PanelLeft,
    top: PanelTop,
};

/** The Hide / Show toggles' tooltip tail: what a right-click and a drag do. */
export const filesDockHint = (side: FilesPaneSide): string =>
    `right-click: dock on the ${nextFilesPaneSide(side)} · drag to the left or top edge to dock`;
