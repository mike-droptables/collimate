// The stack head's pips (C06): one per cell, the front cell's the knob. At
// rest they are quiet dots under the name; over the head the knob rises to a
// 15px ring on a rail; on the track a sheet-2 field comes up; the knob drags
// and snaps to the nearest pip, showing each cell as it passes, the knob
// sliding to it.

import { afterEach, describe, it, expect, vi } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/react';

const motion = vi.hoisted(() => ({ reduce: false }));
vi.mock('motion/react', async (original) => ({
    ...await original<typeof import('motion/react')>(),
    useReducedMotion: () => motion.reduce,
}));

import { StackCellScrubber, StackSingleRail } from './StackCellScrubber';
import { cellPipTitle } from '@/lib/stackPips';
import { retiredOr } from '@/test/retiredTokens';

type Props = React.ComponentProps<typeof StackCellScrubber>;
const TITLES = ['t0', 't1', 't2', 't3'];
const mount = (over: Partial<Props> = {}) => {
    const onSetTopSlot = vi.fn();
    const view = render(
        <StackCellScrubber slotCount={4} titles={TITLES} topSlot={1} selected={false} raised={false} onSetTopSlot={onSetTopSlot} {...over} />,
    );
    return { ...view, onSetTopSlot };
};

const track = () => document.querySelector('[data-stack-scrubber]') as HTMLElement;
const pips = () => Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-pip]'));
/** The pip in slot `i` (the front slot has none: its dot is the knob's). */
const pip = (i: number) => document.querySelector(`[data-scrub-slot="${i}"] [data-scrub-pip]`) as HTMLElement | null;
const knob = () => document.querySelector('[data-scrub-knob]') as HTMLElement;
const knobColumn = () => document.querySelector('[data-scrub-knob-column]') as HTMLElement;
/** The knob's dot, in its own column over the slots. */
const knobDot = () => knobColumn().querySelector('[data-scrub-pip]') as HTMLElement;
const field = () => document.querySelector('[data-scrub-field]') as HTMLElement;
const rail = () => document.querySelector('[data-scrub-rail]') as HTMLElement;

// Lay the slots out 40px apart from x=100 (jsdom has no layout).
const layOut = () => {
    document.querySelectorAll<HTMLElement>('[data-scrub-slot]').forEach(slot => {
        const i = Number(slot.dataset.scrubSlot);
        slot.getBoundingClientRect = () => ({ left: 100 + i * 40, width: 15, top: 0, height: 30, right: 115 + i * 40, bottom: 30, x: 100 + i * 40, y: 0, toJSON() {} }) as DOMRect;
    });
};
// jsdom has no PointerEvent with `button` / `buttons`; a MouseEvent under the
// pointer type name reaches React's handlers with those fields.
const pointer = (el: Element, type: string, init: MouseEventInit = {}) =>
    fireEvent(el, new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: 1, ...init }));

afterEach(() => { cleanup(); motion.reduce = false; });

