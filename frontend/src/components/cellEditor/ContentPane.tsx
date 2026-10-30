// File operations use pinned bodies and conditional durable receipts.

import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cn } from '@/lib/utils';
import { useGuestOccluder } from '@/lib/guestOcclusion';

import { PooledIframeConcealContext, usePooledIframeConcealed } from '@/contexts/RendererConcealContext';
import {
    MIN_LEAF_SHARE, allLeaves, allTabs, moveTab, resolvePinnedPath, setActiveLeaf, setSplitSizes, splitWithTab,
    type SplitEdge,
} from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Cell } from '@/lib/types';
import type { UpdateFiles } from '@/lib/fileTransfer';
import type {
    CellLayout, CellTab, ContentLeaf, ContentNode, ContentSplit, FilesPaneState,
} from '@/stores/collimate/types';
import type { FullFileEditorHandle } from '@/components/FullFileEditor';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { GUEST_FOCUS_EVENT } from '@/components/guestMount';
import { isFocusWheel } from '@/lib/wheelReplay';
import { TabStrip } from './TabStrip';
import { TabBody } from './TabBody';
import { useCellValidation } from '@/hooks/useCellValidation';
import type { CellValidation } from '@/lib/definitionApi';
import { ACTIVE_TAB_BODY_ATTR } from '@/lib/selectionFocus';

/** Where a dropped tab lands inside a leaf body: the middle takes the tab
 *  into the leaf, an edge quarter splits the leaf that way. */
type DropZone = 'center' | SplitEdge;

export interface ContentPaneProps {
    cell: Cell;
    channel?: string;
    /** The cell sits on the Capabilities stack (TabStrip shows no status
     *  caret). */
    capabilitiesCell?: boolean;
    layout: CellLayout;
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void;
    isActive: boolean;
    visible: boolean;
    readOnly: boolean;
    diffBaseFiles?: Record<string, string>;
    /** Before/after labels for diffBaseFiles (the base and current cell names). */
    diffLabels?: { base: string; current: string };
    registerEditorHandle: (tabId: string, handle: FullFileEditorHandle | null) => void;
    /** File operations use pinned bodies and conditional durable receipts. */
    onBeforeStructuralChange: () => void | Promise<void>;
    dirtyByName: Record<string, boolean>;
    lastSavedByName: Record<string, Date>;
    onFlushNow?: () => void | Promise<void>;
    onToggleMaximized: () => void;
    /** True while the whole content region is expanded to the fullscreen
     *  overlay (TabStrip shows the collapse button; TabBody swaps pooled iframes
     *  for standalone ones). */
    maximized?: boolean;
    onCloseFileTab: (path: string) => void;
    /** Id-keyed close for non-file tabs (history tabs must never route
     *  through the path-keyed close). */
    onCloseTabById: (tabId: string) => void;
    /** History-mode view data for history-tab bodies (see TabBody). */
    historyView?: import('./TabBody').TabBodyProps['historyView'];
    onTogglePin?: (fileName: string) => void;
    onUpdateFiles: UpdateFiles;
    /** The Files pane's state: its dock side names the tab strip's Show
     *  files icon. */
    filesPane: FilesPaneState;
    isZoomTransitioning: boolean;
    /** Present while the Files pane is hidden: the first leaf's tab strip
     *  leads with the pane's toggle. */
    onShowFiles?: () => void;
}

/** Everything the recursive node renderers need. Rebuilt each render — no
 *  memoisation, the tree is tiny and correctness beats a skipped re-render. */
interface PaneCtx extends ContentPaneProps {
    validation: CellValidation | null;
    pinnedPath: string | null;
    /** Tab ids that have been the active tab at least once (lazy mount). */
    seen: React.RefObject<Set<string>>;
    /** A tab of THIS cell, or one of its panes' dock icons, is being dragged
     *  right now. */
    dragActive: boolean;
    /** The top-left leaf: its strip carries the hidden Files pane's toggle. */
    firstLeafId: string;
}

