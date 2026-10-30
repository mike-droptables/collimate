import { render } from 'vitest-browser-react';
import { expect, it } from 'vitest';
import { page } from 'vitest/browser';
import '@/app/globals.css';
import { TagChips } from './TagChips';
import { px } from '@/test/rootFontSize';

/** The wrapping row's two-line cap (TagChips `max-h-[2.5rem]`). */
const twoLines = () => px(2.5);

const TAGS = [
    'wip', 'needs review', 'ui', 'overview', 'tags', 'design pass', 'bug', 'blocked on api',
    'q4', 'docs', 'follow up', 'experiment',
];

/** The chips a viewer sees, and the "+N" chip (null when it is hidden). */
const seen = (row: HTMLElement) => {
    const shown = [...row.querySelectorAll<HTMLElement>('[data-tag-chip]')].filter(chip => !chip.hidden);
    const more = row.querySelector<HTMLElement>('[data-tag-more]')!;
    return { shown, more: more.hidden ? null : more };
};

it('a wrapping row keeps to two lines and counts the tags it hides in a last "+N" chip that names them, refitting on a new width (tags toggle)', async () => {
    await page.viewport(1000, 600);
    // A 272px column's card body: 244px wide, the card's 12px text.
    const view = await render(
        <div data-testid="body" style={{ width: 244 }} className="text-[12px]">
            <TagChips data-ov-tags tags={TAGS} wrap />
        </div>,
    );
    try {
        const row = document.querySelector<HTMLElement>('[data-ov-tags]')!;
        const body = document.querySelector<HTMLElement>('[data-testid="body"]')!;
        const rowTop = row.getBoundingClientRect().top;
        const check = () => {
            const { shown, more } = seen(row);
            const hidden = TAGS.length - shown.length;
            // What is shown is the list's head, in order.
            expect(shown.map(chip => chip.textContent)).toEqual(TAGS.slice(0, shown.length));
            // Every visible chip, the "+N" one too, sits on the first two lines.
            for (const el of more ? [...shown, more] : shown) {
                const r = el.getBoundingClientRect();
                expect(r.bottom - rowTop).toBeLessThanOrEqual(twoLines() + 0.5);
                expect(r.width).toBeGreaterThan(0);
            }
            expect(row.scrollHeight).toBeLessThanOrEqual(Math.ceil(twoLines()));
            return { shown: shown.length, hidden, more };
        };

        const narrow = check();
        expect(narrow.hidden).toBeGreaterThan(0);
        expect(narrow.more).not.toBeNull();
        expect(narrow.more!.textContent).toBe(`+${narrow.hidden}`);
        expect(narrow.shown + Number(narrow.more!.textContent!.slice(1))).toBe(TAGS.length);
        expect(narrow.more!.title).toBe(TAGS.slice(narrow.shown).join(', '));

        // Wider: more of them fit, still counted to the total.
        body.style.width = '420px';
        await expect.poll(() => seen(row).shown.length).toBeGreaterThan(narrow.shown);
        const wide = check();
        expect(wide.shown + (wide.more ? Number(wide.more.textContent!.slice(1)) : 0)).toBe(TAGS.length);

        // Wide enough for all: no "+N".
        body.style.width = '1200px';
        await expect.poll(() => seen(row).shown.length).toBe(TAGS.length);
        expect(check().more).toBeNull();

        // Narrow again: back to the first fit.
        body.style.width = '244px';
        await expect.poll(() => seen(row).shown.length).toBe(narrow.shown);
        expect(check().more!.textContent).toBe(`+${narrow.hidden}`);
    } finally {
        await view.unmount();
    }
});

it('a new tag list refits, and a short one shows whole with no "+N" (tags toggle)', async () => {
    const view = await render(
        <div style={{ width: 244 }} className="text-[12px]">
            <TagChips data-ov-tags tags={TAGS} wrap />
        </div>,
    );
    try {
        const row = () => document.querySelector<HTMLElement>('[data-ov-tags]')!;
        expect(seen(row()).more).not.toBeNull();
        await view.rerender(
            <div style={{ width: 244 }} className="text-[12px]">
                <TagChips data-ov-tags tags={['wip', 'ui']} wrap />
            </div>,
        );
        const { shown, more } = seen(row());
        expect(shown.map(chip => chip.textContent)).toEqual(['wip', 'ui']);
        expect(more).toBeNull();
    } finally {
        await view.unmount();
    }
});
