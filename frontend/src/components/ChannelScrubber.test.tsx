import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, createEvent, cleanup, within } from '@testing-library/react';
import type { Stack } from '@/lib/types';
import { ChannelScrubber } from './ChannelScrubber';

/** One pip's slot, px (the mock's `.tscrub i`). */
const PIP_SLOT = 17;
import { retiredOr } from '@/test/retiredTokens';

// Synthetic display columns: the scrubber reads ids, names, cell names and
// the fan markers of a fanned stack's columns.
const stack = (id: string, name: string, cells: string[]): Stack =>
    ({ id, name, cells: cells.map((c, i) => ({ id: `${id}-c${i}`, name: c, files: [] })) }) as unknown as Stack;
const fanColumn = (source: Stack, k: number): Stack => ({
    id: `fan-${source.cells[k].id}`, name: source.cells[k].name, cells: [source.cells[k]],
    synthetic: true, sourceStackId: source.id, sourceCellId: String(source.cells[k].id),
    fanIndex: k, fanCount: source.cells.length,
}) as unknown as Stack;

const alpha = stack('a', 'Alpha', ['a1']);
const beta = stack('b', 'Beta', ['b1', 'b2', 'b3']);
const gamma = stack('c', '', ['g1', 'g2']);
const plain = [alpha, beta, gamma];
const fanned = [alpha, fanColumn(beta, 0), fanColumn(beta, 1), fanColumn(beta, 2), gamma];

/** jsdom has no PointerEvent: the init fields are set by hand. */
const pointer = (el: Element, ev: Event, fields: Record<string, number>) => {
    for (const [k, v] of Object.entries(fields)) Object.defineProperty(ev, k, { value: v });
    fireEvent(el, ev);
};

const dot = (pip: HTMLElement) => pip.firstElementChild as HTMLElement;
/** The knob's layer over the pips, and its dot. */
const knobLayer = () => document.querySelector('[data-pip-knob]') as HTMLElement;
const knobDot = () => knobLayer().firstElementChild as HTMLElement;

afterEach(cleanup);

