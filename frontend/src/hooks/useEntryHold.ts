import { useEffect, useRef, useState } from 'react';
import type { MotionValue } from 'motion/react';
import { enteringIds, entryReleased, slotInView } from '@/lib/stackEntry';

/** A laid-out display column as the hold sees it (useStackDrag's items). */
export interface EntryItem {
    id: string;
    /** World-x center. */
    x: number;
    computedWidth: number;
    synthetic?: boolean;
}

interface UseEntryHoldParams {
    items: ReadonlyArray<EntryItem>;
    /** useStackLayout's scene-swap counter: a swap is a hard cut, not an entry. */
    sceneSwapNonce: number;
    cameraXMV: MotionValue<number>;
    zoomMV: MotionValue<number>;
    /** useStackCamera's "the camera is on its way somewhere". */
    isCameraSettling: () => boolean;
    viewWidth: number;

    gap: number;
}

interface EntryTrack {
    /** The ids of the last measured render with a changed id list, joined —
     *  null until the first one, whose stacks are the scene's starting
     *  population, not entries. */
    key: string | null;
    ids: ReadonlySet<string>;
    nonce: number;
    /** The stacks still held. */
    held: ReadonlySet<string>;
}

/** Whether an entering item may show right now (see entryReleased). */
function releasedNow(item: EntryItem, ageMs: number, p: UseEntryHoldParams): boolean {
    return entryReleased({
        ageMs,
        cameraSettling: p.isCameraSettling(),
        inView: slotInView(item.x - item.computedWidth / 2, item.x + item.computedWidth / 2, {
            cameraX: p.cameraXMV.get(), zoom: p.zoomMV.get(), viewWidth: p.viewWidth, gap: p.gap,
        }),
    });
}

/** The entering stacks StackScene must keep invisible right now.
 *
 *  A stack that mounts while the camera is at rest with its slot in view
 *  is never held — it fades in at its slot on the spot, and selecting it
 *  will not move the camera (the in-view test is Effect 1's). Any other
 *  entering stack is held from its first frame: while the camera moves
 *  (an add path selecting it pans the camera there) and while its slot is
 *  out of view. A per-frame check releases it once the camera has settled
 *  with the slot in view, or — a background arrival nobody selected —
 *  once the camera is at rest and ENTRY_HOLD_MAX_MS has passed; nothing is
 *  held past ENTRY_HOLD_LIMIT_MS. Released stacks are ordinary stacks from
 *  then on. */
export function useEntryHold(params: UseEntryHoldParams): ReadonlySet<string> {
    const { items, sceneSwapNonce, viewWidth } = params;
    const [track, setTrack] = useState<EntryTrack>(() => ({
        key: null, ids: new Set(), nonce: sceneSwapNonce, held: new Set(),
    }));

    // The id list changed: record the entries during render (the stored-
    // previous-value pattern), so an entering stack is already held on its
    // mount frame and its opacity target starts at 0. Unmeasured renders
    // (viewWidth 0) carry no positions and are skipped.
    const key = items.map(it => it.id).join('\n');
    if (viewWidth > 0 && key !== track.key) {
        const ids = new Set(items.map(it => it.id));
        const held = new Set([...track.held].filter(id => ids.has(id)));
        if (track.key !== null) {
            for (const id of enteringIds(track.ids, items, sceneSwapNonce !== track.nonce)) {
                const item = items.find(it => it.id === id);
                if (item && !releasedNow(item, 0, params)) held.add(id);
            }
        }
        setTrack({ key, ids, nonce: sceneSwapNonce, held });
    }

    // The per-frame release check reads the latest layout and camera.
    const paramsRef = useRef(params);
    useEffect(() => { paramsRef.current = params; });
    // Mount time of each held stack, stamped on its first check.
    const sinceRef = useRef(new Map<string, number>());

    useEffect(() => {
        const since = sinceRef.current;
        for (const id of [...since.keys()]) if (!track.held.has(id)) since.delete(id);
        if (track.held.size === 0) return;
        let raf = 0;
        const tick = () => {
            const now = performance.now();
            const p = paramsRef.current;
            const released: string[] = [];
            for (const id of track.held) {
                if (!since.has(id)) since.set(id, now);
                const item = p.items.find(it => it.id === id);
                if (!item || releasedNow(item, now - since.get(id)!, p)) released.push(id);
            }
            if (released.length > 0) {
                setTrack(t => ({ ...t, held: new Set([...t.held].filter(id => !released.includes(id))) }));
                return;   // the new held set restarts the loop
            }
            raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(raf);
    }, [track.held]);

    return track.held;
}
