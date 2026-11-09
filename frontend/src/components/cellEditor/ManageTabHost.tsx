import { createContext, useContext, useEffect, useLayoutEffect, useRef } from 'react';
import { isFocusWheel } from '@/lib/wheelReplay';

// A Capabilities cell's Manage tab keeps its body across the full-window
// toggle. The content region remounts on that toggle (inline ⇄ the
// body-level overlay), and the Manage body holds its work in component
// state (the view, the search and kind filter, an open runner or handler
// editor's draft, a Create… questionnaire's answers) that no flush saves.
// So CellEditor renders the body once, into an element of its own
// (ManageTabHost.box) from a tree position the toggle never moves, and the
// tab only places that element in its slot: the body stays mounted while
// the slot remounts. The same move as the cell panel's box (CellEditor's
// panelBox).

export interface ManageTabHost {
    /** The element the cell's Manage body renders into. */
    box: HTMLElement;
    /** The tab has shown once: CellEditor mounts the body from now on (a
     *  tab body mounts on its first activation, so this one does too). */
    claim: () => void;
    /** Make the tab's leaf the active one. The body's React events reach
     *  CellEditor, not the leaf's own handlers (the body is portaled from
     *  CellEditor), so the slot does the leaf's pointer-down and focusing
     *  wheel itself. */
    activate: (tabId: string) => void;
}

export const ManageTabHostContext = createContext<ManageTabHost | null>(null);

/** The Manage tab's body slot: holds the host's element while mounted. */
export function ManageTabSlot({ tabId }: { tabId: string }) {
    const host = useContext(ManageTabHostContext);
    const slotRef = useRef<HTMLDivElement>(null);
    useLayoutEffect(() => {
        const slot = slotRef.current;
        if (!host || !slot) return;
        if (host.box.parentNode !== slot) slot.append(host.box);
        host.claim();
        return () => { if (host.box.parentNode === slot) host.box.remove(); };
    }, [host]);
    useEffect(() => {
        const slot = slotRef.current;
        if (!host || !slot) return;
        const down = () => host.activate(tabId);
        const wheel = (e: WheelEvent) => { if (isFocusWheel(e)) host.activate(tabId); };
        slot.addEventListener('pointerdown', down);
        slot.addEventListener('wheel', wheel, { passive: true });
        return () => {
            slot.removeEventListener('pointerdown', down);
            slot.removeEventListener('wheel', wheel);
        };
    }, [host, tabId]);
    return <div ref={slotRef} data-manage-tab="" className="h-full min-h-0 flex flex-col bg-sheet" />;
}