describe('ChannelScrubber', () => {
    it('draws one pip per stack, titled by its name or its front cell, the current one a 9px brass knob (C03)', () => {
        render(<ChannelScrubber columns={plain} selectedId="b" visibleTopIndices={{ c: 1 }} onSelect={vi.fn()} />);
        const track = screen.getByRole('group', { name: 'Stacks in this channel' });
        const pips = within(track).getAllByRole('button');
        expect(pips.map(p => p.getAttribute('aria-label'))).toEqual(['Alpha', 'Beta', 'g2']);
        expect(pips.map(p => p.dataset.pip)).toEqual(['stack', 'stack', 'stack']);
        expect(pips.map(p => p.getAttribute('aria-current'))).toEqual([null, 'true', null]);
        expect(dot(pips[1])).toBeNull();
        expect(knobDot().className).toEqual(expect.stringContaining('size-[0.5625rem]'));
        expect(knobDot().className.split(/\s+/)).toContain('bg-brass');
        expect(dot(pips[0]).className.split(/\s+/)).toEqual(expect.arrayContaining(['size-[0.3125rem]', 'bg-ink-3']));
        // Pointing at the scrubber swells the pips and rings the knob.
        expect(dot(pips[0]).className).toContain('group-hover/pips:size-1.5');
        expect(knobDot().className).toContain('group-hover/pips:shadow-[inset_0_0_0_8px_var(--canvas),0_0_0_1px_var(--brass)]');
        expect(pips[1].className).toContain('cursor-grab');
        expect(pips.every(p => p.style.width === '17px')).toBe(true);
    });

    it('shows a fanned stack as one 4px teal pip per column, the selected column brass (C03)', () => {
        const { container } = render(
            <ChannelScrubber columns={fanned} selectedId="fan-b-c1" visibleTopIndices={{}} fanName="Beta" onSelect={vi.fn()} />,
        );
        const fans = [...container.querySelectorAll<HTMLElement>('[data-pip="fan"]')];
        expect(fans.map(p => p.getAttribute('aria-label'))).toEqual(['Beta · b1', 'Beta · b2', 'Beta · b3']);
        expect(fans.map(p => p.getAttribute('aria-current'))).toEqual([null, 'true', null]);
        expect(dot(fans[0]).className.split(/\s+/)).toEqual(expect.arrayContaining(['size-1', 'bg-teal']));
        expect(knobDot().className.split(/\s+/)).toContain('bg-brass');
        expect(knobLayer().style.transform).toBe(`translateX(${2 * PIP_SLOT}px)`);
        // The other stacks keep their plain pips.
        expect(container.querySelectorAll('[data-pip="stack"]')).toHaveLength(2);
        expect(container.innerHTML).not.toMatch(retiredOr('rgba', 'color-mix'));
    });

    it('the knob sits over its pip in its own layer and slides to the next one in a short ease, none under reduced motion (C03, polish)', () => {
        const onSelect = vi.fn();
        const { rerender } = render(<ChannelScrubber columns={plain} selectedId="a" visibleTopIndices={{}} onSelect={onSelect} />);
        expect(knobLayer().style.transform).toBe('translateX(0px)');
        const classes = knobLayer().className.split(/\s+/);
        expect(classes).toEqual(expect.arrayContaining(['transition-transform', 'duration-[140ms]', 'motion-reduce:transition-none', 'pointer-events-none']));
        // An arrow step: the selection moves at once, the knob follows.
        fireEvent.click(screen.getByRole('button', { name: 'Next cell' }));
        expect(onSelect).toHaveBeenCalledExactlyOnceWith('b');
        rerender(<ChannelScrubber columns={plain} selectedId="b" visibleTopIndices={{}} onSelect={onSelect} />);
        expect(knobLayer().style.transform).toBe(`translateX(${PIP_SLOT}px)`);
        rerender(<ChannelScrubber columns={plain} selectedId="c" visibleTopIndices={{}} onSelect={onSelect} />);
        expect(knobLayer().style.transform).toBe(`translateX(${2 * PIP_SLOT}px)`);
        // One knob, never a second one left behind.
        expect(document.querySelectorAll('[data-pip-knob]')).toHaveLength(1);
    });

    it('the knob rings and takes its ink centre while its pip is pointed at or dragged (C03, polish)', () => {
        render(<ChannelScrubber columns={plain} selectedId="b" visibleTopIndices={{}} onSelect={vi.fn()} />);
        expect(knobDot().className.split(/\s+/)).not.toContain('bg-ink');
        fireEvent.pointerEnter(screen.getByLabelText('Beta'));
        expect(knobDot().className.split(/\s+/)).toEqual(expect.arrayContaining(['size-[0.9375rem]', 'bg-ink']));
        fireEvent.pointerLeave(screen.getByLabelText('Beta'));
        expect(knobDot().className.split(/\s+/)).toContain('bg-brass');
    });

    it('a click on a pip selects its column; the knob itself does not click (C03)', () => {
        const onSelect = vi.fn();
        render(<ChannelScrubber columns={fanned} selectedId="a" visibleTopIndices={{}} fanName="Beta" onSelect={onSelect} />);
        fireEvent.click(screen.getByLabelText('Beta · b3'));
        expect(onSelect).toHaveBeenCalledWith('fan-b-c2');
        fireEvent.click(screen.getByLabelText('Alpha'));
        expect(onSelect).toHaveBeenCalledTimes(1);
    });

    it('the arrows step display columns, fan columns included, and are disabled at the ends (C03)', () => {
        const onSelect = vi.fn();
        const { rerender } = render(
            <ChannelScrubber columns={fanned} selectedId="a" visibleTopIndices={{}} onSelect={onSelect} />,
        );
        const prev = screen.getByRole('button', { name: 'Previous cell' });
        const next = screen.getByRole('button', { name: 'Next cell' });
        expect(prev).toBeDisabled();
        expect(prev.className).toContain('opacity-25');
        fireEvent.click(next);
        expect(onSelect).toHaveBeenLastCalledWith('fan-b-c0');

        rerender(<ChannelScrubber columns={fanned} selectedId="fan-b-c2" visibleTopIndices={{}} onSelect={onSelect} />);
        fireEvent.click(next);
        expect(onSelect).toHaveBeenLastCalledWith('c');
        fireEvent.click(prev);
        expect(onSelect).toHaveBeenLastCalledWith('fan-b-c1');

        rerender(<ChannelScrubber columns={fanned} selectedId="c" visibleTopIndices={{}} onSelect={onSelect} />);
        expect(next).toBeDisabled();
        expect(prev).not.toBeDisabled();
        // Live arrows sit dim until the scrubber is pointed at.
        expect(prev.className).toContain('opacity-50');
        expect(prev.className).toContain('group-hover/pips:opacity-100');
    });

    it('the knob drags along the pips with the stack head\'s snapping (useKnobDrag), selecting each one it passes (C03)', () => {
        const onSelect = vi.fn();
        const columns = [stack('s0', 'S0', ['x']), stack('s1', 'S1', ['x']), stack('s2', 'S2', ['x']), stack('s3', 'S3', ['x'])];
        const { container, rerender } = render(
            <ChannelScrubber columns={columns} selectedId="s0" visibleTopIndices={{}} onSelect={onSelect} />,
        );
        const track = screen.getByRole('group', { name: 'Stacks in this channel' });
        // jsdom has no layout: the pips sit in 17px slots from x=100.
        track.querySelectorAll<HTMLElement>('[data-pip]').forEach((pip, i) => {
            pip.getBoundingClientRect = () => ({ left: 100 + i * PIP_SLOT, right: 100 + (i + 1) * PIP_SLOT, top: 0, bottom: 36, width: PIP_SLOT, height: 36, x: 100 + i * PIP_SLOT, y: 0, toJSON: () => ({}) });
        });
        track.setPointerCapture = vi.fn();
        track.hasPointerCapture = vi.fn(() => true);
        track.releasePointerCapture = vi.fn();
        const knob = screen.getByLabelText('S0');

        // A press on another pip does not start a drag.
        pointer(screen.getByLabelText('S2'), createEvent.pointerDown(screen.getByLabelText('S2')), { button: 0, pointerId: 1, clientX: 137 });
        pointer(track, createEvent.pointerMove(track), { pointerId: 1, buttons: 1, clientX: 140 });
        expect(onSelect).not.toHaveBeenCalled();

        pointer(knob, createEvent.pointerDown(knob), { button: 0, pointerId: 1, clientX: 107 });
        expect(track.setPointerCapture).toHaveBeenCalledWith(1);
        expect(container.querySelector('[data-dragging]')).not.toBeNull();
        // Slot 1 spans 117..134: anywhere in it is nearest S1, selected once.
        pointer(track, createEvent.pointerMove(track), { pointerId: 1, buttons: 1, clientX: 100 + PIP_SLOT + 2 });
        pointer(track, createEvent.pointerMove(track), { pointerId: 1, buttons: 1, clientX: 100 + PIP_SLOT + 10 });
        expect(onSelect.mock.calls).toEqual([['s1']]);
        rerender(<ChannelScrubber columns={columns} selectedId="s1" visibleTopIndices={{}} onSelect={onSelect} />);
        // Past the last pip clamps to it.
        pointer(track, createEvent.pointerMove(track), { pointerId: 1, buttons: 1, clientX: 400 });
        expect(onSelect).toHaveBeenLastCalledWith('s3');
        pointer(track, createEvent.pointerUp(track), { pointerId: 1, clientX: 400 });
        expect(container.querySelector('[data-dragging]')).toBeNull();
        pointer(track, createEvent.pointerMove(track), { pointerId: 1, buttons: 1, clientX: 100 });
        expect(onSelect).toHaveBeenCalledTimes(2);
    });

    it('windows the pips to the width it is given, keeping the selection in view (C03)', () => {
        const many = Array.from({ length: 30 }, (_, i) => stack(`s${i}`, `S${i}`, ['x']));
        // 84px for the controls leaves room for 6 pips of 17px.
        const width = 84 + 6 * PIP_SLOT;
        const pipsShown = () => within(screen.getByRole('group', { name: 'Stacks in this channel' }))
            .getAllByRole('button').map(p => p.getAttribute('aria-label'));
        const { rerender } = render(
            <ChannelScrubber columns={many} selectedId="s0" visibleTopIndices={{}} onSelect={vi.fn()} maxWidth={width} />,
        );
        expect(pipsShown()).toEqual(['S0', 'S1', 'S2', 'S3', 'S4', 'S5']);
        rerender(<ChannelScrubber columns={many} selectedId="s8" visibleTopIndices={{}} onSelect={vi.fn()} maxWidth={width} />);
        expect(pipsShown()).toEqual(['S3', 'S4', 'S5', 'S6', 'S7', 'S8']);
        // Inside the window the pips hold still.
        rerender(<ChannelScrubber columns={many} selectedId="s4" visibleTopIndices={{}} onSelect={vi.fn()} maxWidth={width} />);
        expect(pipsShown()).toEqual(['S3', 'S4', 'S5', 'S6', 'S7', 'S8']);
        // Near the end the window ends on the last pip.
        rerender(<ChannelScrubber columns={many} selectedId="s29" visibleTopIndices={{}} onSelect={vi.fn()} maxWidth={width} />);
        expect(pipsShown()).toEqual(['S24', 'S25', 'S26', 'S27', 'S28', 'S29']);
    });

    it('a wheel notch steps the selection one pip (C03)', () => {
        const onSelect = vi.fn();
        const { container } = render(<ChannelScrubber columns={plain} selectedId="b" visibleTopIndices={{}} onSelect={onSelect} />);
        const root = container.firstElementChild as HTMLElement;
        fireEvent.wheel(root, { deltaY: 60 });
        expect(onSelect).not.toHaveBeenCalled();
        fireEvent.wheel(root, { deltaY: 60 });
        expect(onSelect).toHaveBeenLastCalledWith('c');
        fireEvent.wheel(root, { deltaY: -100 });
        expect(onSelect).toHaveBeenLastCalledWith('a');
    });

    it('says so when the channel has no stack (C03)', () => {
        render(<ChannelScrubber columns={[]} selectedId={null} visibleTopIndices={{}} onSelect={vi.fn()} />);
        expect(screen.getByText('no stacks open')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Next cell' })).toBeNull();
    });
});
