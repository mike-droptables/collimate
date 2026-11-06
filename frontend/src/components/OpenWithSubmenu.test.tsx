import { fileFixture } from '@/test/fileFixtures';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Channel } from '@/lib/types';

vi.mock('@/hooks/useRendererId', async original => {
    const markdown = { id: 'markdown', name: 'Markdown', extensions: ['.md'], supportsDiff: true, liveRefresh: false, embed: 'native' };
    const code = { id: 'code', name: 'Code', extensions: ['.md'], supportsDiff: true, liveRefresh: false, embed: 'native' };
    return {
        ...(await original<typeof import('@/hooks/useRendererId')>()),
        rendererCandidatesFor: () => [markdown, code],
        resolveRenderer: () => markdown,
    };
});

import { useCollimateStore } from '@/stores/useCollimateStore';
import { OpenWithSubmenu } from './OpenWithSubmenu';
import { CompareWithSubmenu } from './CompareWithSubmenu';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { activeTab, allTabs, createDefaultLayout } from '@/lib/cellLayout';

const OLD_TOKENS = RETIRED_TOKENS;

afterEach(cleanup);

describe('OpenWithSubmenu', () => {
    it('marks the renderer in use as the current row on the menu vocabulary (S04)', () => {
        const { container } = render(<OpenWithSubmenu filename="notes.md" currentId="markdown" onPick={vi.fn()} onClose={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /Open with/ }));
        const [current, other] = screen.getAllByRole('menuitem');
        expect(current).toHaveAttribute('aria-current', 'true');
        expect(current.className).toContain('h-[1.875rem]');
        expect(current.className.split(' ')).toContain('text-ink');
        expect(current.className).toContain('[&_svg]:text-brass');
        expect(current.className).not.toContain('text-ink-2');
        expect(other).not.toHaveAttribute('aria-current');
        expect(other.className).toContain('text-ink-2');
        const flyout = screen.getByRole('menu').className;
        expect(flyout).toContain('bg-sheet');
        expect(flyout).toContain('border-line-2');
        expect(flyout).toContain('shadow-(--shadow)');
        expect(container.innerHTML).not.toMatch(OLD_TOKENS);
    });
});

describe('CompareWithSubmenu', () => {
    const seed = (stackNames: [string, string] = ['Work', 'Draft']) => {
        const channel: Channel = {
            id: 'ch', name: 'Channel', main_selection_id: 's1', nav_selection_id: null, nav_stacks: [],
            main_stacks: [
                { id: 's1', name: stackNames[0], cells: [{ id: 'c1', name: 'Alpha', files: [
                    fileFixture('notes.md', 'a', {language: 'markdown', timestamp: ''}),
                    fileFixture('other.md', 'b', {language: 'markdown', timestamp: ''}),
                ] }] },
                { id: 's2', name: stackNames[1], cells: [{ id: 'c2', name: 'Beta', files: [
                    fileFixture('notes.md', 'c', {language: 'markdown', timestamp: ''}),
                ] }] },
            ],
        };
        useCollimateStore.setState({ state: { channels: [channel], active_channel_id: channel.id }, cellCache: {} });
    };

    it('renders its head, separator and trigger on the menu vocabulary (S04)', () => {
        seed();
        const { container } = render(<CompareWithSubmenu cellId="c1" fileName="notes.md" onClose={vi.fn()} />);
        const trigger = screen.getByRole('button', { name: /Compare with/ });
        expect(trigger.className).toContain('h-[1.875rem]');
        fireEvent.click(trigger);
        const head = screen.getByText('Same file in…').className;
        expect(head).toContain('tracking-[0.08em]');
        expect(head).toContain('text-ink-3');
        expect(container.querySelector('.h-px')?.className).toContain('bg-line');
        expect(screen.getByRole('menuitem', { name: /Draft \/ Beta/ })).toBeInTheDocument();
        expect(container.innerHTML).not.toMatch(OLD_TOKENS);
    });

    it('a pick opens the compare tab in the right-clicked file\'s cell and focuses it', () => {
        seed();
        useCollimateStore.setState({ cellLayouts: { c1: createDefaultLayout({ filePaths: ['notes.md', 'other.md'], pinnedPath: null }) } });
        const onClose = vi.fn();
        render(<CompareWithSubmenu cellId="c1" fileName="notes.md" onClose={onClose} />);
        fireEvent.click(screen.getByRole('button', { name: /Compare with/ }));
        fireEvent.click(screen.getByRole('menuitem', { name: /Draft \/ Beta/ }));
        const layout = useCollimateStore.getState().cellLayouts.c1;
        expect(allTabs(layout).map(t => t.kind)).toEqual(['compare']);
        expect(activeTab(layout)).toMatchObject({
            kind: 'compare',
            base: { cellId: 'c2', fileName: 'notes.md', label: 'Draft / Beta' },
            current: { cellId: 'c1', fileName: 'notes.md', label: 'Work / Alpha' },
        });
        expect(useCollimateStore.getState().cellLayouts.c2).toBeUndefined();
        expect(onClose).toHaveBeenCalledOnce();
    });

    it('names the cells of unnamed stacks by the cell alone, never " / <cell>"', () => {
        seed(['', '']);
        useCollimateStore.setState({ cellLayouts: { c1: createDefaultLayout({ filePaths: ['notes.md', 'other.md'], pinnedPath: null }) } });
        render(<CompareWithSubmenu cellId="c1" fileName="notes.md" onClose={vi.fn()} />);
        fireEvent.click(screen.getByRole('button', { name: /Compare with/ }));
        const item = screen.getByRole('menuitem', { name: 'Beta' });
        expect(item.textContent).not.toContain('/');
        fireEvent.click(item);
        expect(activeTab(useCollimateStore.getState().cellLayouts.c1)).toMatchObject({
            kind: 'compare',
            base: { cellId: 'c2', label: 'Beta' },
            current: { cellId: 'c1', label: 'Alpha' },
        });
    });
});
