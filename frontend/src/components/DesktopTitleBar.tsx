// The VS Code-style title bar the desktop shell expects this page to draw
// when `capabilities.customTitleBar` is set: the shell hid the native bar,
// so this strip supplies the application menu's top-level entries, a drag region, and the double-click-to-maximize gesture the
// native bar would have provided. With `capabilities.customWindowControls`
// (Linux, where the window is fully frameless) it draws the min/max/close
// buttons as well; on Windows the shell's overlay paints native controls
// over the strip's right edge instead, and the layout leaves them room. In
// a browser tab (or on macOS, whose native bar stays) it renders nothing.

import React, { useCallback, useEffect, useState } from 'react';
import { desktopBridge, hasCap, type TitleBarMenu, type TitleBarOps } from '@/lib/desktop';
import { useTitleBarStore } from '@/stores/useTitleBarStore';
import { MARK_LENS, MARK_RAYS, MARK_TIP } from '@/lib/brandMark';

/** Fallback when `env(titlebar-area-height)` is unavailable (it is only
 *  set by the Windows overlay); matches the shell's TITLE_BAR_HEIGHT
 *  (desktop/src/main/window.ts). */
const BAR_HEIGHT_PX = 37;

/** Menu labels may carry Alt-mnemonic ampersands ("&File"); show "File".
 *  An escaped "&&" means a literal ampersand and survives. */
export function stripMnemonics(label: string): string {
    // Split on the escaped pair first so a lone "&" inside each piece can be
    // dropped, then rejoin with the literal ampersand the pair stood for. (A
    // NUL placeholder did the same job but tripped eslint's no-control-regex.)
    return label.split('&&').map(part => part.replace(/&/g, '')).join('&');
}

/** Whether this page draws the window's title bar row: the desktop shell
 *  with `capabilities.customTitleBar`. Never in a browser tab, nor on
 *  macOS, whose native bar stays. */
function hasTitleBarRow(): boolean {
    return hasCap('customTitleBar');
}

/** The shell's title-bar surface, or undefined anywhere it doesn't apply. */
function titleBarOps(): TitleBarOps | undefined {
    if (!hasTitleBarRow()) return undefined;
    return desktopBridge()?.titleBar;
}

const NO_DRAG = { WebkitAppRegion: 'no-drag' } as React.CSSProperties;

/** True while the desktop title bar row is collapsed and there is one to
 *  restore. Desktop-only: in a browser tab the persisted flag is
 *  meaningless — there is no title bar to bring back. */
export function useTitleBarRestorable(): boolean {
    const titleBarHidden = useTitleBarStore(s => s.titleBarHidden);
    return titleBarHidden && hasTitleBarRow();
}

/** The brass collimate glyph (the mock's `i-mark`: three rays, a lens at
 *  its centre and a filled tip), 18px, riding the text colour. The one drawing of the app's
 *  mark in the top chrome: the inert mark and the title bar toggle below
 *  both draw it. */
export function MarkGlyph() {
    return (
        <svg aria-hidden viewBox="0 0 24 24" className="size-[1.125rem] flex-none fill-none stroke-current" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
            <path d={MARK_RAYS} />
            <path d={MARK_TIP} fill="currentColor" stroke="none" />
            <circle cx={MARK_LENS.cx} cy={MARK_LENS.cy} r={MARK_LENS.r} />
        </svg>
    );
}

/** The title bar toggles' accessible name: stable, with `aria-pressed`
 *  saying whether the row is shown and the `title` naming the click. */
const TITLE_BAR_TOGGLE_LABEL = 'Title bar';

const toggleTitle = (hidden: boolean) => (hidden ? 'Show the title bar' : 'Hide the title bar');

/**
 * The top chrome's brass mark as the title bar row's toggle, in the desktop
 * shell (`TopBarMark` below picks it there). One glyph in both states: a
 * click hides the row while it is shown and restores it while it is
 * hidden (the row itself carries no icon). Every surface that replaces the AppTopBar (the
 * channel-creation view's blurred stand-in) must carry it while the row is
 * hidden, or the row can't be brought back from there.
 * The 26px hit box hangs 4px left of the mark's slot (-ml-1) so the glyph
 * sits exactly where the mark does, and its 4px inner edge plus mr-0.5
 * keeps the mark's 6px (mr-1.5) gap to the next control. No-drag, so the
 * frameless window's drag region never swallows the click.
 */
export function TitleBarToggle() {
    const titleBarHidden = useTitleBarStore(s => s.titleBarHidden);
    const toggleTitleBar = useTitleBarStore(s => s.toggleTitleBar);
    return (
        <button
            type="button"
            onClick={toggleTitleBar}
            title={toggleTitle(titleBarHidden)}
            aria-label={TITLE_BAR_TOGGLE_LABEL}
            aria-pressed={!titleBarHidden}
            className="inline-flex flex-none items-center justify-center size-[1.625rem] -ml-1 mr-0.5 rounded-md text-brass transition-colors hover:bg-sheet-2"
            style={NO_DRAG}
        >
            <MarkGlyph />
        </button>
    );
}

/** The AppTopBar's far-left brass mark (the mock's `i-mark`): the title
 *  bar toggle in the desktop shell, inert in a browser tab. */
export function TopBarMark() {
    if (hasTitleBarRow()) return <TitleBarToggle />;
    return (
        <span aria-hidden className="inline-flex flex-none items-center mr-1.5 text-brass">
            <MarkGlyph />
        </span>
    );
}

