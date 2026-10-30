import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// The module never loads; the shadow root and its mount element exist
// regardless, which is all the scrim's replay reaches into.
vi.mock('@/lib/nativeRendererCache', () => ({
    getCachedRenderer: vi.fn(() => Promise.reject(new Error('wheel test: no module'))),
}));

import { NativeRenderer } from './NativeRenderer';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('NativeRenderer focus scrim wheel', () => {
    it('lets the wheel bubble (the card selects on it), cancels its native scroll and replays its travel once, after the flip', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const frames: FrameRequestCallback[] = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        const selected = vi.fn();
        const { container } = render(
            <div onWheel={selected}>
                <NativeRenderer rendererId="wheel-scrim" fileContent="" fileName="w.txt" isActiveFocus={false} />
            </div>,
        );
        await screen.findByText('wheel test: no module');
        const shadow = (container.querySelector('[data-native-renderer] > div') as HTMLElement).shadowRoot!;
        const scroller = document.createElement('div');
        scroller.style.overflowY = 'auto';
        Object.defineProperties(scroller, { scrollHeight: { value: 900 }, clientHeight: { value: 300 } });
        shadow.getElementById('mount')!.append(scroller);
        const hitTest = vi.fn(() => scroller);
        Object.defineProperty(shadow, 'elementFromPoint', { value: hitTest });

        const scrim = screen.getByTitle('Click to focus');
        frames.length = 0;
        const wheels = [
            new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 30, clientX: 40, clientY: 50 }),
            new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 25, clientX: 41, clientY: 52 }),
        ];
        act(() => { for (const w of wheels) scrim.dispatchEvent(w); });
        expect(selected).toHaveBeenCalledTimes(2);
        // The replay is the wheel's only delivery: the native scroll would
        // land in the flipped host too and scroll the first notch twice.
        expect(wheels.map(w => w.defaultPrevented)).toEqual([true, true]);
        expect(scroller.scrollTop).toBe(0);
        // Two frames: the focus commit, then the paint that flips the host.
        frames.shift()!(0);
        expect(scroller.scrollTop).toBe(0);
        frames.shift()!(0);
        expect(hitTest).toHaveBeenCalledWith(41, 52);
        // Then the travel animates from where the content stood, both
        // notches folded into the one run, until it settles at their sum.
        expect(frames).toHaveLength(1);
        let now = 0;
        while (frames.length) frames.shift()!(now += 1000 / 60);
        expect(scroller.scrollTop).toBe(55);
    });

    it('leaves the swipe, the pinch and a wheel over an opaque frame to the browser', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const frames: FrameRequestCallback[] = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        const selected = vi.fn();
        const { container } = render(
            <div onWheel={selected}>
                <NativeRenderer rendererId="wheel-scrim" fileContent="" fileName="w.website" isActiveFocus={false} />
            </div>,
        );
        await screen.findByText('wheel test: no module');
        const shadow = (container.querySelector('[data-native-renderer] > div') as HTMLElement).shadowRoot!;
        // A native renderer's own frame (a .website preview): a sandboxed
        // frame the host cannot see into.
        const frame = document.createElement('iframe');
        Object.defineProperty(frame, 'contentDocument', { value: null });
        frame.getBoundingClientRect = () => ({ left: 0, top: 100, right: 300, bottom: 400, width: 300, height: 300 } as DOMRect);
        shadow.getElementById('mount')!.append(frame);

        const scrim = screen.getByTitle('Click to focus');
        frames.length = 0;
        const wheels = [
            new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaX: 60, deltaY: 5 }),
            new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 30, ctrlKey: true }),
            new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 30, clientX: 20, clientY: 150 }),
        ];
        act(() => { for (const w of wheels) scrim.dispatchEvent(w); });
        expect(selected).toHaveBeenCalledTimes(3);
        expect(wheels.map(w => w.defaultPrevented)).toEqual([false, false, false]);
        expect(frames).toEqual([]);
    });
});
