import { describe, it, expect, afterEach, vi } from 'vitest';
import React from 'react';
import { render, cleanup, fireEvent, screen, within } from '@testing-library/react';
import { menuItem, menuSep } from '@/components/ui/menuStyles';
import { TabStrip, type TabStripProps } from './TabStrip';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { ContentLeaf, RunsPaneState } from '@/stores/collimate/types';
import { retiredOr } from '@/test/retiredTokens';
import { createDefaultLayout, setRunsPane } from '@/lib/cellLayout';
import { TAB_RULE_CLASS } from '@/lib/selectionEdge';

const leaf: ContentLeaf = {
    type: 'leaf', id: 'leaf', activeTabId: 't1',
    tabs: [
        { id: 't1', kind: 'file', path: 'notes.md' },
        { id: 't2', kind: 'file', path: 'pinned.md' },
        { id: 't3', kind: 'empty' },
        { id: 't4', kind: 'history', category: 'file', path: 'old.md' },
    ],
};

function strip(pinnedPath: string | null) {
    return render(
        <TabStrip tools
            cellId="cell-1"
            pinnedPath={pinnedPath}
            leaf={leaf}
            onLayoutChange={() => {}}
            onBeforeStructuralChange={() => {}}
            onToggleMaximized={() => {}}
            onCloseFileTab={() => {}}
        />,
    );
}

afterEach(() => { cleanup(); useCollimateStore.setState({ runs: {}, stoppingRunIds: new Set() }); });

/** A tab's frame (the element around its button and ✕). */
const frameOf = (tab: HTMLElement) => tab.parentElement!;

describe('skin tokens (S04)', () => {
    const OLD = retiredOr('emerald');

    it('the open tab is the sheet in ink, framed with a teal top edge; the rest ink-2 and frameless on the sheet strip (S04, C13)', () => {
        const { getAllByRole, container } = strip('pinned.md');
        const tabs = getAllByRole('tab');
        const open = tabs.find(t => t.getAttribute('aria-selected') === 'true')!;
        expect(frameOf(open).classList.contains('text-ink')).toBe(true);
        expect(frameOf(open).classList.contains('text-ink-2')).toBe(false);
        expect(frameOf(open).className).toMatch(/\bbg-sheet\b/);
        expect(frameOf(open).className).toMatch(/\bborder-line-2\b/);
        expect(frameOf(open).className).toMatch(/\bborder-t-teal\b/);
        expect(frameOf(open).className).not.toMatch(/opacity/);
        const rest = tabs.filter(t => t !== open);
        expect(rest.every(t => frameOf(t).classList.contains('text-ink-2'))).toBe(true);
        expect(rest.every(t => !frameOf(t).classList.contains('text-ink'))).toBe(true);
        // A closed tab's frame is transparent and fills on hover: colour
        // fades, never opacity (one runs on the compositor and turns the
        // cell's text grayscale while it runs). No line stands between tabs.
        expect(rest.every(t => /\bborder-transparent\b/.test(frameOf(t).className) && /hover:bg-sheet-2/.test(frameOf(t).className) && !/teal|brass|opacity|before:/.test(frameOf(t).className))).toBe(true);
        expect(frameOf(open).className).not.toMatch(/before:/);
        // The pin is teal; the strip is the sheet over a hairline drawn behind
        // the tabs, so the open tab's sheet breaks it and joins the body.
        expect(screen.getByTestId('tab-pin').className).toMatch(/\btext-teal\b/);
        const root = container.firstElementChild as HTMLElement;
        expect(root.className).toMatch(/\bbg-sheet\b/);
        expect(root.className).toMatch(/\bafter:bg-line\b/);
        expect(root.className).toMatch(/\bafter:-z-10\b/);
        expect(root.className).toMatch(/\bisolate\b/);
        expect(root.className).not.toMatch(/\bborder-b\b/);
        expect(container.innerHTML).not.toMatch(OLD);
    });

    it('an empty tab opens with the same teal-edged frame as a file tab (S04)', () => {
        const { getAllByRole } = render(
            <TabStrip tools
                cellId="cell-1" pinnedPath={null} leaf={{ ...leaf, activeTabId: 't3' }}
                onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}}
            />,
        );
        const open = getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')!;
        expect(frameOf(open).className).toMatch(/\bborder-t-teal\b/);
        expect(frameOf(open).classList.contains('text-ink')).toBe(true);
        expect(frameOf(open).classList.contains('text-ink-2')).toBe(false);
    });

    it('the tab context menu renders its rows and dividers from the menu vocabulary (S04)', () => {
        const { container } = strip(null);
        fireEvent.contextMenu(container.querySelector('[data-tab-id="t1"]')!);
        const close = screen.getByRole('button', { name: 'Close others' });
        expect(close.className).toBe(menuItem);
        const menu = close.parentElement!;
        expect(menu.querySelector(`[class="${menuSep}"]`)).not.toBeNull();
        for (const name of ['Close', 'Close to the right', 'Split right', 'Split down']) {
            expect(screen.getByRole('button', { name }).className).toBe(menuItem);
        }
    });
});

describe('tab icons (C13)', () => {
    it('no file, history or empty tab carries an icon except the pinned file tab, which carries the pin (C13)', () => {
        const { getAllByRole } = strip('pinned.md');
        const tabs = getAllByRole('tab');
        expect(tabs).toHaveLength(4);
        const withIcon = tabs.filter(t => t.querySelector('svg') !== null).map(t => t.textContent);
        expect(withIcon).toEqual(['pinned.md']);
    });

    it('with nothing pinned, no tab carries an icon at all (C13)', () => {
        const { getAllByRole } = strip(null);
        expect(getAllByRole('tab').every(t => t.querySelector('svg') === null)).toBe(true);
    });
});

