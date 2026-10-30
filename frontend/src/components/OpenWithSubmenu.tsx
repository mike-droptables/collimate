// "Open with" — one context-menu row that flies out the list of every
// renderer able to open a file, so a non-primary renderer is one click away.
// Shared by the file context menu (tree + folder views) and the tab context
// menu; both are hand-rolled fixed-position portals, so the flyout is plain
// absolute positioning inside the row (no second portal — it must move with
// the menu and die with it).

import React, { useState } from 'react';
import { AppWindow, Check, ChevronRight } from 'lucide-react';
import { rendererCandidatesFor, resolveRenderer } from '@/hooks/useRendererId';
import { cn } from '@/lib/utils';
import { menuItem, menuSurface } from '@/components/ui/menuStyles';

/** The submenu is only worth offering when a real choice exists. Callers use
 *  this to skip the row entirely (a one-entry "menu" is noise). */
export function hasOpenWithChoices(filename: string): boolean {
    return rendererCandidatesFor(filename).length > 1;
}

interface OpenWithSubmenuProps {
    filename: string;
    /** The renderer currently in use (a tab's effective renderer). Absent for
     *  a file that isn't open — the primary entry is still marked "default". */
    currentId?: string;
    /** Chosen renderer id; `undefined` = the primary/default resolution. */
    onPick: (rendererId: string | undefined) => void;
    /** Close the host menu after a pick. */
    onClose: () => void;
}

/** Approximate flyout width used only to decide which side it opens on. */
const FLYOUT_WIDTH = 200;

export function OpenWithSubmenu({ filename, currentId, onPick, onClose }: OpenWithSubmenuProps) {
    const [open, setOpen] = useState(false);
    // Which side the flyout opens on, decided from the row's live position at
    // hover time (the host menu is viewport-clamped, so render-time guesses
    // are wrong near the right edge).
    const [flip, setFlip] = useState(false);

    // Snapshot, not subscription: the menu lives for one click, and the
    // registry is already populated (module-load fetch).
    const candidates = rendererCandidatesFor(filename);
    const primaryId = resolveRenderer(filename).id;
    const effectiveId = currentId ?? primaryId;
    if (candidates.length < 2) return null;

    return (
        <div
            className="relative"
            onMouseEnter={(e) => {
                setFlip(e.currentTarget.getBoundingClientRect().right + FLYOUT_WIDTH > window.innerWidth);
                setOpen(true);
            }}
            onMouseLeave={() => setOpen(false)}
        >
            <button
                onClick={() => setOpen(o => !o)}
                aria-haspopup="menu"
                aria-expanded={open}
                className={menuItem}
            >
                <AppWindow className="w-3.5 h-3.5" />
                Open with
                <ChevronRight className="w-3 h-3 ml-auto" />
            </button>
            {open && (
                <div
                    role="menu"
                    className={cn(
                        'absolute top-0 z-10 min-w-[11.25rem]', menuSurface,
                        flip ? 'right-full -mr-0.5' : 'left-full -ml-0.5',
                    )}
                >
                    {candidates.map((r) => {
                        const isPrimary = r.id === primaryId;
                        const isCurrent = r.id === effectiveId;
                        return (
                            <button
                                key={r.id}
                                role="menuitem"
                                onClick={() => {
                                    // Choosing the primary clears the override
                                    // rather than pinning it — the tab then
                                    // follows future preference changes.
                                    onPick(isPrimary ? undefined : r.id);
                                    onClose();
                                }}
                                // The renderer in use reads as the menu's
                                // current row: ink, its check in brass.
                                aria-current={isCurrent || undefined}
                                className={cn(menuItem, isCurrent && 'text-ink [&_svg]:text-brass')}
                            >
                                <Check className={cn('w-3.5 h-3.5', !isCurrent && 'invisible')} />
                                <span className="truncate">{r.name || r.id}</span>
                                {isPrimary && (
                                    <span className="ml-auto text-[0.6875rem] text-ink-3">default</span>
                                )}
                            </button>
                        );
                    })}
                </div>
            )}
        </div>
    );
}
