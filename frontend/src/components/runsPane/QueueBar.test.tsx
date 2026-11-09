// A run's queue: the bar line between the messages above it and the held
// ones below (only while the bar is on), and the reorder by the drag handle,
// from the keyboard (↑↓) and by a pointer drag (over 4px; Esc cancels and
// gives the keyboard back), across the bar both ways.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';

const gesture = vi.hoisted(() => ({ release: vi.fn(), claim: vi.fn() }));
vi.mock('@/lib/keyboardOwner', async (importOriginal) => ({
    ...(await importOriginal<object>()),
    claimKeyboardForGesture: () => { gesture.claim(); return gesture.release; },
}));

import { QueueSection } from './QueueBar';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { CELL_ID, msg, seedPaneStore } from '@/test/annotationFixtures';
import type { Thread } from '@/lib/annotations';

const t = (id: string): Thread => ({ root: msg(id), replies: [] });
const reorder = () => vi.mocked(useCollimateStore.getState().reorderContextQueue);
const mount = (barOn = true, readOnly = false) => render(
    <QueueSection cellId={CELL_ID} contextId="run-1" unheld={[t('a'), t('b')]} held={[t('c')]} barOn={barOn} readOnly={readOnly}
        render={(thread, handle) => <div data-testid="row" data-id={thread.root.id}>{handle}</div>} />,
);
const handleOf = (id: string) => document.querySelector(`[data-id="${id}"] [data-testid="message-drag"]`) as HTMLElement;

/** Each slot (rows and the bar) a 100px band, in order. */
function layout() {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        const slots = [...document.querySelectorAll('[data-queue-slot]')];
        const i = slots.indexOf(this);
        const top = i < 0 ? 0 : i * 100;
        return { x: 0, y: top, top, left: 0, right: 200, bottom: top + 100, width: 200, height: 100, toJSON: () => ({}) } as DOMRect;
    });
}