describe('the pinned tab folds to its pin', () => {
    const pinLeaf: ContentLeaf = {
        type: 'leaf', id: 'leaf', activeTabId: 'p',
        tabs: [{ id: 'p', kind: 'file', path: 'pinned.md' }, { id: 'o', kind: 'file', path: 'other.md' }],
    };
    const mount = (activeTabId: string, compact: boolean) => {
        const changes: Array<(l: import('@/stores/collimate/types').CellLayout) => import('@/stores/collimate/types').CellLayout> = [];
        const utils = render(
            <TabStrip tools
                cellId="cell-1"
                pinnedPath="pinned.md"
                pinnedTabCompact={compact}
                leaf={{ ...pinLeaf, activeTabId }}
                onLayoutChange={(fn) => { changes.push(fn); }}
                onBeforeStructuralChange={() => {}}
                    onToggleMaximized={() => {}}
                onCloseFileTab={() => {}}
            />,
        );
        const layout = { root: pinLeaf, activeLeafId: 'leaf', filesPane: {} as never, runsPane: {} as never, pinnedTabCompact: compact };
        return { ...utils, changes, layout };
    };

    it('a click on the pin of the ACTIVE pinned tab folds it to the pin alone, and again unfolds it', () => {
        const { getByTestId, getAllByRole, changes, layout } = mount('p', false);
        expect(getAllByRole('tab')[0].textContent).toBe('pinned.md');
        getByTestId('tab-pin').click();
        expect(changes).toHaveLength(1);
        expect(changes[0](layout).pinnedTabCompact).toBe(true);
        expect(changes[0](layout).root).toBe(layout.root); // no activation, no surgery

        cleanup();
        const folded = mount('p', true);
        const tab = folded.getAllByRole('tab')[0];
        expect(tab.textContent).toBe('');
        expect(tab.getAttribute('aria-label')).toBe('pinned.md');
        expect(tab.querySelector('svg')).not.toBeNull();
        folded.getByTestId('tab-pin').click();
        expect(folded.changes[0]({ ...folded.layout, pinnedTabCompact: true }).pinnedTabCompact).toBe(false);
    });

    it('a click on the pin of an INACTIVE pinned tab only activates it', () => {
        const { getByTestId, changes, layout } = mount('o', false);
        getByTestId('tab-pin').click();
        expect(changes).toHaveLength(1);
        const next = changes[0](layout);
        expect(next.pinnedTabCompact).toBeFalsy();
        expect((next.root as ContentLeaf).activeTabId).toBe('p');
    });

    it('the fold survives the tab going inactive: the label stays hidden until the pin is clicked while active', () => {
        const { getAllByRole } = mount('o', true);
        expect(getAllByRole('tab')[0].textContent).toBe('');
    });
});

