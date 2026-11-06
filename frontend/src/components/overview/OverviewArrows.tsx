// The lineage arrows (O07): an SVG over the overview, while the journal is
// open, from the journal's subject to each cell it was duplicated, merged or
// split with (both ways), each labelled mid-line with a button that walks to
// that cell. A cell that cannot be seen is met on the Removed bar, its label
// naming it; a cell neither on the board nor on the shelf (on a hidden
// Capabilities stack, or the shelf not loaded yet) is named there but not walked
// to. The cards are measured from the DOM (the attributes in OVERVIEW_DOM), so
// the drawing follows the camera, the shelf's height and scroll, and the
// window; the routing itself is lineageRoutes.ts.

import { useEffect, useRef, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CollimateState } from '@/stores/useCollimateStore';
import {
    JOURNAL_PANE_WIDTH, OVERVIEW_BAR_HEIGHT, cellPlace, connectedOn, relationsOf, subjectCellId,
} from '@/stores/collimate/overviewSlice';
import { cn } from '@/lib/utils';
import { commandPaneInsetOf } from '@/lib/commandPane';
import {
    OVERVIEW_DOM, boardEnd, layoutArrows, shelfEnd,
    type ArrowDrawing, type ArrowLine, type Box, type LineEnd, type LineOp, type OverviewFrame,
} from './lineageRoutes';

interface Drawing extends ArrowDrawing {
    width: number;
    height: number;
}

const NOTHING: Drawing = { width: 0, height: 0, arrows: [], labels: [] };

const LABEL = 'absolute rounded-xs bg-canvas px-[0.3125rem] text-[0.71875rem] leading-[1.125rem] whitespace-nowrap text-ink-2';
const LABEL_NAME = 'font-medium text-ink';

const attr = (name: string, value?: string) => (value === undefined ? `[${name}]` : `[${name}="${value}"]`);

/** A cell's name for a bar label: the lineage's, the shelf's, the board's. */
function nameOf(s: CollimateState, cellId: string): string {
    const known = s.overviewData.lineage?.cells.find(c => c.cell_id === cellId)?.name
        ?? s.overviewData.removed?.find(e => e.cell_id === cellId)?.name;
    if (known) return known;
    for (const stack of s.activeChannel?.main_stacks ?? []) {
        const cell = stack.cells.find(c => String(c.id) === cellId);
        if (cell) return cell.name;
    }
    return cellId;
}