export function DesktopTitleBar() {
    const titleBar = titleBarOps();
    const [menus, setMenus] = useState<TitleBarMenu[]>([]);
    const titleBarHidden = useTitleBarStore(s => s.titleBarHidden);

    useEffect(() => {
        if (!titleBar) return;
        let cancelled = false;
        void titleBar.menus().then((entries) => {
            if (!cancelled) setMenus(entries);
        });
        return () => {
            cancelled = true;
        };
        // The bridge is installed before the page's first script runs and
        // never changes, so this effectively runs once.
    }, [titleBar]);

    if (!titleBar) return null;
    // Collapsed by the app-icon click (or the AppTopBar's mark): the whole
    // row (menus, drag region, window controls) gives its pixels back to
    // the app; the AppTopBar's mark, a toggle in this shell, restores it.
    if (titleBarHidden) return null;

    const openMenu = (index: number, button: HTMLElement) => {
        const rect = button.getBoundingClientRect();
        void titleBar.popupMenu(index, Math.round(rect.left), Math.round(rect.bottom));
    };

    const handleDoubleClick = (e: React.MouseEvent) => {
        // Double-clicking a menu button is two menu clicks, not a maximize;
        // the window-control buttons are their own gestures too.
        if ((e.target as HTMLElement).closest('button')) return;
        void titleBar.toggleMaximize();
    };

    const drawControls = hasCap('customWindowControls');

    return (
        <div
            data-testid="desktop-title-bar"
            // The skin's sheet rather than its canvas: a dark skin's canvas
            // is darker than most desktop backgrounds, and a darker-than-
            // surroundings strip inside the compositor's focus ring reads
            // as a gap around the window, not as the window. The Windows
            // overlay controls paint the same sheet (setWindowTheme).
            className="flex-none flex items-stretch bg-sheet text-ink border-b border-line select-none"
            style={{
                height: `env(titlebar-area-height, ${BAR_HEIGHT_PX}px)`,
                WebkitAppRegion: 'drag',
            } as React.CSSProperties}
            onDoubleClick={handleDoubleClick}
        >
            {/* No app icon: the top bar's brass mark is the row's toggle
                (TitleBarToggle). The menus lead the row. */}
            <div className="flex items-stretch flex-none ml-1.5" style={NO_DRAG}>
                {menus.map((menu, index) => (
                    <button
                        key={`${index}-${menu.label}`}
                        type="button"
                        className="px-2.5 text-[0.78125rem] text-ink-2 hover:text-ink hover:bg-sheet-2"
                        onClick={(e) => openMenu(index, e.currentTarget)}
                    >
                        {stripMnemonics(menu.label)}
                    </button>
                ))}
            </div>
            {/* Everything from here to the right edge is drag area. */}
            <div className="flex-1 min-w-0" />
            {drawControls ? (
                <WindowControls titleBar={titleBar} />
            ) : (
                // Keep clear of the Windows overlay controls: they occupy
                // the space right of `env(titlebar-area-width)`. Without an
                // overlay the env var is unset and this collapses to zero.
                <div
                    aria-hidden
                    className="flex-none"
                    style={{ width: 'calc(100% - env(titlebar-area-width, 100%))' }}
                />
            )}
        </div>
    );
}

/**
 * The frameless window's own min / max-restore / close. Full-height
 * 46px buttons keep the easy hit targets, but the hover/pressed state
 * paints an inset rounded box in the skin's sheet tones — the same
 * treatment as the menu buttons beside them — rather than the
 * edge-to-edge slab (and red close) of the Windows-native controls.
 * Glyphs are inline SVG strokes so they ride the text color.
 */
function WindowControls({ titleBar }: { titleBar: TitleBarOps }) {
    const [maximized, setMaximized] = useState(false);

    const refresh = useCallback(() => {
        void titleBar.isMaximized().then(setMaximized);
    }, [titleBar]);

    useEffect(() => {
        refresh();
        // Maximize/restore always resizes the page — no push channel needed.
        window.addEventListener('resize', refresh);
        return () => window.removeEventListener('resize', refresh);
    }, [refresh]);

    const buttonClass =
        'group flex items-center justify-center w-[2.875rem] h-full text-ink-2 hover:text-ink';
    const glyphBoxClass =
        'flex items-center justify-center w-8 h-6 rounded-sm transition-colors group-hover:bg-sheet-2 group-active:bg-sheet-3';

    return (
        <div className="flex items-stretch flex-none" style={NO_DRAG}>
            <button
                type="button"
                aria-label="Minimize"
                className={buttonClass}
                onClick={() => void titleBar.minimize()}
            >
                <span className={glyphBoxClass}>
                    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                        <path d="M0 5h10" stroke="currentColor" strokeWidth="1" fill="none" />
                    </svg>
                </span>
            </button>
            <button
                type="button"
                aria-label={maximized ? 'Restore' : 'Maximize'}
                className={buttonClass}
                onClick={() => void titleBar.toggleMaximize().then(setMaximized)}
            >
                <span className={glyphBoxClass}>
                    {maximized ? (
                        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                            <path
                                d="M2.5 2.5V.5h7v7h-2M.5 2.5h7v7h-7z"
                                stroke="currentColor"
                                strokeWidth="1"
                                fill="none"
                            />
                        </svg>
                    ) : (
                        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                            <rect x="0.5" y="0.5" width="9" height="9" stroke="currentColor" strokeWidth="1" fill="none" />
                        </svg>
                    )}
                </span>
            </button>
            <button
                type="button"
                aria-label="Close"
                className={buttonClass}
                onClick={() => void titleBar.close()}
            >
                <span className={glyphBoxClass}>
                    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
                        <path d="M0 0l10 10M10 0L0 10" stroke="currentColor" strokeWidth="1" fill="none" />
                    </svg>
                </span>
            </button>
        </div>
    );
}