describe('the tab bar (C13)', () => {
    const base: TabStripProps = {
        cellId: 'cell-1', pinnedPath: 'pinned.md', leaf, onLayoutChange: () => {}, onBeforeStructuralChange: () => {},
        onToggleMaximized: () => {}, onCloseFileTab: () => {}, tools: true,
    };
    const mount = (props: Partial<TabStripProps> = {}, selected = false) => render(
        <CellSelectionContext.Provider value={selected}>
            <TabStrip {...base} {...props} />
        </CellSelectionContext.Provider>,
    );

    it('is 30px; the 26px rounded-top tabs stand on its hairline, the tools on an 18px line beside them (C13)', () => {
        const { container, getAllByRole, getByRole } = mount();
        const root = container.firstElementChild as HTMLElement;
        expect(root.className).toMatch(/\bh-\[1\.875rem\]/);
        expect(root.className).toMatch(/\bitems-end\b/);
        expect(root.className).toMatch(/\bafter:h-px\b/);
        for (const tab of getAllByRole('tab')) {
            expect(frameOf(tab).className).toMatch(/\bh-\[1\.625rem\]/);
            expect(frameOf(tab).className).toMatch(/\brounded-t-md\b/);
            expect(frameOf(tab).className).toMatch(/\bborder-b-0\b/);
        }
        for (const tool of [getByRole('button', { name: 'New tab' }), getByRole('button', { name: /Expand the cell view/ })]) {
            expect(tool.className).toMatch(/\bh-\[1\.125rem\]/);
            expect(tool.className).toMatch(/\bself-end\b/);
        }
    });

    it('the open tab\'s top edge is brass in the selected cell, and the pin with it (C13, polish)', () => {
        const { getAllByRole, getByTestId, unmount } = mount({ leaf: { ...leaf, activeTabId: 't2' } }, true);
        const open = getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')!;
        const frame = frameOf(open);
        expect(frame.className.split(/\s+/)).toContain(TAB_RULE_CLASS);
        expect(frame.className).toMatch(/\bborder-t-brass\b/);
        expect(frame.className).not.toMatch(/teal/);
        expect(getByTestId('tab-pin').className).toMatch(/\btext-brass\b/);
        unmount();
        const other = mount({ leaf: { ...leaf, activeTabId: 't2' } }, false);
        expect(other.getByTestId('tab-pin').className).toMatch(/\btext-teal\b/);
    });

    it('a file tab shows its extension in mono after the name (C13)', () => {
        const { getAllByRole } = mount();
        const notes = getAllByRole('tab').find(t => t.textContent === 'notes.md')!;
        const ext = Array.from(notes.querySelectorAll('span')).find(s => s.textContent === '.md')!;
        expect(ext.className).toMatch(/\bfont-mono\b/);
        expect(ext.className).toMatch(/text-\[0\.6875rem\]/);
        expect(ext.className).toMatch(/\btext-ink-2\b/); // the open tab's
        const pinned = getAllByRole('tab').find(t => t.textContent === 'pinned.md')!;
        expect(Array.from(pinned.querySelectorAll('span')).find(s => s.textContent === '.md')!.className).toMatch(/\btext-ink-3\b/);
    });

    it('an unsaved file shows no save feedback on its tab (owner, 2026-10-13)', () => {
        const { getAllByRole, container } = mount();
        const notes = getAllByRole('tab').find(t => t.textContent?.includes('notes'))!;
        expect(notes).toHaveAccessibleName('notes.md');
        expect(container.querySelector('[data-testid="tab-unsaved"], [data-testid="tab-unsaved-slot"]')).toBeNull();
        expect(container.querySelector('[aria-label="Saved"], [aria-label="Saving"], [aria-label^="Unsaved changes"]')).toBeNull();
        expect(container.querySelector('.lucide-loader-circle, .lucide-check')).toBeNull();
        expect(container.textContent).not.toMatch(/Saving|Saved|Unsaved/);
    });

    it('run chips take the chip style (C13)', () => {
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'cell-1', channel: 'ch', runnerId: 'x', runnerName: 'Runner X', generatorName: 'g', label: 'x-12345678', folder: '_runs/x-12345678' } },
        });
        const runLeaf: ContentLeaf = { type: 'leaf', id: 'leaf', activeTabId: 'a', tabs: [
            { id: 'a', kind: 'file', path: 'notes.md' }, { id: 'b', kind: 'file', path: '_runs/x-12345678/out.md' },
        ] };
        const { getByTestId } = mount({
            leaf: runLeaf, pinnedPath: null,
        });
        for (const el of [getByTestId('tab-run-chip')]) {
            expect(el.className).toMatch(/\brounded-full\b/);
            expect(el.className).toMatch(/\bborder-line-2\b/);
            expect(el.className).toMatch(/\bh-4\b/);
        }
        expect(getByTestId('tab-run-chip').textContent).toContain('Runner X 12345678');
    });

    it('while the Files pane is hidden the bar leads with its toggle (C13, C12)', () => {
        const onShowFiles = vi.fn();
        const { container, getByRole, unmount } = mount({ showFiles: { side: 'left', onShow: onShowFiles } });
        const toggle = getByRole('button', { name: 'Show files' });
        expect(toggle).toHaveAttribute('aria-pressed', 'false');
        expect((container.firstElementChild as HTMLElement).firstElementChild).toBe(toggle);
        toggle.click();
        expect(onShowFiles).toHaveBeenCalledTimes(1);
        unmount();
        expect(mount().queryByRole('button', { name: 'Show files' })).toBeNull();
    });

    it('the Show files toggle docks by drag: a release near the top edge of the pane row docks there and shows the pane (FP-04)', () => {
        const changes: Array<(l: import('@/stores/collimate/types').CellLayout) => import('@/stores/collimate/types').CellLayout> = [];
        const onShow = vi.fn();
        const { getByRole, container } = render(
            <div data-files-dock-region>
                <TabStrip {...base} onLayoutChange={(fn) => { changes.push(fn); }} showFiles={{ side: 'left', onShow }} />
            </div>,
        );
        const region = container.firstElementChild as HTMLElement;
        vi.spyOn(region, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, right: 400, bottom: 300, width: 400, height: 300, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
        const show = getByRole('button', { name: 'Show files' });
        fireEvent.pointerDown(show, { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
        fireEvent.pointerMove(show, { pointerId: 1, clientX: 200, clientY: 20 });
        expect(useCollimateStore.getState().dockDrag).toEqual({ cellId: 'cell-1', pane: 'files', side: 'top' });
        fireEvent.pointerUp(show, { pointerId: 1, clientX: 200, clientY: 20 });
        fireEvent.click(show);
        expect(onShow).not.toHaveBeenCalled();
        expect(useCollimateStore.getState().dockDrag).toBeNull();
        const hidden = { ...createDefaultLayout({ filePaths: [], pinnedPath: null }) };
        const next = changes.at(-1)!(hidden);
        expect(next.filesPane).toMatchObject({ side: 'top', collapsed: false });
    });

    it('the files toggle reads unpressed while the pane is hidden (polish)', () => {
        const { getByRole } = mount({ showFiles: { side: 'left', onShow: vi.fn() } });
        const show = getByRole('button', { name: 'Show files' });
        expect(show).toHaveAttribute('aria-pressed', 'false');
        // Quiet --ink-3 at rest, hovering as the other tools; the pressed
        // fill is the pane's own toggle's (FilesPane), off here.
        expect(show.className).toMatch(/(^|\s)text-ink-3(\s|$)/);
        expect(show.className).toContain('hover:bg-sheet-2');
        expect(show.className).not.toContain('aria-pressed:');
        expect(show.className).not.toMatch(/brass|teal/);
    });

    it('a tab has 12px padding and an 18px close slot, 6px from the name; the name alone shrinks (C13, polish)', () => {
        const { getAllByRole, getByRole } = mount({ pinnedPath: null });
        const notes = getAllByRole('tab').find(t => t.textContent === 'notes.md')!;
        expect(notes.className).toMatch(/\bpl-3\b/);
        expect(notes.className).not.toMatch(/\bpx-2\.5\b|\bpr-/);
        expect(notes.className).toMatch(/\bgap-1\.5\b/);
        expect(notes.className).toMatch(/\bmin-w-0\b/);
        // The name leads the button and ellipsises; the extension keeps its width.
        const stem = within(notes).getByText('notes');
        expect(notes.firstElementChild).toBe(stem);
        expect(stem.className).toMatch(/\btruncate\b/);
        expect(stem.className).toMatch(/\bmin-w-0\b/);
        expect(notes.lastElementChild!.textContent).toBe('.md');
        expect(notes.lastElementChild!.className).toMatch(/\bflex-none\b/);
        // Close: its own 18px slot after the button, in the flow, 6px from
        // the name, then the tab's 12px right padding.
        const close = getByRole('button', { name: 'Close notes.md' });
        expect(close.previousElementSibling).toBe(notes);
        expect(close.className).not.toMatch(/\babsolute\b/);
        expect(close.className).toMatch(/\bw-\[1\.125rem\]/);
        expect(close.className).toMatch(/\bflex-none\b/);
        expect(close.className).toMatch(/\bml-1\.5\b/);
        expect(close.className).toMatch(/\bmr-3\b/);
        expect(close.className).not.toMatch(/\bbg-sheet-2\b/);
        // Shown on the tab's hover or focus; otherwise transparent, a colour
        // fade rather than an opacity one.
        expect(close.className).toMatch(/\bnot-focus:not-group-hover:text-transparent\b/);
        expect(close.className).not.toMatch(/opacity/);
        // The tab caps its width; past it the name gives way first.
        expect(notes.parentElement!.className).toMatch(/\bmax-w-\[15rem\]/);
    });

    it('a pinned tab, with no close slot, closes its padding on the button (C13, polish)', () => {
        const { getAllByRole, queryByRole } = mount();
        const pinned = getAllByRole('tab').find(t => t.textContent === 'pinned.md')!;
        expect(pinned.className).toMatch(/\bpl-3\b/);
        expect(pinned.className).toMatch(/\bpr-3\b/);
        expect(queryByRole('button', { name: 'Close pinned.md' })).toBeNull();
    });

    it('the order is tabs, +, a spacer, Expand (C13)', () => {
        const { getByRole, container } = mount();
        const tablist = getByRole('tablist');
        expect(tablist.className).toMatch(/\bflex-1\b/);
        expect(tablist.lastElementChild).toBe(getByRole('button', { name: 'New tab' }));
        expect((container.firstElementChild as HTMLElement).lastElementChild).toBe(getByRole('button', { name: /Expand the cell view/ }));
    });

    it.each([false, true])('a strip without the tools (every leaf but the top-right one) shows no status pill, annotation toggle or expand / collapse, its tabs running to the edge (maximized: %s)', maximized => {
        const { container, getByRole, queryByRole, queryByTestId } = mount({ tools: false, maximized });
        expect(queryByTestId('cell-status-toggle-strip')).toBeNull();
        expect(queryByTestId('annotation-pane-toggle')).toBeNull();
        expect(queryByRole('button', { name: maximized ? 'Back to the canvas' : /Expand the cell view/ })).toBeNull();
        // The tablist, still flex-1, is the strip's last child: no cluster,
        // no margins left behind; New tab still rides the tab row.
        const root = container.firstElementChild as HTMLElement;
        expect(root.lastElementChild).toBe(getByRole('tablist'));
        expect(getByRole('tablist').lastElementChild).toBe(getByRole('button', { name: 'New tab' }));
    });
});

describe('the compare tab, the cell\'s temporary tab', () => {
    const compareLeaf: ContentLeaf = {
        type: 'leaf', id: 'leaf', activeTabId: 'c',
        tabs: [
            { id: 't1', kind: 'file', path: 'notes.md' },
            {
                id: 'c', kind: 'compare',
                base: { channel: 'ch', cellId: 'cell-2', fileName: 'notes.md', label: 'Draft / Beta' },
                current: { channel: 'ch', cellId: 'cell-1', fileName: 'notes.md', label: 'Work / Alpha' },
            },
        ],
    };
    const mount = () => {
        const onCloseFileTab = vi.fn();
        const onLayoutChange = vi.fn();
        const utils = render(
            <TabStrip tools
                cellId="cell-1" pinnedPath="notes.md" leaf={compareLeaf}
                onLayoutChange={onLayoutChange} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={onCloseFileTab}
            />,
        );
        const tab = (utils.container.querySelector('[data-tab-id="c"]') as HTMLElement);
        return { ...utils, tab, onCloseFileTab, onLayoutChange };
    };
    const layout = { root: compareLeaf, activeLeafId: 'leaf', filesPane: {} as never, runsPane: {} as never };
    const settle = () => new Promise(r => setTimeout(r, 0));

    it('reads the file\'s name in italic, the extension in mono, no icon, both sides in the tooltip (C13)', () => {
        const { tab } = mount();
        const button = within(tab).getByRole('tab');
        expect(button.textContent).toBe('notes.md');
        expect(button.className).toMatch(/\bitalic\b/);
        expect(within(button).getByText('.md').className).toMatch(/\bfont-mono\b/);
        expect(button.querySelector('svg')).toBeNull();
        expect(button.getAttribute('title')).toBe('Compare · Draft / Beta · notes.md vs Work / Alpha · notes.md');
        // The live file tab of the same name stays upright.
        const live = within(tab.parentElement!.querySelector('[data-tab-id="t1"]') as HTMLElement).getByRole('tab');
        expect(live.className).not.toMatch(/\bitalic\b/);
    });

    it('closes by id from its glyph and a middle click, never through the file close (polish)', async () => {
        const { tab, getByRole, onCloseFileTab, onLayoutChange } = mount();
        // Pinning notes.md pins the file tab, never the compare of it.
        expect(getByRole('button', { name: 'Close notes.md' })).toBeTruthy();
        fireEvent.click(getByRole('button', { name: 'Close notes.md' }));
        fireEvent(within(tab).getByRole('tab'), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }));
        await settle();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect(onLayoutChange).toHaveBeenCalledTimes(2);
        for (const [fn] of onLayoutChange.mock.calls) {
            expect((fn(layout).root as ContentLeaf).tabs.map((t: { id: string }) => t.id)).toEqual(['t1']);
        }
    });

    it('the context menu closes it by id and offers no pin, Open with or Compare with (R22)', async () => {
        const { tab, onCloseFileTab, onLayoutChange } = mount();
        fireEvent.contextMenu(tab);
        expect(screen.queryByRole('button', { name: /Pin as preview|Unpin/ })).toBeNull();
        expect(screen.queryByRole('button', { name: /Open with|Compare with/ })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Close' }));
        await settle();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect((onLayoutChange.mock.calls[0][0](layout).root as ContentLeaf).tabs.map(t => t.id)).toEqual(['t1']);
    });
});

