// The fan bracket (C08): a lead, the name "· fanned", a line over the
// columns, Collapse and the stack menu. No border, no per-column heads; the
// name and Collapse slide along the bracket to stay in view, and a press on
// it selects the fanned stack and drags the camera.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act, within } from '@testing-library/react';
import { motionValue } from 'motion/react';
import { FanFrame } from './FanFrame';

import { useCollimateStore } from '@/stores/useCollimateStore';
import { retiredOr } from '@/test/retiredTokens';

type Props = React.ComponentProps<typeof FanFrame>;
const base = (): Props => ({
    left: 100,
    width: 1000,
    top: 36,
    name: 'Drafts',
    count: 3,
    cellIds: ['a', 'b', 'c'],
    selected: false,
    targeted: false,
    transition: { duration: 0 },
    onCollapse: vi.fn(),
    onSelectStack: vi.fn(),
    onRename: vi.fn(),
    onDuplicateStack: vi.fn(),
    onMergeStack: vi.fn(),
    onRemove: vi.fn(),
});
const mount = (over: Partial<Props> = {}) => {
    const props = { ...base(), ...over };
    const view = render(<FanFrame {...props} />);
    return { ...view, props };
};

const group = () => screen.getByRole('group', { name: /^Fanned-out stack/ });
const frame = () => document.querySelector('[data-fan-frame]') as HTMLElement;
const lead = () => document.querySelector('[data-fan-lead]') as HTMLElement;
const line = () => document.querySelector('[data-fan-line]') as HTMLElement;
const track = () => document.querySelector('[data-fan-track]') as HTMLElement;
const fanName = () => document.querySelector('[data-fan-name]') as HTMLElement;
const menuButton = () => screen.getByRole('button', { name: 'Stack menu' });
// jsdom has no PointerEvent with a `button`; a MouseEvent under the pointer
// type name carries the fields the gesture reads.
const press = (el: HTMLElement, type: string, init: MouseEventInit = {}) =>
    fireEvent(el, new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, ...init }));

const resetStore = () => useCollimateStore.setState({ runs: {}, stoppingRunIds: new Set(), runPhases: {} } as never);

describe('FanFrame — the bracket (C08)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('a 33px bracket: lead, "name · fanned", line, Collapse, …; no border (C08)', () => {
        mount();
        expect(frame().style.height).toBe('33px');
        expect(frame().style.border).toBe('');
        expect(fanName().textContent).toBe('Drafts · fanned');
        expect(lead().style.width).toBe('16px');
        expect(lead().style.height).toBe('1px');
        expect(line().style.flex).toContain('1');
        expect(screen.getByRole('button', { name: 'Collapse fan-out' })).toHaveTextContent('Collapse');
        expect(menuButton().getAttribute('aria-haspopup')).toBe('menu');
        expect(screen.queryByText(/3 cells/)).toBeNull();
        expect(group()).toHaveAccessibleName('Fanned-out stack: Drafts');
    });

    it('the bracket reads "name · fanned" only: no cell count, the name as on the stack head (C08, polish)', () => {
        mount({ count: 4 });
        expect(fanName().textContent).toBe('Drafts · fanned');
        expect(document.querySelector('[data-fan-count], [data-stack-count]')).toBeNull();
        expect(fanName().nextElementSibling!.hasAttribute('data-fan-line')).toBe(true);
    });

    it('an unnamed stack reads as Untitled stack (C08)', () => {
        mount({ name: '' });
        expect(fanName().textContent).toBe('Untitled stack · fanned');
    });

    it('the line answers the pointer: a sheet-2 field, a lighter line drawn in 8px at each end (C08)', () => {
        mount();
        expect(line().style.background).toBe('var(--line-2)');
        fireEvent.pointerEnter(track());
        expect(track().style.background).toBe('var(--sheet-2)');
        expect(line().style.background).toBe('var(--ink-2)');
        expect(lead().style.width).toBe('8px');
        expect(lead().style.marginLeft).toBe('8px');
        expect(line().style.marginRight).toBe('8px');
        fireEvent.pointerLeave(track());
        expect(track().style.background).toBe('transparent');
    });

    it('brass on the selected stack: name, line and Collapse; the … always shows (C08) (C10)', () => {
        // Hidden is a colour (transparent until the fan is hovered or the
        // button focused), never opacity: the fade is a colour fade.
        mount();
        expect(menuButton().className).toContain('not-focus-visible:not-group-hover/fan:text-transparent');
        expect(menuButton().className).not.toMatch(/opacity/);
        cleanup();
        mount({ selected: true });
        expect(fanName().className).toContain('text-brass');
        expect(line().style.background).toBe('var(--brass)');
        expect(lead().style.background).toBe('var(--brass)');
        expect(screen.getByRole('button', { name: 'Collapse fan-out' }).className).toContain('text-brass');
        expect(menuButton().className).not.toContain('text-transparent');
        expect(menuButton().className).not.toMatch(/opacity/);
    });

    it('an unselected fan names itself in ink-2 (C08)', () => {
        mount();
        expect(fanName().className).toContain('text-ink-2');
    });

    it('a drop target inside the fan lights the line teal (C08)', () => {
        mount({ targeted: true });
        expect(line().style.background).toBe('var(--teal)');
        expect(frame().outerHTML).not.toMatch(retiredOr('color-mix', 'rgba', '#[0-9a-fA-F]{3,8}\\b'));
    });

    it('Collapse fires onCollapse and does not select the stack (C08)', () => {
        const { props } = mount();
        const collapse = screen.getByRole('button', { name: 'Collapse fan-out' });
        fireEvent.pointerDown(collapse);
        fireEvent.click(collapse);
        expect(props.onCollapse).toHaveBeenCalledTimes(1);
        expect(props.onSelectStack).not.toHaveBeenCalled();
    });
});

