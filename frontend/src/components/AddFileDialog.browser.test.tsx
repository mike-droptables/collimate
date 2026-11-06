import { expect, it, vi } from 'vitest';
import { page, userEvent } from 'vitest/browser';
import { render } from 'vitest-browser-react';
import '@/app/globals.css';
import { AddFileDialog } from './AddFileDialog';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: () => {} }));
vi.mock('@/hooks/useRendererId', () => ({
    useSupportedExtensions: () => [
        { ext: '.note', renderers: ['Note view'] },
        { ext: '.notes', renderers: ['Notes view'] },
        { ext: '.other', renderers: ['Other view'] },
    ],
    useRendererId: (name: string) => ({
        renderer: { name: name.endsWith('.notes') ? 'Notes view' : name.endsWith('.note') ? 'Note view' : 'Text view' },
        error: null,
    }),
}));

it('cycles inline extensions without changing the typed prefix, and Tab accepts before Enter creates', async () => {
    const onCreate = vi.fn();
    const view = await render(<AddFileDialog x={40} y={40} basePath="" existingNames={[]} mode="file" onCancel={() => {}} onCreate={onCreate} />);
    try {
        const field = page.getByRole('textbox', { name: 'File name' });
        const ghost = () => document.querySelector('[data-filename-completion]');
        await userEvent.type(field, 'draft.');
        await expect.poll(() => ghost()?.textContent).toBe('draft.note');
        await userEvent.keyboard('{ArrowDown}');
        await expect.poll(() => ghost()?.textContent).toBe('draft.notes');
        await expect.element(field).toHaveValue('draft.');
        await userEvent.keyboard('{ArrowUp}');
        await expect.poll(() => ghost()?.textContent).toBe('draft.note');
        await userEvent.keyboard('n{ArrowDown}');
        await expect.element(field).toHaveValue('draft.n');
        await expect.poll(() => ghost()?.textContent).toBe('draft.notes');
        // The hint follows the typed name, not the completion, until Tab accepts it.
        await expect.element(page.getByText('Default renderer:', { exact: false })).toMatchTextContent('Text view');
        await userEvent.keyboard('{Tab}');
        await expect.element(field).toHaveValue('draft.notes');
        await expect.element(page.getByText('Default renderer:', { exact: false })).toMatchTextContent('Notes view');
        expect(document.activeElement).toBe(field.element());
        await userEvent.keyboard('{Enter}');
        expect(onCreate).toHaveBeenCalledWith('draft.notes');
    } finally {
        await view.unmount();
    }
});

it('selects only a pasted file stem so native typing preserves its extension', async () => {
    const onCreate = vi.fn();
    const view = await render(<AddFileDialog x={40} y={40} basePath="" existingNames={[]} mode="file" initialStem="clipboard" initialExt=".note" onCancel={() => {}} onCreate={onCreate} />);
    try {
        const field = page.getByRole('textbox', { name: 'File name' });
        const input = field.element() as HTMLInputElement;
        expect([input.selectionStart, input.selectionEnd]).toEqual([0, 'clipboard'.length]);
        await userEvent.keyboard('renamed');
        await expect.element(field).toHaveValue('renamed.note');
        await userEvent.keyboard('{Enter}');
        expect(onCreate).toHaveBeenCalledWith('renamed.note');
    } finally {
        await view.unmount();
    }
});

it('keeps the dialog and its buttons inside the viewport when completion hints grow', async () => {
    await page.viewport(640, 360);
    const view = await render(<AddFileDialog x={window.innerWidth - 2} y={window.innerHeight - 2} basePath="" existingNames={[]} mode="file" onCancel={() => {}} onCreate={() => {}} />);
    try {
        const dialog = page.getByRole('dialog', { name: 'New file' }).element();
        await expect.poll(() => dialog.getAnimations().length).toBe(0);
        const fits = () => {
            const box = dialog.getBoundingClientRect();
            return box.right <= window.innerWidth - 7 && box.bottom <= window.innerHeight - 7;
        };
        await expect.poll(fits).toBe(true);
        const field = page.getByRole('textbox', { name: 'File name' });
        await userEvent.type(field, 'a-long-filename-that-wraps-the-renderer-hint.');
        await expect.poll(() => document.querySelector('[data-filename-completion]')).not.toBeNull();
        await expect.poll(fits).toBe(true);
        const create = page.getByRole('button', { name: 'Create' }).element().getBoundingClientRect();
        expect(create.bottom).toBeLessThan(window.innerHeight);
        expect(dialog.scrollWidth).toBeLessThanOrEqual(dialog.clientWidth);
    } finally {
        await view.unmount();
    }
});
