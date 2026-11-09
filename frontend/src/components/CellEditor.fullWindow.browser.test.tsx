import { fileFixture } from '@/test/fileFixtures';
/**
 * The Commands pane over the full-window view (CellEditor's maximised
 * content region), in Chromium with real CSS: the pane lies above the
 * view, the window's height on its side, and the view's region lays out
 * in the width the pane leaves. Run with VITEST_BROWSER=1 (see
 * vitest.config.ts).
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render } from 'vitest-browser-react';
import { page } from 'vitest/browser';
import { act } from 'react';
import '@/app/globals.css';
import { FocusProvider } from '@/contexts/FocusContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH, defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { COMMAND_PANE_INSET_MS } from '@/lib/commandPane';
import type { Cell, Stack } from '@/lib/types';
import type { SceneActions } from '@/hooks/useSceneActions';

vi.mock('@/hooks/useIdleCheckpoint', () => ({ useIdleCheckpoint: () => ({ notifyEdit: () => {}, flushCheckpoint: () => {} }) }));
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    listCellVersions: async () => ({ versions: [] }),
    fetchCellHistory: async () => ({ nodes: [], total_versions: 1, current: { node_id: null, version_cell_id: 'cell-1' } }),
}));

import CellEditor from './CellEditor';
import { CommandPane } from './CommandPane';

const cell: Cell = { id: 'cell-1', name: 'Work', files: [fileFixture('a.txt', 'x', {language: 'plaintext', timestamp: 't'})] };
const stack: Stack = { id: 's1', name: 'One', cells: [cell] };
const s = () => useCollimateStore.getState();
const rect = (el: Element) => el.getBoundingClientRect();
const settle = () => new Promise(r => setTimeout(r, COMMAND_PANE_INSET_MS + 120));

beforeEach(async () => {
    await page.viewport(1400, 900);
    useCollimateStore.setState({
        cellLayouts: {}, state:{active_channel_id:'ch',channels:[{id:'ch',name:'ch',main_stacks:[stack],nav_stacks:[],main_selection_id:null,nav_selection_id:null}]}, cellCache: { [cell.id]: cell }, runs: {}, stoppingRunIds: new Set(), pickSessions: {}, cellHistory: {},
        commandPane: defaultCommandPane(), maximizedOverlayCellId: null,
        activeChannel: { id: 'ch', name: 'ch', main_stacks: [stack], nav_stacks: [] } as never,
    });
});
afterEach(() => {
    useCollimateStore.setState({ commandPane: defaultCommandPane(), maximizedOverlayCellId: null });
});

async function mount() {
    await render(
        <FocusProvider>
            <div style={{ position: 'relative', width: 600, height: 400 }}>
                <CellEditor cell={cell} onUpdate={() => {}} />
                <CommandPane scene={{
                    displayStacks: [stack], displaySelectedId: 's1', visibleTopIndices: {}, fanRange: null,
                    selectDisplayId: () => {}, setFannedOutStackId: () => {}, actions: {} as SceneActions,
                    mainStacks: [stack], selectedStackId: 's1', isZoomedOut: false, toggleZoom: () => {},
                    capabilities: { shown: false, toggle: () => {}, create: () => {} },
                }} />
            </div>
        </FocusProvider>,
    );
    await page.getByRole('button', { name: 'Expand the cell view to the full window' }).click();
    await expect.poll(() => document.querySelector('[data-full-window]')).not.toBeNull();
    return document.querySelector<HTMLElement>('[data-full-window]')!;
}

for (const side of ['left', 'right'] as const) {
    it(`the pane docked ${side} lies above the full-window view, and the region lies clear of it (polish)`, async () => {
        const view = await mount();
        act(() => { s().setCommandPaneSide(side); s().openCommandPane(); });
        await settle();
        const pane = document.querySelector<HTMLElement>('aside[data-command-pane]')!;
        const p = rect(pane);
        // The window's height on its side.
        expect(p.top).toBe(0);
        expect(p.height).toBe(900);
        expect(p.width).toBe(COMMAND_PANE_WIDTH);
        expect(side === 'left' ? p.left : 1400 - p.right).toBe(0);
        // Above the view: what the eye meets at the pane's middle is the pane.
        expect(pane.contains(document.elementFromPoint(p.left + p.width / 2, 450))).toBe(true);
        expect(view.contains(document.elementFromPoint(p.left + p.width / 2, 450))).toBe(false);
        // The region lays out in the width the pane leaves.
        const region = rect(view.querySelector('[data-content-region]')!);
        expect(region.width).toBe(1400 - COMMAND_PANE_WIDTH);
        if (side === 'left') expect(region.left).toBeGreaterThanOrEqual(p.right);
        else expect(region.right).toBeLessThanOrEqual(p.left);
        // The strip's line goes on under the pane at the strip's height.
        const ground = view.querySelector<HTMLElement>('[data-full-window-ground] > :first-child')!;
        expect(rect(ground).width).toBe(1400);
        expect(getComputedStyle(ground).borderBottomWidth).toBe('1px');
        // Shut, the region takes the window's width back.
        act(() => s().closeCommandPane());
        await settle();
        expect(rect(view.querySelector('[data-content-region]')!).width).toBe(1400);
    });
}
