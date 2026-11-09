import { fileFixture } from '@/test/fileFixtures';
/**
 * The Files pane's rail with real CSS, docked left beside a stand-in for the
 * content pane whose first tab starts at the pane's edge, as in a cell. At
 * the default 194px column the rail holds every tool (hide, Tree, Folder,
 * Expand all, Collapse all, History, Find file) inside the pane; at the
 * 154px minimum it drops Expand all and Collapse all and keeps the rest.
 * Find file never runs under the tab strip: before the fix its box ran
 * 15px past the pane, and a click at its centre hit the first tab. Run with
 * VITEST_BROWSER=1 (see vitest.config.ts).
 */
import { afterEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import '@/app/globals.css';
import { FilesPane } from './FilesPane';
import { FILES_PANE_MIN_WIDTH, FILES_PANE_WIDTH, createDefaultLayout, filesPaneRowClass, setFilesPane } from '@/lib/cellLayout';

const RAIL_TOOLS = ['Hide files', 'Tree', 'Folder', 'Expand all', 'Collapse all', 'History', 'Find file'];

async function mount(width: number) {
    await render(
        <div data-content-region style={{ position: 'relative', width: 550, height: 400, display: 'flex', flexDirection: 'column' }}>
            <div className={`flex ${filesPaneRowClass('left')}`} style={{ flex: 1, minHeight: 0 }}>
                <FilesPane
                    cell={{ id: 'cell-1', name: 'Cell', files: [fileFixture('candidate/view.renderer', '', { language: 'yaml', timestamp: 't' })] }}
                    layout={setFilesPane(createDefaultLayout({ filePaths: new Set(['candidate/view.renderer']), pinnedPath: null }), { collapsed: false, side: 'left', width })}
                    onLayoutChange={vi.fn()}
                    onOpenFile={vi.fn()}
                    onUpdateFiles={vi.fn()}
                    onToggleHistory={vi.fn()}
                    readOnly={false}
                    activePath={null}
                />
                {/* The content pane: painted after the pane, a sheet, its
                    first tab at its left edge (TabStrip). */}
                <div className="flex-1 min-w-0 flex flex-col bg-sheet">
                    <div className="flex items-end h-[1.875rem]">
                        <div data-tab-id="t1" className="flex-none h-[1.625rem] w-[9rem]" />
                    </div>
                </div>
            </div>
        </div>,
    );
    const pane = document.querySelector<HTMLElement>('aside[aria-label="Files"]')!;
    const tool = (label: string) => pane.querySelector<HTMLElement>(`[aria-label="${label}"]`)!;
    const tab = document.querySelector<HTMLElement>('[data-tab-id]')!;
    return { pane, tool, tab };
}

const shown = (el: HTMLElement) => el.getBoundingClientRect().width > 0;

const intersects = (a: DOMRect, b: DOMRect) =>
    a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

afterEach(() => document.querySelector('[data-content-region]')?.parentElement?.remove());

for (const [width, fold] of [[FILES_PANE_WIDTH, true], [FILES_PANE_MIN_WIDTH, false]] as const) {
    it(`at ${width}px every shown rail tool sits inside the pane and Find file takes its own click`, async () => {
        const { pane, tool, tab } = await mount(width);
        const box = pane.getBoundingClientRect();
        expect(box.width).toBeCloseTo(width, 0);
        // The content edge: the pane's right border.
        const inner = box.right - parseFloat(getComputedStyle(pane).borderRightWidth);
        expect(shown(tool('Expand all'))).toBe(fold);
        expect(shown(tool('Collapse all'))).toBe(fold);
        for (const label of RAIL_TOOLS.filter(l => fold || (l !== 'Expand all' && l !== 'Collapse all'))) {
            const b = tool(label).getBoundingClientRect();
            expect(b.width, label).toBeGreaterThan(0);
            expect(b.left, label).toBeGreaterThanOrEqual(box.left);
            expect(b.right, label).toBeLessThanOrEqual(inner);
        }
        const find = tool('Find file');
        const f = find.getBoundingClientRect();
        expect(intersects(f, tab.getBoundingClientRect())).toBe(false);
        const hit = document.elementFromPoint(f.left + f.width / 2, f.top + f.height / 2);
        expect(find.contains(hit)).toBe(true);
    });
}
