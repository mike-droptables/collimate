import { fileFixture } from '@/test/fileFixtures';
// The Files pane's width (C12, owner polish 2026-10-07, over D-C12's fixed
// pane): docked left, 176px by default, resizable by a 6px strip on its
// right edge between 140px and two thirds of the cell, the width kept per
// cell in the layout (`filesPane.width`, persisted). The drag previews
// locally and commits once on release. Hidden, the pane draws nothing and
// the tab bar leads with the one files toggle — in the browser and under
// the desktop shell alike. The other sides: FilesPane.dock.test.tsx.

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

import { FilesPane } from './FilesPane';
import { TabStrip } from './TabStrip';
import {
    FILES_PANE_MIN_WIDTH, FILES_PANE_WIDTH, createDefaultLayout, setFilesPane,
} from '@/lib/cellLayout';
import type { Cell } from '@/lib/types';
import type { CellLayout } from '@/stores/collimate/types';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { isDesktop } from '@/lib/desktop';

const cell: Cell = { id: 'cell-1', name: 'Test', files: [fileFixture('a.md', '', {language: 'markdown', timestamp: 't'})] };

const shownLayout = (width?: number): CellLayout => setFilesPane(
    createDefaultLayout({ filePaths: new Set(['a.md']), pinnedPath: null }),
    width === undefined ? { collapsed: false } : { collapsed: false, width },
);

afterEach(() => { cleanup(); vi.restoreAllMocks(); uninstallDesktopShell(); });

/** Every element's box: the cell (the pane's parent) is `cellWidth` wide,
 *  the pane is its own inline width. */
function mockBoxes(cellWidth: number) {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
        const width = this.tagName === 'ASIDE' ? parseFloat(this.style.width) || 0 : cellWidth;
        return { x: 0, y: 0, top: 0, left: 0, right: width, bottom: 400, width, height: 400, toJSON: () => ({}) } as DOMRect;
    });
}

function renderPane(layout: CellLayout = shownLayout()) {
    const onLayoutChange = vi.fn();
    const view = render(
        <div style={{ display: 'flex' }}>
            <FilesPane
                cell={cell}
                layout={layout}
                onLayoutChange={onLayoutChange}
                onOpenFile={vi.fn()}
                onUpdateFiles={vi.fn()}
                readOnly={false}
                activePath={null}
            />
        </div>,
    );
    return { ...view, onLayoutChange };
}

const pane = () => screen.getByRole('complementary', { name: 'Files' });
const handle = () => screen.getByRole('separator', { name: 'Resize files' });
/** The width the last layout write stored. */
const storedWidth = (fn: ReturnType<typeof vi.fn>) =>
    (fn.mock.calls.at(-1)![0] as (l: CellLayout) => CellLayout)(shownLayout()).filesPane.width;

function drag(from: number, to: number) {
    const h = handle();
    fireEvent.pointerDown(h, { clientX: from, button: 0, pointerId: 1 });
    fireEvent.pointerMove(h, { clientX: to, pointerId: 1 });
    return () => fireEvent.pointerUp(h, { clientX: to, pointerId: 1 });
}

describe('FilesPane width', () => {
    it('uses the native Files pane width, held between its minimum and two thirds of the cell, short of the content minimum (C12, polish)', () => {
        renderPane();
        expect(FILES_PANE_WIDTH).toBe(194);
        expect(FILES_PANE_MIN_WIDTH).toBe(154);
        expect(pane().style.width).toBe('194px');
        expect(pane().style.minWidth).toBe('min(154px, min(66.6667%, 100% - 160px))');
        expect(pane().style.maxWidth).toBe('min(66.6667%, 100% - 160px)');
    });

    it('shows the cell\'s stored width (C12, polish)', () => {
        renderPane(shownLayout(230));
        expect(pane().style.width).toBe('230px');
    });

    it('a 6px strip on the right edge, line-2 on hover, with the col-resize cursor (C12, polish)', () => {
        renderPane();
        const h = handle();
        expect(pane().contains(h)).toBe(true);
        expect(pane().className).toMatch(/\brelative\b/);
        expect(h).toHaveAttribute('aria-orientation', 'vertical');
        expect(h.className).toMatch(/\bw-1\.5\b/);
        expect(h.className).toMatch(/\babsolute\b/);
        expect(h.className).toMatch(/\btop-0\b/);
        expect(h.className).toMatch(/\bbottom-0\b/);
        expect(h.className).toMatch(/-right-px\b/);
        expect(h.className).toMatch(/\bcursor-col-resize\b/);
        expect(h.className).toMatch(/\bhover:bg-line-2\b/);
    });

    it('a drag previews the width live and stores it once, on release (C12, polish)', () => {
        mockBoxes(800);
        const { onLayoutChange } = renderPane();
        const before = onLayoutChange.mock.calls.length;
        const release = drag(194, 254);
        expect(pane().style.width).toBe('254px');
        expect(onLayoutChange.mock.calls.length).toBe(before);
        release();
        expect(onLayoutChange.mock.calls.length).toBe(before + 1);
        expect(storedWidth(onLayoutChange)).toBe(254);
    });

    it('a drag stops at the minimum and at two thirds of the cell (C12, polish)', () => {
        mockBoxes(600);
        const { onLayoutChange, unmount } = renderPane();
        drag(194, 0)();
        expect(storedWidth(onLayoutChange)).toBe(FILES_PANE_MIN_WIDTH);
        unmount();

        const second = renderPane();
        drag(194, 900)();
        expect(storedWidth(second.onLayoutChange)).toBe(400);
    });

    it('a cancelled drag stores nothing and returns to the stored width (C12, polish)', () => {
        mockBoxes(800);
        const { onLayoutChange } = renderPane(shownLayout(200));
        const before = onLayoutChange.mock.calls.length;
        drag(200, 260);
        expect(pane().style.width).toBe('260px');
        fireEvent.pointerCancel(handle(), { pointerId: 1 });
        expect(pane().style.width).toBe('200px');
        expect(onLayoutChange.mock.calls.length).toBe(before);
    });

    it('the arrow keys move the focused edge 8px, within the bounds (C12, polish)', () => {
        mockBoxes(800);
        const { onLayoutChange, unmount } = renderPane();
        fireEvent.keyDown(handle(), { key: 'ArrowRight' });
        expect(storedWidth(onLayoutChange)).toBe(202);
        unmount();

        const atMin = renderPane(shownLayout(FILES_PANE_MIN_WIDTH));
        const before = atMin.onLayoutChange.mock.calls.length;
        fireEvent.keyDown(handle(), { key: 'ArrowLeft' });
        expect(atMin.onLayoutChange.mock.calls.length).toBe(before);
    });

    it('the stored width is the cell\'s and survives hiding the pane (C12, polish)', () => {
        const hiddenAgain = setFilesPane(shownLayout(250), { collapsed: true });
        expect(hiddenAgain.filesPane.width).toBe(250);
        renderPane(setFilesPane(hiddenAgain, { collapsed: false }));
        expect(pane().style.width).toBe('250px');
    });
});