describe('StackCellScrubber (C06)', () => {
    it('at rest: one quiet pip per cell under the name, the front one 6px teal; the track takes no pointer (C06)', () => {
        mount();
        // A pip per cell, the front one included, plus the knob's dot.
        expect(pips()).toHaveLength(5);
        const [a, cur] = [pip(0)!, knobDot()];
        // The front cell keeps its pip under the knob, which covers it.
        expect(pip(1)).not.toBeNull();
        expect(Number(getComputedStyle(knobColumn().parentElement!).zIndex)).toBeGreaterThan(Number(getComputedStyle(document.querySelector('[data-scrub-slot="1"]')!).zIndex));
        expect(a.style.width).toBe('4px');
        expect(a.style.top).toBe('23px');
        // Its colour is a class (ink-3 at 60%); nothing here fades by
        // opacity, which would run on the compositor and turn nearby text
        // grayscale while it ran.
        expect(a.className).toBe('bg-ink-3/60');
        expect(a.style.opacity).toBe('');
        expect(cur.style.width).toBe('6px');
        expect(cur.style.background).toBe('var(--teal)');
        expect(track().style.pointerEvents).toBe('none');
        expect(knob().style.pointerEvents).toBe('none');
        expect(rail().dataset.tone).toBe('hidden');
        expect(field().style.background).toBe('transparent');
    });

    it('the front pip is brass while the stack holds the selection (C06)', () => {
        mount({ selected: true });
        expect(knobDot().style.background).toBe('var(--brass)');
        cleanup();
        mount({ selected: true, raised: true });
        expect(knobDot().style.boxShadow).toContain('0 0 0 1px var(--brass)');
    });

    it('over the head the knob rises to a 15px ring on the rail and the other pips step aside (C06)', () => {
        mount({ raised: true });
        const [a, cur] = [pip(0)!, knobDot()];
        expect(cur.style.top).toBe('50%');
        expect(cur.style.width).toBe('15px');
        // Hollow in the colour under it (the canvas), a 1px teal collar.
        expect(cur.style.background).toBe('var(--canvas)');
        expect(cur.style.boxShadow).toBe('inset 0 0 0 8px var(--canvas), 0 0 0 1px var(--teal), 0 0 0 3px var(--canvas)');
        expect(a.className).toBe('bg-transparent');
        expect(rail().dataset.tone).toBe('quiet');
        expect(rail().className).toBe('bg-ink-3/55');
        expect(track().style.pointerEvents).toBe('auto');
        expect(knob().style.pointerEvents).toBe('auto');
    });

    it('on the track a sheet-2 field comes up and the rail goes a step lighter; on the knob every pip rises and the knob takes its centre dot (C06)', () => {
        mount({ raised: true });
        fireEvent.pointerEnter(track());
        expect(field().style.background).toBe('var(--sheet-2)');
        expect(field().style.height).toBe('22px');
        expect(field().style.borderRadius).toBe('4px');
        expect(rail().className).toBe('bg-ink-2');
        expect(rail().dataset.tone).toBe('lit');
        expect(knobDot().style.background).toBe('var(--sheet-2)');
        fireEvent.pointerEnter(knob());
        const [a, cur] = [pip(0)!, knobDot()];
        expect(a.style.top).toBe('50%');
        expect(a.style.width).toBe('5px');
        expect(a.className).toBe('bg-ink-2');
        expect(cur.style.background).toBe('var(--ink)');
        expect(rail().dataset.tone).toBe('hidden');
    });

    it('the knob drags: it snaps to the nearest pip and shows each cell as it passes (C06)', () => {
        const onDraggingChange = vi.fn();
        const { onSetTopSlot, rerender } = mount({ raised: true, onDraggingChange });
        layOut();
        const t = track();
        t.setPointerCapture = vi.fn();
        t.hasPointerCapture = vi.fn(() => true);
        t.releasePointerCapture = vi.fn();
        pointer(knob(), 'pointerdown', { clientX: 147 });
        expect(t.setPointerCapture).toHaveBeenCalled();
        expect(onDraggingChange).toHaveBeenLastCalledWith(true);
        // Nearest slot centre to 190 is slot 2 (187.5).
        pointer(t, 'pointermove', { clientX: 190 });
        expect(onSetTopSlot).toHaveBeenLastCalledWith(2);
        rerender(<StackCellScrubber slotCount={4} titles={TITLES} topSlot={2} selected={false} raised onSetTopSlot={onSetTopSlot} onDraggingChange={onDraggingChange} />);
        layOut();
        // Still the same slot: no second commit.
        pointer(t, 'pointermove', { clientX: 192 });
        expect(onSetTopSlot).toHaveBeenCalledTimes(1);
        // Far right: the last pip.
        pointer(t, 'pointermove', { clientX: 400 });
        expect(onSetTopSlot).toHaveBeenLastCalledWith(3);
        pointer(t, 'pointerup', { clientX: 400, buttons: 0 });
        expect(onDraggingChange).toHaveBeenLastCalledWith(false);
        expect(t.releasePointerCapture).toHaveBeenCalled();
        // Moves after the release scrub nothing.
        pointer(t, 'pointermove', { clientX: 100 });
        expect(onSetTopSlot).toHaveBeenCalledTimes(2);
    });

    it('the click that ends a knob press is the knob\'s: it never reaches the head (C06)', () => {
        const onHeadClick = vi.fn();
        render(
            <div onClick={onHeadClick}>
                <StackCellScrubber slotCount={3} titles={TITLES} topSlot={0} selected={false} raised onSetTopSlot={vi.fn()} />
            </div>,
        );
        const t = track();
        t.setPointerCapture = vi.fn();
        pointer(knob(), 'pointerdown');
        pointer(t, 'pointerup', { buttons: 0 });
        fireEvent.click(t);
        expect(onHeadClick).not.toHaveBeenCalled();
        // A later click on the track (the head's empty space) goes through.
        pointer(t, 'pointerdown');
        fireEvent.click(t);
        expect(onHeadClick).toHaveBeenCalledTimes(1);
    });

    it('a buttonless move ends a drag that lost its release (C06)', () => {
        const onDraggingChange = vi.fn();
        const { onSetTopSlot } = mount({ raised: true, onDraggingChange });
        layOut();
        pointer(knob(), 'pointerdown', { clientX: 147 });
        pointer(track(), 'pointermove', { clientX: 230, buttons: 0 });
        expect(onSetTopSlot).not.toHaveBeenCalled();
        expect(onDraggingChange).toHaveBeenLastCalledWith(false);
    });

    it('reports dragging false when unmounted mid-drag (C06)', () => {
        const onDraggingChange = vi.fn();
        const { unmount } = mount({ raised: true, onDraggingChange });
        pointer(knob(), 'pointerdown');
        expect(onDraggingChange).toHaveBeenLastCalledWith(true);
        unmount();
        expect(onDraggingChange).toHaveBeenLastCalledWith(false);
    });

    it('at rest the count sits right of the last pip, mono 11px ink-3, centred on the dots 6px past it (polish)', () => {
        mount();
        const count = document.querySelector('[data-stack-count]') as HTMLElement;
        expect(count.textContent).toBe('4');
        expect(count.parentElement).toBe(track());
        // After the last slot in the row.
        expect(track().lastElementChild!.previousElementSibling).toBe(count);
        expect(document.querySelector('[data-scrub-slot="3"]')!.nextElementSibling).toBe(count);
        expect(count.style.top).toBe(pip(0)!.style.top);
        expect(count.style.transform).toBe('translateY(-50%)');
        expect(count.style.left).toBe('calc(100% + 0.5px)');
        expect(count.style.fontFamily).toBe('var(--font-mono)');
        expect(count.style.fontSize).toBe('0.6875rem');
        expect(count.style.color).toBe('var(--ink-3)');
        expect(count.style.pointerEvents).toBe('none');
        expect(count.style.opacity).toBe('');
    });

    it('a two-digit count moves the track\'s right end in by a digit, so it stays clear of the right arrow', () => {
        mount();
        expect(track().style.right).toBe('58px');
        cleanup();
        render(<StackCellScrubber slotCount={11} titles={Array.from({ length: 11 }, (_, i) => `slot ${i}`)} topSlot={0} selected={false} raised={false} onSetTopSlot={vi.fn()} />);
        expect(track().style.right).toBe('65px');
        expect(document.querySelector('[data-stack-count]')!.textContent).toBe('11');
    });

    it('the count fades out as the pips rise: over the head, on a drag or a reveal; with the pips\' ease, none under reduced motion (polish)', () => {
        const { rerender, onSetTopSlot } = mount();
        const count = () => document.querySelector('[data-stack-count]') as HTMLElement;
        expect(count().style.transition).toBe('color 160ms ease');
        rerender(<StackCellScrubber slotCount={4} titles={TITLES} topSlot={1} selected={false} raised onSetTopSlot={onSetTopSlot} />);
        expect(count().style.color).toBe('transparent');
        rerender(<StackCellScrubber slotCount={4} titles={TITLES} topSlot={1} selected={false} raised={false} revealed onSetTopSlot={onSetTopSlot} />);
        expect(count().style.color).toBe('transparent');
        cleanup();
        motion.reduce = true;
        mount({ raised: true });
        expect(count().style.color).toBe('transparent');
        expect(count().style.transition).toBe('none');
    });

    it('a front change reveals every pip on the rail for a moment (C06)', () => {
        mount({ revealed: true });
        const [a, cur] = [pip(0)!, knobDot()];
        expect(a.style.top).toBe('50%');
        expect(a.className).toBe('bg-ink-2');
        expect(cur.style.width).toBe('15px');
    });

    it('the knob is its own column at its pip, and snaps to a new pip like a magnet while the front changes at once (C06, polish)', () => {
        const { rerender, onSetTopSlot } = mount({ raised: true });
        // Slot 1 of 4: a third of the way from the first pip's centre to the last's.
        expect(Number.parseFloat(knobColumn().style.left)).toBeCloseTo(100 / 3, 3);
        expect(knobColumn().style.marginLeft).toBe('-7.5px');
        // The magnet: 220ms, a gentle start then a hard pull into place, no
        // overshoot (the browser test samples the motion itself).
        expect(knobColumn().style.transition).toBe('left 220ms cubic-bezier(.65,0,.15,1)');
        // A step (Alt-↓, the Commands pane) or a drop: the new front, the knob's new place.
        rerender(<StackCellScrubber slotCount={4} titles={TITLES} topSlot={3} selected={false} raised onSetTopSlot={onSetTopSlot} />);
        expect(knobColumn().style.left).toBe('100%');
        // The pip the knob slides toward stays until the knob covers it, and
        // the one it left stays too: neither vanishes as the front changes.
        expect(pip(1)).not.toBeNull();
        expect(pip(3)).not.toBeNull();
        expect(knob().title).toBe('t3');
        // The knob's grab target and title travel with it.
        expect(knobColumn().contains(knob())).toBe(true);
    });

    it('the knob slides while dragged too, following quickly: each snap moves the column (C06, polish)', () => {
        const { onSetTopSlot, rerender } = mount({ raised: true });
        layOut();
        const t = track();
        t.setPointerCapture = vi.fn();
        pointer(knob(), 'pointerdown', { clientX: 147 });
        pointer(t, 'pointermove', { clientX: 230 });
        expect(onSetTopSlot).toHaveBeenLastCalledWith(3);
        rerender(<StackCellScrubber slotCount={4} titles={TITLES} topSlot={3} selected={false} raised onSetTopSlot={onSetTopSlot} />);
        expect(knobColumn().style.left).toBe('100%');
        // Under the drag the pointer leads: the quick follow, not the magnet.
        expect(knobColumn().style.transition).toBe('left 140ms cubic-bezier(.2,.7,.2,1)');
        pointer(t, 'pointerup', { clientX: 230, buttons: 0 });
        expect(knobColumn().style.transition).toBe('left 220ms cubic-bezier(.65,0,.15,1)');
    });

    it('under reduced motion the knob jumps to its pip: no slide (C06, polish)', () => {
        motion.reduce = true;
        mount({ raised: true });
        expect(knobColumn().style.transition).toBe('none');
        expect(knobDot().style.transition).toBe('none');
    });

    it('draws only from the skin tokens (S04) (C06)', () => {
        const { container } = mount({ raised: true });
        expect(container.innerHTML).not.toMatch(retiredOr('rgba', 'color-mix', '#[0-9a-fA-F]{3,8}\\b'));
    });

    it('titles every pip, and the knob, with its cell\'s time and name (C06)', () => {
        mount({ raised: true });
        const slots = Array.from(document.querySelectorAll<HTMLElement>('[data-scrub-slot]'));
        expect(slots.map(slot => slot.title)).toEqual(TITLES);
        expect(knob().title).toBe('t1');
    });

    it('a pip title reads `<day> <mon> <year> · <HH:MM> · <name>` in local time, the name alone without a time (C06)', () => {
        const at = new Date(2026, 9, 4, 10, 20);
        expect(cellPipTitle({ name: 'Draft', updated_at: at.toISOString() })).toBe('4 Oct 2026 · 10:20 · Draft');
        expect(cellPipTitle({ name: 'Draft', created_at: at.toISOString() })).toBe('4 Oct 2026 · 10:20 · Draft');
        expect(cellPipTitle({ name: '' })).toBe('Untitled');
    });
});

describe('StackSingleRail', () => {
    const box = () => document.querySelector('[data-stack-single-rail]') as HTMLElement;

    it('a single cell\'s head: no pip, no knob and no count; over the head only the hairline rail comes up, and it still takes the pointer (polish)', () => {
        const { rerender } = render(<StackSingleRail raised={false} />);
        expect(document.querySelector('[data-scrub-knob], [data-scrub-slot], [data-scrub-knob-column], [data-stack-count]')).toBeNull();
        expect(pips()).toHaveLength(0);
        expect(box().style.pointerEvents).toBe('none');
        expect(rail().dataset.tone).toBe('hidden');
        rerender(<StackSingleRail raised />);
        expect(pips()).toHaveLength(0);
        expect(document.querySelector('[data-scrub-knob-column]')).toBeNull();
        expect(box().style.pointerEvents).toBe('auto');
        expect(rail().dataset.tone).toBe('quiet');
        expect(field().style.background).toBe('transparent');
        fireEvent.pointerEnter(box());
        expect(rail().dataset.tone).toBe('lit');
        expect(rail().className).toBe('bg-ink-2');
        expect(field().style.background).toBe('var(--sheet-2)');
    });
});
