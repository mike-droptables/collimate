import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { CENTER_CLICK_DELAY_MS, useCenterPanGesture } from './useCenterPanGesture';

// The handlers only touch button/pointerId/target/currentTarget and the two
// event methods, so a hand-rolled event object stands in for React.PointerEvent.
const pointerEvent = (button = 0, captured = true) => {
    const el = {
        setPointerCapture: vi.fn(),
        hasPointerCapture: vi.fn(() => captured),
        releasePointerCapture: vi.fn(),
    };
    return {
        button,
        pointerId: 7,
        target: null,
        stopPropagation: vi.fn(),
        preventDefault: vi.fn(),
        currentTarget: el,
        el,
    } as unknown as React.PointerEvent & { el: typeof el };
};

// ``detail`` is the click count: 1 a single pointer click, 2 a double-click's
// second click, 0 a keyboard click.
const clickEvent = (detail = 1) => ({ detail, stopPropagation: vi.fn() }) as unknown as React.MouseEvent;
// The double-click window a pointer click's recentre waits out.
const settle = () => vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS);

describe('useCenterPanGesture', () => {
    beforeEach(() => { vi.useFakeTimers(); });
    afterEach(() => { vi.useRealTimers(); });

    it('a plain click recenters the stack once the double-click window passes', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterStack }));
        const e = clickEvent();
        result.current.handlers.onClick(e);
        expect(e.stopPropagation).toHaveBeenCalled();
        settle();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });

    it('a single click recentres after the window, not before (polish)', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterStack }));
        result.current.handlers.onClick(clickEvent());
        vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS - 1);
        expect(onCenterStack).not.toHaveBeenCalled();
        vi.advanceTimersByTime(1);
        expect(onCenterStack).toHaveBeenCalledTimes(1);
        expect(CENTER_CLICK_DELAY_MS).toBe(125);
    });

    it('a double-click within the window recentres nothing (polish)', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({
            onCenterStack, onCenterPanStart: vi.fn(), onCenterPanEnd: () => false,
        }));
        const h = result.current.handlers;
        h.onPointerDown(pointerEvent()); h.onPointerUp(pointerEvent()); h.onClick(clickEvent(1));
        vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS / 2);
        h.onPointerDown(pointerEvent()); h.onPointerUp(pointerEvent()); h.onClick(clickEvent(2));
        vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS * 4);
        expect(onCenterStack).not.toHaveBeenCalled();
    });

    it('a click then a drag recentres nothing (polish)', () => {
        const onCenterStack = vi.fn();
        let moved = false;
        const { result } = renderHook(() => useCenterPanGesture({
            onCenterStack, onCenterPanStart: vi.fn(), onCenterPanEnd: () => moved,
        }));
        const h = result.current.handlers;
        h.onPointerDown(pointerEvent()); h.onPointerUp(pointerEvent()); h.onClick(clickEvent());
        vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS / 2);
        moved = true;
        h.onPointerDown(pointerEvent()); h.onPointerMove(pointerEvent()); h.onPointerUp(pointerEvent());
        h.onClick(clickEvent());
        vi.advanceTimersByTime(CENTER_CLICK_DELAY_MS * 4);
        expect(onCenterStack).not.toHaveBeenCalled();
    });

    it('a keyboard click recentres at once (polish)', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterStack }));
        result.current.handlers.onClick(clickEvent(0));
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });

    it('unmounting drops a waiting recentre (polish)', () => {
        const onCenterStack = vi.fn();
        const { result, unmount } = renderHook(() => useCenterPanGesture({ onCenterStack }));
        result.current.handlers.onClick(clickEvent());
        unmount();
        settle();
        expect(onCenterStack).not.toHaveBeenCalled();
    });

    it('pointerdown captures the pointer and starts the pan', () => {
        const onCenterPanStart = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterPanStart }));
        const e = pointerEvent();
        result.current.handlers.onPointerDown(e);
        expect(e.stopPropagation).toHaveBeenCalled();
        expect(e.el.setPointerCapture).toHaveBeenCalledWith(7);
        expect(onCenterPanStart).toHaveBeenCalledWith(e);
    });

    it('a primary press never starts a native text selection, with or without a pan', () => {
        const { result } = renderHook(() => useCenterPanGesture({ onCenterPanStart: vi.fn() }));
        const e = pointerEvent();
        result.current.handlers.onPointerDown(e);
        expect(e.preventDefault).toHaveBeenCalled();

        const { result: bare } = renderHook(() => useCenterPanGesture({}));
        const plain = pointerEvent();
        bare.current.handlers.onPointerDown(plain);
        expect(plain.preventDefault).toHaveBeenCalled();

        const right = pointerEvent(2);
        result.current.handlers.onPointerDown(right);
        expect(right.preventDefault).not.toHaveBeenCalled();
    });

    it('the trailing click still recenters after a press that did not drag', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({
            onCenterStack, onCenterPanStart: vi.fn(), onCenterPanEnd: () => false,
        }));
        result.current.handlers.onPointerDown(pointerEvent());
        result.current.handlers.onPointerUp(pointerEvent());
        result.current.handlers.onClick(clickEvent());
        settle();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });

    it('ignores non-primary buttons and does nothing without a pan-start callback', () => {
        const onCenterPanStart = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterPanStart }));
        const right = pointerEvent(2);
        result.current.handlers.onPointerDown(right);
        expect(right.el.setPointerCapture).not.toHaveBeenCalled();
        expect(onCenterPanStart).not.toHaveBeenCalled();

        const { result: bare } = renderHook(() => useCenterPanGesture({}));
        const e = pointerEvent();
        bare.current.handlers.onPointerDown(e);
        expect(e.el.setPointerCapture).not.toHaveBeenCalled();

        const up = pointerEvent(2);
        result.current.handlers.onPointerUp(up);
        expect(up.el.releasePointerCapture).not.toHaveBeenCalled();
    });

    it('forwards pointer moves to the pan callback', () => {
        const onCenterPanMove = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterPanMove }));
        const e = pointerEvent();
        result.current.handlers.onPointerMove(e);
        expect(onCenterPanMove).toHaveBeenCalledWith(e);
        // And tolerates the callback being absent.
        const { result: bare } = renderHook(() => useCenterPanGesture({}));
        expect(() => bare.current.handlers.onPointerMove(e)).not.toThrow();
    });

    it('a drag that moved swallows exactly one trailing click', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({
            onCenterStack,
            onCenterPanStart: () => {},
            onCenterPanEnd: () => true, // "the gesture moved"
        }));
        result.current.handlers.onPointerDown(pointerEvent());
        const up = pointerEvent();
        result.current.handlers.onPointerUp(up);
        expect(up.el.releasePointerCapture).toHaveBeenCalledWith(7);
        result.current.handlers.onClick(clickEvent());
        settle();
        expect(onCenterStack).not.toHaveBeenCalled();
        // The suppression is one-shot: the next click recenters again.
        result.current.handlers.onClick(clickEvent());
        settle();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });

    it('a drag that never moved lets the click through', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({
            onCenterStack,
            onCenterPanStart: () => {},
            onCenterPanEnd: () => false,
        }));
        result.current.handlers.onPointerDown(pointerEvent());
        result.current.handlers.onPointerUp(pointerEvent());
        result.current.handlers.onClick(clickEvent());
        settle();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });

    it('pointerup without capture or an end callback resets suppression', () => {
        const onCenterStack = vi.fn();
        const { result } = renderHook(() => useCenterPanGesture({ onCenterStack }));
        const e = pointerEvent(0, false); // capture was never taken
        result.current.handlers.onPointerUp(e);
        expect(e.el.releasePointerCapture).not.toHaveBeenCalled();
        result.current.handlers.onClick(clickEvent());
        settle();
        expect(onCenterStack).toHaveBeenCalledTimes(1);
    });
});
