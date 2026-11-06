import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/nativeRendererCache', () => ({ getCachedRenderer: vi.fn() }));
vi.mock('@/lib/revealFile', () => ({ revealCellFile: vi.fn() }));
vi.mock('@/lib/openLink', () => ({ openLink: vi.fn(async () => {}) }));

import { NativeRenderer } from './NativeRenderer';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import { revealCellFile } from '@/lib/revealFile';
import { openLink } from '@/lib/openLink';

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('NativeRenderer api.revealFile', () => {
    it('reveals a file of its own cell and passes only the renderer\'s newTab through', async () => {
        type RevealApi = { revealFile(path: string, options?: { newTab?: boolean; focus?: boolean }): void; ready?(): void };
        let api: RevealApi | null = null;
        vi.mocked(getCachedRenderer).mockResolvedValueOnce({
            module: { mount: (_el: Element, a: RevealApi) => { api = a; a.ready?.(); } },
            blobUrl: null, moduleUrl: 'blob:reveal-test',
        } as never);
        render(<NativeRenderer rendererId="reveal-api" fileContent="" fileName="notes.md" cellId="cell-1" isActiveFocus />);
        await waitFor(() => expect(api).not.toBeNull());

        api!.revealFile('data.csv');
        expect(revealCellFile).toHaveBeenLastCalledWith('cell-1', 'data.csv', { newTab: false });
        api!.revealFile('data.csv', { newTab: true });
        expect(revealCellFile).toHaveBeenLastCalledWith('cell-1', 'data.csv', { newTab: true });
        // Taking the main focus area is the host's (an annotation pick), never a renderer's.
        api!.revealFile('data.csv', { newTab: true, focus: true });
        expect(revealCellFile).toHaveBeenLastCalledWith('cell-1', 'data.csv', { newTab: true });
    });

    it('follows a link through openLink, beside the named document or its own file', async () => {
        type LinkApi = { openLink(href: string, options?: { documentPath?: string }): void; ready?(): void };
        let api: LinkApi | null = null;
        vi.mocked(getCachedRenderer).mockResolvedValueOnce({
            module: { mount: (_el: Element, a: LinkApi) => { api = a; a.ready?.(); } },
            blobUrl: null, moduleUrl: 'blob:link-test',
        } as never);
        render(<NativeRenderer rendererId="link-api" fileContent="" fileName="docs/notes.md" cellId="cell-1" isActiveFocus />);
        await waitFor(() => expect(api).not.toBeNull());

        api!.openLink('../a.md');
        expect(openLink).toHaveBeenLastCalledWith('../a.md', { cellId: 'cell-1', channel: '', documentPath: 'docs/notes.md' });
        api!.openLink('a.md', { documentPath: '' });
        expect(openLink).toHaveBeenLastCalledWith('a.md', { cellId: 'cell-1', channel: '', documentPath: '' });
        api!.openLink('http://localhost:5173/', { documentPath: 'other/x.md' });
        expect(openLink).toHaveBeenLastCalledWith('http://localhost:5173/', { cellId: 'cell-1', channel: '', documentPath: 'other/x.md' });
    });
});
