import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import type { Stack } from '@/lib/types';
import type { StackCallbacks } from './StackGroup';
import { FocusProvider } from '@/contexts/FocusContext';

vi.mock('./StackItem', () => ({ StackItem: () => <div /> }));

import { StackGroup } from './StackGroup';

const stack: Stack = { id: 's1', name: 'Research', cells: [{ id: 'a', name: 'a', files: [] }] };
const callbacks: StackCallbacks = { onSelect: vi.fn(), onClose: vi.fn(), onRemoveCell: vi.fn(), onUpdate: vi.fn() };
const ui = (width: number) => (
    <FocusProvider>
        <StackGroup stack={stack} width={width} height={500} isSelected viewMode="stack" callbacks={callbacks} />
    </FocusProvider>
);
const group = () => document.querySelector<HTMLElement>('[data-stack-id="s1"]')!;

describe('the stack group is promoted only while it animates', () => {
    afterEach(cleanup);

    it('is a stacking context with no compositor promotion at rest', async () => {
        render(ui(600));
        await waitFor(() => expect(group().style.willChange).toBe(''));
        const classes = group().className.split(/\s+/);
        // Its stacking context keeps the head's z under the scene's
        // merge-target overlay; it needs no layer for that.
        expect(classes).toContain('isolate');
        expect(group().className).not.toMatch(/will-change|transform-gpu/);
    });

    it('takes the hint from the start of its own animation, before a frame, to its end', async () => {
        const view = render(ui(600));
        await waitFor(() => expect(group().style.willChange).toBe(''));
        act(() => view.rerender(ui(640)));
        expect(group().style.willChange).toBe('transform, opacity');
        await waitFor(() => expect(group().style.willChange).toBe(''), { timeout: 3000 });
        expect(group().style.width).toBe('640px');
        expect(group().className).not.toMatch(/will-change|transform-gpu/);
    });
});
