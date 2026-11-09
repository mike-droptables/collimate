import React from 'react';
import { TitleBarToggle, useTitleBarRestorable } from './DesktopTitleBar';

/**
 * Non-interactive, blurred stand-in for the top bar, shown while the
 * channel-creation view is up (the real bar can't act without a channel).
 * It keeps the bar's 36px row and its three columns.
 *
 * One live control rides over it: while the desktop title bar row is
 * collapsed, the brass mark at the far left of the AppTopBar (which this
 * stand-in replaces) is the toggle that restores it. It has to be here too, crisp and
 * clickable above the blur, or the row can't be brought back from the
 * create view at all (first launch with the flag persisted from a previous
 * run, or a hidden bar when "New channel" is picked from the channel menu).
 */
export function BlurredTopBar() {
    const restorable = useTitleBarRestorable();
    return (
        <>
            <div
                aria-hidden
                className="absolute top-0 left-0 right-0 h-9 z-[410] grid grid-cols-[1fr_auto_1fr] items-center px-2.5 bg-canvas border-b border-line pointer-events-none select-none"
                style={{ filter: 'blur(2px)', opacity: 0.55 }}
            >
                <div className="flex items-center gap-1.5 justify-self-start">
                    <div className="size-[1.125rem] rounded-xs bg-sheet-3" />
                    <div className="w-24 h-[1.125rem] rounded-sm bg-sheet-2" />
                    <div className="w-px h-3.5 mx-1.5 bg-line" />
                    <div className="size-[1.125rem] rounded-sm bg-sheet-2" />
                    <div className="size-[1.125rem] rounded-sm bg-sheet-2" />
                </div>
                <div className="flex items-center gap-2.5">
                    <div className="size-[0.5625rem] rounded-full bg-sheet-3" />
                    <div className="size-[0.3125rem] rounded-full bg-line-2" />
                    <div className="size-[0.3125rem] rounded-full bg-line-2" />
                    <div className="size-[0.3125rem] rounded-full bg-line-2" />
                </div>
                <div className="flex items-center gap-1.5 justify-self-end">
                    <div className="w-20 h-[1.125rem] rounded-sm bg-sheet-2" />
                    <div className="w-px h-3.5 mx-1.5 bg-line" />
                    <div className="w-16 h-[1.125rem] rounded-sm bg-sheet-2" />
                    <div className="w-16 h-[1.125rem] rounded-sm bg-sheet-2" />
                    <div className="size-[1.125rem] rounded-sm bg-sheet-2" />
                </div>
            </div>
            {restorable && (
                // Same slot as the AppTopBar's far-left icon (its row is
                // h-9 px-2.5), layered above the blur and outside its
                // pointer-events-none so the click lands.
                <div className="absolute top-0 left-0 h-9 z-[411] flex items-center px-2.5">
                    <TitleBarToggle />
                </div>
            )}
        </>
    );
}
