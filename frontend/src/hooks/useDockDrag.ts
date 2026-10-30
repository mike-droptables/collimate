// Drag-to-dock for a cell's docking panes (the Files pane, the runs pane):
// the pane's dock icon carries it. A primary press starts tracking the
// pointer at window level, so moves and the release are seen wherever they
// land (the icon is far smaller than one move sample). A press that moves
// past the threshold lifts the gesture: the pointer is captured, the scene
// gives up the keyboard (Esc cancels), and the store's `dockDrag` names the
// pane and the edge of the drag's region the pointer is nearest, among the
// pane's own `sides`. The region is the icon's closest ancestor carrying
// `regionAttr`, else the one inside the icon's cell card (an icon in the
// cell's header, outside the region: the status caret). CellEditor draws
// that edge as a DockPreview band and ContentPane
// answers by concealing pooled iframes and occluding guests. A release over
// the region docks the pane there (`onDock`); a release outside, Esc, a
// cancelled pointer or an unmount docks nothing. A press below the
// threshold stays a click, so the icon's button still toggles; the click
// that ends a drag is swallowed. Every new press starts afresh: a press
// whose release never arrived is dropped, never resumed.
//
// The handlers go on a wrapper around the button, not the button: a button
// may be disabled (the Files pane's Hide in history mode), and a disabled
// button takes no pointer events.

import React, { useCallback, useEffect, useRef } from 'react';
import { nearestDockSide, type DockSide } from '@/lib/dockDrag';
import { claimKeyboardForGesture } from '@/lib/keyboardOwner';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { DockPane } from '@/stores/collimate/types';

/** How far (px) a press moves before it is a drag rather than a click. */
export const DOCK_DRAG_THRESHOLD = 5;

interface Press<S extends DockSide> {
    pointerId: number;
    x0: number;
    y0: number;
    /** The wrapper the press began on; it holds the capture once lifted. */
    holder: HTMLElement;
    lifted: boolean;
    side: S | null;
    /** Gives the keyboard back to the scene (set once lifted). */
    releaseKeyboard: (() => void) | null;
    /** Removes the window listeners that track this press. */
    detach: () => void;
}

export interface DockDragHandlers {
    onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
    onLostPointerCapture: (e: React.PointerEvent<HTMLElement>) => void;
    onClickCapture: (e: React.MouseEvent<HTMLElement>) => void;
}

export interface DockDragOptions<S extends DockSide> {
    cellId: string | number;
    /** The pane the drag docks, as the store's `dockDrag` names it. */
    pane: DockPane;
    /** The attribute on the drag's hit area: an ancestor of the icon, or
     *  an element inside the icon's cell card (`[data-cell-id]`). */
    regionAttr: string;
    /** The edges the pane may dock on, in tie order. */
    sides: readonly S[];
    /** Docks the pane on `side` (a release over the region). */
    onDock: (side: S) => void;
}

export function useDockDrag<S extends DockSide>({ cellId, pane, regionAttr, sides, onDock }: DockDragOptions<S>): DockDragHandlers {
    const id = String(cellId);
    const press = useRef<Press<S> | null>(null);
    const swallowClick = useRef(false);
    const latest = useRef({ regionAttr, sides, onDock });
    useEffect(() => { latest.current = { regionAttr, sides, onDock }; });

    /** Ends the press: drops the window listeners, the capture, the keyboard
     *  claim and the store's drag. Returns the side a release would dock
     *  on, or null when nothing was lifted. */
    const finish = useCallback((): S | null => {
        const p = press.current;
        press.current = null;
        if (!p) return null;
        p.detach();
        if (!p.lifted) return null;
        swallowClick.current = true;
        p.releaseKeyboard?.();
        try { p.holder.releasePointerCapture(p.pointerId); } catch { /* not held */ }
        const store = useCollimateStore.getState();
        if (store.dockDrag?.cellId === id && store.dockDrag.pane === pane) store.setDockDrag(null);
        return p.side;
    }, [id, pane]);

    // Unmounting mid-drag (the icon goes once its pane shows, the cell
    // scrolls away) docks nothing and leaves no drag behind.
    useEffect(() => () => { finish(); }, [finish]);

    const onPointerDown = (e: React.PointerEvent<HTMLElement>) => {
        swallowClick.current = false;
        finish();
        // The press still bubbles: the cell card selects its cell on it.
        if (e.button !== 0) return;
        const pointerId = e.pointerId;

        const onMove = (m: PointerEvent) => {
            const p = press.current;
            if (!p || m.pointerId !== pointerId) return;
            if (!p.lifted) {
                if (Math.hypot(m.clientX - p.x0, m.clientY - p.y0) <= DOCK_DRAG_THRESHOLD) return;
                p.lifted = true;
                try { p.holder.setPointerCapture(pointerId); } catch { /* the pointer is gone */ }
                p.releaseKeyboard = claimKeyboardForGesture();
            }
            const { regionAttr: attr, sides: allowed } = latest.current;
            const region = p.holder.closest(`[${attr}]`)
                ?? p.holder.closest('[data-cell-id]')?.querySelector(`[${attr}]`)
                ?? null;
            p.side = region ? nearestDockSide(region.getBoundingClientRect(), m.clientX, m.clientY, allowed) : null;
            const store = useCollimateStore.getState();
            const cur = store.dockDrag;
            if (cur?.cellId !== id || cur.pane !== pane || cur.side !== p.side) store.setDockDrag({ cellId: id, pane, side: p.side });
        };
        const onUp = (u: PointerEvent) => {
            if (u.pointerId !== pointerId) return;
            const side = finish();
            if (side) latest.current.onDock(side);
        };
        const onCancel = (c: PointerEvent) => {
            if (c.pointerId === pointerId) finish();
        };
        const onKey = (k: KeyboardEvent) => {
            if (k.key !== 'Escape' || !press.current?.lifted) return;
            k.preventDefault();
            k.stopImmediatePropagation();
            finish();
        };
        window.addEventListener('pointermove', onMove, true);
        window.addEventListener('pointerup', onUp, true);
        window.addEventListener('pointercancel', onCancel, true);
        window.addEventListener('keydown', onKey, true);
        press.current = {
            pointerId, x0: e.clientX, y0: e.clientY, holder: e.currentTarget,
            lifted: false, side: null, releaseKeyboard: null,
            detach: () => {
                window.removeEventListener('pointermove', onMove, true);
                window.removeEventListener('pointerup', onUp, true);
                window.removeEventListener('pointercancel', onCancel, true);
                window.removeEventListener('keydown', onKey, true);
            },
        };
    };

    // The wrapper loses the capture only when the drag is cut short (the
    // release ends the press first): dock nothing.
    const onLostPointerCapture = (e: React.PointerEvent<HTMLElement>) => {
        const p = press.current;
        if (p?.lifted && e.pointerId === p.pointerId) finish();
    };

    const onClickCapture = (e: React.MouseEvent<HTMLElement>) => {
        if (!swallowClick.current) return;
        swallowClick.current = false;
        e.preventDefault();
        e.stopPropagation();
    };

    return { onPointerDown, onLostPointerCapture, onClickCapture };
}
