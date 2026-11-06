// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { fireShortcut } from '../fire';

describe('fireShortcut', () => {
    const cleanups: Array<() => void> = [];
    afterEach(() => {
        while (cleanups.length) cleanups.pop()!();
    });

    function count(target: EventTarget, capture: boolean): { n: number } {
        const c = { n: 0 };
        const fn = () => { c.n += 1; };
        target.addEventListener('keydown', fn, capture);
        cleanups.push(() => target.removeEventListener('keydown', fn, capture));
        return c;
    }

    it('delivers one keydown to window (capture and bubble) and document listeners', () => {
        const winBubble = count(window, false);
        const winCapture = count(window, true);
        const doc = count(document, false);
        expect(fireShortcut('global.openSettings')).toBe(true);
        expect(winBubble.n).toBe(1);
        expect(winCapture.n).toBe(1);
        expect(doc.n).toBe(1);
    });

    it('returns false and dispatches nothing for an unknown id', () => {
        const winBubble = count(window, false);
        const doc = count(document, false);
        expect(fireShortcut('nope.unknown')).toBe(false);
        expect(winBubble.n).toBe(0);
        expect(doc.n).toBe(0);
    });
});
