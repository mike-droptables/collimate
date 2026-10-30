import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// A renderer whose module never loads: the failure floor paints.
vi.mock('@/lib/nativeRendererCache', () => ({
    getCachedRenderer: vi.fn(() => Promise.reject(new Error('S04 controlled load failure'))),
}));

import { NativeRenderer } from './NativeRenderer';
import { RETIRED_TOKENS } from '@/test/retiredTokens';
import { getCachedRenderer } from '@/lib/nativeRendererCache';
import { applyEditingSnapshot } from '@/lib/editingSnapshot';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('NativeRenderer failure floor', () => {
    it('paints the failure panel and plain-text fallback on the skin tokens (S04)', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const { container } = render(
            <NativeRenderer rendererId="s04-broken" fileContent="plain body" fileName="broken.md" isActiveFocus />,
        );
        const message = await screen.findByText('S04 controlled load failure');
        expect(message.className).toContain('text-red');
        const retry = screen.getByRole('button', { name: 'Retry' }).className;
        expect(retry).toContain('bg-sheet');
        expect(retry).toContain('[&_svg]:text-brass');
        const plain = screen.getByRole('button', { name: 'Open as plain text' });
        expect(plain.className).toContain('border-line-2');
        expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);

        fireEvent.click(plain);
        const pre = screen.getByText('plain body');
        expect(pre.tagName).toBe('PRE');
        expect(pre.className).toContain('bg-code-bg');
        expect(screen.getByText('Renderer failed — showing plain text').parentElement?.className).toContain('h-[1.875rem]');
        expect(container.innerHTML).not.toMatch(RETIRED_TOKENS);
    });
});

describe('NativeRenderer editing preferences', () => {
    afterEach(() => applyEditingSnapshot({ spellcheck: true }));

    it('api.getEditing reads <html spellcheck>; onEditingChange fires once on subscribe, then on each change, until unsubscribed', async () => {
        applyEditingSnapshot({ spellcheck: false });
        let api: { getEditing(): { spellcheck: boolean }; onEditingChange(cb: (e: { spellcheck: boolean }) => void): () => void; ready?(): void } | null = null;
        vi.mocked(getCachedRenderer).mockResolvedValueOnce({
            module: { mount: (_el: Element, a: typeof api) => { api = a; a!.ready?.(); } },
            blobUrl: null, moduleUrl: 'blob:editing-test',
        } as never);
        render(<NativeRenderer rendererId="editing-api" fileContent="" fileName="e.md" isActiveFocus />);
        await waitFor(() => expect(api).not.toBeNull());
        expect(api!.getEditing()).toEqual({ spellcheck: false });

        const heard: boolean[] = [];
        const off = api!.onEditingChange(e => heard.push(e.spellcheck));
        expect(heard).toEqual([false]);
        act(() => applyEditingSnapshot({ spellcheck: true }));
        expect(heard).toEqual([false, true]);
        expect(api!.getEditing()).toEqual({ spellcheck: true });
        off();
        act(() => applyEditingSnapshot({ spellcheck: false }));
        expect(heard).toEqual([false, true]);
    });
});
