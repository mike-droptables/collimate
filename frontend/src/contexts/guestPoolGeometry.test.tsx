import { expect, it } from 'vitest';
import {
    applyRect, holdsRect, movesSurface, guestLayoutOf, overlayRectOf, overlayScale, pixelGridOf, snapToDevice, CAMERA_ORIGIN_ATTR,
    OVERLAY_RULER_PX, type PixelGrid, type PoolEntry,
} from '@/lib/guestPoolGeometry';

const rect = (x: number, y: number, width: number, height: number) => ({
    x, y, left: x, top: y, right: x + width, bottom: y + height, width, height,
}) as DOMRect;

/** One CSS px per device px, from the viewport's origin: placement as written. */
const UNIT_GRID: PixelGrid = { dpr: 1, originX: 0, originY: 0 };

it('F0700 window geometry floors fractional and negative coordinates to the shell zoom grid', () => {
    const layout = guestLayoutOf('guest', rect(-2.1, 3.3, 99.9, 51.7), { css: '7px', max: 7 }, 0.75, 1.25);
    expect(layout).toEqual({ guestId: 'guest', x: -2.4, y: 3.2, w: 99.2, h: 51.2, radius: 5 });
});

it('F0700 overlay-local geometry removes camera scale and keeps unframed corners square', () => {
    const host = document.createElement('div');
    expect(overlayRectOf(rect(125, 75, 250, 125), host, rect(100, 50, 800, 700), 1.25, 'iframe', UNIT_GRID))
        .toEqual({ x: 20, y: 20, w: 200, h: 100, radius: { css: '0px', max: 0 } });
});

it('a pooled frame\'s translation from the camera\'s layout origin is whole device pixels, through the overlay\'s offset and scale', () => {
    const host = document.createElement('div');
    // The diagnosed rest at 1.25×: the camera hangs from a board laid out
    // under a 70px top bar (87.5 device px), and its container sat at
    // 195.6, 181.6 CSS px.
    const grid: PixelGrid = { dpr: 1.25, originX: 0, originY: 70 };
    const cRect = rect(195.6, 181.6, 320.8, 1473.8);
    for (const [oRect, scale] of [
        [rect(761, 70, OVERLAY_RULER_PX, 0), 1],
        [rect(-565.251, 70.3, OVERLAY_RULER_PX, 0), 1],
        [rect(12.3, 41.7, OVERLAY_RULER_PX * 0.8, 0), 0.8],
        [rect(-3.21, 9.87, OVERLAY_RULER_PX * 0.37, 0), 0.37],
    ] as const) {
        const r = overlayRectOf(cRect, host, oRect, scale, 'iframe', grid);
        // The translation from the origin to where the frame lands: the
        // overlay's position plus its translate through the overlay's scale.
        const dx = (oRect.left + r.x * scale - grid.originX) * grid.dpr;
        const dy = (oRect.top + r.y * scale - grid.originY) * grid.dpr;
        expect(dx).toBeCloseTo(245, 9); // 244.5 rounds up
        expect(dy).toBeCloseTo(140, 9); // 139.5 rounds up
        // The far edges snap on their own, onto the device pixel the
        // container's right and bottom edges round to (516.4 → 645.5 →
        // 646 device px, 1655.4 → 1981.75 → 1982 below the origin), so the frame never
        // reaches into a border Chromium paints on that grid.
        expect((oRect.left + (r.x + r.w) * scale - grid.originX) * grid.dpr).toBeCloseTo(646, 9);
        expect((oRect.top + (r.y + r.h) * scale - grid.originY) * grid.dpr).toBeCloseTo(1982, 9);
    }
    // A guest placeholder keeps its exact rect: the native view's own
    // zoom-grid contract places its pixels.
    const guest = overlayRectOf(cRect, host, rect(10, 20, OVERLAY_RULER_PX, 0), 1, 'guest', grid);
    expect(guest.x).toBeCloseTo(185.6, 9);
    expect(guest.y).toBeCloseTo(161.6, 9);
});

