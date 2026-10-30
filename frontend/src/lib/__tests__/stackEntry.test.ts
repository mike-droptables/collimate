import { describe, it, expect } from 'vitest';
import {
    chooseLayoutAnchor, slotInView, entryReleased, enteringIds, cameraSettling,
    recordSelectionHandOff, dropSelectionHandOff, takeSelectionHandOff,
    ENTRY_HOLD_MAX_MS, ENTRY_HOLD_LIMIT_MS, ENTRY_VIEW_TOLERANCE, CAMERA_SETTLED_PX, CAMERA_SETTLED_SPEED, type LayoutAnchorInput,
} from '../stackEntry';

// Seven 500px stacks at a 514 pitch (world centers i·514). S3 (1542) is
// selected at the left edge of the view, so the camera target (2078) sits
// nearer S4 (2056) than S3: on an insert right of the selection, anchoring
// the survivor nearest the camera would move the selection.
const PITCH = 514;
const ids = (...xs: string[]) => xs;
const row = (list: string[]) => list.map((_, i) => i * PITCH);
const widths = (list: string[]) => list.map(() => 500);

const base = (over: Partial<LayoutAnchorInput>): LayoutAnchorInput => {
    const prevIds = ids('S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6');
    return {
        prevIds,
        currentIds: prevIds,
        prevPositions: row(prevIds),
        widths: widths(prevIds),
        layoutOffset: 0,
        cameraTarget: 3 * PITCH + 536,   // S4's center is nearer than S3's
        selectedId: 'S3',
        prevSelectedId: 'S3',
        alias: null,
        inheritedFrom: null,
        ...over,
    };
};

