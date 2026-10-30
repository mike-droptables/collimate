import { afterEach, expect, it, vi } from 'vitest';
import {
    alignToDevicePixels, deviceAlignOffset, devicePixelRemainder, deviceSnappedTransform, ORIGIN_DX_VAR, ORIGIN_DY_VAR,
} from '@/lib/deviceOrigin';

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

it('the remainder is how far past a whole device pixel a CSS position lies', () => {
    // The diagnosed case: a 70px top bar at 1.25× is 87.5 device px.
    expect(devicePixelRemainder(70, 1.25)).toBeCloseTo(0.5, 12);
    expect(devicePixelRemainder(72, 1.25)).toBe(0); // 90
    expect(devicePixelRemainder(70, 1.5)).toBe(0); // 105
    expect(devicePixelRemainder(70, 1.75)).toBeCloseTo(0.5, 12); // 122.5
    expect(devicePixelRemainder(70.3, 1)).toBeCloseTo(0.3, 9);
    expect(devicePixelRemainder(-0.4, 1.25)).toBeCloseTo(0.5, 9); // -0.5
    // Float noise either side of the grid counts as on it.
    expect(devicePixelRemainder(0.1 * 3, 10)).toBe(0); // 3.0000000000000004
    expect(devicePixelRemainder(2.9999999, 1)).toBe(0);
});

it('the offset reaches the next whole device pixel on layout\'s 1/64 px grid, and is 0 on the grid', () => {
    for (const [v, dpr] of [[70, 1.25], [70, 1.75], [12.3, 1.25], [360.2, 1.1], [0.3, 2], [-0.4, 1.25], [33, 1.333]] as const) {
        const off = deviceAlignOffset(v, dpr);
        expect(off).toBeGreaterThan(0);
        expect(off * dpr).toBeLessThan(1);
        expect(Number.isInteger(off * 64)).toBe(true);
        // Within a 1/128 px quantum of a whole device pixel: it paints there.
        const d = (v + off) * dpr;
        expect(Math.abs(d - Math.round(d))).toBeLessThanOrEqual(dpr / 128 + 1e-9);
    }
    expect(deviceAlignOffset(70, 1.25)).toBe(0.40625); // 70.40625 → 88.008 device px
    expect(deviceAlignOffset(72, 1.25)).toBe(0);
    expect(deviceAlignOffset(70, 2)).toBe(0);
});

/** A board whose layout position is `base` plus the shift it holds. */
function board(base: { left: number; top: number }) {
    const props = new Map<string, string>();
    const style = {
        getPropertyValue: (name: string) => props.get(name) ?? '',
        setProperty: (name: string, value: string) => { props.set(name, value); },
    };
    const shift = (name: string) => parseFloat(style.getPropertyValue(name)) || 0;
    const el = {
        style,
        getBoundingClientRect: () => {
            const left = base.left + shift(ORIGIN_DX_VAR);
            const top = base.top + shift(ORIGIN_DY_VAR);
            return { x: left, y: top, left, top, right: left + 800, bottom: top + 600, width: 800, height: 600 } as DOMRect;
        },
    };
    return el as unknown as HTMLElement;
}

/** A window at device ratio `initial` whose ratio can change, firing the
 *  resolution query armed for the old one. */
function stubRatio(initial: number) {
    const listeners = new Map<string, Set<() => void>>();
    const win = Object.assign(new EventTarget(), {
        devicePixelRatio: initial,
        matchMedia: (query: string) => {
            const set = listeners.get(query) ?? new Set<() => void>();
            listeners.set(query, set);
            return {
                media: query,
                addEventListener: (_: string, f: () => void) => set.add(f),
                removeEventListener: (_: string, f: () => void) => set.delete(f),
            };
        },
    });
    vi.stubGlobal('window', win);
    return {
        set(next: number) {
            const old = `(resolution: ${win.devicePixelRatio}dppx)`;
            win.devicePixelRatio = next;
            for (const f of [...(listeners.get(old) ?? [])]) f();
        },
        resize: () => win.dispatchEvent(new Event('resize')),
        armed: () => [...listeners].filter(([, s]) => s.size > 0).map(([q]) => q),
    };
}

it('a board under a 70px bar at 1.25× is shifted onto the grid, without feeding back on its own shift', () => {
    const ratio = stubRatio(1.25);
    const el = board({ left: 0, top: 70 });
    const stop = alignToDevicePixels(el);
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe('0.40625px');
    // Left at 0 is already whole: no shift written.
    expect(el.style.getPropertyValue(ORIGIN_DX_VAR)).toBe('');
    // Measuring again (a resize) keeps the shift it already holds.
    ratio.resize();
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe('0.40625px');
    expect(el.getBoundingClientRect().top * 1.25).toBeCloseTo(88, 1);
    stop();
});

it('a left edge off the grid (a pane docked left) is shifted too, and an edge that comes back to the grid drops its shift', () => {
    const ratio = stubRatio(1.25);
    const base = { left: 361.3, top: 72 };
    const el = board(base);
    const stop = alignToDevicePixels(el);
    const dx = parseFloat(el.style.getPropertyValue(ORIGIN_DX_VAR));
    expect(dx).toBeGreaterThan(0);
    expect(Number.isInteger(Math.round(el.getBoundingClientRect().left * 1.25 * 64) / 64)).toBe(true);
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe('');
    base.left = 360;
    ratio.resize();
    expect(el.style.getPropertyValue(ORIGIN_DX_VAR)).toBe('0px');
    stop();
});

it('a device ratio change re-measures and re-arms on the new ratio; the teardown stops listening', () => {
    const ratio = stubRatio(1.25);
    const el = board({ left: 0, top: 70 });
    const stop = alignToDevicePixels(el);
    expect(ratio.armed()).toEqual(['(resolution: 1.25dppx)']);
    ratio.set(1.5); // 105 device px: whole
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe('0px');
    expect(ratio.armed()).toEqual(['(resolution: 1.5dppx)']);
    ratio.set(1.75); // 122.5
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe(`${deviceAlignOffset(70, 1.75)}px`);
    stop();
    expect(ratio.armed()).toEqual([]);
    const before = el.style.getPropertyValue(ORIGIN_DY_VAR);
    ratio.set(1.25);
    ratio.resize();
    expect(el.style.getPropertyValue(ORIGIN_DY_VAR)).toBe(before);
});

it('a moved box\'s translation lands on whole device pixels, leaving its other transforms as motion built them', () => {
    vi.stubGlobal('window', { devicePixelRatio: 1.25 });
    // A stack slot mid-spring at 1.25×: 56.25 device px snaps to 56.
    expect(deviceSnappedTransform({}, 'translateX(45px) translateY(14px) scale(0.92)'))
        .toBe('translateX(44.8px) translateY(14.4px) scale(0.92)');
    expect(deviceSnappedTransform({}, 'translateX(-680.3125px)')).toBe('translateX(-680px)');
    expect(deviceSnappedTransform({}, 'translateY(1.2e-7px) scaleX(1.05) scaleY(0.97)'))
        .toBe('translateY(0px) scaleX(1.05) scaleY(0.97)');
    // motion passes '' for an identity transform.
    expect(deviceSnappedTransform({}, '')).toBe('');
    vi.stubGlobal('window', { devicePixelRatio: 2 });
    expect(deviceSnappedTransform({}, 'translateX(10.3px)')).toBe('translateX(10.5px)');
});
