// A stack head pip's title (the mock's `.scrub i` title): when the cell
// last changed, `4 Oct 2026 · 10:20` in local time, then the cell's name.
// The stack head (StackCellScrubber) shows it on each pip and on the knob.
// The head has one pip per front slot (lib/frontSlots): the Capabilities
// stack's first pip is its manage card, titled 'Manage'.

import type { Cell, Stack } from '@/lib/types';
import { clockOf } from '@/lib/cellLog';
import { leadingSlots } from '@/lib/frontSlots';

/** The manage card's pip title (the Capabilities stack's slot 0). */
export const MANAGE_PIP_TITLE = 'Manage';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** A wire timestamp in ms: ISO, or the offset-less `YYYY-MM-DD HH:MM:SS`
 *  the server writes in UTC. NaN when absent or unreadable. */
export function stampOf(at: string | undefined): number {
    if (!at) return NaN;
    const bare = /^\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(at);
    return Date.parse(bare ? `${at.replace(' ', 'T')}Z` : at);
}

/** `4 Oct 2026 · 10:20`, local time. */
export function pipWhen(ms: number): string {
    const d = new Date(ms);
    return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()} · ${clockOf(ms)}`;
}

/** The pip's title: `<when> · <name>`, or the name alone when the cell
 *  carries no readable time. */
export function cellPipTitle(cell: Pick<Cell, 'name' | 'updated_at' | 'created_at'>): string {
    const ms = stampOf(cell.updated_at ?? cell.created_at);
    const name = cell.name || 'Untitled';
    return Number.isNaN(ms) ? name : `${pipWhen(ms)} · ${name}`;
}

/** Every pip's title, by slot: 'Manage' ahead of the cells on Capabilities,
 *  then each cell's cellPipTitle. */
export function stackPipTitles(stack: Pick<Stack, 'stack_type' | 'cells'>): string[] {
    const cells = stack.cells.map(cellPipTitle);
    return leadingSlots(stack) ? [MANAGE_PIP_TITLE, ...cells] : cells;
}
