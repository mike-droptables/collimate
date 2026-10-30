import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/nativeRendererCache', () => ({ getCachedRenderer: vi.fn() }));
vi.mock('@/lib/revealFile', () => ({ revealCellFile: vi.fn() }));

import { NativeRenderer } from './NativeRenderer';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import { revealCellFile } from '@/lib/revealFile';

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
});
