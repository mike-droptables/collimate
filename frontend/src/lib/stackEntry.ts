// How a stack ENTERS the main view: which surviving stack pins the layout
// when the stack set changes (chooseLayoutAnchor, used by useStackLayout),
// and when a newly mounted stack may fade in (the entry hold, driven by
// hooks/useEntryHold for StackScene). Both rules are pure so they are
// unit-tested; the one piece of state here is the selection hand-off record
// a user remove leaves for the anchor.
//
// Together they make every add appear AT its slot: the anchor keeps the
// camera still when a stack is inserted (neighbours make room around the
// user's focus instead of the focus sliding away and the camera chasing
// it), and the hold keeps a stack invisible while a camera pan would carry
// it across the screen — it fades in once the camera has settled with its
// slot in view.

/** Index of the anchor in the current list and of the same stack in the
 *  previous one. */
export interface LayoutAnchor {
    idx: number;
    prevIdx: number;
}

export interface LayoutAnchorInput {
    prevIds: string[];
    currentIds: string[];
    /** Previous raw (un-offset) world positions, index-aligned with prevIds. */
    prevPositions: number[];
    /** Current raw widths, index-aligned with currentIds (0 = collapsed
     *  single-cell move source, never an anchor). */
    widths: number[];
    /** The accumulated layout offset before this change. */
    layoutOffset: number;
    /** World x the camera is heading to. */
    cameraTarget: number;
    /** The scene's selection in this render and in the previous one. */
    selectedId: string | null;
    prevSelectedId: string | null;
    /** A fan toggle's stack/column pair (lib/fanOut layoutAlias). */
    alias: { prevIdx: number; newIdx: number } | null;
    /** The stack a user remove handed the selection over from
     *  (takeSelectionHandOff), or null. */
    inheritedFrom: string | null;
}

/** Which surviving stack keeps its world x across an id-set change (the
 *  layout offset absorbs the rest).
 *
 *  1. A fan toggle anchors the stack/column pair that is the same thing on
 *     screen.
 *  2. When the change brings a new stack in, the selected stack holds
 *     still — this render's selection, else the previous render's (an add
 *     path that selects its new stack in the same commit). The user's
 *     focus stays pinned, the neighbours make room, and the camera has no
 *     reason to move.
 *  3. A pure removal around an untouched selection (the same stack in
 *     this render and the previous one, e.g. another client removed a stack
 *     beside it) holds that selection still, like rule 2 — unless the
 *     selection was INHERITED from a stack this change removed
 *     (`inheritedFrom`).
 *  4. Otherwise — an inherited selection, or one that did not survive — the
 *     surviving stack nearest the camera target, skipping the selection:
 *     when the selected stack was removed, the neighbour that inherited the
 *     selection slides into the vacated slot. */
export function chooseLayoutAnchor(inp: LayoutAnchorInput): LayoutAnchor | null {
    const {
        prevIds, currentIds, prevPositions, widths, layoutOffset, cameraTarget,
        selectedId, prevSelectedId, alias, inheritedFrom,
    } = inp;
    if (alias && widths[alias.newIdx] > 0) return { idx: alias.newIdx, prevIdx: alias.prevIdx };

    const prevSet = new Set(prevIds);
    const entering = currentIds.some(id => !prevSet.has(id));
    const inherited = inheritedFrom !== null && !currentIds.includes(inheritedFrom);
    if (entering) {
        for (const id of [selectedId, prevSelectedId]) {
            if (!id) continue;
            const idx = currentIds.indexOf(id);
            const prevIdx = prevIds.indexOf(id);
            if (idx !== -1 && prevIdx !== -1 && widths[idx] > 0) return { idx, prevIdx };
        }
    } else if (selectedId && selectedId === prevSelectedId && !inherited) {
        const idx = currentIds.indexOf(selectedId);
        const prevIdx = prevIds.indexOf(selectedId);
        if (idx !== -1 && prevIdx !== -1 && widths[idx] > 0) return { idx, prevIdx };
    }

    let best: LayoutAnchor | null = null;
    let bestDist = Infinity;
    for (let i = 0; i < currentIds.length; i++) {
        if (selectedId && currentIds[i] === selectedId) continue;
        const prevIdx = prevIds.indexOf(currentIds[i]);
        if (prevIdx === -1) continue;
        const dist = Math.abs(prevPositions[prevIdx] + layoutOffset - cameraTarget);
        if (dist < bestDist) {
            bestDist = dist;
            best = { idx: i, prevIdx };
        }
    }
    return best;
}

// ── Selection hand-off ──────────────────────────────────────────────

// A user remove of the SELECTED stack hands the selection to a neighbour
// before the removal lands (useSceneActions handleRemoveStack), so by the
// time the ids change the heir reads as this render's and the previous
// render's selection, exactly like an untouched one. This record (removed
// stack id → heir id) is what tells them apart. One scene removes stacks,
// so one module-level record serves it.
const selectionHandOffs = new Map<string, string>();

/** A user remove is taking `removedId` away and handed its selection to
 *  `heirId`. */
export function recordSelectionHandOff(removedId: string, heirId: string): void {
    selectionHandOffs.set(removedId, heirId);
}

