import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { useMotionValue, useReducedMotion, type MotionValue } from 'motion/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { claimKeyboardForGesture } from '@/lib/keyboardOwner';
import { OVERVIEW_BAR_HEIGHT, type ShelfZone } from '@/stores/collimate/overviewSlice';

// Cards crossing the line between the overview's board and its Removed
// shelf. A board card dragged (useStackDrag) onto the open shelf, or onto
// the Removed bar, is removed when it is let go; a shelf card dragged onto
// the board (this hook) is restored where a board card would land: over a
// column, into that stack at the position the pointer points to, the column
// opening its marked gap there; between columns, as a new column, the board
// opening the same marked gap with its columns reflowing round it
// (useStackDrag writes the target to the store's `overview.drop`; the
// geometry is sceneDropGeometry's). Both drags capture the pointer once
// they lift, so the card follows it across the board, the bar and the
// shelf, and both stop at Escape.

/** A press that moves this far lifts the card (the board's 5px). */
const LIFT_PX = 5;

/** A rect in window px (a DOMRect, or its parts). */
export interface ClientRect {
    left: number;
    right: number;
    top: number;
    bottom: number;
}

/** Where a board card held at (`x`, `y`) would be removed to: the bar (the
 *  30px band right above the board) or the shelf (above the bar, inside the
 *  overview); null over the board, the journal pane or outside. */
export function shelfZoneAt(x: number, y: number, board: ClientRect, overview: ClientRect): ShelfZone | null {
    if (x < board.left || x > board.right || y >= board.top || y < overview.top) return null;
    return board.top - y <= OVERVIEW_BAR_HEIGHT ? 'bar' : 'shelf';
}

/** Inside a rect (edges included). */
export const insideRect = (x: number, y: number, r: ClientRect): boolean =>
    x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;

/** The overview's board element, from any element inside the overview. */
export const overviewBoardOf = (el: Element): HTMLElement | null =>
    el.closest('[data-overview-root]')?.querySelector<HTMLElement>('[data-overview-board]') ?? null;

/** The remove zones' strip (ZoneStrip, on the Removed bar and the shelf's
 *  drop area, one horizontal extent), from any element inside the overview. */
export const zoneStripOf = (el: Element): HTMLElement | null =>
    el.closest('[data-overview-root]')?.querySelector<HTMLElement>('[data-rm-zone-strip]') ?? null;

/** A refused drop's reason stays in its target's title this long. */
export const REFUSAL_SHOWN_MS = 3000;

/** A board card held over the bar of a shut shelf this long opens it. */
export const SHELF_HOVER_OPEN_MS = 400;

/** A drop target (the bar or the shelf) that refuses a card: it shakes once
 *  per refusal and returns the reason while it is fresh, for its title. A
 *  refusal from before the target mounted is old news. */
export function useRefusalShake(ref: RefObject<HTMLElement | null>, at: ShelfZone): string | null {
    const refusal = useCollimateStore(s => (s.overview.refusal?.at === at ? s.overview.refusal : null));
    const reduceMotion = useReducedMotion();
    const still = useRef(reduceMotion);
    useEffect(() => { still.current = reduceMotion; }, [reduceMotion]);
    const nonce = refusal?.nonce ?? null;
    const seen = useRef(nonce);
    const [expired, setExpired] = useState(nonce);
    useEffect(() => {
        if (nonce === null || nonce === seen.current) return undefined;
        seen.current = nonce;
        if (!still.current) {
            ref.current?.animate?.([
                { transform: 'translateX(0)' }, { transform: 'translateX(-6px)' }, { transform: 'translateX(5px)' },
                { transform: 'translateX(-3px)' }, { transform: 'translateX(0)' },
            ], { duration: 320, easing: 'ease-out' });
        }
        const timer = setTimeout(() => setExpired(nonce), REFUSAL_SHOWN_MS);
        return () => clearTimeout(timer);
    }, [nonce, ref]);
    return refusal && expired !== refusal.nonce ? refusal.reason : null;
}

/** A lifted shelf card: its id, its size and where the pointer holds it. */
export interface LiftedShelfCard {
    cellId: string;
    width: number;
    height: number;
    grabX: number;
    grabY: number;
}

interface Press extends LiftedShelfCard {
    pointerId: number;
    x0: number;
    y0: number;
    lifted: boolean;
    /** The element that holds the pointer once the card lifts. */
    holder: HTMLElement | null;
}

export interface ShelfDrag {
    /** The lifted card (the shelf leaves it out of its grid), else null. */
    lifted: LiftedShelfCard | null;
    /** The pointer, window px, for the ghost. */
    pointerX: MotionValue<number>;
    pointerY: MotionValue<number>;
    /** For the shelf's grid: a press on a card arms the drag. */
    gridHandlers: {
        onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
        onPointerMove: (e: React.PointerEvent<HTMLElement>) => void;
        onPointerUp: (e: React.PointerEvent<HTMLElement>) => void;
        onPointerCancel: (e: React.PointerEvent<HTMLElement>) => void;
        onClickCapture: (e: React.MouseEvent<HTMLElement>) => void;
    };
}