describe('the expand button sits on the tools\' line', () => {
    it('stands on the strip\'s hairline beside the tabs, centred on their text (polish)', () => {
        const { getByRole } = strip(null);
        const btn = getByRole('button', { name: /Expand the cell view/ });
        expect(btn.className).toMatch(/\bself-end\b/);
        expect(btn.className).toMatch(/\bmb-1\b/);
        expect(btn.className).not.toMatch(/\bmt-px\b|\bself-center\b/);
    });
});

describe('the annotation pane toggle, always on the strip', () => {
    const withRunsPane = (patch: Partial<RunsPaneState> = {}) => useCollimateStore.setState({
        cellLayouts: { 'cell-1': setRunsPane(createDefaultLayout({ filePaths: [], pinnedPath: null }), patch) },
    } as never);
    const runsPane = () => useCollimateStore.getState().cellLayouts['cell-1'].runsPane;
    const toggle = () => screen.getByTestId('annotation-pane-toggle');
    const glyph = () => toggle().querySelector('svg.lucide-list')!;
    afterEach(() => { useCollimateStore.setState({ cellLayouts: {} } as never); });

    it.each([false, true])('sits on the tools\' line directly left of the expand / collapse button (maximized: %s), the strip\'s one copy', maximized => {
        withRunsPane();
        render(
            <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}} maximized={maximized} />,
        );
        expect(screen.getAllByTestId('annotation-pane-toggle')).toHaveLength(1);
        expect(toggle().className).toMatch(/\bself-end\b/);
        expect(toggle().className).toContain('text-ink-3');
        const expand = screen.getByRole('button', { name: maximized ? 'Back to the canvas' : 'Expand the cell view to the full window' });
        expect(toggle().closest('[data-runs-dock-handle]')!.nextElementSibling).toBe(expand);
    });

    it('is a plain List glyph turned for the pane\'s side and state, with no count or status ink, idle or live (C16)', () => {
        const at = (patch: Partial<RunsPaneState>) => { cleanup(); withRunsPane(patch); strip(null); };
        at({});
        // Closed on the right: as drawn.
        expect(glyph().classList.contains('-scale-x-100')).toBe(false);
        expect(toggle().getAttribute('aria-expanded')).toBe('false');
        expect(toggle().getAttribute('aria-label')).toBe('Show the annotation pane');
        at({ open: true });
        // Open on the right: mirrored.
        expect(glyph().classList.contains('-scale-x-100')).toBe(true);
        expect(glyph().classList.contains('rotate-90')).toBe(false);
        expect(toggle().getAttribute('aria-expanded')).toBe('true');
        expect(toggle().getAttribute('aria-label')).toBe('Hide the annotation pane');
        // Closed at the bottom: pointed down; open there: pointed up.
        at({ side: 'bottom' });
        expect(glyph().classList.contains('rotate-90')).toBe(true);
        at({ side: 'bottom', open: true });
        expect(glyph().classList.contains('-rotate-90')).toBe(true);
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'cell-1', channel: 'ch', runnerId: 'x', runnerName: 'X', generatorName: 'g', label: 'x-1', folder: '_runs/x-1' } },
            stoppingRunIds: new Set(),
        } as never);
        at({});
        expect(toggle().querySelector('[data-run-count]')).toBeNull();
        expect(toggle().textContent).toBe('');
        expect(glyph().getAttribute('class')).not.toMatch(/text-(teal|brass|red)/);
    });

    it('a click toggles the pane, its press reaching the card (which selects the cell); a right-click docks it on the other side, unopened (C16)', () => {
        withRunsPane();
        const cardPress = vi.fn();
        render(
            <div onPointerDown={cardPress}>
                <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                    onToggleMaximized={() => {}} onCloseFileTab={() => {}} />
            </div>,
        );
        fireEvent.pointerDown(toggle(), { button: 0 });
        expect(cardPress).toHaveBeenCalledTimes(1);
        fireEvent.click(toggle());
        expect(runsPane()).toMatchObject({ open: true, side: 'right' });
        expect(useCollimateStore.getState().statusSection['cell-1']?.open ?? false).toBe(false);
        fireEvent.click(toggle());
        expect(runsPane().open).toBe(false);
        expect(fireEvent.contextMenu(toggle())).toBe(false);
        expect(runsPane()).toMatchObject({ open: false, side: 'bottom' });
        expect(glyph().classList.contains('rotate-90')).toBe(true);
        fireEvent.contextMenu(toggle());
        expect(runsPane().side).toBe('right');
    });

    it('its tooltip names the toggle, the right-click and the drag (C16)', () => {
        withRunsPane();
        strip(null);
        expect(toggle().title).toBe('Show the annotation pane · right-click: dock on the bottom · drag to the right or bottom edge to dock');
        cleanup();
        withRunsPane({ open: true, side: 'bottom' });
        strip(null);
        expect(toggle().title).toBe('Hide the annotation pane · right-click: dock on the right · drag to the right or bottom edge to dock');
    });
});