it('the grid is the device ratio and the camera origin\'s viewport position, or the viewport\'s own without one', () => {
    const board = document.createElement('div');
    board.setAttribute(CAMERA_ORIGIN_ATTR, '');
    board.getBoundingClientRect = () => rect(12.5, 70, 900, 600);
    const pan = document.createElement('div');
    const overlay = document.createElement('div');
    board.append(pan);
    pan.append(overlay);
    expect(pixelGridOf(overlay)).toEqual({ dpr: window.devicePixelRatio, originX: 12.5, originY: 70 });
    expect(pixelGridOf(document.createElement('div'))).toEqual({ dpr: window.devicePixelRatio, originX: 0, originY: 0 });
});

it('the camera scale is read off the overlay\'s exact ruler width, whatever the canvas width', () => {
    // At rest the scale is exactly 1, so a frame's offset from the overlay
    // is its container's: no rounded layout width to skew it (a 1522.4px
    // canvas once read as 1522px put a frame 0.15px off its container).
    expect(overlayScale(rect(761, 70, OVERLAY_RULER_PX, 0))).toBe(1);
    expect(overlayScale(rect(-40.5, 12, OVERLAY_RULER_PX * 0.3, 0))).toBeCloseTo(0.3, 12);
    // An overlay not laid out yet reads as unscaled.
    expect(overlayScale(rect(0, 0, 0, 0))).toBe(1);
    const host = document.createElement('div');
    const placed = overlayRectOf(rect(195.6, 181.6, 320.8, 1473.8), host, rect(761, 70, OVERLAY_RULER_PX, 0), 1, 'iframe', UNIT_GRID);
    // Each edge on the (unit) grid: 195.6 → 196 and 516.4 → 516 across,
    // 181.6 → 182 and 1655.4 → 1655 down.
    expect(placed.x).toBe(196 - 761);
    expect(placed.w).toBe(320);
    expect(placed.h).toBe(1473);
});

it('device snapping rounds to the nearest device pixel at any ratio', () => {
    expect(snapToDevice(195.7485809326172, 1.25)).toBe(196);
    expect(snapToDevice(111.6, 1.25)).toBe(112);
    expect(snapToDevice(10.3, 2)).toBe(10.5);
    expect(snapToDevice(-2.3, 1)).toBe(-2);
    expect(snapToDevice(7, 1.5)).toBe(7.333333333333333);
});

it('F0700 cache equality uses CSSOM echoes and detects foreign writes without reading layout', () => {
    const element = document.createElement('div');
    const entry = { element } as unknown as PoolEntry;
    const target = { x: 1 / 3, y: 2 / 3, w: 400 / 3, h: 100, radius: { css: '4px', max: 4 } };
    applyRect(entry, target);
    expect(holdsRect(entry, target)).toBe(true);
    expect(entry.appliedTransform).toBe(element.style.transform);
    element.style.width = '1px';
    expect(holdsRect(entry, target)).toBe(false);
    applyRect(entry, target);
    expect(holdsRect(entry, target)).toBe(true);
});

it('a native guest\'s view covers its container (polish)', () => {
    const square = { css: '0px', max: 0 };
    expect(guestLayoutOf('guest', rect(10, 20, 300, 200), square, 1, 1).h).toBe(200);
    expect(guestLayoutOf('guest', rect(10, 20, 150, 100), square, 0.5, 1).h).toBe(100);
});

it('a pooled element on a card\'s bottom corners rounds them (polish)', () => {
    const card = document.createElement('div');
    card.setAttribute('data-stack-card', '');
    card.style.borderTopLeftRadius = '10px';
    // A border width computes to 0 without a style (as the card's `border` class sets).
    card.style.borderTop = '1px solid';
    card.getBoundingClientRect = () => rect(0, 0, 202, 102);
    const host = document.createElement('div');
    card.append(host);
    document.body.append(card);
    const cRect = rect(1, 1, 200, 100);
    try {
        expect(overlayRectOf(cRect, host, rect(0, 0, 800, 700), 1, 'iframe', UNIT_GRID).radius.css).toBe('9px 9px 9px 9px');
    } finally {
        card.remove();
    }
});

it('only a new position moves a surface: a first placement or a resize in place do not', () => {
    // First placement: the rect cache is NaN.
    expect(movesSurface(NaN, NaN, 20, 30)).toBe(false);
    // The same position: whatever else the write changes, nothing moved.
    expect(movesSurface(20, 30, 20, 30)).toBe(false);
    expect(movesSurface(20, 30, 20.8, 30)).toBe(true);
    expect(movesSurface(20, 30, 20, 29.6)).toBe(true);
});