beforeEach(() => {
    gesture.claim.mockReset();
    gesture.release.mockReset();
    seedPaneStore({ reorderContextQueue: vi.fn(async () => {}) });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('QueueSection', () => {
    it('draws the bar line between the queue and the held messages only while the bar is on', () => {
        mount(true);
        const slots = [...document.querySelectorAll('[data-queue-slot]')].map(el => el.getAttribute('data-queue-slot'));
        expect(slots).toEqual(['a', 'b', 'bar', 'c']);
        expect(screen.getByTestId('queue-bar-line').textContent).toBe('Held below the bar');
        cleanup();
        mount(false);
        expect(screen.queryByTestId('queue-bar-line')).toBeNull();
        cleanup();
        render(<QueueSection cellId={CELL_ID} contextId="run-1" unheld={[]} held={[]} barOn={false} readOnly={false} render={() => null} />);
        expect(screen.queryByTestId('queue-section')).toBeNull();
    });

    it('↑↓ on a handle move the message one place, across the bar both ways', () => {
        mount(true);
        fireEvent.keyDown(handleOf('b'), { key: 'ArrowDown' });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['a', 'b', 'c'], 1);
        fireEvent.keyDown(handleOf('c'), { key: 'ArrowUp' });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['a', 'b', 'c'], 3);
        fireEvent.keyDown(handleOf('b'), { key: 'ArrowUp' });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['b', 'a', 'c'], 2);
        fireEvent.keyDown(handleOf('a'), { key: 'ArrowUp' });
        fireEvent.keyDown(handleOf('a'), { key: 'x' });
        expect(reorder()).toHaveBeenCalledTimes(3);
    });

    it('a pointer drag shows where the message lands and reorders on release', () => {
        layout();
        mount(true);
        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 12 });
        expect(screen.queryByTestId('queue-drop-indicator')).toBeNull();
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 260 });
        expect(gesture.claim).toHaveBeenCalledTimes(1);
        const indicator = screen.getByTestId('queue-drop-indicator');
        expect(indicator.nextElementSibling).toBe(document.querySelector('[data-queue-slot="c"]'));
        expect(document.querySelector('[data-queue-slot="a"]')!.className).toContain('opacity-45');
        fireEvent.pointerMove(window, { pointerId: 2, clientX: 10, clientY: 400 });
        fireEvent.pointerUp(window, { pointerId: 1, clientX: 10, clientY: 260 });
        expect(reorder()).toHaveBeenCalledWith(CELL_ID, 'run-1', ['b', 'a', 'c'], 1);
        expect(gesture.release).toHaveBeenCalled();
        expect(screen.queryByTestId('queue-drop-indicator')).toBeNull();
    });

    it('a drop at the end puts the indicator last; a drop where it started changes nothing', () => {
        layout();
        mount(true);
        fireEvent.pointerDown(handleOf('b'), { button: 0, pointerId: 1, clientX: 10, clientY: 110 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 900 });
        expect(screen.getByTestId('queue-section').lastElementChild).toBe(screen.getByTestId('queue-drop-indicator'));
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 140 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(reorder()).not.toHaveBeenCalled();
    });

    it('Esc, a cancelled pointer or a lost capture cancel the drag and give the keyboard back', () => {
        layout();
        mount(true);
        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 260 });
        const later = vi.fn();
        window.addEventListener('keydown', later);
        fireEvent.keyDown(window, { key: 'a' });
        fireEvent.keyDown(window, { key: 'Escape' });
        window.removeEventListener('keydown', later);
        expect(later).toHaveBeenCalledTimes(1);
        expect(gesture.release).toHaveBeenCalledTimes(1);
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(reorder()).not.toHaveBeenCalled();

        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 3, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 3, clientX: 10, clientY: 260 });
        fireEvent.pointerCancel(window, { pointerId: 9 });
        expect(screen.getByTestId('queue-drop-indicator')).toBeInTheDocument();
        fireEvent.pointerCancel(window, { pointerId: 3 });
        expect(screen.queryByTestId('queue-drop-indicator')).toBeNull();

        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 4, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 4, clientX: 10, clientY: 260 });
        fireEvent(handleOf('a'), new Event('lostpointercapture'));
        expect(screen.queryByTestId('queue-drop-indicator')).toBeNull();
        expect(reorder()).not.toHaveBeenCalled();
    });

    it('a press that never moves 4px is no drag; a secondary press is ignored; an unmount mid-drag gives the keyboard back', () => {
        layout();
        const { unmount } = mount(true);
        fireEvent.pointerDown(handleOf('a'), { button: 2, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 300 });
        expect(gesture.claim).not.toHaveBeenCalled();
        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 12, clientY: 12 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(gesture.claim).not.toHaveBeenCalled();
        expect(reorder()).not.toHaveBeenCalled();
        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 2, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 2, clientX: 10, clientY: 260 });
        unmount();
        expect(gesture.release).toHaveBeenCalled();
    });

    it('under a filter it shows only the messages shown and the bar, yet each move orders the whole queue', () => {
        layout();
        // a, x, b | bar | y, c with x and y hidden.
        render(<QueueSection cellId={CELL_ID} contextId="run-1" unheld={[t('a'), t('x'), t('b')]} held={[t('y'), t('c')]} shown={new Set(['a', 'b', 'c'])}
            barOn readOnly={false} render={(thread, handle) => <div data-testid="row" data-id={thread.root.id}>{handle}</div>} />);
        expect([...document.querySelectorAll('[data-queue-slot]')].map(el => el.getAttribute('data-queue-slot'))).toEqual(['a', 'b', 'bar', 'c']);
        fireEvent.keyDown(handleOf('b'), { key: 'ArrowUp' });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['b', 'a', 'x', 'y', 'c'], 3);
        fireEvent.keyDown(handleOf('c'), { key: 'ArrowUp' });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['a', 'x', 'b', 'c', 'y'], 4);
        // A drag below the bar's band lands after it: the hidden y stays first below the bar.
        fireEvent.pointerDown(handleOf('a'), { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(window, { pointerId: 1, clientX: 10, clientY: 260 });
        fireEvent.pointerUp(window, { pointerId: 1 });
        expect(reorder()).toHaveBeenLastCalledWith(CELL_ID, 'run-1', ['x', 'b', 'y', 'a', 'c'], 2);
    });

    it('read-only: no handles', () => {
        mount(true, true);
        expect(screen.queryByTestId('message-drag')).toBeNull();
        expect(screen.getAllByTestId('row')).toHaveLength(3);
    });
});