describe('the status toggle in the full-window view', () => {
    it('sits on the tools\' line directly left of the annotation toggle while maximized, idle or not, and not otherwise', () => {
        const { queryByTestId } = strip(null);
        expect(queryByTestId('cell-status-toggle-strip')).toBeNull();
        cleanup();
        const { getByTestId } = render(
            <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}} maximized />,
        );
        const handle = getByTestId('annotation-pane-toggle').closest('[data-runs-dock-handle]')!;
        const status = getByTestId('cell-status-toggle-strip');
        // The pill on the tools' line, the light first inside it.
        expect(status.nextElementSibling).toBe(handle);
        expect(status.className).toMatch(/\bself-end\b/);
        expect(status.querySelector('[data-pill-content]')!.firstElementChild!.getAttribute('data-testid')).toBe('run-light');
        expect(status.getAttribute('aria-expanded')).toBe('false');
        // The strip-sized pill, at the tools' 18px height.
        expect(status.getAttribute('data-pill-variant')).toBe('strip');
        expect(status.className).toMatch(/\bh-\[1\.125rem\]/);
        expect(status.className).toMatch(/\brounded-full\b/);
        expect(status.querySelector('[data-pill-sizer]')!.textContent).toBe('99 running');
    });

    const maximizedStrip = (props: Partial<TabStripProps> = {}, selected = true) => render(
        <CellSelectionContext.Provider value={selected}>
            <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}} maximized {...props} />
        </CellSelectionContext.Provider>,
    );
    const pillText = (status: HTMLElement) => status.querySelector('[data-pill-content]')!.textContent;

    it('the strip\'s pill reads ▶ Run where a run may start, Status where none may, in plain ink, the stronger border marking the selected cell', () => {
        useCollimateStore.setState({ runs: {}, stoppingRunIds: new Set(), runPhases: {}, statusSection: {}, statusSectionFocus: {} } as never);
        let status = maximizedStrip({ allowRun: true }).getByTestId('cell-status-toggle-strip');
        expect(pillText(status)).toBe('Run');
        expect(status.querySelector('[data-play-glyph]')).not.toBeNull();
        expect(status.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-ink-2', 'border-line-2']));
        expect(status.className.split(/\s+/).filter(c => c.includes('brass'))).toEqual([]);
        expect(status.getAttribute('data-selected')).not.toBeNull();
        expect(status.querySelector('[data-chevron]')!.getAttribute('data-chevron')).toBe('down');
        cleanup();
        status = maximizedStrip({ allowRun: true }, false).getByTestId('cell-status-toggle-strip');
        expect(pillText(status)).toBe('Run');
        expect(status.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-ink-2', 'border-line', 'group-hover/card:border-line-2']));
        expect(status.className.split(/\s+/)).not.toContain('border-line-2');
        expect(status.hasAttribute('data-selected')).toBe(false);
        cleanup();
        status = maximizedStrip({ allowRun: false }).getByTestId('cell-status-toggle-strip');
        expect(pillText(status)).toBe('Status');
        expect(status.querySelector('[data-play-glyph]')).toBeNull();
        expect(status.className.split(/\s+/)).toEqual(expect.arrayContaining(['text-ink-2', 'border-line-2']));
    });

    it('the strip\'s pill reads "N running" in the light\'s ink and toggles the status section, its chevron turning up', () => {
        useCollimateStore.setState({
            runs: { r: { runId: 'r', cellId: 'cell-1', channel: 'ch', runnerId: 'x', runnerName: 'X', generatorName: 'g', label: 'x-1', folder: '_runs/x-1' } },
            stoppingRunIds: new Set(), runPhases: {}, statusSection: {}, statusSectionFocus: {},
        } as never);
        const { getByTestId } = render(
            <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}} maximized />,
        );
        const status = getByTestId('cell-status-toggle-strip');
        expect(pillText(status)).toBe('1 running');
        expect(status.querySelector('[data-pill-count]')!.getAttribute('data-pill-count')).toBe('1');
        expect(status.className).toContain('text-teal');
        expect(status.getAttribute('aria-label')).toBe('Show status · 1 running');
        fireEvent.click(status);
        expect(useCollimateStore.getState().statusSection['cell-1']?.open).toBe(true);
        expect(status.getAttribute('aria-expanded')).toBe('true');
        expect(status.querySelector('[data-chevron]')!.getAttribute('data-chevron')).toBe('up');
        expect(status.className.split(/\s+/).filter(c => c.startsWith('bg-'))).toEqual(['bg-transparent']);
        useCollimateStore.setState({ runs: {}, statusSection: {} } as never);
    });

    it.each([false, true])('neither toggle shows on a Capabilities cell, which has neither a status section nor an annotation pane (maximized: %s)', maximized => {
        const { queryByTestId, getByRole } = render(
            <TabStrip tools cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={() => {}} onBeforeStructuralChange={() => {}}
                onToggleMaximized={() => {}} onCloseFileTab={() => {}} maximized={maximized} capabilitiesCell />,
        );
        expect(queryByTestId('annotation-pane-toggle')).toBeNull();
        expect(queryByTestId('cell-status-toggle-strip')).toBeNull();
        expect(getByRole('button', { name: maximized ? 'Back to the canvas' : 'Expand the cell view to the full window' })).toBeTruthy();
    });
});

