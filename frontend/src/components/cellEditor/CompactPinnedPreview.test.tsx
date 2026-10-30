import { act } from '@testing-library/react';
import { fixtureText } from '@/test/fileFixtures';
import { fileFixture } from '@/test/fileFixtures';
import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { CompactPinnedPreview } from './CompactPinnedPreview';
import { RETIRED_TOKENS } from '@/test/retiredTokens';

it('bounds transient markdown preview work while preserving the full file __F0817', async () => {
    const content = '# Zoom file\n\n' + 'A paragraph for the zoom preview.\n\n'.repeat(1500) + '\nTAIL_ONLY_TEXT';
    const cell = { id: 'preview', name: 'Preview', pinned: 'note.md', files: [
        fileFixture('note.md', content, {language: 'markdown', timestamp: 'stable'}),
    ] };
    render(<CompactPinnedPreview cell={cell} />);
    await act(async () => {});
    expect(screen.getByRole('heading', { name: 'Zoom file' })).toBeInTheDocument();
    expect(screen.queryByText('TAIL_ONLY_TEXT')).not.toBeInTheDocument();
    expect(fixtureText(cell.files[0])).toBe(content);
    expect(fixtureText(cell.files[0])).toContain('TAIL_ONLY_TEXT');
});

it('the stand-in reads in prose ink, a plain file in ink-2, never the old palette (S04)', async () => {
    const { container, unmount } = render(<CompactPinnedPreview cell={{ id: 'p', name: 'P', pinned: 'note.md', files: [
        fileFixture('note.md', '# Hi', {language: 'markdown', timestamp: 'stable'}),
    ] }} />);
    await act(async () => {});
    expect(container.querySelector('.text-prose-ink')).not.toBeNull();
    expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);
    unmount();
    const plain = render(<CompactPinnedPreview cell={{ id: 'q', name: 'Q', pinned: 'a.txt', files: [
        fileFixture('a.txt', 'plain body', {language: 'text', timestamp: 'stable'}),
    ] }} />);
    await act(async () => {});
    expect(screen.getByText('plain body').className).toMatch(/\btext-ink-2\b/);
    expect(plain.container.innerHTML).not.toMatch(RETIRED_TOKENS);
});

it('keeps a short pinned markdown document complete __F0817', async () => {
    render(<CompactPinnedPreview cell={{ id: 'short', name: 'Short', pinned: 'note.md', files: [
        fileFixture('note.md', '# Heading\n\n**Last paragraph**', {language: 'markdown', timestamp: 'stable'}),
    ] }} />);
    await act(async () => {});
    expect(screen.getByRole('heading', { name: 'Heading' })).toBeInTheDocument();
    expect(screen.getByText('Last paragraph')).toBeInTheDocument();
});
