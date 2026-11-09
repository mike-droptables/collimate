// "Compare with…" — shared model for the file compare flow: the two sides a
// compare tab (CellTab 'compare') carries, plus the pure candidate
// enumeration the context-menu submenu uses (same-named files in other cells
// of the channel, and the cell's own other text files). Kept out of the
// components so it is unit-testable without a DOM.

import type { Cell, Channel, VirtualFile } from '@/lib/types';
import { BINARY_EXTS, IMAGE_EXTS, PDF_EXTS } from '@/lib/fileKinds';

/** One side of a compare: where the file lives + a human label for the
 *  tab's tooltip and the diff panes (compareCellLabel). `channel` is the
 *  immutable channel UUID. */
export interface CompareSide {
    channel: string;
    cellId: string;
    fileName: string;
    label: string;
}

/** A compare to open. `base` is the "before" side (the picked target),
 *  `current` the file the user right-clicked — matching FullFileEditor's
 *  diffBaseContent / diffLabels orientation. */
export interface CompareRequest {
    base: CompareSide;
    current: CompareSide;
}

/** How a compare names a cell: "<stack> / <cell>", or the cell alone when
 *  its stack has no name (a stack made from dropped files starts unnamed),
 *  never a bare " / <cell>". */
export const compareCellLabel = (stackName: string | null | undefined, cellName: string): string =>
    stackName?.trim() ? `${stackName} / ${cellName}` : cellName;

/** "<cell label> · <file>" — how a side is named to the user. */
export const compareSideTitle = (s: CompareSide): string => `${s.label} · ${s.fileName}`;

/** The same side: same file of the same cell, under the same label. */
export const sameCompareSide = (a: CompareSide, b: CompareSide): boolean =>
    a.channel === b.channel && a.cellId === b.cellId && a.fileName === b.fileName && a.label === b.label;

// Extensions the compare flow refuses — no text content to diff: the shared
// image/binary lists (the ones useRendererId dispatches on) plus other
// non-text types.
const NON_TEXT_EXTS = [
    ...IMAGE_EXTS, ...PDF_EXTS, ...BINARY_EXTS,
    '.bin', '.exe', '.dll', '.so', '.dylib', '.mp3', '.wav', '.ogg',
    '.woff', '.woff2', '.ttf', '.otf', '.excalidraw',
];

/** True when the file's bytes are diffable text: not CAS-backed binary and
 *  not a known binary/image extension. */
export function isComparableTextFile(f: VirtualFile): boolean {
    if (f.encoding==='binary') return false;
    const lower = f.name.toLowerCase();
    return !NON_TEXT_EXTS.some(ext => lower.endsWith(ext));
}

export interface CompareCandidate {
    cellId: string;
    fileName: string;
    label: string;
}

/** Files with the SAME name in other cells of the channel (both stack
 *  columns), labelled by compareCellLabel. The right-clicked cell
 *  itself is excluded — its other files come from otherComparableFiles. */
export function sameNameCandidates(
    channel: Channel, currentCellId: string, fileName: string,
): CompareCandidate[] {
    const out: CompareCandidate[] = [];
    for (const stacks of [channel.main_stacks, channel.nav_stacks]) {
        for (const stack of stacks) {
            for (const cell of stack.cells) {
                if (cell.id === currentCellId) continue;
                const f = cell.files?.find(x => x.name === fileName);
                if (!f || !isComparableTextFile(f)) continue;
                out.push({ cellId: cell.id, fileName, label: compareCellLabel(stack.name, cell.name) });
            }
        }
    }
    return out;
}

/** The cell's OTHER text files — the "Other file in this cell…" flyout. */
export function otherComparableFiles(cell: Cell, fileName: string): VirtualFile[] {
    return (cell.files ?? []).filter(f => f.name !== fileName && isComparableTextFile(f));
}