/** The drag of a shelf card onto the board. `enabled`: the shelf's cards
 *  drag (not while Only connected keeps a set, as on the board). */
export function useShelfDrag(enabled: boolean): ShelfDrag {
    const press = useRef<Press | null>(null);
    const swallowClick = useRef(false);
    const [lifted, setLifted] = useState<LiftedShelfCard | null>(null);
    const pointerX = useMotionValue(0);
    const pointerY = useMotionValue(0);

    const release = (p: Press) => {
        try { p.holder?.releasePointerCapture(p.pointerId); } catch { /* not held */ }
    };

    /** Put the card back where it was: no restore. */
    const cancel = useCallback(() => {
        const p = press.current;
        press.current = null;
        if (!p?.lifted) return;
        release(p);
        setLifted(null);
        const store = useCollimateStore.getState();
        if (store.overview.drop?.kind === 'restore') store.setOverviewDrop(null);
    }, []);

    useEffect(() => {
        if (!lifted) return undefined;
        const release = claimKeyboardForGesture();
        const onKey = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            e.preventDefault();
            e.stopImmediatePropagation();
            cancel();
        };
        window.addEventListener('keydown', onKey, true);
        return () => {
            window.removeEventListener('keydown', onKey, true);
            release();
        };
    }, [lifted, cancel]);

    // A shelf that stops dragging (Only connected, the overview closing)
    // drops a lifted card where it was.
    useEffect(() => {
        if (!enabled) cancel();
    }, [enabled, cancel]);
    useEffect(() => cancel, [cancel]);

    const onPointerDown = (e: React.PointerEvent<HTMLElement>) => {
        swallowClick.current = false;
        if (!enabled || e.button !== 0) return;
        const target = e.target as Element;
        const card = target.closest<HTMLElement>('[data-rm-card]');
        if (!card || target.closest('button, input')) return;
        const r = card.getBoundingClientRect();
        press.current = {
            cellId: card.dataset.rmCard ?? '', pointerId: e.pointerId, x0: e.clientX, y0: e.clientY,
            grabX: e.clientX - r.left, grabY: e.clientY - r.top, width: r.width, height: r.height,
            lifted: false, holder: null,
        };
    };

    const onPointerMove = (e: React.PointerEvent<HTMLElement>) => {
        const p = press.current;
        if (!p || e.pointerId !== p.pointerId) return;
        const store = useCollimateStore.getState();
        if (!p.lifted) {
            if (Math.hypot(e.clientX - p.x0, e.clientY - p.y0) <= LIFT_PX) return;
            p.lifted = true;
            p.holder = e.currentTarget;
            // The grid holds the pointer: the card itself leaves the grid.
            try { e.currentTarget.setPointerCapture(e.pointerId); } catch { /* the pointer is gone */ }
            const { cellId, width, height, grabX, grabY } = p;
            setLifted({ cellId, width, height, grabX, grabY });
            store.setOverviewDrop({ kind: 'restore', cellId, point: null, target: null });
        }
        pointerX.set(e.clientX);
        pointerY.set(e.clientY);
        const board = overviewBoardOf(e.currentTarget);
        const over = board ? insideRect(e.clientX, e.clientY, board.getBoundingClientRect()) : false;
        const drop = useCollimateStore.getState().overview.drop;
        if (drop?.kind !== 'restore') return;
        store.setOverviewDrop(over
            ? { ...drop, point: { x: e.clientX, y: e.clientY } }
            : { ...drop, point: null, target: null });
    };

    const onPointerUp = (e: React.PointerEvent<HTMLElement>) => {
        const p = press.current;
        if (!p || e.pointerId !== p.pointerId) return;
        press.current = null;
        if (!p.lifted) return;
        swallowClick.current = true;
        release(p);
        setLifted(null);
        const store = useCollimateStore.getState();
        const drop = store.overview.drop;
        if (drop?.kind !== 'restore' || drop.cellId !== p.cellId || !drop.point || !drop.target) {
            if (drop?.kind === 'restore') store.setOverviewDrop(null);
            return;
        }
        // The gap stays open until the cell lands in it.
        void store.restoreRemoved(p.cellId, drop.target).finally(() => {
            const now = useCollimateStore.getState();
            if (now.overview.drop?.kind === 'restore' && now.overview.drop.cellId === p.cellId) now.setOverviewDrop(null);
        });
    };

    const onPointerCancel = (e: React.PointerEvent<HTMLElement>) => {
        if (press.current && e.pointerId === press.current.pointerId) cancel();
    };

    // The click that ends a drag is not a click on a card.
    const onClickCapture = (e: React.MouseEvent<HTMLElement>) => {
        if (!swallowClick.current) return;
        swallowClick.current = false;
        e.stopPropagation();
    };

    return {
        lifted, pointerX, pointerY,
        gridHandlers: { onPointerDown, onPointerMove, onPointerUp, onPointerCancel, onClickCapture },
    };
}
