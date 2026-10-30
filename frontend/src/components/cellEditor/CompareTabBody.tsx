import {readPinnedText,descriptorOf} from '@/lib/fileBodies';
// The body of a "Compare with…" tab (CellTab 'compare'): the right-clicked
// file (`current`, always this cell's own) diffed against the picked target
// (`base`) through the SAME renderer path the history views use —
// FullFileEditor with an explicit diffBaseContent (NativeRenderer isDiffMode /
// SandboxIframe diffHash under the hood). When the file's renderer lacks
// supports_diff, a dependency-free line diff (lib/textDiff) stands in for the
// editor's "filetype doesn't support diff" dead end: side by side in a wide
// pane, stacked current side first in a narrow one (a cell-width tab).
//
// The two sides age differently. The CURRENT side is the cell's live file,
// mounted under the cell's own id (tab.current.cellId), so its relative
// asset URLs resolve against the cell, and it follows every write to that
// file (a save from the file's own tab, a run's write, another writer): a
// native diff view takes the store's new body in place (the SDK's diff mount
// follows its host's content push), an iframe diff view is rebuilt from it
// (FullFileEditor), and the plain diff re-diffs the live text. The editor is
// read-only and holds no typing of its own, so sharing the (cellId, file)
// key with the file's live tab is harmless: that tab's renderer events and
// flushes reach this editor too, and its read-only save path writes nothing.
// The BASE side is the snapshot read when the tab opened (the next Compare
// with… reads it afresh); once the base cell leaves the channel or the base
// file is gone, the body says so instead of diffing against something no
// longer there.
//
// Esc closes the tab: FullFileEditor's EDITOR_CLOSE while the editor shows,
// this body's own otherwise (loading, notice, plain diff). CellEditor claims
// mainFocusMode 'file-editor' for a focused compare tab as for a file tab, so
// the scene's Esc ladder stops there instead of moving focus to the nav.

import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useShortcut } from '@/lib/keybindings/useShortcut';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { cn } from '@/lib/utils';
import type { VirtualFile } from '@/lib/types';
import type { CellTab } from '@/stores/collimate/types';
import { compareSideTitle, type CompareSide } from '@/lib/compare';
import { currentFirst, diffLines, diffStats, type DiffRow } from '@/lib/textDiff';
import { useRendererId, refreshRendererCache } from '@/hooks/useRendererId';
import { FullFileEditor } from '@/components/FullFileEditor';
import { TabNotice } from './TabNotice';

type CompareTab = Extract<CellTab, { kind: 'compare' }>;

/** File operations use pinned bodies and conditional durable receipts. */
async function loadSideContent(side: CompareSide): Promise<string> {
    const store = useCollimateStore.getState();
    const loc = store.locateCell(side.cellId);
    const f = loc?.cell.files?.find(x => x.name === side.fileName);
    if(!f)throw new Error('Compared file is unavailable.');
    return readPinnedText(side.channel,side.cellId,descriptorOf(f));
}

/** Why the base side is no longer there to compare against: its cell left
 *  the channel, or the file is gone from it. Null while it stands. */
function useBaseGone(base: CompareSide): 'cell' | 'file' | null {
    return useCollimateStore(s => {
        const loc = s.locateCell(base.cellId);
        if (!loc) return 'cell';
        return loc.cell.files?.some(f => f.name === base.fileName) ? null : 'file';
    });
}

/** File operations use pinned bodies and conditional durable receipts. */
function useLiveText(file: VirtualFile, side: CompareSide, enabled: boolean): { text: string | null; error: string | null } {
    const [fetched,setFetched]=useState<{hash:string|null;revision:number;text:string}|null>(null);
    const [error,setError]=useState<string|null>(null);
    useEffect(()=>{
        if(!enabled)return;let active=true;setError(null);
        void readPinnedText(side.channel,side.cellId,descriptorOf(file)).then(text=>{if(active)setFetched({hash:file.body_hash,revision:file.file_revision,text});},e=>{if(active)setError(String(e?.message??e));});
        return()=>{active=false;};
    },[enabled,side.channel,side.cellId,file.name,file.body_hash,file.file_revision]);
    return {text:fetched?.hash===file.body_hash && fetched.revision===file.file_revision?fetched.text:null,error};
}


export interface CompareTabBodyProps {
    tab: CompareTab;
    /** The current side: this cell's live file named by tab.current. */
    file: VirtualFile;
    /** R9: the focused tab of the focused leaf of the active cell. */
    isActiveFocus: boolean;
    /** R10: the renderer is actually painting. */
    visible: boolean;
    /** R13: mounted in CellEditor's maximized overlay. */
    fullscreen: boolean;
    /** R11: only the focused tab of the focused cell answers Esc. */
    editorShortcutsEnabled: boolean;
    onCloseTabById: (tabId: string) => void;
}

