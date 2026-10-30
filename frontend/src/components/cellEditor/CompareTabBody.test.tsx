import { fileFetchCache } from '@/lib/fileFetchCache';
import type { FileDescriptor } from '@/lib/fileProtocol';
import { fileFixture } from '@/test/fileFixtures';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Channel, VirtualFile } from '@/lib/types';
import type { CellTab } from '@/stores/collimate/types';

const mocks = vi.hoisted(() => ({
    renderer: { id: 'plain', name: 'Plain', extensions: ['.log'], supportsDiff: false, liveRefresh: false, embed: 'native' },
    editors: [] as Record<string, unknown>[],
    fetch: vi.fn(async (_channel: string, _cellId: string, descriptor: FileDescriptor) => `fetched ${descriptor.path}\n`),
}));
vi.mock('@/hooks/useRendererId', async original => ({
    ...(await original<typeof import('@/hooks/useRendererId')>()),
    useRendererId: () => ({renderer:mocks.renderer,error:null}),
}));
// The editor records what it was handed; it answers no key of its own.
vi.mock('@/components/FullFileEditor', () => ({
    FullFileEditor: (props: Record<string, unknown>) => { mocks.editors.push(props); return <div data-testid="editor" />; },
}));
vi.mock('@/lib/fileBodies', async original => ({
    ...(await original<typeof import('@/lib/fileBodies')>()),
    readPinnedText: mocks.fetch,
}));

import { useCollimateStore } from '@/stores/useCollimateStore';
import { CompareTabBody } from './CompareTabBody';
import { retiredOr } from '@/test/retiredTokens';

const file = (content: string, extra: Partial<VirtualFile> = {}): VirtualFile =>
    (fileFixture('run.log', content, {language: '', timestamp: '', ...extra}));

const tab: Extract<CellTab, { kind: 'compare' }> = {
    id: 'cmp', kind: 'compare',
    base: { channel: 'Channel', cellId: 'base', fileName: 'run.log', label: 'Work / Before' },
    current: { channel: 'Channel', cellId: 'cur', fileName: 'run.log', label: 'Work / After' },
};

/** The channel: the base cell holding `baseFiles` (no base cell at all for
 *  null) beside the current cell. */
function seed(baseFiles: VirtualFile[] | null) {
    const channel: Channel = {
        id: 'ch', name: 'Channel', main_selection_id: 's1', nav_selection_id: null, nav_stacks: [],
        main_stacks: [{ id: 's1', name: 'Work', cells: [
            ...(baseFiles ? [{ id: 'base', name: 'Before', files: baseFiles }] : []),
            { id: 'cur', name: 'After', files: [file('kept\nnew line\n')] },
        ] }],
    };
    useCollimateStore.setState({ state: { channels: [channel], active_channel_id: channel.id }, cellCache: {} });
}

const mount = (current: VirtualFile, props: Partial<Parameters<typeof CompareTabBody>[0]> = {}) => {
    const onCloseTabById = vi.fn();
    const utils = render(
        <CompareTabBody tab={tab} file={current} isActiveFocus visible fullscreen={false}
            editorShortcutsEnabled onCloseTabById={onCloseTabById} {...props} />,
    );
    return { ...utils, onCloseTabById };
};
const pressEsc = () => act(() => { fireEvent.keyDown(window, { key: 'Escape', code: 'Escape' }); });

/** The laid-out width every element reports (jsdom lays nothing out: 0). */
let width = 0;
const widthSpy = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => width);
const plainDiff = (container: HTMLElement) => container.querySelector<HTMLElement>('[data-fallback-diff]')!;
/** The plain diff's lines, top to bottom, as "<mark><text>" ("·" unchanged). */
const stackedLines = (container: HTMLElement) =>
    Array.from(plainDiff(container).querySelectorAll('pre')).map((pre) => {
        const mark = pre.previousElementSibling?.textContent || '·';
        return `${mark}${pre.textContent}`;
    });

beforeEach(() => {
    mocks.renderer.supportsDiff = false;
    mocks.editors.length = 0;
    mocks.fetch.mockClear().mockImplementation(async (_channel,_cell,file)=>fileFetchCache.get<string>(`body:${file.body_hash}`)??`fetched ${file.path}\n`);
    width = 0;
});
afterEach(() => {
    cleanup();
    useCollimateStore.setState({ state: null, cellCache: {} } as never);
});
afterAll(() => widthSpy.mockRestore());

