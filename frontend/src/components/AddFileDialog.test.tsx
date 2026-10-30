// A create the caller refuses (an existing folder, a reserved name, a `_runs`
// path) keeps the dialog open and shows the caller's error, instead of the
// dialog closing as if nothing had been asked.

import { beforeEach, describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';

import { AddFileDialog } from './AddFileDialog';
import { refreshRendererCache } from '@/hooks/useRendererId';

const catalog = vi.hoisted(() => {
    const state = { prefs: {} as Record<string, string> };
    const renderers = [
        { id: 'text', name: 'Plain text', extensions: ['.txt'], embed: 'native' },
        { id: 'prose', name: 'Prose', extensions: ['.md', '.mdx'] },
        { id: 'deck', name: 'Slide deck', extensions: ['.slides.md'] },
        { id: 'alternate', name: 'Alternate editor', extensions: ['.md'] },
        { id: 'image', name: 'Image view', extensions: ['.png'], default_for: ['.png'] },
    ];
    vi.stubGlobal('fetch', vi.fn(async (url: unknown) => ({
        ok: true,
        json: async () => String(url).includes('renderer_preferences') ? state.prefs
            : String(url).endsWith('/handlers') ? [{ types: [{ ext: '.memo', render_as: '.md' }] }]
                : renderers,
    })));
    return state;
});

beforeEach(async () => {
    catalog.prefs = {};
    await refreshRendererCache();
});

const completion = () => document.querySelector('[data-filename-completion]')?.textContent;


describe('AddFileDialog', () => {
    it('shows a refused folder create and stays open', async () => {
        const onCancel = vi.fn();
        const onCreate = vi.fn(async () => ({ ok: false as const, error: '`docs` already exists' }));
        render(
            <AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="folder" onCancel={onCancel} onCreate={onCreate} />,
        );
        const input = screen.getByPlaceholderText('folder-name');
        fireEvent.change(input, { target: { value: 'docs' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onCreate).toHaveBeenCalledWith('docs');
        expect(await screen.findByText('`docs` already exists')).toBeTruthy();
        expect(onCancel).not.toHaveBeenCalled();
    });

    it('shows a refused file create the dialog could not pre-check', async () => {
        const onCreate = vi.fn(async () => ({ ok: false as const, error: 'Read-only' }));
        render(
            <AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="file" onCancel={vi.fn()} onCreate={onCreate} />,
        );
        const input = screen.getByPlaceholderText('filename');
        fireEvent.change(input, { target: { value: 'notes' } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onCreate).toHaveBeenCalledTimes(1);
        expect(await screen.findByText('Read-only')).toBeTruthy();
    });
});

it('dismisses on focus entering a renderer iframe but not an ordinary window blur __F0788', () => {
    const onCancel = vi.fn();
    const view = render(<AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="file"
        onCancel={onCancel} onCreate={vi.fn()} />);
    fireEvent.blur(window);
    expect(onCancel).not.toHaveBeenCalled();
    const frame = document.createElement('iframe');
    document.body.append(frame);
    frame.focus();
    fireEvent.blur(window);
    expect(onCancel).toHaveBeenCalledTimes(1);
    view.unmount();
    fireEvent.blur(window);
    expect(onCancel).toHaveBeenCalledTimes(1);
    frame.remove();
});

describe('AddFileDialog as the paste prompt (FP-05)', () => {
    it('prefills and selects the stem, starts on the given extension and names its verb (FP-05)', async () => {
        const onCreate = vi.fn(async () => ({ ok: true as const }));
        render(<AddFileDialog x={0} y={0} basePath="docs" existingNames={[]} mode="file" title="Paste image"
            initialStem="pasted-image-20261012-090102" initialExt=".png" onCancel={vi.fn()} onCreate={onCreate} />);
        expect(screen.getByText(/Paste image in/)).toBeTruthy();
        const input = screen.getByPlaceholderText('filename') as HTMLInputElement;
        expect(input.value).toBe('pasted-image-20261012-090102.png');
        expect(document.activeElement).toBe(input);
        expect([input.selectionStart, input.selectionEnd]).toEqual([0, 'pasted-image-20261012-090102'.length]);
        expect(screen.getByText('Image view')).toBeTruthy();
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onCreate).toHaveBeenCalledWith('pasted-image-20261012-090102.png');
    });

    it('an empty initial extension keeps an extensionless name; a taken name is refused (FP-05)', () => {
        const onCreate = vi.fn();
        render(<AddFileDialog x={0} y={0} basePath="" existingNames={['Makefile']} mode="file"
            initialStem="Makefile" initialExt="" onCancel={vi.fn()} onCreate={onCreate} />);
        fireEvent.keyDown(screen.getByPlaceholderText('filename'), { key: 'Enter' });
        expect(onCreate).not.toHaveBeenCalled();
        expect(screen.getByText('`Makefile` already exists')).toBeTruthy();
    });

    it('owns its keys and is a dialog named by its heading (FP-05)', () => {
        render(<AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="file" onCancel={vi.fn()} onCreate={vi.fn()} />);
        const root = screen.getByPlaceholderText('filename').closest('[data-owns-keyboard]');
        expect(root).not.toBeNull();
        expect(root).toBe(screen.getByRole('dialog', { name: 'New file' }));
    });
});

describe('inline filename completion', () => {
    function open(onCreate = vi.fn()) {
        render(<AddFileDialog x={0} y={0} basePath="docs" existingNames={[]} mode="file"
            onCancel={vi.fn()} onCreate={onCreate} />);
        return screen.getByRole('textbox', { name: 'File name' }) as HTMLInputElement;
    }

    it('uses one filename field, starts after a dot and cycles without changing the typed prefix', () => {
        const input = open();
        expect(screen.getAllByRole('textbox')).toHaveLength(1);
        expect(screen.queryByRole('combobox')).toBeNull();
        fireEvent.change(input, { target: { value: 'draft' } });
        expect(completion()).toBeUndefined();
        fireEvent.change(input, { target: { value: 'draft.m' } });
        expect(completion()).toBe('draft.md');
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(completion()).toBe('draft.mdx');
        expect(input.value).toBe('draft.m');
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(completion()).toBe('draft.memo');
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(completion()).toBe('draft.md');
        fireEvent.keyDown(input, { key: 'ArrowUp' });
        expect(completion()).toBe('draft.memo');
        expect(input.value).toBe('draft.m');
        fireEvent.change(input, { target: { value: 'draft.md' } });
        expect(completion()).toBe('draft.mdx');
        fireEvent.change(input, { target: { value: 'draft.m' } });
        expect(completion()).toBe('draft.md');
    });

    it('cycles the whole catalog after a dot; the hint stays on the typed name until Tab accepts', () => {
        const input = open();
        fireEvent.change(input, { target: { value: 'draft.' } });
        const seen = new Set<string | undefined>();
        for (let i = 0; i < 20; i++) {
            seen.add(completion());
            fireEvent.keyDown(input, { key: 'ArrowDown' });
        }
        for (const filename of ['draft.md', 'draft.mdx', 'draft.slides.md', 'draft.txt', 'draft.memo', 'draft.png']) {
            expect(seen.has(filename)).toBe(true);
        }
        expect(input.value).toBe('draft.');
        fireEvent.change(input, { target: { value: 'draft.sl' } });
        expect(completion()).toBe('draft.slides.md');
        expect(screen.queryByText('Slide deck')).toBeNull();
        expect(screen.getByText(/Default renderer:/).textContent).toBe('Default renderer: Plain text');
        fireEvent.keyDown(input, { key: 'Tab' });
        expect(screen.getByText('Slide deck')).toBeTruthy();
    });

    it('Tab accepts the displayed suffix; submission uses the accepted filename', () => {
        const onCreate = vi.fn();
        const input = open(onCreate);
        fireEvent.change(input, { target: { value: 'draft.m' } });
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        expect(fireEvent.keyDown(input, { key: 'Tab' })).toBe(false);
        expect(input.value).toBe('draft.mdx');
        expect(document.activeElement).toBe(input);
        expect(completion()).toBeUndefined();
        expect(fireEvent.keyDown(input, { key: 'Tab' })).toBe(true);
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onCreate).toHaveBeenCalledWith('draft.mdx');
    });

    it('supports compound extensions from either dot boundary and resolves the longest claim', () => {
        const input = open();
        fireEvent.change(input, { target: { value: 'tour.slides.m' } });
        expect(completion()).toBe('tour.slides.md');
        expect(screen.queryByText('Slide deck')).toBeNull();
        fireEvent.keyDown(input, { key: 'Tab' });
        expect(input.value).toBe('tour.slides.md');
        expect(screen.getByText('Slide deck')).toBeTruthy();
    });

    it('accepts mixed-case extension prefixes without changing the typed characters', () => {
        const input = open();
        fireEvent.change(input, { target: { value: 'DRAFT.SL' } });
        expect(completion()).toBe('DRAFT.SLides.md');
        fireEvent.keyDown(input, { key: 'Tab' });
        expect(input.value).toBe('DRAFT.SLides.md');
        expect(screen.getByText('Slide deck')).toBeTruthy();
    });

    it('hints the renderer of the name as typed (what Enter creates), with the channel\'s preferences and handler aliases; never a completion\'s', async () => {
        catalog.prefs = { '.md': 'alternate' };
        await refreshRendererCache();
        const input = open();
        // 'draft.m' completes to draft.md, but Enter would create draft.m: the hint says so.
        fireEvent.change(input, { target: { value: 'draft.m' } });
        expect(completion()).toBe('draft.md');
        expect(screen.queryByText('Alternate editor')).toBeNull();
        expect(screen.getByText(/Default renderer:/).textContent).toBe('Default renderer: Plain text');
        expect(screen.queryByText(/ for /)).toBeNull();
        fireEvent.change(input, { target: { value: 'draft.md' } });
        expect(screen.getByText('Alternate editor')).toBeTruthy();
        fireEvent.change(input, { target: { value: 'draft.memo' } });
        expect(screen.getByText('Alternate editor')).toBeTruthy();
    });

    it.each(['Makefile', 'draft.unknown', 'draft.m'])('submits %s literally without accepting a suggestion', name => {
        const onCreate = vi.fn();
        const input = open(onCreate);
        fireEvent.change(input, { target: { value: name } });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onCreate).toHaveBeenCalledWith(name);
    });

    it('rejects an already existing accepted completion in the target folder', () => {
        const onCreate = vi.fn();
        render(<AddFileDialog x={0} y={0} basePath="docs" existingNames={['docs/draft.mdx']} mode="file"
            onCancel={vi.fn()} onCreate={onCreate} />);
        const input = screen.getByRole('textbox', { name: 'File name' });
        fireEvent.change(input, { target: { value: 'draft.m' } });
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        fireEvent.keyDown(input, { key: 'Tab' });
        fireEvent.click(screen.getByRole('button', { name: 'Create' }));
        expect(onCreate).not.toHaveBeenCalled();
        expect(screen.getByText('`docs/draft.mdx` already exists')).toBeTruthy();
    });

    it('leaves selection, Shift+Tab and IME composition alone', () => {
        const input = open();
        fireEvent.change(input, { target: { value: 'draft.m' } });
        expect(fireEvent.keyDown(input, { key: 'Tab', shiftKey: true })).toBe(true);
        expect(fireEvent.keyDown(input, { key: 'Tab', isComposing: true })).toBe(true);
        expect(input.value).toBe('draft.m');
        input.setSelectionRange(0, 5);
        fireEvent.select(input);
        expect(completion()).toBeUndefined();
        expect(fireEvent.keyDown(input, { key: 'ArrowDown' })).toBe(true);
    });

    it('does not offer file extensions for folder names', () => {
        render(<AddFileDialog x={0} y={0} basePath="" existingNames={[]} mode="folder"
            onCancel={vi.fn()} onCreate={vi.fn()} />);
        const input = screen.getByRole('textbox', { name: 'Folder name' });
        fireEvent.change(input, { target: { value: 'folder.' } });
        expect(completion()).toBeUndefined();
        expect(screen.queryByText(/Default renderer/)).toBeNull();
    });
});
