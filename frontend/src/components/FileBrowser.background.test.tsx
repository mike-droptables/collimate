// @vitest-environment jsdom
import { fileFixture } from '@/test/fileFixtures';
// The files pane has no toolbar (C12): its rail sits straight over the tree,
// as the mock's `.files`. The tree's background menu carries what the
// toolbar did: New file, New folder, Expand all, Collapse all.
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FileBrowser } from './FileBrowser';

afterEach(cleanup);

const files = [
    fileFixture('docs/a.md', '', {language: 'markdown', timestamp: ''}),
    fileFixture('notes.md', '', {language: 'markdown', timestamp: ''}),
];

it('the tree has no toolbar of its own: no New file, New folder or Expand / Collapse buttons (C12)', () => {
    render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} />);
    for (const title of ['New file', 'New folder', 'Expand all folders', 'Collapse all folders']) {
        expect(screen.queryByTitle(title)).toBeNull();
    }
});

it('the background menu offers New file, New folder and Expand / Collapse all (C12)', () => {
    const view = render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} />);
    fireEvent.contextMenu(view.container.firstElementChild!);
    for (const label of ['New file', 'New folder', 'Expand all folders', 'Collapse all folders']) {
        expect(screen.getByText(label)).toBeDefined();
    }
    fireEvent.click(screen.getByText('New folder'));
    expect(screen.getByPlaceholderText('folder-name')).toBeDefined();
});

it('a read-only tree\'s background menu keeps only Expand / Collapse all (C12)', () => {
    const view = render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} readOnly />);
    fireEvent.contextMenu(view.container.firstElementChild!);
    expect(screen.queryByText('New file')).toBeNull();
    expect(screen.getByText('Expand all folders')).toBeDefined();
    expect(screen.getByText('Collapse all folders')).toBeDefined();
});

it('a right-click in a rename field keeps the browser menu: neither the row\'s nor the tree\'s menu opens', () => {
    render(<FileBrowser files={files} onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} />);
    const row = document.querySelector('[data-filenode-path="notes.md"]') as HTMLElement;
    fireEvent.click(row.querySelector('[title="Rename"]')!);
    const field = row.querySelector('input')!;
    expect(fireEvent.contextMenu(field)).toBe(true);
    expect(screen.queryByText('New file')).toBeNull();
    expect(screen.queryByText('Delete')).toBeNull();
});

/** A synthetic paste event carrying `text` (the hook reads focus, not the target). */
function pasteText(text: string): boolean {
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { files: [], items: [], getData: (f: string) => (f === 'text/plain' ? text : '') } });
    act(() => { document.body.dispatchEvent(event); });
    return event.defaultPrevented;
}

it('Mod+V is left to the browser; its paste event, with focus on a folder row, prompts for a name in that folder (FP-05)', () => {
    render(<FileBrowser files={files} cellId="cell" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} />);
    const row = document.querySelector('[data-filenode-path="docs"]') as HTMLElement;
    const focusable = (row.matches('[tabindex]') ? row : row.querySelector('[tabindex]')) as HTMLElement;
    act(() => focusable.focus());
    expect(fireEvent.keyDown(focusable, { key: 'v', ctrlKey: true })).toBe(true);
    expect(pasteText('plain words')).toBe(true);
    expect(screen.getByText(/Paste text in/)).toBeDefined();
    expect(screen.getByText('docs', { selector: 'span.font-mono' })).toBeDefined();
});

it('the background menu offers Paste here without an internal clipboard (FP-05)', () => {
    const view = render(<FileBrowser files={files} cellId="cell" channel="ch" onOpenFile={vi.fn()} onUpdateFiles={vi.fn()} />);
    fireEvent.contextMenu(view.container.firstElementChild!);
    expect(screen.getByText('Paste here')).toBeDefined();
});