describe('FanFrame — the stack menu (C08)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    const item = (label: string) =>
        within(screen.getByRole('menu')).getByRole('menuitem', { name: new RegExp(`^${label}`) });

    it('the … opens the stack menu: Rename, Collapse, Duplicate, Merge, Remove; each acts on the fanned stack (C08)', () => {
        const { props } = mount();
        fireEvent.pointerDown(menuButton());
        fireEvent.click(menuButton());
        const menu = screen.getByRole('menu', { name: 'Stack menu: Drafts' });
        const names = within(menu).getAllByRole('menuitem').map(el => el.textContent ?? '');
        ['Rename', 'Collapse', 'Duplicate', 'Merge', 'Remove'].forEach((label, i) => expect(names[i].startsWith(label)).toBe(true));
        expect(props.onSelectStack).not.toHaveBeenCalled();
        fireEvent.click(item('Collapse'));
        expect(props.onCollapse).toHaveBeenCalledTimes(1);
        fireEvent.click(menuButton());
        fireEvent.click(item('Duplicate'));
        expect(props.onDuplicateStack).toHaveBeenCalledTimes(1);
        fireEvent.click(menuButton());
        fireEvent.click(item('Merge'));
        expect(props.onMergeStack).toHaveBeenCalledTimes(1);
        fireEvent.click(menuButton());
        fireEvent.click(item('Remove'));
        expect(props.onRemove).toHaveBeenCalledTimes(1);
    });

    it('Rename puts an input in the name\'s place; Enter saves (C08)', () => {
        const { props } = mount();
        fireEvent.click(menuButton());
        fireEvent.click(item('Rename'));
        const input = screen.getByLabelText('Stack name') as HTMLInputElement;
        expect(input.value).toBe('Drafts');
        expect(document.querySelector('[data-fan-name]')).toBeNull();
        fireEvent.change(input, { target: { value: ' Final ' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(props.onRename).toHaveBeenCalledExactlyOnceWith('Final');
        expect(fanName().textContent).toBe('Drafts · fanned');
    });

    it('a live run greys Merge and Remove, and Remove rechecks at activation (C08)', () => {
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'b', channel: 'ch', runnerId: 'x', runnerName: 'X', generatorName: 'g', label: 'x', folder: '_runs/x' } },
            stoppingRunIds: new Set(), runPhases: {},
        } as never);
        mount();
        fireEvent.click(menuButton());
        expect(item('Merge').getAttribute('aria-disabled')).toBe('true');
        expect(item('Remove').getAttribute('aria-disabled')).toBe('true');
        expect(item('Duplicate').getAttribute('aria-disabled')).toBeNull();
    });

    it('a fan has no add action and no Recall: Remove closes its menu (C08)', () => {
        mount();
        fireEvent.click(menuButton());
        const names = within(screen.getByRole('menu')).getAllByRole('menuitem').map(el => el.textContent ?? '');
        expect(names.some(name => /^Add |^Recall/.test(name))).toBe(false);
        expect(names[names.length - 1].startsWith('Remove')).toBe(true);
    });
});

