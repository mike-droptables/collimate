// The Removed shelf's grid columns (RemovedShelf).

/** A shelf card's least width; the grid's padding and gap. */
export const SHELF_CARD_MIN = 260;
export const SHELF_PAD = 20;
export const SHELF_GAP = 20;

/** The grid's columns for a shelf `width` wide with nothing over it: as
 *  many cards as fit at SHELF_CARD_MIN, stretched to fill the row. The
 *  width is fixed (auto-fill), so a narrower grid takes fewer of them. */
export function shelfColumns(width: number): string {
    const inner = width - 2 * SHELF_PAD;
    const n = Math.max(1, Math.floor((inner + SHELF_GAP) / (SHELF_CARD_MIN + SHELF_GAP)));
    const w = Math.max(SHELF_CARD_MIN, (inner - (n - 1) * SHELF_GAP) / n);
    return `repeat(auto-fill, ${Math.floor(w * 100 + 1e-6) / 100}px)`;
}