/** The removal of `removedId` did not go through: its stack stays. */
export function dropSelectionHandOff(removedId: string): void {
    selectionHandOffs.delete(removedId);
}

/** For an id-set change: the stack `selectedId` inherited the selection
 *  from, when this change removed it; else null. Settles every hand-off
 *  whose removal has landed. */
export function takeSelectionHandOff(selectedId: string | null, currentIds: readonly string[]): string | null {
    if (selectionHandOffs.size === 0) return null;
    const current = new Set(currentIds);
    let from: string | null = null;
    for (const [removed, heir] of selectionHandOffs) {
        if (current.has(removed)) continue;
        selectionHandOffs.delete(removed);
        if (heir === selectedId) from = removed;
    }
    return from;
}

// ── Entry hold ──────────────────────────────────────────────────────

/** Longest an entering stack stays hidden while the camera is at rest
 *  with its slot out of view. Covers the gap between a stack landing and
 *  its add path selecting it (which is what pans the camera there): the
 *  paths select in the same tick they apply the response, but the SSE push
 *  of the same state can arrive a little earlier. Past it the stack is a
 *  background arrival (another client, a running generator) nobody is
 *  looking at, and it becomes an ordinary stack where it sits. */
export const ENTRY_HOLD_MAX_MS = 500;

/** Longest any entering stack stays hidden, camera or not. A camera pan
 *  settles well inside it; it only bounds a camera state the settling
 *  check misreads, so a stack can never stay invisible for good. */
export const ENTRY_HOLD_LIMIT_MS = 2000;

/** How close to its goal, and how slow, a pan spring must be to count as
 *  settled. The spring's tail past that point moves the view by under a
 *  pixel, and waiting it out left the new slot visibly empty for ~250 ms
 *  after the pan. The speed bound matters: the camera springs overshoot
 *  (by ~4 px on a one-slot pan), so the first frame within a pixel of the
 *  goal is often the overshoot crossing it at speed. */
export const CAMERA_SETTLED_PX = 1;
export const CAMERA_SETTLED_SPEED = 50;   // px/s

/** Whether the camera is still on its way somewhere, for the entry hold.
 *  A zoom spring always counts; a pan spring until it is within
 *  CAMERA_SETTLED_PX of its goal and slower than CAMERA_SETTLED_SPEED; and
 *  a target the pan effect has not acted on yet counts unless a gate
 *  (zoom-out, a hold, a manual pan, …) keeps the camera where it is. */
export function cameraSettling(c: {
    zoomAnimating: boolean;
    panAnimating: boolean;
    panX: number;
    /** px/s */
    panVelocity: number;
    panGoal: number;
    gated: boolean;
    targetPending: boolean;
}): boolean {
    if (c.zoomAnimating) return true;
    if (c.panAnimating && (Math.abs(c.panX - c.panGoal) > CAMERA_SETTLED_PX
        || Math.abs(c.panVelocity) > CAMERA_SETTLED_SPEED)) return true;
    return !c.gated && c.targetPending;
}

/** How far past the visible band a card may reach and still count as in
 *  view — useStackCamera Effect 1's scroll-into-view tolerance, so "in
 *  view" here means exactly "selecting it would not move the camera". */
export const ENTRY_VIEW_TOLERANCE = 8;

export interface EntryView {
    cameraX: number;
    zoom: number;
    viewWidth: number;

    gap: number;
}

/** Whether a world-space slot [left, right] sits inside the visible band
 *  (the viewport minus the GAP inset each side), at the camera's current
 *  position and zoom. At zoom 1 this is Effect 1's no-pan test. */
export function slotInView(left: number, right: number, v: EntryView, tolerance: number = ENTRY_VIEW_TOLERANCE): boolean {
    const z = v.zoom || 1;
    const visibleLeft = v.cameraX + (-v.viewWidth / 2 + v.gap) / z;
    const visibleRight = v.cameraX + (v.viewWidth / 2 - v.gap) / z;
    return left >= visibleLeft - tolerance && right <= visibleRight + tolerance;
}

/** An entering stack's hold ends once the camera is at rest and either its
 *  slot is in view (it fades in right there) or the hold has run its
 *  course (a background arrival off-screen). While the camera moves it
 *  stays hidden — a visible stack would ride the pan — up to
 *  ENTRY_HOLD_LIMIT_MS. */
export function entryReleased(e: { ageMs: number; cameraSettling: boolean; inView: boolean }): boolean {
    if (e.ageMs >= ENTRY_HOLD_LIMIT_MS) return true;
    if (e.cameraSettling) return false;
    return e.inView || e.ageMs >= ENTRY_HOLD_MAX_MS;
}

/** Ids of the stacks that just entered the scene: in `items` but not in
 *  the previous render's ids. Fan columns (synthetic) mount at their slot
 *  under the fan toggle's own rules, and a scene swap (channel switch) is
 *  a hard cut with the camera landing on the new selection, so neither
 *  enters. */
export function enteringIds(
    prevIds: ReadonlySet<string>,
    items: ReadonlyArray<{ id: string; synthetic?: boolean }>,
    sceneSwap: boolean,
): string[] {
    if (sceneSwap) return [];
    return items.filter(it => !it.synthetic && !prevIds.has(it.id)).map(it => it.id);
}