describe('the files toggle while the pane is hidden', () => {
    /** The pane region as CellEditor composes it: the pane, then the first
     *  leaf's tab bar carrying the show toggle while the pane is hidden. */
    function renderRegion() {
        const layout = setFilesPane(createDefaultLayout({ filePaths: new Set(['a.md']), pinnedPath: null }), { collapsed: true });
        const leaf = layout.root;
        if (leaf.type !== 'leaf') throw new Error('expected one leaf');
        return render(
            <div style={{ display: 'flex' }}>
                <FilesPane
                    cell={cell} layout={layout} onLayoutChange={vi.fn()} onOpenFile={vi.fn()}
                    onUpdateFiles={vi.fn()} readOnly={false} activePath={null}
                />
                <TabStrip
                    cellId="cell-1" pinnedPath={null} leaf={leaf} onLayoutChange={vi.fn()} onBeforeStructuralChange={vi.fn()}
                    onToggleMaximized={vi.fn()} onCloseFileTab={vi.fn()}
                    showFiles={{ side: layout.filesPane.side, onShow: vi.fn() }}
                />
            </div>,
        );
    }

    const toggles = (root: HTMLElement) => ({
        buttons: root.querySelectorAll('button[aria-label="Show files"], button[aria-label="Hide files"]').length,
        glyphs: root.querySelectorAll('.lucide-panel-left').length,
    });

    it('exactly one toggle, in the browser and under the desktop shell', () => {
        const browser = renderRegion();
        expect(toggles(browser.container)).toEqual({ buttons: 1, glyphs: 1 });
        expect(browser.queryByRole('separator')).toBeNull();
        browser.unmount();

        installDesktopShell();
        const desktop = renderRegion();
        expect(isDesktop()).toBe(true);
        expect(toggles(desktop.container)).toEqual({ buttons: 1, glyphs: 1 });
        expect(desktop.queryByRole('complementary', { name: 'Files' })).toBeNull();
    });
});

describe('the Show files toggle names the dock side (FP-02, FP-03)', () => {
    const stripProps = (onLayoutChange = vi.fn(), onShow = vi.fn()) => {
        const layout = shownLayout();
        const leaf = layout.root.type === 'leaf' ? layout.root : null;
        if (!leaf) throw new Error('expected a leaf');
        return { cellId: 'cell-1', validation: { issuesByName: {} } as never, pinnedPath: null, leaf, onLayoutChange, onBeforeStructuralChange: vi.fn(async () => {}), dirtyByName: {}, onCloseFileTab: vi.fn(), onToggleMaximized: vi.fn(), onShow };
    };

    it.each([
        ['left', 'panel-left'], ['top', 'panel-top'],
    ] as const)('docked %s, the toggle draws the %s icon (FP-02)', (side, icon) => {
        const { onShow, ...props } = stripProps();
        render(<TabStrip {...props} showFiles={{ side, onShow }} />);
        const show = screen.getByRole('button', { name: 'Show files' });
        expect(show.querySelector(`.lucide-${icon}`)).not.toBeNull();
        expect(show.querySelectorAll('svg')).toHaveLength(1);
    });

    it('a right-click on the hidden pane\'s toggle docks on the other side without opening it (FP-03)', () => {
        const onLayoutChange = vi.fn();
        const { onShow, ...props } = stripProps(onLayoutChange);
        render(<TabStrip {...props} showFiles={{ side: 'left', onShow }} />);
        const show = screen.getByRole('button', { name: 'Show files' });
        expect(show).toHaveAttribute('title', 'Show files · right-click: dock on the top · drag to the left or top edge to dock');
        expect(fireEvent.contextMenu(show)).toBe(false);
        expect(onShow).not.toHaveBeenCalled();
        const hidden = setFilesPane(shownLayout(), { collapsed: true });
        const next = (onLayoutChange.mock.calls.at(-1)![0] as (l: CellLayout) => CellLayout)(hidden);
        expect(next.filesPane.side).toBe('top');
        expect(next.filesPane.collapsed).toBe(true);
    });
});
