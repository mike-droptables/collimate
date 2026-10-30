import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, cleanup, screen } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell } from '@/lib/types';
import { GUEST_FOCUS_EVENT } from './guestMount';

vi.mock('./CellEditor', () => ({ default: () => <div data-testid="cell-editor" /> }));
vi.mock('./MarkdownView', () => ({ MarkdownView: () => null }));
vi.mock('./StackWidthControl', () => ({ StackWidthControl: () => null }));
vi.mock('@/hooks/useRunRequest', () => ({ useRunRequest: vi.fn() }));

import { StackItem } from './StackItem';

const cell: Cell = { id: 'cell-1', name: 'A cell', files: [], channel: 'ch' };
type Props = React.ComponentProps<typeof StackItem>;
const mount = (over: Partial<Props>) => {
    const onSelect = vi.fn();
    const view = render(<StackItem
        stackId="s1" cell={cell} index={0} isFront isSelected viewMode="stack"
        width={600} height={400}
        focusArea="main" onSelect={onSelect} onUpdate={vi.fn()}
        {...over}
    />);
    return { onSelect, view };
};
const wheelOn = (el: Element, init: WheelEventInit) => act(() => {
    el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, ...init }));
});
const card = () => document.querySelector('[data-stack-card]')!;

describe('a wheel over an unfocused cell selects it', () => {
    beforeEach(() => {
        cleanup();
        useCollimateStore.setState({
            cellCache: {}, runs: {}, stoppingRunIds: new Set(), runPhases: {}, runLogs: {},
            cellLayouts: {}, state: null, pendingSelectCellId: null,
        } as never);
    });

    it('selects an unselected cell on a vertical wheel, from anywhere in the card', () => {
        const { onSelect } = mount({ isSelected: false, index: 2 });
        wheelOn(screen.getByTestId('cell-editor'), { deltaY: 30 });
        expect(onSelect).toHaveBeenCalledWith(2);
    });

    it('takes the main focus for the nav cursor\'s cell: selected, but its renderer unfocused', () => {
        const { onSelect } = mount({ isSelected: true, focusArea: 'nav' });
        wheelOn(card(), { deltaY: 30 });
        expect(onSelect).toHaveBeenCalledWith(0);
    });

    it('leaves the focused cell alone: its renderer takes the wheel', () => {
        const { onSelect } = mount({ isSelected: true, focusArea: 'main' });
        wheelOn(card(), { deltaY: 30 });
        expect(onSelect).not.toHaveBeenCalled();
    });

    it('leaves the horizontal wheel to the canvas swipe and ctrl-wheel to the pinch', () => {
        const { onSelect } = mount({ isSelected: false });
        wheelOn(card(), { deltaX: 60, deltaY: 10 });
        wheelOn(card(), { deltaY: 30, ctrlKey: true });
        expect(onSelect).not.toHaveBeenCalled();
    });
});

describe('a wheel-select takes the keys from another cell\'s editor', () => {
    /** A renderer host whose shadow root holds a focused textarea. */
    const focusShadowTextarea = (parent: Element) => {
        const host = document.createElement('div');
        parent.appendChild(host);
        const textarea = document.createElement('textarea');
        host.attachShadow({ mode: 'open' }).appendChild(textarea);
        textarea.focus();
        return { host, textarea };
    };
    /** Another cell's card, after the mounted one (card() finds the first). */
    const otherCard = () => {
        const el = document.createElement('div');
        el.setAttribute('data-stack-card', '');
        el.setAttribute('data-other-card', '');
        document.body.appendChild(el);
        return el;
    };
    beforeEach(() => { cleanup(); });
    afterEach(() => {
        (document.activeElement as HTMLElement | null)?.blur?.();
        document.querySelectorAll('[data-other-card]').forEach(el => el.remove());
    });

    it('blurs the textarea focused in another card\'s shadow root, focusing nothing here', () => {
        const { onSelect } = mount({ isSelected: false, index: 1 });
        const { host, textarea } = focusShadowTextarea(otherCard());
        expect(host.shadowRoot!.activeElement).toBe(textarea);
        wheelOn(screen.getByTestId('cell-editor'), { deltaY: 30 });
        expect(onSelect).toHaveBeenCalledWith(1);
        expect(host.shadowRoot!.activeElement).toBeNull();
        expect(document.activeElement).toBe(document.body);
    });

    it('leaves this cell\'s own editor focused (the nav cursor\'s cell)', () => {
        const { onSelect } = mount({ isSelected: true, focusArea: 'nav' });
        const { host, textarea } = focusShadowTextarea(screen.getByTestId('cell-editor'));
        wheelOn(card(), { deltaY: 30 });
        expect(onSelect).toHaveBeenCalledWith(0);
        expect(host.shadowRoot!.activeElement).toBe(textarea);
    });

    it('does nothing on a wheel over the already-focused cell', () => {
        const { onSelect } = mount({ isSelected: true, focusArea: 'main' });
        const { host, textarea } = focusShadowTextarea(otherCard());
        wheelOn(card(), { deltaY: 30 });
        expect(onSelect).not.toHaveBeenCalled();
        expect(host.shadowRoot!.activeElement).toBe(textarea);
    });

    it('leaves the editor focused on a sideways or ctrl wheel', () => {
        const { onSelect } = mount({ isSelected: false });
        const { host, textarea } = focusShadowTextarea(otherCard());
        wheelOn(card(), { deltaX: 60, deltaY: 10 });
        wheelOn(card(), { deltaY: 30, ctrlKey: true });
        expect(onSelect).not.toHaveBeenCalled();
        expect(host.shadowRoot!.activeElement).toBe(textarea);
    });
});

describe('a live guest taking focus selects its cell', () => {
    beforeEach(() => { cleanup(); });

    it('selects on the guest\'s composed focus event, as a click would', () => {
        const { onSelect } = mount({ isSelected: false, index: 1 });
        act(() => {
            screen.getByTestId('cell-editor').dispatchEvent(new CustomEvent(GUEST_FOCUS_EVENT, { bubbles: true, composed: true }));
        });
        expect(onSelect).toHaveBeenCalledWith(1);
    });

    it('leaves the focused cell alone', () => {
        const { onSelect } = mount({ isSelected: true, focusArea: 'main' });
        act(() => { card().dispatchEvent(new CustomEvent(GUEST_FOCUS_EVENT, { bubbles: true })); });
        expect(onSelect).not.toHaveBeenCalled();
    });

    it('follows the card in and out of the overview', () => {
        const { onSelect, view } = mount({ isSelected: false, viewMode: 'overview' });
        expect(document.querySelector('[data-stack-card]')).toBeNull();
        view.rerender(<StackItem
            stackId="s1" cell={cell} index={0} isFront isSelected={false} viewMode="stack"
            width={600} height={400} focusArea="main" onSelect={onSelect} onUpdate={vi.fn()}
        />);
        act(() => { card().dispatchEvent(new CustomEvent(GUEST_FOCUS_EVENT, { bubbles: true })); });
        expect(onSelect).toHaveBeenCalledWith(0);
    });
});