export const ContentPane: React.FC<ContentPaneProps> = (props) => {
    const { cell, layout } = props;
    const validation = useCellValidation(props.channel, cell.id, cell.files, props.visible, props.lastSavedByName);
    const tabDrag = useCollimateStore(s => s.tabDrag);
    // A dock drag of one of the cell's panes draws its band over this
    // cell's panes (DockPreview), so it hides what a tab drag hides.
    const dockDrag = useCollimateStore(s => s.dockDrag?.cellId === cell.id);
    const dragActive = tabDrag?.cellId === cell.id || dockDrag;

    // R17: a drop-zone highlight is plain in-cell DOM and cannot paint above a
    // native guest view; freeze this cell's guests for the duration. The pool
    // freezes an occluded guest WITH a capture and leaves its placeholder
    // painted, dimmed and click-through — the page stays visible under the
    // highlight and the leaf beneath receives the dragover/drop.
    useGuestOccluder({ cellIds: [cell.id] }, dragActive);
    // R17: …and conceal this cell's pooled IFRAMES (which live outside this
    // DOM entirely) so the highlight is actually visible. Not the full
    // RendererConcealContext: that would also conceal the guests, which hides
    // their placeholder without a capture and leaves a blank pane for the
    // whole drag. Inherit any iframe conceal already in force above.
    const parentIframeConceal = usePooledIframeConcealed();

    const seen = useRef<Set<string>>(new Set());
    useEffect(() => {
        // Drop hasBeenActive entries for tabs that no longer exist, so a
        // recycled tab id can't resurrect a stale mount.
        const live = new Set(allTabs(layout).map(t => t.id));
        for (const id of Array.from(seen.current)) {
            if (!live.has(id)) seen.current.delete(id);
        }
    }, [layout]);

    const pinnedPath = resolvePinnedPath(cell.pinned, cell.files);

    const ctx: PaneCtx = {
        ...props,
        validation,
        pinnedPath,
        seen,
        dragActive,
        firstLeafId: allLeaves(layout.root)[0].id,
    };

    return (
        <PooledIframeConcealContext.Provider value={parentIframeConceal || dragActive}>
            <div className="flex-1 min-w-0 min-h-0 flex flex-col">
                <LayoutNode node={layout.root} ctx={ctx} />
            </div>
        </PooledIframeConcealContext.Provider>
    );
};

// Memoized so a sash drag (SplitNode's local state; `node` and `ctx` keep
// their identity) re-renders only the flex-basis wrappers, not every tab body
// below. Outside a drag `ctx` is a new object each render, so nothing is skipped.
const LayoutNode = React.memo(function LayoutNode({ node, ctx }: { node: ContentNode; ctx: PaneCtx }) {
    return node.type === 'leaf'
        ? <LeafNode leaf={node} ctx={ctx} />
        : <SplitNode node={node} ctx={ctx} />;
});

// ─── Split node ─────────────────────────────────────────────────────────────