describe('FanFrame — drag to pan (C08)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    it('a press on the bracket selects the stack and starts the camera pan; moves and the release follow (C08)', () => {
        const onPanStart = vi.fn();
        const onPanMove = vi.fn();
        const onPanEnd = vi.fn(() => true);
        const { props } = mount({ onPanStart, onPanMove, onPanEnd });
        const el = group();
        el.setPointerCapture = vi.fn();
        el.hasPointerCapture = vi.fn(() => true);
        el.releasePointerCapture = vi.fn();
        // The press lands on the line; it bubbles to the bracket.
        press(line(), 'pointerdown', { clientX: 10, clientY: 10 });
        expect(props.onSelectStack).toHaveBeenCalledTimes(1);
        expect(onPanStart).toHaveBeenCalledTimes(1);
        press(el, 'pointermove', { clientX: 40, clientY: 10 });
        expect(onPanMove).toHaveBeenCalledTimes(1);
        press(el, 'pointerup');
        expect(onPanEnd).toHaveBeenCalledTimes(1);
        expect(track().style.cursor).toBe('grab');
    });

    it('a click centres nothing: the bracket has no click-to-centre (C08)', () => {
        const onPanStart = vi.fn();
        const onPanEnd = vi.fn(() => false);
        const { props } = mount({ onPanStart, onPanEnd });
        const el = group();
        el.setPointerCapture = vi.fn();
        el.hasPointerCapture = vi.fn(() => true);
        el.releasePointerCapture = vi.fn();
        press(el, 'pointerdown');
        press(el, 'pointerup');
        fireEvent.click(el);
        expect(props.onSelectStack).toHaveBeenCalledTimes(1);
        expect(props.onCollapse).not.toHaveBeenCalled();
        expect(onPanEnd).toHaveBeenCalledTimes(1);
    });

    it('without pan callbacks a press still only selects (C08)', () => {
        const { props } = mount();
        press(group(), 'pointerdown');
        expect(props.onSelectStack).toHaveBeenCalledTimes(1);
        expect(track().style.cursor).toBe('');
    });
});

// The content box inside the bracket: its x offset and width.
const headerContent = () => {
    const el = document.querySelector('[data-fan-header-content]') as HTMLElement;
    const m = /translateX\((-?[\d.]+)px\)/.exec(el.style.transform);
    return { x: m ? Number(m[1]) : 0, width: parseFloat(el.style.width) };
};

describe('FanFrame — sliding labels (C08)', () => {
    beforeEach(() => { cleanup(); resetStore(); });

    // Bracket 100..1100 (world). A 1400px viewport at zoom 1 sees world
    // [cameraX − 700, cameraX + 700].

    it('without a camera the content spans the whole bracket (C08)', () => {
        mount();
        expect(headerContent()).toEqual({ x: 0, width: 1000 });
    });

    it('a bracket that fits on screen is unchanged (C08)', () => {
        mount({ cameraX: motionValue(600), zoom: motionValue(1), viewportWidth: 1400 });
        expect(headerContent()).toEqual({ x: 0, width: 1000 });
    });

    it('a fan wider than the viewport keeps its name and Collapse in the visible window (C08)', () => {
        // Viewport 400px wide around world 600 → world 400..800.
        mount({ cameraX: motionValue(600), zoom: motionValue(1), viewportWidth: 400 });
        expect(headerContent()).toEqual({ x: 300, width: 400 });
        const content = document.querySelector('[data-fan-header-content]') as HTMLElement;
        expect(content).toContainElement(fanName());
        expect(content).toContainElement(screen.getByRole('button', { name: 'Collapse fan-out' }));
        expect(content).toContainElement(menuButton());
    });

    it('follows the camera without a re-render, clamped to the bracket (C08)', async () => {
        const cameraX = motionValue(600);
        mount({ cameraX, zoom: motionValue(1), viewportWidth: 400 });
        // Pan left: world 100..500 → the bracket's left end is on screen.
        act(() => cameraX.set(300));
        await waitFor(() => expect(headerContent()).toEqual({ x: 0, width: 400 }));
        // Pan right: world 700..1100 → its right end is on screen.
        act(() => cameraX.set(900));
        await waitFor(() => expect(headerContent()).toEqual({ x: 600, width: 400 }));
        // Only 200px visible (world 900..1300): the content keeps its 220px
        // minimum, flush with the bracket's right end — never past it.
        act(() => cameraX.set(1100));
        await waitFor(() => expect(headerContent()).toEqual({ x: 780, width: 220 }));
    });

    it('reads the bracket\'s own animated position when it moves (C08)', async () => {
        const props = { ...base(), cameraX: motionValue(600), zoom: motionValue(1), viewportWidth: 400 };
        const { rerender } = render(<FanFrame {...props} />);
        expect(headerContent()).toEqual({ x: 300, width: 400 });
        // The bracket slides 200px right: 300..1300, window still 400..800.
        rerender(<FanFrame {...props} left={300} />);
        await waitFor(() => expect(headerContent()).toEqual({ x: 100, width: 400 }));
        expect(frame().style.transform).toContain('translateX(300px)');
    });
});