describe('a middle click closes a tab', () => {
    const mount = () => {
        const onCloseFileTab = vi.fn();
        const onLayoutChange = vi.fn();
        const onBeforeStructuralChange = vi.fn();
        const utils = render(
            <TabStrip tools
                cellId="cell-1"
                pinnedPath="pinned.md"
                leaf={leaf}
                onLayoutChange={onLayoutChange}
                onBeforeStructuralChange={onBeforeStructuralChange}
                onToggleMaximized={() => {}}
                onCloseFileTab={onCloseFileTab}
            />,
        );
        return { ...utils, onCloseFileTab, onLayoutChange, onBeforeStructuralChange };
    };
    const tabEl = (container: HTMLElement, id: string) => container.querySelector(`[data-tab-id="${id}"]`) as HTMLElement;
    /** The browser's middle click: the auxiliary press, then its auxclick. */
    const middleClick = (el: Element) => {
        const press = fireEvent.mouseDown(el, { button: 1, buttons: 4 });
        fireEvent.mouseUp(el, { button: 1 });
        const aux = fireEvent(el, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }));
        return { press, aux };
    };
    const settle = () => new Promise(r => setTimeout(r, 0));

    it('a right click does not close the tab and still opens the context menu (polish)', async () => {
        const { container, onCloseFileTab, onLayoutChange, onBeforeStructuralChange } = mount();
        const t = tabEl(container, 't1');
        fireEvent(t, new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 2 }));
        fireEvent.contextMenu(t);
        await settle();
        expect(onBeforeStructuralChange).not.toHaveBeenCalled();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect(onLayoutChange).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Close others' })).toBeTruthy();
    });

    it('closes a file tab through the same callback the close glyph uses, unsaved draft and all (polish)', async () => {
        const viaGlyph = mount();
        fireEvent.click(viaGlyph.getByRole('button', { name: 'Close notes.md' }));
        await settle();
        expect(viaGlyph.onCloseFileTab.mock.calls).toEqual([['notes.md']]);
        cleanup();

        const viaMiddle = mount();
        middleClick(within(tabEl(viaMiddle.container, 't1')).getByRole('tab'));
        await settle();
        expect(viaMiddle.onBeforeStructuralChange).toHaveBeenCalledTimes(1);
        expect(viaMiddle.onCloseFileTab.mock.calls).toEqual(viaGlyph.onCloseFileTab.mock.calls);
        expect(viaMiddle.onLayoutChange).not.toHaveBeenCalled();
    });

    it('closes a non-file tab with the same layout surgery as its glyph (polish)', async () => {
        const viaGlyph = mount();
        fireEvent.click(viaGlyph.getByRole('button', { name: 'Close old.md' }));
        await settle();
        cleanup();
        const viaMiddle = mount();
        middleClick(tabEl(viaMiddle.container, 't4'));
        await settle();
        expect(viaMiddle.onCloseFileTab).not.toHaveBeenCalled();
        expect(viaMiddle.onLayoutChange).toHaveBeenCalledTimes(1);
        const layout = { root: leaf, activeLeafId: 'leaf', filesPane: {} as never, runsPane: {} as never };
        expect(viaMiddle.onLayoutChange.mock.calls[0][0](layout)).toEqual(viaGlyph.onLayoutChange.mock.calls[0][0](layout));
    });

    it('does not close the pinned tab, which shows no close glyph (polish)', async () => {
        const { container, onCloseFileTab, onLayoutChange, onBeforeStructuralChange } = mount();
        const { press } = middleClick(within(tabEl(container, 't2')).getByRole('tab'));
        await settle();
        expect(press).toBe(false);
        expect(onBeforeStructuralChange).not.toHaveBeenCalled();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect(onLayoutChange).not.toHaveBeenCalled();
    });

    it('does nothing on the New tab button or the strip\'s empty space (polish)', async () => {
        const { getByRole, onCloseFileTab, onLayoutChange, onBeforeStructuralChange } = mount();
        middleClick(getByRole('button', { name: 'New tab' }));
        middleClick(getByRole('tablist'));
        await settle();
        expect(onBeforeStructuralChange).not.toHaveBeenCalled();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect(onLayoutChange).not.toHaveBeenCalled();
    });

    it('prevents the default on the press, so it neither scrolls the strip nor pastes (polish)', () => {
        const { container } = mount();
        const { press, aux } = middleClick(within(tabEl(container, 't1')).getByRole('tab'));
        expect(press).toBe(false);
        expect(aux).toBe(false);
        // A primary press keeps its default: focus and drag start as before.
        expect(fireEvent.mouseDown(within(tabEl(container, 't1')).getByRole('tab'), { button: 0 })).toBe(true);
    });
});