function CompareTabBodyContent({
    tab, file, isActiveFocus, visible, fullscreen, editorShortcutsEnabled, onCloseTabById,
}: CompareTabBodyProps) {
    const { base, current } = tab;
    const close = useCallback(() => onCloseTabById(tab.id), [onCloseTabById, tab.id]);
    const gone = useBaseGone(base);

    const [baseText, setBaseText] = useState<string | null>(null);
    const [baseError, setBaseError] = useState<string | null>(null);
    useEffect(() => {
        if (gone) return;
        let cancelled = false;
        loadSideContent(base)
            .then((text) => { if (!cancelled) { setBaseText(text); setBaseError(null); } })
            .catch((e) => { if (!cancelled) setBaseError(String(e?.message ?? e)); });
        return () => { cancelled = true; };
    }, [base, gone]);

    // The compared pair shares a filetype in the same-name case; in the
    // other-file case the RIGHT (current) side's renderer drives dispatch.
    const {renderer,error:rendererError} = useRendererId(current.fileName);
    const rendererCanDiff = !!renderer && renderer.supportsDiff && renderer.id !== 'image' && renderer.id !== 'binary';
    const live = useLiveText(file, current, !rendererCanDiff);

    const diffLabels = useMemo(() => ({
        base: compareSideTitle(base),
        current: compareSideTitle(current),
    }), [base, current]);

    const error = rendererError ?? baseError ?? (rendererCanDiff ? null : live.error);
    const editorShown = !gone && !error && baseText !== null && rendererCanDiff;
    useShortcut(SHORTCUT_IDS.EDITOR_CLOSE, (e) => {
        e.preventDefault();
        close();
    }, { target: 'window', skipWhenTyping: false, enabled: editorShortcutsEnabled && !editorShown });

    if (gone) {
        return (
            <TabNotice
                title={gone === 'cell' ? `${base.label} left the channel` : `${base.fileName} is gone from ${base.label}`}
                sub="Nothing to compare against: close this tab, or compare with another file"
            />
        );
    }
    if (error) {
        return (
            <div className="h-full flex items-center justify-center text-[0.8125rem] text-red font-mono px-6 text-center">
                Failed to load files for compare: {error}
                {rendererError&&<button onClick={()=>void refreshRendererCache()}>Retry renderer</button>}
            </div>
        );
    }
    if (!renderer || baseText === null || (!rendererCanDiff && live.text === null)) {
        return (
            <div className="h-full flex items-center justify-center">
                <div className="flex flex-col items-center gap-3">
                    <div className="w-8 h-8 border-2 border-teal border-t-transparent rounded-full animate-spin" />
                    <span className="text-[0.8125rem] text-ink-3 font-mono">Loading files…</span>
                </div>
            </div>
        );
    }
    if (rendererCanDiff) {
        return (
            <FullFileEditor
                file={file}
                onClose={close}
                readOnly
                isActive={isActiveFocus}
                cellId={current.cellId}
                channel={current.channel}
                diffBaseContent={baseText}
                diffLabels={diffLabels}
                fullscreen={fullscreen}
                editorShortcutsEnabled={editorShortcutsEnabled}
                visible={visible}
            />
        );
    }
    return <FallbackDiff base={baseText} current={live.text ?? ''} labels={diffLabels} />;
}

/** Re-renders only when its own props move: a write to another file of the
 *  cell leaves the compare alone. */
export const CompareTabBody = React.memo(CompareTabBodyContent);

/** Row cap for the fallback view — beyond this the diff is almost certainly
 *  machine output and the DOM cost isn't worth it. */
const MAX_FALLBACK_ROWS = 5000;

/** Narrower than this, the plain diff stacks its two sides instead of
 *  setting them side by side: below 360px a half, a line of code stops
 *  reading. */
const SIDE_BY_SIDE_MIN_WIDTH = 720;

/** Whether the element is narrower than `px`: measured before the first
 *  paint, then kept current by a ResizeObserver. A zero width (not laid
 *  out, as in a hidden tab) leaves the last answer standing; unmeasured,
 *  the answer is "narrow", the layout that shows the current side at any
 *  width. */
function useNarrowerThan(ref: React.RefObject<HTMLElement | null>, px: number): boolean {
    const [narrow, setNarrow] = useState(true);
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) return;
        const measure = () => {
            const width = el.clientWidth;
            if (width > 0) setNarrow(width < px);
        };
        measure();
        if (typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, [ref, px]);
    return narrow;
}

type SideLabels = { base: string; current: string };

/** Monospace line diff for filetypes whose renderer lacks supports_diff,
 *  laid out by the pane's width. Wide: side by side, base (before) on the
 *  left and current on the right, two equal grid columns whose long lines
 *  wrap, as the built-in diff view's do, so every row's halves stay level.
 *  Narrow (a cell-width tab): the halves stack into one full-width column,
 *  the current side first: unchanged lines once, and in each changed run
 *  the current side's lines (+) ahead of the base side's (−), so the
 *  user's own file is what shows without a sideways scroll. */
