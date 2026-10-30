// The history scrub rail: the vertical strip of run-node pips beside the
// Files pane's history sections.
//
// Always-visible bar AND pips (unlike the stack scrubber's hover-only dot):
// the rail is the mode's primary affordance. Newest maps to the top. The
// selected node reads as the brass dot; a running node's pip
// is teal; a nested node (file-version scrubbing within one node) expands its
// versions in place as small inset sub-pips — the outer rail stays visible, so overall position is never
// lost. Pip spacing is CAPPED (MAX_GAP): with few nodes the track
// terminates early instead of stretching across the whole pane, so
// scrubbing between adjacent nodes is a short mouse move, never a
// full-pane drag. Pips suppress below the density gate (they'd smear
// into a dashed line); the track + dot + an "n / N" title stay.
//
// The scrub position is local state inside useScrubTrack, but the rail
// SELECTS live: once per pip crossed (only on an index change onto an item
// that isn't already selected, never per pointermove), so the pane follows
// the drag. Release commits the final index; a plain click commits
// immediately. Each live select is a cellHistory store write, which
// re-renders the cell and remounts an open history file tab (NativeRenderer's
// mount deps include cellId/diffContent).

import React, { useEffect, useMemo, useRef, useState } from 'react';

import { HistoryNode } from '@/lib/api';
import { cn } from '@/lib/utils';
import { scrubSpacing, useScrubTrack } from '@/hooks/useScrubTrack';

const PAD = 10;
const PIP = 5;
const SUB_PIP = 4;
const DOT = 11;
const PIP_MIN_GAP = PIP + 3;
/** Cap on the distance between adjacent pips — a comfortable snap step. */
const MAX_GAP = 28;

type RailItem =
    | { type: 'node'; nodeId: string; label: string; kind: 'run' | 'files'; running: boolean }
    | { type: 'version'; nodeId: string; versionId: string; label: string };

export interface HistoryRailProps {
    /** Newest first (index 0 renders at the top). */
    nodes: HistoryNode[];
    selNodeId: string | null;
    /** Non-null = nested file-version scrubbing within the selected node. */
    selVersionId: string | null;
    nested: boolean;
    onSelect: (nodeId: string, versionId: string | null) => void;
}