describe('the Manage tab, a Capabilities cell\'s fixed tab', () => {
    const manageLeaf: ContentLeaf = {
        type: 'leaf', id: 'leaf', activeTabId: 'm',
        tabs: [
            { id: 'm', kind: 'manage' },
            { id: 't1', kind: 'file', path: 'notes.md' },
            { id: 't2', kind: 'file', path: 'pinned.md' },
        ],
    };
    const layout = { root: manageLeaf, activeLeafId: 'leaf', filesPane: {} as never, runsPane: {} as never };
    const settle = () => new Promise(r => setTimeout(r, 0));
    const mount = (over: Partial<TabStripProps> = {}) => {
        const onCloseFileTab = vi.fn();
        const onLayoutChange = vi.fn();
        const onBeforeStructuralChange = vi.fn();
        const utils = render(
            <TabStrip tools
                cellId="cell-1" pinnedPath="pinned.md" leaf={manageLeaf}
                onLayoutChange={onLayoutChange} onBeforeStructuralChange={onBeforeStructuralChange}
                onToggleMaximized={() => {}} onCloseFileTab={onCloseFileTab} {...over}
            />,
        );
        const frame = utils.container.querySelector('[data-tab-id="m"]') as HTMLElement;
        return { ...utils, frame, onCloseFileTab, onLayoutChange, onBeforeStructuralChange };
    };

    it('leads the strip, reads ‘Manage’ with no icon, and has no close slot', () => {
        const { frame, getAllByRole } = mount();
        expect(getAllByRole('tab')[0]).toBe(within(frame).getByRole('tab'));
        const button = within(frame).getByRole('tab');
        expect(button.textContent).toBe('Manage');
        expect(button.getAttribute('title')).toBe('Manage the capabilities this cell holds');
        expect(button.querySelector('svg')).toBeNull();
        expect(button.className).toMatch(/\bpr-3\b/);
        expect(screen.queryByRole('button', { name: 'Close Manage' })).toBeNull();
        expect(within(frame).queryByTestId('tab-pin')).toBeNull();
    });

    it('keeps its label with the pinned tab folded: the fold is the pin\'s alone', () => {
        const { frame, container } = mount({ pinnedTabCompact: true });
        const button = within(frame).getByRole('tab');
        expect(button.textContent).toBe('Manage');
        expect(button.getAttribute('aria-label')).toBeNull();
        // The pinned file tab beside it is the one that folds.
        const pinned = within(container.querySelector('[data-tab-id="t2"]') as HTMLElement).getByRole('tab');
        expect(pinned.textContent).toBe('');
    });

    it('is not draggable, and a drag start on it is refused', () => {
        const { frame, container } = mount();
        expect(frame.getAttribute('draggable')).toBe('false');
        expect((container.querySelector('[data-tab-id="t1"]') as HTMLElement).getAttribute('draggable')).toBe('true');
        useCollimateStore.setState({ tabDrag: null });
        fireEvent.dragStart(frame, { dataTransfer: { setData: () => {}, effectAllowed: '' } });
        expect(useCollimateStore.getState().tabDrag).toBeNull();
    });

    it('a middle click leaves it open', async () => {
        const { frame, onCloseFileTab, onLayoutChange, onBeforeStructuralChange } = mount();
        fireEvent(within(frame).getByRole('tab'), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }));
        await settle();
        expect(onBeforeStructuralChange).not.toHaveBeenCalled();
        expect(onCloseFileTab).not.toHaveBeenCalled();
        expect(onLayoutChange).not.toHaveBeenCalled();
    });

    it('its context menu disables Close and the splits; Close others and Close to the right keep it', async () => {
        const { frame, onLayoutChange } = mount();
        fireEvent.contextMenu(frame);
        expect(screen.getByRole('button', { name: 'Close' })).toBeDisabled();
        expect(screen.getByRole('button', { name: 'Split right' })).toBeDisabled();
        expect(screen.getByRole('button', { name: 'Split down' })).toBeDisabled();
        expect(screen.queryByRole('button', { name: /Pin as preview|Unpin/ })).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Close to the right' }));
        await settle();
        // The pinned file stays as well: Close to the right closes what closes.
        expect((onLayoutChange.mock.calls[0][0](layout).root as ContentLeaf).tabs.map(t => t.id)).toEqual(['m', 't2']);
    });

    it('a tab dropped at the strip\'s head lands after it, where its insertion bar shows', async () => {
        const { getByRole, container, onLayoutChange } = mount();
        // Three 50px tabs side by side.
        (['m', 't1', 't2'] as const).forEach((id, i) => {
            const el = container.querySelector(`[data-tab-id="${id}"]`) as HTMLElement;
            el.getBoundingClientRect = () => ({ left: i * 50, right: i * 50 + 50, width: 50, top: 0, bottom: 26, height: 26, x: i * 50, y: 0, toJSON: () => ({}) });
        });
        useCollimateStore.setState({ tabDrag: { cellId: 'cell-1', leafId: 'leaf', tabId: 't2' } });
        const list = getByRole('tablist');
        const drag = (type: string) => {
            const event = new Event(type, { bubbles: true, cancelable: true });
            Object.assign(event, { clientX: 5, dataTransfer: { dropEffect: '' } });
            fireEvent(list, event);
        };
        // Over the Manage tab's own left half: the bar stands after it.
        drag('dragover');
        const bar = list.querySelector('[aria-hidden].bg-teal') as HTMLElement;
        expect(bar.style.left).toBe('49px');
        drag('drop');
        await settle();
        expect((onLayoutChange.mock.calls[0][0](layout).root as ContentLeaf).tabs.map(t => t.id)).toEqual(['m', 't2', 't1']);
        useCollimateStore.setState({ tabDrag: null });
    });
});