describe('chooseLayoutAnchor', () => {
    it('an insert right of the selection anchors the selected stack, not the nearer neighbour', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'S3', 'NEW', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({ currentIds, widths: widths(currentIds) }));
        expect(a).toEqual({ idx: 3, prevIdx: 3 });
    });

    it('an add that selects its new stack in the same commit anchors the previous selection', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'S3', 'NEW', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({ currentIds, widths: widths(currentIds), selectedId: 'NEW' }));
        expect(a).toEqual({ idx: 3, prevIdx: 3 });
    });

    it('an insert LEFT of the selection keeps the selection still too', () => {
        const currentIds = ids('NEW', 'S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({ currentIds, widths: widths(currentIds) }));
        expect(a).toEqual({ idx: 4, prevIdx: 3 });
    });

    it("a user remove of the selected stack slides its heir into the vacated slot (F0631)", () => {
        // S3 was removed; handleRemoveStack already moved the selection to
        // its left neighbour S2, which must slide into the vacated slot — so
        // S4, the survivor nearest the camera other than S2, anchors.
        const currentIds = ids('S0', 'S1', 'S2', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: 'S2', prevSelectedId: 'S2',
            cameraTarget: 3 * PITCH, inheritedFrom: 'S3',
        }));
        expect(currentIds[a!.idx]).toBe('S4');
        expect(a!.prevIdx).toBe(4);
    });

    it('the same removal with the selection untouched (another client removed S3) holds S2 still (F0631)', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: 'S2', prevSelectedId: 'S2',
            cameraTarget: 3 * PITCH,
        }));
        expect(a).toEqual({ idx: 2, prevIdx: 2 });
    });

    it('a removal LEFT of an untouched selection holds the selection still, even with no survivor right of it (F0631)', () => {
        // Another client removes S2; S3 was selected all along and is the
        // rightmost stack, so the nearest other survivor sits left of the gap.
        const prevIds = ids('S0', 'S1', 'S2', 'S3');
        const currentIds = ids('S0', 'S1', 'S3');
        const a = chooseLayoutAnchor(base({
            prevIds, prevPositions: row(prevIds), currentIds, widths: widths(currentIds),
            selectedId: 'S3', prevSelectedId: 'S3', cameraTarget: 3 * PITCH,
        }));
        expect(a).toEqual({ idx: 2, prevIdx: 3 });
    });

    it('a removal left of an untouched selection that sits right of the camera target holds it too (F0631)', () => {
        // S3 is selected at the view's right edge, so S1 (left of the removed
        // S2) is the survivor nearest the camera target.
        const currentIds = ids('S0', 'S1', 'S3', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), cameraTarget: 3 * PITCH - 536,
        }));
        expect(a).toEqual({ idx: 2, prevIdx: 3 });
    });

    it('[B, A]: another client removing B holds the untouched selection A still (F0631)', () => {
        const prevIds = ids('B', 'A');
        const currentIds = ids('A');
        const a = chooseLayoutAnchor(base({
            prevIds, prevPositions: row(prevIds), currentIds, widths: widths(currentIds),
            selectedId: 'A', prevSelectedId: 'A', cameraTarget: PITCH,
        }));
        expect(a).toEqual({ idx: 0, prevIdx: 1 });
    });

    it('[B, A]: a user remove of the selected B hands A the selection, and A slides into its slot (F0631)', () => {
        const prevIds = ids('B', 'A');
        const currentIds = ids('A');
        const a = chooseLayoutAnchor(base({
            prevIds, prevPositions: row(prevIds), currentIds, widths: widths(currentIds),
            selectedId: 'A', prevSelectedId: 'A', cameraTarget: 0, inheritedFrom: 'B',
        }));
        // No other survivor to pin: the row keeps its raw positions, so A
        // lands at world 0, where B was.
        expect(a).toBeNull();
    });

    it('[C, B, A]: another client removing B holds A still; a user remove of the selected B slides its heir C (F0631)', () => {
        const prevIds = ids('C', 'B', 'A');
        const currentIds = ids('C', 'A');
        const common = { prevIds, prevPositions: row(prevIds), currentIds, widths: widths(currentIds) };
        expect(chooseLayoutAnchor(base({
            ...common, selectedId: 'A', prevSelectedId: 'A', cameraTarget: PITCH,
        }))).toEqual({ idx: 1, prevIdx: 2 });
        // The heir C is skipped, so A pins and C lands in B's old slot.
        expect(chooseLayoutAnchor(base({
            ...common, selectedId: 'C', prevSelectedId: 'C', cameraTarget: PITCH, inheritedFrom: 'B',
        }))).toEqual({ idx: 1, prevIdx: 2 });
    });

    it("another client removing the untouched leftmost selection's right neighbour holds the selection still (F0631)", () => {
        // A (leftmost) is selected, the camera sits near C; B goes. The
        // nearest survivor C would otherwise pin and A would slide right.
        const prevIds = ids('A', 'B', 'C', 'D');
        const currentIds = ids('A', 'C', 'D');
        const a = chooseLayoutAnchor(base({
            prevIds, prevPositions: row(prevIds), currentIds, widths: widths(currentIds),
            selectedId: 'A', prevSelectedId: 'A', cameraTarget: 2 * PITCH,
        }));
        expect(a).toEqual({ idx: 0, prevIdx: 0 });
    });

    it('a user remove of the leftmost selected stack hands the selection right; that heir is skipped (F0631)', () => {
        // handleRemoveStack moved the selection from S0 to S1 before the
        // removal landed: S1 is an heir, not an untouched selection.
        const currentIds = ids('S1', 'S2', 'S3', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: 'S1', prevSelectedId: 'S1', cameraTarget: 0,
            inheritedFrom: 'S0',
        }));
        expect(currentIds[a!.idx]).toBe('S2');
    });

    it('an inheritedFrom that this change did not remove leaves the selection untouched (F0631)', () => {
        const currentIds = ids('S0', 'S1', 'S3', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), cameraTarget: 3 * PITCH - 536, inheritedFrom: 'S4',
        }));
        expect(a).toEqual({ idx: 2, prevIdx: 3 });
    });

    it('falls back to the survivor nearest the camera target when the selection did not survive', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'X', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: 'X', prevSelectedId: 'S3',
            cameraTarget: 5 * PITCH + 10,
        }));
        expect(currentIds[a!.idx]).toBe('S5');
    });

    it('never anchors a collapsed (width 0) selection', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'S3', 'NEW', 'S4', 'S5', 'S6');
        const w = widths(currentIds);
        w[3] = 0;
        const a = chooseLayoutAnchor(base({ currentIds, widths: w, cameraTarget: 4 * PITCH }));
        expect(currentIds[a!.idx]).toBe('S4');
    });

    it('a fan toggle alias wins over the selection', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'fan-a', 'fan-b', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: 'fan-b',
            alias: { prevIdx: 3, newIdx: 4 },
        }));
        expect(a).toEqual({ idx: 4, prevIdx: 3 });
    });

    it('accounts for the accumulated layout offset when measuring distance', () => {
        const currentIds = ids('S0', 'S1', 'S2', 'S4', 'S5', 'S6');
        const a = chooseLayoutAnchor(base({
            currentIds, widths: widths(currentIds), selectedId: null, prevSelectedId: null,
            layoutOffset: -2 * PITCH, cameraTarget: 0,
        }));
        // S2 sits at world 2·514 − 2·514 = 0.
        expect(currentIds[a!.idx]).toBe('S2');
    });
});

describe('selection hand-off record', () => {
    it('names the removed stack once its removal lands with the heir selected, then forgets it (F0631)', () => {
        recordSelectionHandOff('S3', 'S2');
        // The removal has not landed yet: nothing to report, nothing settled.
        expect(takeSelectionHandOff('S2', ['S0', 'S1', 'S2', 'S3'])).toBeNull();
        expect(takeSelectionHandOff('S2', ['S0', 'S1', 'S2'])).toBe('S3');
        // Settled: a later change no longer treats S2 as an heir.
        expect(takeSelectionHandOff('S2', ['S0', 'S2'])).toBeNull();
    });

    it('settles a landed hand-off whose heir is no longer selected without reporting it (F0631)', () => {
        recordSelectionHandOff('X', 'Y');
        expect(takeSelectionHandOff('Z', ['Y', 'Z'])).toBeNull();
        expect(takeSelectionHandOff('Y', ['Z'])).toBeNull();
    });

    it('a dropped hand-off (the removal failed) is never reported (F0631)', () => {
        recordSelectionHandOff('P', 'Q');
        dropSelectionHandOff('P');
        expect(takeSelectionHandOff('Q', ['Q'])).toBeNull();
    });
});

