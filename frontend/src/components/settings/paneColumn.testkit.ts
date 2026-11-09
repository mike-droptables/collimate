// The settings sections' column checks. A section renders in the Commands
// pane's settings view: the native-width column whose body pads px-3.5 a
// side. jsdom does not lay anything out, so
// these read what decides the width: the class lists and the inline styles.

import { createElement, type ReactElement } from 'react';
import { render } from '@testing-library/react';
import { expect } from 'vitest';
import { COMMAND_PANE_WIDTH } from '@/stores/collimate/commandPaneSlice';

/** The Commands pane's width. */
export const PANE_COLUMN_PX = COMMAND_PANE_WIDTH;
/** Tests do not load the app stylesheet, so pin its native 110% rem baseline here. */
const REM_PX = 16 * 1.1;
const SPACING_PX = REM_PX / 4;
/** What a section has inside the pane's body padding. */
export const SECTION_CONTENT_PX = PANE_COLUMN_PX - 2 * 3.5 * SPACING_PX;

/** Renders `ui` as the settings view's section body: inside the Commands pane,
 *  under the body's padding. `body` is that padded element, the root the
 *  checks below read. */
export function renderInPaneColumn(ui: ReactElement) {
    const result = render(createElement(
        'div',
        { 'data-pane-column': '', style: { width: `${PANE_COLUMN_PX}px` } },
        createElement('div', { 'data-pane-body': '', className: 'flex-1 min-h-0 overflow-y-auto px-3.5 py-3' }, ui),
    ));
    return { ...result, body: result.container.querySelector<HTMLElement>('[data-pane-body]')! };
}

const classesOf = (el: Element) => (el.getAttribute('class') ?? '').split(/\s+/).filter(Boolean);

/** Tailwind's named container widths, in rem. */
const NAMED_REM: Record<string, number> = { '3xs': 16, '2xs': 18, xs: 20, sm: 24, md: 28, lg: 32, xl: 36, '2xl': 42, '3xl': 48, '4xl': 56, '5xl': 64, '6xl': 72, '7xl': 80 };

/** The px a width utility fixes (`w-44`, `min-w-[11.25rem]`, `max-w-2xl`,
 *  `basis-40`), or null for a fluid one (`w-full`, `flex-1`, `min-w-0`). */
function fixedPx(token: string): number | null {
    const m = /^(?:w|min-w|max-w|basis)-(.+)$/.exec(token);
    if (!m) return null;
    const value = m[1];
    if (/^\d+(?:\.\d+)?$/.test(value)) return Number(value) * SPACING_PX;
    const length = /^\[(\d+(?:\.\d+)?)(px|rem)\]$/.exec(value);
    if (length) return Number(length[1]) * (length[2] === 'rem' ? REM_PX : 1);
    return value in NAMED_REM ? NAMED_REM[value] * REM_PX : null;
}

/** Nothing under `root` is wider than the section's column: no auto-fill
 *  grid, no grid of more than two columns, and no width utility or inline
 *  width past the column. */
export function expectFitsPaneColumn(root: Element) {
    for (const el of root.querySelectorAll('*')) {
        for (const token of classesOf(el)) {
            expect(token, `${el.tagName} ${token}`).not.toMatch(/^grid-cols-\[/);
            const cols = /^grid-cols-(\d+)$/.exec(token);
            if (cols) expect(Number(cols[1]), `${el.tagName} ${token}`).toBeLessThanOrEqual(2);
            const px = fixedPx(token);
            if (px !== null) expect(px, `${el.tagName} ${token}`).toBeLessThanOrEqual(SECTION_CONTENT_PX);
        }
        const style = (el as HTMLElement).style;
        for (const width of [style?.width, style?.minWidth, style?.flexBasis]) {
            const length = /^(\d+(?:\.\d+)?)(px|rem)$/.exec(width ?? '');
            if (length) expect(Number(length[1]) * (length[2] === 'rem' ? REM_PX : 1), `${el.tagName} inline ${width}`).toBeLessThanOrEqual(SECTION_CONTENT_PX);
        }
    }
}

/** Every labelled field under `root` sits under its label: the label wraps
 *  the field in a column, or the two share a column parent with the label
 *  first. Returns how many fields it checked. */
export function expectStackedFields(root: Element, skip: (field: Element) => boolean = () => false): number {
    const fields = [...root.querySelectorAll(
        'input:not([type=checkbox]):not([type=color]):not([type=radio]):not([type=hidden]), select, textarea',
    )].filter(field => !skip(field));
    let checked = 0;
    for (const field of fields) {
        const label = (field as HTMLInputElement).labels?.[0];
        if (!label) continue;
        checked += 1;
        const column = label.contains(field) ? label : field.parentElement!;
        expect(classesOf(column), `${label.textContent} stacks`).toEqual(expect.arrayContaining(['flex', 'flex-col']));
        if (!label.contains(field)) {
            expect(label.parentElement, `${label.textContent} shares the field's column`).toBe(field.parentElement);
            expect(label.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING, `${label.textContent} is above`).toBeTruthy();
        }
    }
    return checked;
}

/** Every row of two or more buttons under `root` wraps. Returns how many
 *  such rows it found. */
export function expectButtonRowsWrap(root: Element): number {
    let rows = 0;
    for (const el of root.querySelectorAll('*')) {
        const classes = classesOf(el);
        if (!classes.includes('flex') || classes.includes('flex-col')) continue;
        if ([...el.children].filter(child => child.tagName === 'BUTTON').length < 2) continue;
        rows += 1;
        expect(classes, `${el.textContent} wraps`).toContain('flex-wrap');
    }
    return rows;
}
