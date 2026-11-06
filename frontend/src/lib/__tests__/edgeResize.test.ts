import { describe, it, expect } from 'vitest';
import {
    edgeResizeGeometry, edgeDragWidth, anchorFor, EDGE_RESIZE_INSET, EDGE_GLOW_WIDTH, type EdgeResizeColumn,
} from '../edgeResize';

// Two 500px cards with a 14px gap, card 77..894, header
// bottom at 115 (the compact header's 38px).
const col = (id: string, left: number, extra: Partial<EdgeResizeColumn> = {}): EdgeResizeColumn => ({
    id, left, right: left + 500, cardTop: 77, cardBottom: 894, headerBottomWorldY: 115, ...extra,
});

describe('edgeResizeGeometry', () => {
    it('splits the gap at its midline: inset inside the card + half the gap outside, header to card bottom', () => {
        const [a, b] = edgeResizeGeometry([col('a', 14), col('b', 528)], 14);
        expect(a.right.rects).toEqual([{ left: 514 - EDGE_RESIZE_INSET, top: 115, width: EDGE_RESIZE_INSET + 7, height: 894 - 115 }]);
        expect(b.left.rects).toEqual([{ left: 521, top: 115, width: 7 + EDGE_RESIZE_INSET, height: 894 - 115 }]);
        // Row ends get half the standard gap.
        expect(a.left.rects).toEqual([{ left: 7, top: 115, width: 7 + EDGE_RESIZE_INSET, height: 779 }]);
        expect(b.right.rects[0].left + b.right.rects[0].width).toBe(1028 + 7);
    });

    it('never reaches the diff zone: the gap half starts below BOTH neighbours\' headers', () => {
        // Right column is a fan column, 33px lower: its header ends 33px lower too.
        const [a, b] = edgeResizeGeometry([
            col('a', 14),
            col('fan-x', 528, { cardTop: 110, headerBottomWorldY: 148 }),
        ], 14);
        // Left card's inside part starts at its own header; its gap part below the neighbour's.
        expect(a.right.rects).toEqual([
            { left: 514 - EDGE_RESIZE_INSET, top: 115, width: EDGE_RESIZE_INSET, height: 894 - 115 },
            { left: 514, top: 148, width: 7, height: 894 - 148 },
        ]);
        // The fan column's own band ends lower than its neighbour's: one rect.
        expect(b.left.rects).toEqual([{ left: 521, top: 148, width: 7 + EDGE_RESIZE_INSET, height: 894 - 148 }]);
    });

    it('lights a 3px bar inside the card edge over the card\'s full height', () => {
        const [a] = edgeResizeGeometry([col('a', 14, { cardTop: 110 })], 14);
        expect(a.left.glow).toEqual({ left: 14, top: 110, width: EDGE_GLOW_WIDTH, height: 894 - 110 });
        expect(a.right.glow).toEqual({ left: 514 - EDGE_GLOW_WIDTH, top: 110, width: EDGE_GLOW_WIDTH, height: 894 - 110 });
    });

    it('overlapping neighbours (mid-spring) do not count as neighbours', () => {
        const [a] = edgeResizeGeometry([col('a', 14), col('b', 400)], 14);
        expect(a.right.rects).toEqual([{ left: 514 - EDGE_RESIZE_INSET, top: 115, width: EDGE_RESIZE_INSET + 7, height: 779 }]);
    });
});

describe('edgeDragWidth', () => {
    it('right side grows with the pointer moving right, left side with it moving left', () => {
        expect(edgeDragWidth(500, 120, 1, 'right', 300, 1500)).toBe(620);
        expect(edgeDragWidth(500, -120, 1, 'left', 300, 1500)).toBe(620);
        expect(edgeDragWidth(500, 120, 1, 'left', 300, 1500)).toBe(380);
    });

    it('converts screen px to world px through the zoom', () => {
        expect(edgeDragWidth(500, 60, 0.5, 'right', 300, 1500)).toBe(620);
    });

    it('clamps to the layout\'s limits', () => {
        expect(edgeDragWidth(500, -400, 1, 'right', 300, 1500)).toBe(300);
        expect(edgeDragWidth(500, 2000, 1, 'right', 300, 1500)).toBe(1500);
    });

    it('anchors the opposite edge', () => {
        expect(anchorFor('right')).toBe('left');
        expect(anchorFor('left')).toBe('right');
    });
});

it('uses the absolute header bottom world coordinate (F0654)', () => {
    const columns = [
        { id: 'a', left: 14, right: 514, cardTop: 77, cardBottom: 894, headerBottomWorldY: 115 },
        { id: 'b', left: 528, right: 1028, cardTop: 77, cardBottom: 894, headerBottomWorldY: 115 },
    ];
    const [geometry] = edgeResizeGeometry(columns, 14);
    expect(geometry.right.rects).toEqual([
        { left: 513, top: 115, width: 8, height: 779 },
    ]);
});