describe('slotInView', () => {
    const view = { cameraX: 0, zoom: 1, viewWidth: 1600, gap: 14 };

    it('is Effect 1’s no-pan band at zoom 1: the viewport minus the gap inset', () => {
        expect(slotInView(-786, -286, view)).toBe(true);      // left edge at the inset
        expect(slotInView(286, 786, view)).toBe(true);        // right edge at the inset
        expect(slotInView(800, 1300, view)).toBe(false);      // wholly off-screen right
        expect(slotInView(500, 1000, view)).toBe(false);      // straddles the right edge
    });

    it('allows the tracking tolerance past either edge', () => {
        expect(slotInView(-786 - ENTRY_VIEW_TOLERANCE, 0, view)).toBe(true);
        expect(slotInView(-786 - ENTRY_VIEW_TOLERANCE - 1, 0, view)).toBe(false);
        expect(slotInView(0, 786 + ENTRY_VIEW_TOLERANCE, view)).toBe(true);
    });

    it('widens with zoom-out and follows the camera', () => {
        expect(slotInView(800, 1300, { ...view, zoom: 0.4 })).toBe(true);
        expect(slotInView(800, 1300, { ...view, cameraX: 1000 })).toBe(true);
    });
});

describe('entryReleased', () => {
    it('does not release while the camera is settling', () => {
        expect(entryReleased({ ageMs: 0, cameraSettling: true, inView: true })).toBe(false);
        expect(entryReleased({ ageMs: 2 * ENTRY_HOLD_MAX_MS, cameraSettling: true, inView: false })).toBe(false);
    });

    it('releases at the hard limit whatever the camera reports', () => {
        expect(entryReleased({ ageMs: ENTRY_HOLD_LIMIT_MS - 1, cameraSettling: true, inView: false })).toBe(false);
        expect(entryReleased({ ageMs: ENTRY_HOLD_LIMIT_MS, cameraSettling: true, inView: false })).toBe(true);
    });

    it('releases at rest with the slot in view, at once', () => {
        expect(entryReleased({ ageMs: 0, cameraSettling: false, inView: true })).toBe(true);
    });

    it('holds an out-of-view slot at rest until the hold runs out', () => {
        expect(entryReleased({ ageMs: ENTRY_HOLD_MAX_MS - 1, cameraSettling: false, inView: false })).toBe(false);
        expect(entryReleased({ ageMs: ENTRY_HOLD_MAX_MS, cameraSettling: false, inView: false })).toBe(true);
    });
});

describe('enteringIds', () => {
    const prev = new Set(['a', 'b']);

    it('lists the ids new since the previous render', () => {
        expect(enteringIds(prev, [{ id: 'a' }, { id: 'n' }, { id: 'b' }], false)).toEqual(['n']);
    });

    it('skips fan columns', () => {
        expect(enteringIds(prev, [{ id: 'a' }, { id: 'fan-x', synthetic: true }], false)).toEqual([]);
    });

    it('a scene swap enters nothing', () => {
        expect(enteringIds(prev, [{ id: 'x' }, { id: 'y' }], true)).toEqual([]);
    });
});

describe('cameraSettling', () => {
    const rest = { zoomAnimating: false, panAnimating: false, panX: 100, panVelocity: 0, panGoal: 100, gated: false, targetPending: false };

    it('is false at rest', () => {
        expect(cameraSettling(rest)).toBe(false);
    });

    it('is true while a pan spring is more than a pixel from its goal', () => {
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 400, panGoal: 100 })).toBe(true);
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 100 + CAMERA_SETTLED_PX + 0.1 })).toBe(true);
    });

    it('treats the spring’s slow sub-pixel tail as settled', () => {
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 100 + CAMERA_SETTLED_PX - 0.1, panVelocity: 20 })).toBe(false);
    });

    it('an overshoot crossing the goal at speed is still settling', () => {
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 100.2, panVelocity: CAMERA_SETTLED_SPEED + 1 })).toBe(true);
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 100.2, panVelocity: -300 })).toBe(true);
    });

    it('is true while a zoom spring runs', () => {
        expect(cameraSettling({ ...rest, zoomAnimating: true })).toBe(true);
    });

    it('counts a target the pan effect has not acted on, unless a gate holds the camera', () => {
        expect(cameraSettling({ ...rest, targetPending: true })).toBe(true);
        expect(cameraSettling({ ...rest, targetPending: true, gated: true })).toBe(false);
    });

    it('a settling tail with a new target pending still counts', () => {
        expect(cameraSettling({ ...rest, panAnimating: true, panX: 100.5, targetPending: true })).toBe(true);
    });
});
