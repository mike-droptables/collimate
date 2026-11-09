import { act } from '@testing-library/react';
import { fileFixture } from '@/test/fileFixtures';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { OverviewCardPreview } from './OverviewCardPreview';
import { retiredOr } from '@/test/retiredTokens';

afterEach(cleanup);
it('the grid preview shows pinned text without editor mounts__F0604_F0588', async () => {
    const { container, rerender } = render(<OverviewCardPreview cell={{ id: 'preview', name: 'Preview', pinned: 'note.txt', files: [
        fileFixture('note.txt', 'PINNED BODY', {language: 'plaintext', timestamp: '2026-10-03T00:00:00Z'}),
    ] }} isOffscreen={false} author="you" />);
    await act(async () => {});
    expect(screen.getByText('PINNED BODY')).toBeTruthy();
    expect(container.querySelector('iframe')).toBeNull();
    rerender(<OverviewCardPreview cell={{ id: 'preview', name: 'Preview', files: [] }} isOffscreen author="you" />);
    expect(screen.getByText('0 files')).toBeTruthy();
});

it('the grid preview reads in the ink tiers on a code-bg well (S04)', async () => {
    const { container } = render(<OverviewCardPreview cell={{ id: 'g', name: 'G', pinned: 'a.txt', files: [
        fileFixture('a.txt', 'BODY', {language: 'plaintext', timestamp: '2026-10-03T00:00:00Z'}),
    ] }} isOffscreen={false} author="you" />);
    await act(async () => {});
    expect(container.querySelector('.bg-code-bg')).not.toBeNull();
    expect(screen.getByText('BODY').className).toContain('text-ink-2');
    expect(container.innerHTML).not.toMatch(retiredOr('bg-black'));
});