/** Measure the overview under `root` and lay out the subject's arrows. */
function measureArrows(
    root: HTMLElement, s: CollimateState, measure: (text: string, name: string | null) => number,
): Drawing {
    const subject = subjectCellId(s);
    const b = root.getBoundingClientRect();
    const sized = { ...NOTHING, width: b.width, height: b.height };
    if (!s.overview.journalOpen || !subject) return sized;
    const box = (el: Element): Box => {
        const r = el.getBoundingClientRect();
        return { l: r.left - b.left, r: r.right - b.left, t: r.top - b.top, b: r.bottom - b.top };
    };
    const shelfPx = Number.parseFloat(root.style.getPropertyValue('--ov-shelf')) || 0;
    // The board keeps clear of the Commands pane (StackScene's boardStyle)
    // and the journal docks against it.
    const pane = commandPaneInsetOf(s.commandPane);
    const frame: OverviewFrame = {
        width: b.width, height: b.height, shelfPx, left: pane.left, paneWidth: JOURNAL_PANE_WIDTH + pane.right,
    };

    const bar = root.querySelector(attr(OVERVIEW_DOM.bar));
    const count = bar?.querySelector(attr(OVERVIEW_DOM.barCount));
    const nameR = count ? box(count).r : frame.left + 18;
    const controls = bar
        ? [...bar.querySelectorAll(`button, ${attr(OVERVIEW_DOM.barChevron)}`)].map(box).filter(r => r.r > r.l)
        : [];
    const controlsLeft = controls.length ? Math.min(...controls.map(r => r.l)) : b.width - frame.paneWidth;

    // The shelf's cards count only while the shelf shows a row of them.
    const gridEl = shelfPx > OVERVIEW_BAR_HEIGHT + 40 ? root.querySelector(attr(OVERVIEW_DOM.shelfGrid)) : null;
    const grid = gridEl ? box(gridEl) : null;
    const shelfCards = new Map<string, Box>();
    gridEl?.querySelectorAll(attr(OVERVIEW_DOM.shelfCard)).forEach(el => {
        shelfCards.set(el.getAttribute(OVERVIEW_DOM.shelfCard) ?? '', box(el));
    });
    const boardCards = new Map<string, Element>();
    root.querySelectorAll(attr(OVERVIEW_DOM.boardCard)).forEach(el => {
        boardCards.set(el.getAttribute(OVERVIEW_DOM.boardCard) ?? '', el);
    });

    const endOf = (cellId: string): LineEnd | null => {
        const place = cellPlace(s, cellId);
        if (place === 'shelf') {
            const card = shelfCards.get(cellId);
            if (!card) return null;
            const others = [...shelfCards].filter(([id]) => id !== cellId).map(([, r]) => r);
            return shelfEnd(card, grid, others);
        }
        if (place === 'board') {
            const el = boardCards.get(cellId);
            if (!el) return null;
            const first = el.closest(attr(OVERVIEW_DOM.column))?.querySelector(attr(OVERVIEW_DOM.boardCard)) ?? el;
            return boardEnd(box(el), box(first).t, first === el, frame);
        }
        return null;
    };

    const anchor = endOf(subject);
    if (!anchor) return sized;
    const hotOn = connectedOn(s.overview);
    const lines: ArrowLine[] = relationsOf(s.overviewData.lineage, subject).map(rel => {
        const place = cellPlace(s, rel.cellId);
        return {
            cellId: rel.cellId,
            op: rel.move.op as LineOp,
            out: rel.out,
            hot: hotOn && (place === 'shelf' ? s.overview.picked === rel.cellId : s.selectedCellId === rel.cellId),
            name: nameOf(s, rel.cellId),
            walkable: place !== null,
            end: endOf(rel.cellId),
        };
    });
    return { ...sized, ...layoutArrows({ frame, nameR, controlsLeft, anchor, lines, measure }) };
}

/** What a redraw depends on in the store (the DOM is watched separately). */
const drawKey = (s: CollimateState) => [
    s.overview.journalOpen, subjectCellId(s), s.overviewData.lineage, s.overviewData.removed, s.overview.picked,
    s.selectedCellId, connectedOn(s.overview), s.activeChannel, commandPaneInsetOf(s.commandPane),
] as const;

const sameKey = (a: readonly unknown[], b: readonly unknown[]) => a.every((v, i) => Object.is(v, b[i]));