const SplitNode: React.FC<{ node: ContentSplit; ctx: PaneCtx }> = ({ node, ctx }) => {
    const isRow = node.dir === 'row';
    const containerRef = useRef<HTMLDivElement>(null);
    // Sash drags track locally at 1:1 and commit ONCE on release (the
    // StackWidthControl precedent) — a store write per pointermove
    // would re-render every renderer in the cell for the whole gesture. The
    // children are memoized LayoutNodes, so during a drag only the flex-basis
    // wrappers below re-render.
    const [dragSizes, setDragSizes] = useState<number[] | null>(null);
    const dragSizesRef = useRef<number[] | null>(null);
    const dragRef = useRef<{ index: number; start: number; total: number; base: number[] } | null>(null);

    const sizes = dragSizes ?? node.sizes;

    const onSashDown = (index: number) => (e: React.PointerEvent) => {
        const rect = containerRef.current?.getBoundingClientRect();
        const total = (isRow ? rect?.width : rect?.height) ?? 0;
        if (!total) return;
        dragRef.current = { index, start: isRow ? e.clientX : e.clientY, total, base: node.sizes };
        // jsdom implements neither capture call — optional-chained, not guarded.
        e.currentTarget.setPointerCapture?.(e.pointerId);
    };

    const onSashMove = (e: React.PointerEvent) => {
        const d = dragRef.current;
        if (!d) return;
        const delta = ((isRow ? e.clientX : e.clientY) - d.start) / d.total;
        const a = d.base[d.index];
        const b = d.base[d.index + 1];
        const pair = a + b;
        // Both neighbours keep at least MIN_LEAF_SHARE; a pair too small to
        // honour that just doesn't move.
        if (pair < MIN_LEAF_SHARE * 2) return;
        const next = [...d.base];
        next[d.index] = Math.max(MIN_LEAF_SHARE, Math.min(pair - MIN_LEAF_SHARE, a + delta));
        next[d.index + 1] = pair - next[d.index];
        dragSizesRef.current = next;
        setDragSizes(next);
    };

    const onSashUp = (e: React.PointerEvent) => {
        if (!dragRef.current) return;
        dragRef.current = null;
        e.currentTarget.releasePointerCapture?.(e.pointerId);
        const committed = dragSizesRef.current;
        dragSizesRef.current = null;
        setDragSizes(null);
        if (committed) ctx.onLayoutChange(l => setSplitSizes(l, node.id, committed));
    };

    return (
        <div
            ref={containerRef}
            className={cn('flex-1 min-w-0 min-h-0 flex', isRow ? 'flex-row' : 'flex-col')}
        >
            {node.children.map((child, i) => (
                <React.Fragment key={child.id}>
                    {i > 0 && (
                        <div
                            role="separator"
                            aria-orientation={isRow ? 'vertical' : 'horizontal'}
                            onPointerDown={onSashDown(i - 1)}
                            onPointerMove={onSashMove}
                            onPointerUp={onSashUp}
                            onPointerCancel={onSashUp}
                            className={cn(
                                'flex-none bg-line hover:bg-ink-3/60 transition-colors',
                                isRow ? 'w-[4px] cursor-col-resize' : 'h-[4px] cursor-row-resize',
                            )}
                            style={{ touchAction: 'none' }}
                            title="Drag to resize"
                        />
                    )}
                    <div
                        className="flex min-w-0 min-h-0 flex-col"
                        style={{
                            flexBasis: `${(sizes[i] ?? 1 / node.children.length) * 100}%`, flexGrow: 0, flexShrink: 1,
                        }}
                    >
                        <LayoutNode node={child} ctx={ctx} />
                    </div>
                </React.Fragment>
            ))}
        </div>
    );
};

// ─── Leaf node ──────────────────────────────────────────────────────────────

/** Five-zone hit test: the middle half is "into this leaf", each edge quarter
 *  is "split that way". */
function zoneAt(e: React.DragEvent, rect: DOMRect): DropZone {
    const x = (e.clientX - rect.left) / (rect.width || 1);
    const y = (e.clientY - rect.top) / (rect.height || 1);
    const edges: [DropZone, number][] = [['left', x], ['right', 1 - x], ['top', y], ['bottom', 1 - y]];
    const [zone, dist] = edges.reduce((best, cur) => (cur[1] < best[1] ? cur : best));
    return dist < 0.25 ? zone : 'center';
}

const ZONE_STYLE: Record<DropZone, React.CSSProperties> = {
    center: { inset: 0 },
    left: { left: 0, top: 0, bottom: 0, width: '50%' },
    right: { right: 0, top: 0, bottom: 0, width: '50%' },
    top: { left: 0, right: 0, top: 0, height: '50%' },
    bottom: { left: 0, right: 0, bottom: 0, height: '50%' },
};

