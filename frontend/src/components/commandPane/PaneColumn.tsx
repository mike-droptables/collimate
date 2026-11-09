// The Commands pane's column (the mock's `.cmdk`): a COMMAND_PANE_WIDTH
// sheet docked flat against the left or right edge of what it lies over,
// its full height, with the 45px name row on top (the column's title, then
// whatever the host puts beside it: the pane's dock-side group, a panel's
// close X). The Commands pane and the channels page's docked settings panel
// are both this column, so the two read alike; each brings its own
// behaviour (the pane's focus containment, the panel's Esc and X).
//
// `layer` says what it lies over: the canvas (absolute, under the cell
// editor's own overlays), the full-window view or the channels page from
// above (fixed, the host portals it to the body), or the page itself
// (absolute inside the page's root, under the page's dialogs). The page's
// root sits under the blurred stand-in top bar (BlurredTopBar, a 36px band
// at z-410 over the page's z-400), so a page column starts below that band,
// where the page's own content starts too.

import type { KeyboardEventHandler, MouseEventHandler, ReactNode, Ref } from 'react';
import { cn } from '@/lib/utils';
import { COMMAND_PANE_WIDTH, type CommandPaneSide } from '@/stores/collimate/commandPaneSlice';

export type PaneColumnLayer = 'canvas' | 'overlay' | 'page';

const LAYER_CLASS: Record<PaneColumnLayer, string> = {
    canvas: 'absolute z-20 top-0',
    overlay: 'fixed z-[455] top-0',
    page: 'absolute z-10 top-9',
};

export interface PaneColumnProps {
    /** The edge it lies flat against. */
    side: CommandPaneSide;
    layer: PaneColumnLayer;
    /** The name row's words ('Commands' for the pane). */
    title: string;
    /** Beside the title, at the row's end. */
    headExtra?: ReactNode;
    /** The column's accessible name. */
    label: string;
    paneRef?: Ref<HTMLElement>;
    /** Host markers on the root (`data-command-pane`, `data-pane-view`,
     *  `data-settings-panel`). */
    dataAttrs?: Record<string, string>;
    onMouseDown?: MouseEventHandler<HTMLElement>;
    onKeyDown?: KeyboardEventHandler<HTMLElement>;
    children: ReactNode;
}

/** The pane-width column: the name row, then the host's view. An <aside>,
 *  so it is a complementary landmark named by `label`. */
export function PaneColumn({ side, layer, title, headExtra, label, paneRef, dataAttrs, onMouseDown, onKeyDown, children }: PaneColumnProps) {
    const left = side === 'left';
    return (
        <aside
            ref={paneRef}
            // The shell a pane-width section body sits in
            // (settings/paneColumn.testkit mimics it).
            data-pane-column=""
            {...dataAttrs}
            aria-label={label}
            onMouseDown={onMouseDown}
            onKeyDown={onKeyDown}
            // no-drag above the full-window view: the head lies in the
            // desktop title bar's drag band (see FullscreenContentOverlay).
            style={{ width: COMMAND_PANE_WIDTH, ...(layer === 'overlay' && { WebkitAppRegion: 'no-drag' }) } as React.CSSProperties}
            className={cn(
                LAYER_CLASS[layer],
                'bottom-0 flex flex-col bg-sheet text-ink',
                left
                    ? 'left-0 border-r border-line-2 shadow-[24px_0_40px_-18px_var(--lift)]'
                    : 'right-0 border-l border-line-2 shadow-[-24px_0_40px_-18px_var(--lift)]',
            )}
        >
            <div className="relative flex-none h-[2.8125rem] flex items-start gap-0.5 pt-[0.9375rem] pr-2 pl-3.5 border-b border-line text-[0.75rem]">
                <span className="flex-1 text-ink-2 font-medium leading-[1.125rem]">{title}</span>
                {headExtra}
            </div>
            {children}
        </aside>
    );
}
