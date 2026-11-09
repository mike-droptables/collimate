import { act } from '@testing-library/react';
import { fileFixture } from '@/test/fileFixtures';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { Cell, VirtualFile } from '@/lib/types';
import { OverviewCardPreview } from './OverviewCardPreview';
import { fileLabels } from './overview/journalText';

afterEach(cleanup);
const file = (changes: Partial<VirtualFile> = {}): VirtualFile => (fileFixture('notes.md', '', {language: 'markdown', timestamp: '2026-10-03T00:00:00Z', ...changes}));
const cell = (pinned: VirtualFile): Cell => ({
    id: 'grid', name: 'Grid', pinned: pinned.name, files: [pinned], created_at: '2026-10-03T00:00:00Z',
});

it('loads pinned text from its immutable body without eager metadata content', async () => {
 const pinned=fileFixture('notes.md','Loaded body',{language:'markdown'});
 expect(pinned).not.toHaveProperty('content');
 render(<OverviewCardPreview cell={cell(pinned)} isOffscreen={false} author="you" />);
 await act(async () => {});
 expect(screen.getByText('Loaded body')).toBeInTheDocument();
});

it.each([
    ['loaded empty text', file({  }), false],
    ['binary', file({ name: 'image.png', body_hash: 'a'.repeat(64), encoding: 'binary',  }), false],
    ['culled text', file({  }), true],
] as const)('does not label %s as an omitted text preview __F0555', async (_name, pinned, isOffscreen) => {
    render(<OverviewCardPreview cell={cell(pinned)} isOffscreen={isOffscreen} author="you" />);
    await act(async () => {});
    expect(screen.queryByText('Large file, open the cell')).toBeNull();
});

it('names files by their base names, and two that share one by the shortest folder suffix that tells them apart (O01)', async () => {
    expect(fileLabels(['Scratch/scratch.txt', 'Scratch (Copy)/scratch.txt', 'notes.md', 'src/a/util.py', 'lib/a/util.py', 'util.py']))
        .toEqual(['Scratch/scratch.txt', 'Scratch (Copy)/scratch.txt', 'notes.md', 'src/a/util.py', 'lib/a/util.py', 'util.py']);
    expect(fileLabels(['a/b/c.txt', 'd/b/c.txt', 'e/c.txt', 'x/y/z.txt'])).toEqual(['a/b/c.txt', 'd/b/c.txt', 'e/c.txt', 'z.txt']);
    const merged: Cell = {
        id: 'merged', name: 'Merged', created_at: '2026-10-03T00:00:00Z',
        files: [file({ name: 'Scratch/scratch.txt' }), file({ name: 'Scratch (Copy)/scratch.txt' }), file({ name: 'Scratch/plot.py' })],
    };
    const { container } = render(<OverviewCardPreview cell={merged} isOffscreen={false} author="you" />);
    await act(async () => {});
    const rows = [...container.querySelectorAll('ul > li')].map(li => li.textContent);
    // By path, as the card lists them.
    expect(rows).toEqual(['Scratch (Copy)/scratch.txt', 'plot.py', 'Scratch/scratch.txt']);
});
