import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi, afterEach } from 'vitest';
import React from 'react';
import { render, cleanup, act, fireEvent } from '@testing-library/react';
import { ContentPane } from './ContentPane';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { createDefaultLayout, openFileTab, splitWithTab } from '@/lib/cellLayout';
import { usePooledIframeConcealed, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { isCellOccluded, resetGuestOcclusion } from '@/lib/guestOcclusion';
import type { Cell } from '@/lib/types';

// The pane's own children are heavy (editors, renderers); this test is about
// the CONTEXTS the pane provides while one of its tabs is being dragged, so
// the strip and the tab bodies are replaced by probes of those contexts.
const bodyRenders = vi.hoisted(() => ({ count: 0 }));
vi.mock('./TabStrip', () => ({ TabStrip: () => null }));
vi.mock('./TabBody', () => ({
    TabBody: () => {
        bodyRenders.count++;
        const full = useRendererConcealed();
        const iframes = usePooledIframeConcealed();
        return <div data-testid="probe" data-full={String(full)} data-iframes={String(iframes)} />;
    },
}));
vi.mock('@/contexts/GuestPoolContext', () => ({
    useGuestPool: () => ({ containerOf: () => null }),
}));

const cell: Cell = { id: 'cell-1', name: 'c', files: [fileFixture('a.md', '', {language: 'markdown', timestamp: ''})] } as Cell;

function paneProps() {
    const layout = createDefaultLayout({ filePaths: ['a.md'], pinnedPath: null });
    return {
        cell, layout,
        onLayoutChange: () => {},
        isActive: true, visible: true, readOnly: false,
        registerEditorHandle: () => {},
        onBeforeStructuralChange: () => {},
        dirtyByName: {}, lastSavedByName: {},
        onToggleMaximized: () => {}, onCloseFileTab: () => {}, onCloseTabById: () => {},
        updateFileContent: () => {}, onUpdateFiles: () => {},
        filesPane: layout.filesPane,
        isZoomTransitioning: false,
    };
}

afterEach(() => { cleanup(); resetGuestOcclusion(); useCollimateStore.setState({ tabDrag: null }); });

describe('ContentPane during a tab drag', () => {
    it('conceals the pooled iframes, freezes the guests through the occluder, and does NOT conceal them', () => {
        const { getByTestId } = render(<ContentPane {...paneProps()} />);
        expect(getByTestId('probe').dataset.iframes).toBe('false');
        expect(isCellOccluded('cell-1')).toBe(false);

        act(() => { useCollimateStore.setState({ tabDrag: { cellId: 'cell-1', leafId: 'x', tabId: 'y' } }); });
        // Pooled iframes hide so the in-cell drop highlight is visible…
        expect(getByTestId('probe').dataset.iframes).toBe('true');
        // …native guests are frozen to their screenshot (placeholder kept,
        // dimmed and click-through by the pool) — a full conceal would blank
        // the pane for the whole drag and give nothing to drop onto.
        expect(isCellOccluded('cell-1')).toBe(true);
        expect(getByTestId('probe').dataset.full).toBe('false');

        act(() => { useCollimateStore.setState({ tabDrag: null }); });
        expect(getByTestId('probe').dataset.iframes).toBe('false');
        expect(isCellOccluded('cell-1')).toBe(false);
    });

    it("a drag in another cell touches nothing here", () => {
        const { getByTestId } = render(<ContentPane {...paneProps()} />);
        act(() => { useCollimateStore.setState({ tabDrag: { cellId: 'other', leafId: 'x', tabId: 'y' } }); });
        expect(getByTestId('probe').dataset.iframes).toBe('false');
        expect(isCellOccluded('cell-1')).toBe(false);
    });
});

describe('ContentPane sash drag', () => {
    afterEach(() => { vi.restoreAllMocks(); });

    it.each(['pointerup', 'pointercancel'])('re-sizes locally and disarms on %s__F0789_F0803', (end) => {
        vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(
            { x: 0, y: 0, left: 0, top: 0, right: 400, bottom: 300, width: 400, height: 300, toJSON: () => ({}) } as DOMRect,
        );
        const base = openFileTab(createDefaultLayout({ filePaths: ['a.md'], pinnedPath: null }), 'a.md', null, undefined, { newTab: true });
        const leaf = base.root.type === 'leaf' ? base.root : null;
        expect(leaf?.tabs.length).toBe(2);
        const layout = splitWithTab(base, leaf!.id, leaf!.tabs[1].id, 'right');
        expect(layout.root.type).toBe('split');
        const onLayoutChange = vi.fn();
        const { getByRole } = render(
            <ContentPane {...paneProps()} layout={layout} filesPane={layout.filesPane} onLayoutChange={onLayoutChange} />,
        );
        const sash = getByRole('separator');
        const wrapper = sash.nextElementSibling as HTMLElement;
        const basisBefore = wrapper.style.flexBasis;
        // jsdom has no PointerEvent with clientX; a MouseEvent under the
        // pointer type name carries the fields the sash reads.
        const press = (type: string, clientX: number) =>
            fireEvent(sash, new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX }));
        press('pointerdown', 200);
        const rendersAtStart = bodyRenders.count;
        press('pointermove', 220);
        press('pointermove', 240);
        press('pointermove', 260);
        expect(bodyRenders.count).toBe(rendersAtStart);
        expect(wrapper.style.flexBasis).not.toBe(basisBefore);
        expect(onLayoutChange).not.toHaveBeenCalled();
        press(end, 260);
        expect(onLayoutChange).toHaveBeenCalledTimes(1);
        press('pointermove', 280);
        expect(onLayoutChange).toHaveBeenCalledTimes(1);
    });
});