describe('CompareTabBody', () => {
    it('paints the plain-text diff rows on the skin tokens when the renderer has no diff view (S04)', async () => {
        seed([file('kept\ngone line\n')]);
        const { container } = mount(file('kept\nnew line\n'));
    await act(async () => {});
        const removed = await screen.findByText('gone line');
        const added = screen.getByText('new line');
        expect(removed.parentElement?.className).toContain('bg-red-soft');
        expect(added.parentElement?.className).toContain('bg-green/15');
        expect(removed.closest('.bg-code-bg')).not.toBeNull();
        expect(screen.getByText('Plain text diff (renderer has no diff view)')).toBeTruthy();
        expect(container.innerHTML).not.toMatch(retiredOr('1A1C23', 'emerald-', 'red-300', 'red-500'));
        expect(mocks.editors).toHaveLength(0);
    });

    it('sets the sides side by side in a pane 720px or wider: two equal wrapping columns under the sides\' names', async () => {
        width = 720;
        seed([file('kept\ngone line\n')]);
        const { container } = mount(file('kept\nnew line\n'));
    await act(async () => {});
        const added = await screen.findByText('new line');
        // One grid keeps every row's halves level; lines wrap, and no fixed
        // minimum pushes the current side out of the pane.
        const grid = plainDiff(container);
        expect(grid.dataset.fallbackDiff).toBe('side-by-side');
        expect(grid.className.split(' ')).toEqual(expect.arrayContaining(['grid', 'grid-cols-2']));
        expect(added.parentElement?.parentElement).toBe(grid);
        expect(added.className.split(' ')).toEqual(expect.arrayContaining(['whitespace-pre-wrap', 'break-words', 'min-w-0']));
        expect(Array.from(grid.children).slice(0, 2).map(e => e.textContent))
            .toEqual(['Work / Before · run.log', 'Work / After · run.log']);
        expect(screen.getAllByText('kept')).toHaveLength(2);
        expect(container.innerHTML).not.toMatch(/min-w-\[|min-w-max|w-1\/2|whitespace-pre /);
    });

    it('stacks the sides in a narrower pane, the current side first, so a cell-width tab shows the user\'s own file', async () => {
        width = 322;
        seed([file('kept\ngone line\nalso gone\ntail\n')]);
        const { container } = mount(file('kept\nnew line\ntail\n'));
    await act(async () => {});
        await screen.findByText('new line');
        const list = plainDiff(container);
        expect(list.dataset.fallbackDiff).toBe('stacked');
        expect(list.className).not.toMatch(/grid/);
        // Unchanged lines once; the changed run's current line ahead of its
        // base lines, each with its own side's number.
        expect(stackedLines(container)).toEqual(['·kept', '+new line', '−gone line', '−also gone', '·tail', '·']);
        const numbers = Array.from(list.querySelectorAll('pre')).map(pre => pre.previousElementSibling?.previousElementSibling?.textContent);
        expect(numbers).toEqual(['1', '2', '2', '3', '3', '4']);
        expect(screen.getByText('new line').parentElement?.className).toContain('bg-green/15');
        expect(screen.getByText('gone line').parentElement?.className).toContain('bg-red-soft');
        // The names head the column, current first.
        expect(list.firstElementChild?.textContent).toBe('+ Work / After · run.log− Work / Before · run.log');
        expect(container.innerHTML).not.toMatch(/min-w-\[|min-w-max|w-1\/2|grid-cols-2|whitespace-pre /);
    });

    it('follows the pane\'s width as it resizes; an unlaid-out (0px) pane keeps the last layout', async () => {
        const observers: { cb: () => void; disconnect: ReturnType<typeof vi.fn> }[] = [];
        const Original = globalThis.ResizeObserver;
        class RO {
            cb: () => void;
            disconnect = vi.fn();
            constructor(cb: () => void) { this.cb = cb; observers.push(this); }
            observe() {}
            unobserve() {}
        }
        globalThis.ResizeObserver = RO as unknown as typeof ResizeObserver;
        try {
            width = 900;
            seed([file('kept\ngone line\n')]);
            const { container, unmount } = mount(file('kept\nnew line\n'));
    await act(async () => {});
            await screen.findByText('new line');
            const layout = () => plainDiff(container).dataset.fallbackDiff;
            expect(layout()).toBe('side-by-side');
            width = 500;
            act(() => observers.at(-1)!.cb());
            expect(layout()).toBe('stacked');
            width = 0;
            act(() => observers.at(-1)!.cb());
            expect(layout()).toBe('stacked');
            width = 1200;
            act(() => observers.at(-1)!.cb());
            expect(layout()).toBe('side-by-side');
            unmount();
            expect(observers.at(-1)!.disconnect).toHaveBeenCalled();
        } finally {
            globalThis.ResizeObserver = Original;
        }
    });

    it('mounts the read-only diff editor on the live file, under its own cell, against the base snapshot', async () => {
        mocks.renderer.supportsDiff = true;
        seed([file('base text\n')]);
        const current = file('current text\n');
        mount(current, { fullscreen: true });
    await act(async () => {});
        await screen.findByTestId('editor');
        const props = mocks.editors.at(-1)!;
        expect(props.file).toBe(current);
        expect(props.diffBaseContent).toBe('base text\n');
        expect(props.readOnly).toBe(true);
        expect(props.cellId).toBe('cur');
        expect(props.channel).toBe('Channel');
        expect(props.fullscreen).toBe(true);
        expect(props.editorShortcutsEnabled).toBe(true);
        expect(props.diffLabels).toEqual({ base: 'Work / Before · run.log', current: 'Work / After · run.log' });
    });

    it('the editor owns Esc while it shows: the body adds no second close', async () => {
        mocks.renderer.supportsDiff = true;
        seed([file('base text\n')]);
        const { onCloseTabById } = mount(file('current text\n'));
    await act(async () => {});
        await screen.findByTestId('editor');
        pressEsc();
        expect(onCloseTabById).not.toHaveBeenCalled();
        (mocks.editors.at(-1)!.onClose as () => void)();
        expect(onCloseTabById).toHaveBeenCalledWith('cmp');
    });

    it('Esc closes the plain diff by its id when the tab answers shortcuts, and not otherwise (R11)', async () => {
        seed([file('kept\ngone line\n')]);
        const quiet = mount(file('kept\nnew line\n'), { editorShortcutsEnabled: false });
    await act(async () => {});
        await screen.findByText('gone line');
        pressEsc();
        expect(quiet.onCloseTabById).not.toHaveBeenCalled();
        cleanup();
        const focused = mount(file('kept\nnew line\n'));
    await act(async () => {});
        await screen.findByText('gone line');
        pressEsc();
        expect(focused.onCloseTabById).toHaveBeenCalledWith('cmp');
    });

    it('fetches stripped (>50 KB) sides through the cell file endpoint', async () => {
        width = 900;
        seed([file('', {body_hash:'uncached-base'})]);
        mount(file('', {  body_hash: 'h1' }));
    await act(async () => {});
        expect(await screen.findAllByText('fetched run.log')).toHaveLength(2);
        expect(mocks.fetch.mock.calls.map(c => c.slice(0, 3))).toEqual([
            ['Channel', 'base', expect.objectContaining({path:'run.log',file_revision:1})],
            ['Channel', 'cur', expect.objectContaining({path:'run.log',file_revision:1})],
        ]);
    });

    it('follows the live file: a new body on the current side re-diffs against the same base', async () => {
        seed([file('kept\ngone line\n')]);
        const { rerender, onCloseTabById } = mount(file('kept\nnew line\n'));
    await act(async () => {});
        await screen.findByText('new line');
        rerender(<CompareTabBody tab={tab} file={file('kept\nlater line\n')} isActiveFocus visible fullscreen={false}
            editorShortcutsEnabled onCloseTabById={onCloseTabById} />);
        expect(await screen.findByText('later line')).toBeTruthy();
        expect(screen.getByText('gone line')).toBeTruthy();
    });

    it('says so when the base cell leaves the channel or its file is gone, and Esc still closes', async () => {
        seed(null);
        const { onCloseTabById } = mount(file('kept\n'));
    await act(async () => {});
        expect(screen.getByText('Work / Before left the channel')).toBeTruthy();
        pressEsc();
        expect(onCloseTabById).toHaveBeenCalledWith('cmp');
        cleanup();
        seed([file('other', { name: 'other.log' })]);
        mount(file('kept\n'));
    await act(async () => {});
        expect(screen.getByText('run.log is gone from Work / Before')).toBeTruthy();
        expect(mocks.fetch.mock.calls.every(call=>call[1]==='cur')).toBe(true);
    });
});
