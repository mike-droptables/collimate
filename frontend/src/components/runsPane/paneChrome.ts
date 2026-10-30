import { createContext } from 'react';
import type { DockDragHandlers } from '@/hooks/useDockDrag';

/** What the status pane hands its views for their rails: close, the Hide
 *  toggle's title, its dock drag and right-click side switch, and whether
 *  the cell is selected (the option rule's brass or teal). */
export interface RunsPaneChrome {
    close: () => void;
    hideTitle: string;
    dockDrag: DockDragHandlers;
    cycleSide: () => void;
    selected: boolean;
}

export const RunsPaneChromeContext = createContext<RunsPaneChrome | null>(null);

/** A view's title on the rail. */
export const railTitle = 'h-[1.125rem] leading-[1.125rem] px-1 text-[0.8125rem] text-ink truncate';