/** The arrows layer and its labels; nothing while the journal is shut. */
export function OverviewArrows() {
    const open = useCollimateStore(s => s.overview.journalOpen);
    const walkTo = useCollimateStore(s => s.walkTo);
    const svgRef = useRef<SVGSVGElement>(null);
    const labelsRef = useRef<HTMLDivElement>(null);
    const measureRef = useRef<HTMLSpanElement>(null);
    const [drawing, setDrawing] = useState<Drawing>(NOTHING);

    useEffect(() => {
        const svg = svgRef.current;
        const labels = labelsRef.current;
        const meter = measureRef.current;
        if (!open || !svg || !labels || !meter) return undefined;
        const root = svg.closest<HTMLElement>(attr(OVERVIEW_DOM.root)) ?? svg.parentElement;
        if (!root) return undefined;

        const measure = (text: string, name: string | null) => {
            meter.replaceChildren(text);
            if (name) {
                const nm = document.createElement('span');
                nm.className = LABEL_NAME;
                nm.textContent = name;
                meter.append(' ', nm);
            }
            return meter.getBoundingClientRect().width;
        };
        let last = '';
        const draw = () => {
            const next = measureArrows(root, useCollimateStore.getState(), measure);
            const json = JSON.stringify(next);
            if (json === last) return;
            last = json;
            setDrawing(next);
        };
        let frame = 0;
        const schedule = () => {
            if (!frame) frame = requestAnimationFrame(() => { frame = 0; draw(); });
        };

        // The camera's transform, the shelf's height and the cards coming and
        // going all write the DOM under the root; this layer's own writes are
        // left out.
        const own = (node: Node) => svg.contains(node) || labels.contains(node);
        const mutations = new MutationObserver(records => {
            if (records.some(record => !own(record.target))) schedule();
        });
        mutations.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ['style', 'class', 'hidden'] });
        const resize = new ResizeObserver(schedule);
        resize.observe(root);
        root.addEventListener('scroll', schedule, true);
        // The slide out from under the Commands pane ends in CSS.
        root.addEventListener('transitionend', schedule, true);
        window.addEventListener('resize', schedule);
        let key = drawKey(useCollimateStore.getState());
        const unsubscribe = useCollimateStore.subscribe(state => {
            const next = drawKey(state);
            if (sameKey(next, key)) return;
            key = next;
            schedule();
        });
        // Labels sit over the board: a press on one is not a pan.
        const stop = (e: Event) => e.stopPropagation();
        labels.addEventListener('pointerdown', stop);
        draw();
        return () => {
            cancelAnimationFrame(frame);
            mutations.disconnect();
            resize.disconnect();
            root.removeEventListener('scroll', schedule, true);
            root.removeEventListener('transitionend', schedule, true);
            window.removeEventListener('resize', schedule);
            unsubscribe();
            labels.removeEventListener('pointerdown', stop);
        };
    }, [open]);

    if (!open) return null;
    return (
        <>
            <svg
                ref={svgRef}
                data-ov-arrows
                aria-hidden
                width={drawing.width}
                height={drawing.height}
                className="pointer-events-none absolute top-0 left-0 z-[7] overflow-visible"
            >
                {drawing.arrows.map((arrow, i) => (
                    <g key={i} data-arrow={arrow.dashed ? 'dashed' : 'solid'} data-hot={arrow.hot || undefined}>
                        <path
                            d={arrow.d}
                            fill="none"
                            style={{
                                stroke: arrow.hot ? 'var(--ink)' : 'var(--ink-2)',
                                strokeWidth: arrow.hot ? 1.75 : 1.25,
                                strokeDasharray: arrow.dashed ? '4 3' : undefined,
                            }}
                        />
                        <path d={arrow.head} style={{ fill: arrow.hot ? 'var(--ink)' : 'var(--ink-2)', stroke: 'none' }} />
                    </g>
                ))}
            </svg>
            <div ref={labelsRef} data-ov-labels data-overview-overlay className="absolute top-0 left-0 z-[8] h-0 w-0">
                <span ref={measureRef} aria-hidden className={cn(LABEL, 'invisible top-0 left-0')} />
                {drawing.labels.map((label, i) => {
                    const content = (
                        <>
                            {label.text}
                            {label.name && <>{' '}<span className={LABEL_NAME}>{label.name}</span></>}
                        </>
                    );
                    const place = { left: label.left, top: label.top };
                    // A cell neither on the board nor on the shelf is named,
                    // not walked to.
                    if (!label.walkable) {
                        return (
                            <span key={`${label.cellId}-${i}`} data-ov-lab={label.cellId} title={label.title} className={LABEL} style={place}>
                                {content}
                            </span>
                        );
                    }
                    return (
                        <button
                            key={`${label.cellId}-${i}`}
                            type="button"
                            data-ov-lab={label.cellId}
                            aria-label={label.title}
                            title={label.title}
                            className={cn(LABEL, 'cursor-pointer hover:bg-sheet-2 hover:text-ink')}
                            style={place}
                            onClick={(e) => { e.stopPropagation(); walkTo(label.cellId); }}
                        >
                            {content}
                        </button>
                    );
                })}
            </div>
        </>
    );
}