export function HistoryRail({ nodes, selNodeId, selVersionId, nested, onSelect }: HistoryRailProps) {
    const items = useMemo<RailItem[]>(() => {
        const out: RailItem[] = [];
        for (const n of nodes) {
            out.push({
                type: 'node',
                nodeId: n.node_id,
                label: n.kind === 'run'
                    ? `Run · ${n.run?.runner_id ?? n.run?.generator_name ?? ''}${n.run?.status ? ` · ${n.run.status}` : ''}`
                    : (n.versions[n.versions.length - 1]?.name || 'File edits'),
                kind: n.kind,
                running: n.kind === 'run' && n.run?.status === 'running',
            });
            if (nested && n.node_id === selNodeId && n.versions.length > 1) {
                // Newest first, matching the rail's direction.
                for (let i = n.versions.length - 1; i >= 0; i--) {
                    const v = n.versions[i];
                    out.push({
                        type: 'version',
                        nodeId: n.node_id,
                        versionId: v.cell_id,
                        label: v.name || `v${i + 1}`,
                    });
                }
            }
        }
        return out;
    }, [nodes, nested, selNodeId]);

    const selIndex = useMemo(() => {
        if (nested && selVersionId) {
            const i = items.findIndex(it => it.type === 'version' && it.versionId === selVersionId);
            if (i >= 0) return i;
        }
        const i = items.findIndex(it => it.type === 'node' && it.nodeId === selNodeId);
        return i >= 0 ? i : 0;
    }, [items, selNodeId, selVersionId, nested]);

    // Freeze the item list for the duration of a gesture: a live run's
    // refetch can grow `nodes` at the head mid-drag, and both the snap math
    // and the release commit must resolve against the list the user is
    // actually scrubbing, not the shifted one.
    const frozenItemsRef = useRef(items);
    const scrubDraggingRef = useRef(false);
    if (!scrubDraggingRef.current) frozenItemsRef.current = items;
    const scrub = useScrubTrack({
        axis: 'y',
        count: frozenItemsRef.current.length,
        pad: PAD,
        maxGap: MAX_GAP,
        // Unfreeze on EVERY gesture end, not just a commit: a cancelled
        // scrub (pointercancel, lost capture) never reaches onCommit.
        onEnd: () => { scrubDraggingRef.current = false; },
        onCommit: (index) => {
            const it = frozenItemsRef.current[index];
            if (!it) return;
            if (it.type === 'node') onSelect(it.nodeId, null);
            else onSelect(it.nodeId, it.versionId);
        },
    });
    if (scrub.dragging) scrubDraggingRef.current = true;
    const railItems = scrub.dragging ? frozenItemsRef.current : items;

    // LIVE commit while dragging: the moment the drag snaps onto a different
    // node/version, select it — the pane follows the scrub instead of waiting
    // for release. Fires only on an index CHANGE onto an item that isn't
    // already selected, so the per-snap store write stays rare (once per pip
    // crossed, not per pointermove).
    const liveIndexRef = useRef<number | null>(null);
    useEffect(() => {
        if (!scrub.dragging || scrub.dragIndex === null) {
            liveIndexRef.current = null;
            return;
        }
        if (liveIndexRef.current === scrub.dragIndex) return;
        liveIndexRef.current = scrub.dragIndex;
        const it = frozenItemsRef.current[scrub.dragIndex];
        if (!it) return;
        const already = it.type === 'node'
            ? (it.nodeId === selNodeId && !selVersionId)
            : (it.nodeId === selNodeId && it.versionId === selVersionId);
        if (already) return;
        if (it.type === 'node') onSelect(it.nodeId, null);
        else onSelect(it.nodeId, it.versionId);
        // selNodeId/selVersionId deliberately absent from the deps: they
        // update as a RESULT of this effect, and re-running on them would
        // re-fire for the index we just committed.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [scrub.dragging, scrub.dragIndex, onSelect]);

    // Density gate — measure the rail's height.
    const [extent, setExtent] = useState(0);
    const rootRef = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
        const el = rootRef.current;
        if (!el) return;
        const measure = () => setExtent(el.getBoundingClientRect().height);
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);
    // Capped spacing: with few items the track ends early (short scrub
    // travel); with many it degrades toward the density gate as before.
    // Pre-measure (extent 0) falls back to MAX_GAP so the first paint
    // doesn't stack every pip at the origin.
    const spacing = extent > 0
        ? scrubSpacing(extent, railItems.length, PAD, MAX_GAP)
        : (railItems.length > 1 ? MAX_GAP : 0);
    const trackLen = spacing * (railItems.length - 1);
    const showPips = railItems.length > 1 && spacing >= PIP_MIN_GAP;

    const displayIndex = scrub.dragIndex ?? selIndex;

    const posStyle = (i: number, size: number): React.CSSProperties => {
        const along = PAD + i * spacing - size / 2;
        return { top: along, left: '50%', marginLeft: -size / 2 };
    };

    const current = railItems[displayIndex];

    return (
        <div
            ref={rootRef}
            role="slider"
            aria-orientation="vertical"
            aria-valuemin={0}
            aria-valuemax={Math.max(0, railItems.length - 1)}
            aria-valuenow={displayIndex}
            aria-label="Version history"
            title={current
                ? `${displayIndex + 1} / ${railItems.length} — ${current.label}`
                : 'Version history'}
            className="flex-shrink-0 relative w-6 h-full border-r border-line bg-sheet select-none"
            style={{ touchAction: 'none', cursor: 'ns-resize' }}
            {...scrub.handlers}
        >
            {/* Track line */}
            <div
                ref={scrub.trackRef}
                className="absolute inset-0"
            >
                <div
                    className="absolute bg-line-2"
                    style={{ left: '50%', top: PAD, height: Math.max(0, trackLen), width: 1, marginLeft: -0.5 }}
                />
                {showPips && railItems.map((it, i) => (
                    i === displayIndex ? null : (
                        <div
                            key={it.type === 'node' ? it.nodeId : `${it.nodeId}:${it.versionId}`}
                            className={cn(
                                'absolute rounded-full pointer-events-none',
                                it.type === 'node' && it.running
                                    ? 'bg-teal animate-pulse'
                                    : it.type === 'version' || it.kind === 'run'
                                        ? 'bg-ink-2'
                                        : 'bg-ink-3',
                            )}
                            style={{
                                width: it.type === 'version' ? SUB_PIP : PIP,
                                height: it.type === 'version' ? SUB_PIP : PIP,
                                ...posStyle(i, it.type === 'version' ? SUB_PIP : PIP),
                                // Sub-pips inset toward the content side.
                                ...(it.type === 'version' ? { left: '65%' } : {}),
                            }}
                        />
                    )
                ))}
                {/* Selection dot */}
                {railItems.length > 0 && (
                    <div
                        className={cn(
                            'absolute rounded-full pointer-events-none bg-brass shadow-(--shadow)',
                            scrub.dragging && 'ring-2 ring-brass-soft',
                        )}
                        style={{
                            width: DOT,
                            height: DOT,
                            transition: scrub.dragging ? undefined : 'top 140ms ease-out',
                            ...posStyle(displayIndex, DOT),
                        }}
                    />
                )}
            </div>
        </div>
    );
}
