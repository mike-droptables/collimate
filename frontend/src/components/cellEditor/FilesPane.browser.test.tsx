import { fileFixture } from '@/test/fileFixtures';
/**
 * The Files pane in its cell's pane row, with real CSS: the left column
 * runs to the cell's bottom with its Import / export link at that edge,
 * and the top band sits at the cell's top. Run with VITEST_BROWSER=1 (see
 * vitest.config.ts).
 */
import { expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import '@/app/globals.css';
import { FilesPane } from './FilesPane';
import { createDefaultLayout, filesPaneRowClass, setFilesPane } from '@/lib/cellLayout';
import type { FilesPaneSide } from '@/stores/collimate/types';

/** The content region as CellEditor nests it: the pane row runs per dock side. */
async function mount(side: FilesPaneSide) {
    await render(
        <div data-content-region style={{ position: 'relative', width: 600, height: 400, display: 'flex', flexDirection: 'column' }}>
            <div data-pane-region className={`flex ${filesPaneRowClass(side)}`} style={{ flex: 1, minHeight: 0 }}>
                <FilesPane
                    cell={{ id: 'cell-1', name: 'Cell', files: [fileFixture('notes.md', '', {language: 'markdown', timestamp: 't'})] }}
                    layout={setFilesPane(createDefaultLayout({ filePaths: new Set(['notes.md']), pinnedPath: null }), { collapsed: false, side })}
                    onLayoutChange={vi.fn()}
                    onOpenFile={vi.fn()}
                    onUpdateFiles={vi.fn()}
                    readOnly={false}
                    activePath={null}
                />
            </div>
        </div>,
    );
    const region = document.querySelector<HTMLElement>('[data-content-region]')!;
    const pane = region.querySelector<HTMLElement>('aside[aria-label="Files"]')!;
    const link = [...pane.querySelectorAll('button')].find(b => b.textContent === 'Import / export')!;
    return { region, pane, link };
}

it('the left column runs to the cell\'s bottom, its Import / export link at that edge (TR-11)', async () => {
    const { region, pane, link } = await mount('left');
    expect(getComputedStyle(pane).paddingBottom).toBe('0px');
    const bottom = region.getBoundingClientRect().bottom;
    expect(pane.getBoundingClientRect().bottom).toBeCloseTo(bottom, 1);
    // The link over its strip's bottom padding.
    const pad = parseFloat(getComputedStyle(link.parentElement!).paddingBottom);
    expect(link.getBoundingClientRect().bottom).toBeCloseTo(bottom - pad, 1);
    region.parentElement!.remove();
});

it('the top band sits at the cell\'s top with no bottom padding (FP-02)', async () => {
    const { region, pane } = await mount('top');
    expect(getComputedStyle(pane).paddingBottom).toBe('0px');
    expect(pane.getBoundingClientRect().top).toBeCloseTo(region.getBoundingClientRect().top, 1);
    expect(pane.getBoundingClientRect().width).toBeCloseTo(region.getBoundingClientRect().width, 1);
});