const LeafNode: React.FC<{ leaf: ContentLeaf; ctx: PaneCtx }> = ({ leaf, ctx }) => {
    const [zone, setZone] = useState<DropZone | null>(null);
    const bodyRef = useRef<HTMLDivElement>(null);
    const setTabDrag = useCollimateStore(s => s.setTabDrag);

    const isActiveLeaf = ctx.layout.activeLeafId === leaf.id;

    // R9 for surfaces the DOM can't see being used: a native guest view
    // taking focus (its mount fires GUEST_FOCUS_EVENT on its host, inside
    // this body) and an iframe taking focus (the window blurs with the
    // iframe as the active element; a pooled iframe sits in the world-space
    // overlay, so it is mapped back to the container it covers). Either
    // makes this the active leaf, like a pointerdown in it would.
    const { containerOf } = useGuestPool();
    const { onLayoutChange } = ctx;
    useEffect(() => {
        const body = bodyRef.current;
        if (!body) return;
        const activate = () => onLayoutChange(l => (l.activeLeafId === leaf.id ? l : setActiveLeaf(l, leaf.id)));
        const onWindowBlur = () => {
            const el = document.activeElement;
            if (!el || el.tagName !== 'IFRAME') return;
            const host = body.contains(el) ? el : containerOf(el);
            if (host && body.contains(host)) activate();
        };
        body.addEventListener(GUEST_FOCUS_EVENT, activate);
        window.addEventListener('blur', onWindowBlur);
        return () => {
            body.removeEventListener(GUEST_FOCUS_EVENT, activate);
            window.removeEventListener('blur', onWindowBlur);
        };
    }, [leaf.id, onLayoutChange, containerOf]);

    const onDragOver = (e: React.DragEvent) => {
        const drag = useCollimateStore.getState().tabDrag;
        if (!drag || drag.cellId !== ctx.cell.id) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const rect = bodyRef.current?.getBoundingClientRect();
        if (rect) setZone(zoneAt(e, rect));
    };

    const onDrop = async (e: React.DragEvent) => {
        const drag = useCollimateStore.getState().tabDrag;
        // Recompute the zone from the DROP's own coordinates — the `zone`
        // state is only the highlight. A drop can be delivered before React
        // commits the last dragover's setZone (dragover renders at continuous
        // priority via a scheduler task), and reading the state would then
        // no-op the drop or land it in the previous pointer position's zone.
        setZone(null);
        // Tab drags are cell-scoped — a tab from another cell is not ours.
        if (!drag || drag.cellId !== ctx.cell.id) return;
        const rect = bodyRef.current?.getBoundingClientRect();
        if (!rect) return;
        const target = zoneAt(e, rect);
        e.preventDefault();
        try {await ctx.onBeforeStructuralChange();} catch {return;}
        if (target === 'center') {
            // Center drop appends to the leaf's tab list (its length is read
            // from the layout the op is applied to, not this render's copy).
            ctx.onLayoutChange(l => moveTab(
                l, drag.tabId, leaf.id,
                allLeaves(l.root).find(x => x.id === leaf.id)?.tabs.length ?? 0,
            ));
        } else {
            ctx.onLayoutChange(l => splitWithTab(l, leaf.id, drag.tabId, target));
        }
        setTabDrag(null);
    };

    return (
        <div
            className="flex-1 min-w-0 min-h-0 flex flex-col"
            // R9: pointer-down anywhere in a leaf makes it the focused leaf,
            // and so does a focusing wheel (lib/wheelReplay): the wheel that
            // focuses the cell focuses the leaf under the pointer, so the
            // content there is what takes it.
            onPointerDown={() => { if (!isActiveLeaf) ctx.onLayoutChange(l => setActiveLeaf(l, leaf.id)); }}
            onWheel={(e) => { if (!isActiveLeaf && isFocusWheel(e)) ctx.onLayoutChange(l => setActiveLeaf(l, leaf.id)); }}
        >
            <TabStrip
                cellId={ctx.cell.id}
                pinnedPath={ctx.pinnedPath}
                pinnedTabCompact={ctx.layout.pinnedTabCompact ?? false}
                leaf={leaf}
                onLayoutChange={ctx.onLayoutChange}
                onBeforeStructuralChange={ctx.onBeforeStructuralChange}
                onToggleMaximized={ctx.onToggleMaximized}
                maximized={ctx.maximized}
                onCloseFileTab={ctx.onCloseFileTab}
                onTogglePin={ctx.onTogglePin}
                capabilitiesCell={ctx.capabilitiesCell}
                showFiles={leaf.id === ctx.firstLeafId && ctx.onShowFiles
                    ? { side: ctx.filesPane.side, onShow: ctx.onShowFiles } : undefined}
            />
            <div
                ref={bodyRef}
                className="flex-1 relative min-h-0"
                onDragOver={onDragOver}
                onDragLeave={(e) => {
                    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
                    setZone(null);
                }}
                onDrop={onDrop}
            >
                {leaf.tabs.map(tab => (
                    <TabPane key={tab.id} tab={tab} leaf={leaf} ctx={ctx} />
                ))}
                {/* z-[60], not z-20: FullFileEditor self-positions at z-50
                    inside the same stacking context, and the highlight must
                    paint above the file it would split. */}
                {ctx.dragActive && zone && (
                    <div
                        className="absolute z-[60] pointer-events-none bg-teal-soft border border-teal rounded-md"
                        style={ZONE_STYLE[zone]}
                        aria-hidden
                    />
                )}
            </div>
        </div>
    );
};