function FallbackDiff({ base, current, labels }: { base: string; current: string; labels: SideLabels }) {
    const rootRef = useRef<HTMLDivElement>(null);
    const stacked = useNarrowerThan(rootRef, SIDE_BY_SIDE_MIN_WIDTH);
    const rows = useMemo(() => diffLines(base, current), [base, current]);
    const stats = useMemo(() => diffStats(rows), [rows]);
    const ordered = useMemo(() => (stacked ? currentFirst(rows) : rows), [stacked, rows]);
    const shown = ordered.length > MAX_FALLBACK_ROWS ? ordered.slice(0, MAX_FALLBACK_ROWS) : ordered;
    const hidden = ordered.length - shown.length;

    return (
        <div ref={rootRef} className="absolute inset-0 flex flex-col bg-code-bg">
            <div className="flex-none flex items-center gap-3 px-4 py-1.5 border-b border-line bg-sheet text-[0.6875rem] font-mono">
                <span className="text-ink-3">Plain text diff (renderer has no diff view)</span>
                <span className="ml-auto text-green">+{stats.added}</span>
                <span className="text-red">−{stats.removed}</span>
            </div>
            <div className="flex-1 overflow-auto font-mono text-[0.6875rem] leading-5">
                {stacked
                    ? <StackedRows rows={shown} labels={labels} hidden={hidden} />
                    : <SideBySideRows rows={shown} labels={labels} hidden={hidden} />}
            </div>
        </div>
    );
}

interface RowsProps {
    rows: DiffRow[];
    labels: SideLabels;
    /** Rows past MAX_FALLBACK_ROWS, not drawn. */
    hidden: number;
}

const columnHead = 'sticky top-0 z-1 min-w-0 truncate py-0.5 border-b border-line bg-sheet text-ink-3';
const lineText = 'whitespace-pre-wrap break-words m-0 flex-1 min-w-0 pr-4';
const lineNo = 'w-10 shrink-0 text-right pr-2 text-ink-3 select-none';

function HiddenRows({ hidden, className }: { hidden: number; className?: string }) {
    if (hidden <= 0) return null;
    return <div className={cn('px-4 py-2 text-ink-3 italic', className)}>… {hidden} more lines not shown</div>;
}

/** Wide layout: one two-column grid, each row's base half beside its
 *  current half, under the two sides' names. */
function SideBySideRows({ rows, labels, hidden }: RowsProps) {
    return (
        <div data-fallback-diff="side-by-side" className="grid grid-cols-2">
            <div className={cn(columnHead, 'pl-10 pr-4 border-r')} title={labels.base}>{labels.base}</div>
            <div className={cn(columnHead, 'pl-10 pr-4')} title={labels.current}>{labels.current}</div>
            {rows.map((r, i) => (
                <React.Fragment key={i}>
                    {/* Left (base) half */}
                    <div className={cn(
                        'flex min-w-0 border-r border-line',
                        r.kind === 'del' && 'bg-red-soft',
                    )}>
                        <span className={lineNo}>{r.leftNo ?? ''}</span>
                        <pre className={cn(lineText, r.kind === 'del' ? 'text-red' : 'text-ink-2')}>{r.left ?? ''}</pre>
                    </div>
                    {/* Right (current) half */}
                    <div className={cn(
                        'flex min-w-0',
                        r.kind === 'add' && 'bg-green/15',
                    )}>
                        <span className={lineNo}>{r.rightNo ?? ''}</span>
                        <pre className={cn(lineText, r.kind === 'add' ? 'text-green' : 'text-ink-2')}>{r.right ?? ''}</pre>
                    </div>
                </React.Fragment>
            ))}
            <HiddenRows hidden={hidden} className="col-span-2" />
        </div>
    );
}

/** Narrow layout: one column, rows already current-first (currentFirst).
 *  Each line carries its own side's number and a +/− mark; an unchanged
 *  line shows once, numbered as the current side has it. */
function StackedRows({ rows, labels, hidden }: RowsProps) {
    return (
        <div data-fallback-diff="stacked">
            <div className={cn(columnHead, 'flex flex-wrap gap-x-4 pl-10 pr-4')}>
                <span className="min-w-0 truncate" title={labels.current}><span className="text-green">+</span> {labels.current}</span>
                <span className="min-w-0 truncate" title={labels.base}><span className="text-red">−</span> {labels.base}</span>
            </div>
            {rows.map((r, i) => (
                <div key={i} className={cn(
                    'flex min-w-0',
                    r.kind === 'del' && 'bg-red-soft',
                    r.kind === 'add' && 'bg-green/15',
                )}>
                    <span className={lineNo}>{(r.kind === 'del' ? r.leftNo : r.rightNo) ?? ''}</span>
                    <span className={cn('w-3 shrink-0 select-none', r.kind === 'del' ? 'text-red' : 'text-green')}>
                        {r.kind === 'del' ? '−' : r.kind === 'add' ? '+' : ''}
                    </span>
                    <pre className={cn(lineText, r.kind === 'del' ? 'text-red' : r.kind === 'add' ? 'text-green' : 'text-ink-2')}>
                        {(r.kind === 'del' ? r.left : r.right) ?? ''}
                    </pre>
                </div>
            ))}
            <HiddenRows hidden={hidden} />
        </div>
    );
}