// ─── One tab's slot in a leaf body ──────────────────────────────────────────

const TabPane: React.FC<{ tab: CellTab; leaf: ContentLeaf; ctx: PaneCtx }> = ({ tab, leaf, ctx }) => {
    // Stable event wrappers retain the current context even when a sibling
    // file update is skipped by the memoised tab body.
    const latest = useRef(ctx);
    useLayoutEffect(() => { latest.current = ctx; });
    const actions = useMemo(() => ({
        closeFile: (path: string) => latest.current.onCloseFileTab(path),
        closeTab: (id: string) => latest.current.onCloseTabById(id),
        register: (id: string, handle: FullFileEditorHandle | null) => latest.current.registerEditorHandle(id, handle),
        flush: () => { void latest.current.onFlushNow?.(); },
    }), []);

    const active = tab.id === leaf.activeTabId;
    // Lazy first mount, then permanent. Recording during render (rather than
    // in an effect) is safe because only the ACTIVE tab is ever recorded, and
    // the active tab renders unconditionally.
    if (active) ctx.seen.current.add(tab.id);
    if (!active && !ctx.seen.current.has(tab.id)) return null;

    const isFocusedLeaf = ctx.layout.activeLeafId === leaf.id;
    // R9 (renderer interactivity) and R11 (who answers Esc/Cmd+S) resolve to
    // the same predicate: the focused tab of the focused leaf of the active
    // cell.
    const focused = ctx.isActive && isFocusedLeaf && active;

    return (
        <div
            className="absolute inset-0"
            // NEVER visibility/opacity — see the file header.
            style={{ display: active ? undefined : 'none' }}
            // The slot whose editor takes the keyboard when the selection
            // hands it to this cell (lib/selectionFocus).
            {...(isFocusedLeaf && active ? { [ACTIVE_TAB_BODY_ATTR]: '' } : {})}
        >
            <TabBody
                tab={tab}
                validationVerdict={tab.kind === 'file' && !ctx.dirtyByName[tab.path] && !ctx.validation?.pending
                    ? ctx.validation?.verdicts[tab.path] : undefined}
                cell={ctx.cell}
                channel={ctx.channel}
                isActiveFocus={focused}
                visible={ctx.visible && active}
                readOnly={ctx.readOnly}
                editorShortcutsEnabled={focused}
                fullscreen={!!ctx.maximized}
                diffBaseFiles={ctx.diffBaseFiles}
                diffLabels={ctx.diffLabels}
                registerEditorHandle={actions.register}
                onCloseFileTab={actions.closeFile}
                onCloseTabById={actions.closeTab}
                historyView={ctx.historyView}
                onUpdateFiles={ctx.onUpdateFiles}
                isZoomTransitioning={ctx.isZoomTransitioning}
                onAfterSave={ctx.onFlushNow ? actions.flush : undefined}
            />
        </div>
    );
};
