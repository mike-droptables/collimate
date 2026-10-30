// Pure layout engine for the cell editor's VSCode-style content pane: a
// recursive split tree whose leaves hold tab lists, plus the Files-pane and
// runs-pane descriptors. Every structural mutation of a CellLayout goes through these
// ops — components never hand-roll tree surgery. All ops are pure
// (layout, …) => layout and structurally share unchanged subtrees, returning
// the SAME object when nothing changed so store subscribers can bail on
// reference equality.

import type {
    CellLayout,
    CellTab,
    ContentLeaf,
    ContentNode,
    ContentSplit,
    FilesPaneSide,
    FilesPaneState,
    FilesPanelFilters,
    RunsPaneSide,
    RunsPaneState,
} from '@/stores/collimate/types';
import type { CSSProperties } from 'react';
import { sameCompareSide, type CompareRequest } from '@/lib/compare';

export type SplitEdge = 'left' | 'right' | 'top' | 'bottom';

/** Context the engine needs from the cell row to derive pinned-ness and
 *  reconcile auto-managed tabs. Kept minimal (no Cell import) so the engine
 *  stays dependency-free and trivially testable. */
export interface LayoutCellContext {
    /** Names of the cell's files (full paths). */
    filePaths: ReadonlySet<string> | readonly string[];
    /** cell.pinned when it resolves to an existing file, else null. */
    pinnedPath: string | null;
    /** The user's pin just CHANGED to `pinnedPath`: reconcile focuses the
     *  newly pinned tab. Never set for a merely-existing pin — an unchanged
     *  pin must not steal focus. */
    focusPinned?: boolean;
    /** The path the pin just moved OFF (the other half of the same change):
     *  reconcile CLOSES its tab, so ✕ and unpin end in the same place. Set
     *  only for a real change (never on first mount, never when it equals
     *  `pinnedPath`); a tab the user opened again afterwards is a fresh open
     *  and outlives the next reconcile untouched. */
    unpinnedPath?: string | null;
}

/** Minimum share a split child may be dragged down to. */
export const MIN_LEAF_SHARE = 0.15;

let _seq = 0;
const mintId = (prefix: string): string =>
    `${prefix}${(_seq++).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

const toSet = (paths: ReadonlySet<string> | readonly string[]): ReadonlySet<string> =>
    paths instanceof Set ? paths : new Set(paths);

// ─── Predicates & queries ───────────────────────────────────────────────────

/** Pinned-ness is derived: the cell's pinned-preview file's tab. A pinned
 *  tab can't be closed directly — it is unpinned instead (moving/clearing
 *  cell.pinned), and that unpin closes it: reconcile sweeps the tab the pin
 *  left behind (ctx.unpinnedPath), so ✕ and unpin end in the same place. */
export const isTabPinned = (tab: CellTab, pinnedPath: string | null): boolean =>
    tab.kind === 'file' && pinnedPath !== null && tab.path === pinnedPath;

/** The cell's pinned path when it still names one of `files`, else null
 *  (a dangling pointer pins nothing). */
export const resolvePinnedPath = (
    pinned: string | null | undefined,
    files: readonly { name: string }[],
): string | null => (pinned && files.some(f => f.name === pinned) ? pinned : null);

export function allLeaves(node: ContentNode): ContentLeaf[] {
    if (node.type === 'leaf') return [node];
    return node.children.flatMap(allLeaves);
}

export function findLeaf(node: ContentNode, leafId: string): ContentLeaf | null {
    return allLeaves(node).find(l => l.id === leafId) ?? null;
}

export function allTabs(layout: CellLayout): CellTab[] {
    return allLeaves(layout.root).flatMap(l => l.tabs);
}

/** The leaf holding a given tab, or null. */
export function findTabLeaf(layout: CellLayout, tabId: string): ContentLeaf | null {
    return allLeaves(layout.root).find(l => l.tabs.some(t => t.id === tabId)) ?? null;
}

export function findFileTab(layout: CellLayout, path: string): { leaf: ContentLeaf; tab: CellTab } | null {
    for (const leaf of allLeaves(layout.root)) {
        const tab = leaf.tabs.find(t => t.kind === 'file' && t.path === path);
        if (tab) return { leaf, tab };
    }
    return null;
}

/** Paths of every open file tab, in tree order. */
export function openFilePaths(layout: CellLayout): string[] {
    return allTabs(layout).flatMap(t => (t.kind === 'file' ? [t.path] : []));
}

export function activeLeaf(layout: CellLayout): ContentLeaf {
    return findLeaf(layout.root, layout.activeLeafId) ?? allLeaves(layout.root)[0];
}

/** The focused tab: the active tab of the active leaf. */
export function activeTab(layout: CellLayout): CellTab | null {
    const leaf = activeLeaf(layout);
    return leaf.tabs.find(t => t.id === leaf.activeTabId) ?? null;
}

// ─── Construction ───────────────────────────────────────────────────────────

/** The Files pane's default width as a left column (the mock's `.files`).
 *  It opens inside the cell's width; the content beside it reflows. A drag
 *  on its content-facing edge stores the cell's own width in
 *  `filesPane.width`, held at render between the minimum and two thirds of
 *  the cell. */
export const FILES_PANE_WIDTH = 194;
export const FILES_PANE_MIN_WIDTH = 154;
/** Docked at the top, the pane is a band across the cell: this tall by
 *  default (`filesPane.height` once its edge is dragged), held between the
 *  minimum and two thirds of the cell. */
export const FILES_PANE_HEIGHT = 198;
export const FILES_PANE_MIN_HEIGHT = 121;

/** The Files pane's dock sides: a right-click on the Hide / Show icon
 *  switches between them. */
export const FILES_PANE_SIDES: readonly FilesPaneSide[] = ['left', 'top'];

/** The other side: left ↔ top. */
export const nextFilesPaneSide = (side: FilesPaneSide): FilesPaneSide =>
    side === 'left' ? 'top' : 'left';

/** Dock the pane on its other side, leaving it shown or hidden. */
export function cycleFilesPaneSide(layout: CellLayout): CellLayout {
    return setFilesPane(layout, { side: nextFilesPaneSide(layout.filesPane.side) });
}

/** Dock the pane on `side` and show it (a drag-to-dock release, the
 *  Commands pane's Dock files rows). */
export function dockFilesPane(layout: CellLayout, side: FilesPaneSide): CellLayout {
    return setFilesPane(layout, { side, collapsed: false });
}

/** The axis the pane's size runs along: the column's width, the band's height. */
export const filesPaneAxis = (side: FilesPaneSide): 'x' | 'y' =>
    side === 'left' ? 'x' : 'y';

/** The pane's stored size for its side (the width of a column, the height
 *  of a band), or the default when none is stored. Unclamped. */
export function filesPaneSize(layout: CellLayout): number {
    const pane = layout.filesPane;
    return filesPaneAxis(pane.side) === 'x' ? pane.width ?? FILES_PANE_WIDTH : pane.height ?? FILES_PANE_HEIGHT;
}

/** The cell's pane row per dock side. The row always holds the Files pane
 *  first and the content second, so a side change only turns the row and
 *  never remounts a renderer. */
export function filesPaneRowClass(side: FilesPaneSide): string {
    return side === 'left' ? 'flex-row' : 'flex-col';
}

export function defaultFilesPane(): FilesPaneState {
    return {
        side: 'left',
        width: null,
        height: null,
        collapsed: null,
        currentFolder: '',
        selection: null,
        panelFilters: defaultFilesPanelFilters(),
    };
}

/** The Files panel's toggles, both off. */
export function defaultFilesPanelFilters(): FilesPanelFilters {
    return { runsOnly: false, sortByRecency: false };
}

/** Untouched panes start hidden. Keep the nullable descriptor intact so
 *  an explicit choice continues to win. History mode explicitly opens the
 *  pane when entered. */
export function filesPaneCollapsed(layout: CellLayout): boolean {
    return layout.filesPane.collapsed ?? true;
}

/** The runs pane's default width as a right column; `runsPane.width` holds
 *  the cell's own once set. */
export const RUNS_PANE_WIDTH = 280;
export const RUNS_PANE_MIN_WIDTH = 200;
/** Docked at the bottom, the runs pane is a band across the cell: this
 *  tall by default (`runsPane.height` once set). */
export const RUNS_PANE_HEIGHT = 200;
export const RUNS_PANE_MIN_HEIGHT = 120;

/** The runs pane's dock sides, in a dock drag's tie order. */
export const RUNS_PANE_SIDES: readonly RunsPaneSide[] = ['right', 'bottom'];

/** A new cell's runs pane: hidden, on the right, at the default sizes. */
export function defaultRunsPane(): RunsPaneState {
    return { open: false, side: 'right', width: null, height: null };
}

/** The axis the runs pane's size runs along: the column's width, the band's height. */
export const runsPaneAxis = (side: RunsPaneSide): 'x' | 'y' =>
    side === 'right' ? 'x' : 'y';

/** The runs pane's stored size for its side (the width of the column, the
 *  height of the band), or the default when none is stored. Unclamped. */
export function runsPaneSize(layout: CellLayout): number {
    const pane = layout.runsPane;
    return runsPaneAxis(pane.side) === 'x' ? pane.width ?? RUNS_PANE_WIDTH : pane.height ?? RUNS_PANE_HEIGHT;
}

/** The content beside the docked panes keeps at least this much of the
 *  cell: a pane's minimum gives way before the content does. Without it a
 *  narrow cell with both panes open left the renderer a sliver, or nothing
 *  at all (a pooled frame with no width is parked, so it vanished). */
export const CONTENT_MIN_WIDTH = 160;
export const CONTENT_MIN_HEIGHT = 120;

const contentMin = (axis: 'x' | 'y') => (axis === 'x' ? CONTENT_MIN_WIDTH : CONTENT_MIN_HEIGHT);

/** What a docked pane leaves the rest of its flex container along `axis`:
 *  the content's minimum, and for the runs pane also the Files pane's share
 *  when that pane is shown on the same axis (its size, at most half of
 *  what the content's minimum leaves). As a CSS length over the container
 *  (`css`) and in pixels for a container `room` pixels long (`px`), so the
 *  resize handle clamps where the style does. */
export function paneReserve(layout: CellLayout, pane: 'files' | 'runs', axis: 'x' | 'y'): {
    css: string;
    px: (room: number) => number;
} {
    const min = contentMin(axis);
    const files = pane === 'runs' && !filesPaneCollapsed(layout) && filesPaneAxis(layout.filesPane.side) === axis
        ? filesPaneSize(layout)
        : null;
    if (files === null) return { css: `${min}px`, px: () => min };
    return {
        css: `${min}px + min(${files}px, (100% - ${min}px) / 2)`,
        px: (room) => min + Math.max(0, Math.min(files, (room - min) / 2)),
    };
}

/** A docked pane's size style: its size, held under two thirds of its
 *  container and under what leaves the container `reserve` (paneReserve's
 *  `css`). The pane's minimum holds only while that room is there — a CSS
 *  min-width beats max-width, so an unconditional one squeezed the content. */
export function paneSizeStyle(axis: 'x' | 'y', size: number, min: number, reserve: string): CSSProperties {
    // Only a compound reserve needs parentheses; a bare length keeps the
    // value in the form the DOM serializes back.
    const max = `min(66.6667%, 100% - ${reserve.includes(' ') ? `(${reserve})` : reserve})`;
    return axis === 'y'
        ? { height: size, minHeight: `min(${min}px, ${max})`, maxHeight: max }
        : { width: size, minWidth: `min(${min}px, ${max})`, maxWidth: max };
}

/** The other side: right ↔ bottom. */
export const nextRunsPaneSide = (side: RunsPaneSide): RunsPaneSide =>
    side === 'right' ? 'bottom' : 'right';

/** Fresh layout for a cell: one leaf seeded with the pinned file's tab, or a
 *  single empty tab when there is nothing to show. */
export function createDefaultLayout(ctx: LayoutCellContext): CellLayout {
    const tabs: CellTab[] = [];
    if (ctx.pinnedPath !== null) tabs.push({ id: mintId('t'), kind: 'file', path: ctx.pinnedPath });
    if (tabs.length === 0) tabs.push({ id: mintId('t'), kind: 'empty' });
    const leaf: ContentLeaf = { type: 'leaf', id: mintId('g'), tabs, activeTabId: tabs[0].id };
    return { root: leaf, activeLeafId: leaf.id, filesPane: defaultFilesPane(), runsPane: defaultRunsPane() };
}

/** The pinned tab's compact display (pin only, no label) on / off. */
export function togglePinnedTabCompact(layout: CellLayout): CellLayout {
    return { ...layout, pinnedTabCompact: !layout.pinnedTabCompact };
}

// ─── Tree surgery helpers (all structurally sharing) ────────────────────────

function mapLeaves(node: ContentNode, fn: (leaf: ContentLeaf) => ContentLeaf): ContentNode {
    if (node.type === 'leaf') return fn(node);
    let changed = false;
    const children = node.children.map(c => {
        const next = mapLeaves(c, fn);
        if (next !== c) changed = true;
        return next;
    });
    return changed ? { ...node, children } : node;
}

function replaceLeaf(node: ContentNode, leafId: string, replacement: ContentNode): ContentNode {
    if (node.type === 'leaf') return node.id === leafId ? replacement : node;
    let changed = false;
    const children = node.children.map(c => {
        const next = replaceLeaf(c, leafId, replacement);
        if (next !== c) changed = true;
        return next;
    });
    return changed ? { ...node, children } : node;
}

const equalSizes = (n: number): number[] => Array.from({ length: n }, () => 1 / n);

/** Structural invariants: no empty leaves (unless the tree would vanish), no
 *  single-child or same-direction-nested splits, sizes valid, active ids
 *  resolvable. Also reference-stable when nothing needs fixing. */
export function normalize(layout: CellLayout): CellLayout {
    const prune = (node: ContentNode): ContentNode | null => {
        if (node.type === 'leaf') return node.tabs.length === 0 ? null : node;
        const kept: ContentNode[] = [];
        const keptSizes: number[] = [];
        let changed = false;
        node.children.forEach((c, i) => {
            const next = prune(c);
            if (next === null) { changed = true; return; }
            if (next !== c) changed = true;
            // Merge a same-direction child split inline, scaling its sizes.
            if (next.type === 'split' && next.dir === node.dir) {
                changed = true;
                const share = node.sizes[i] ?? 1 / node.children.length;
                next.children.forEach((gc, j) => {
                    kept.push(gc);
                    keptSizes.push(share * (next.sizes[j] ?? 1 / next.children.length));
                });
                return;
            }
            kept.push(next);
            keptSizes.push(node.sizes[i] ?? 1 / node.children.length);
        });
        if (kept.length === 0) return null;
        if (kept.length === 1) return kept[0];
        const total = keptSizes.reduce((a, b) => a + b, 0);
        // Clamp after renorming: a same-direction merge scales grandchildren
        // by the parent share, which can sink them below MIN_LEAF_SHARE (a
        // 0.25-share leaf split in half mints two 0.125s) — and the sash
        // handler refuses to move a sub-minimum pair.
        const sizes = clampShares(total > 0 ? keptSizes.map(s => s / total) : equalSizes(kept.length));
        if (!changed && sizesEqual(sizes, node.sizes)) return node;
        return { ...node, children: kept, sizes };
    };

    let root = prune(layout.root);
    if (root === null) {
        // Every leaf emptied — keep a single leaf; reconcile() reseeds a tab.
        const leaf: ContentLeaf = { type: 'leaf', id: mintId('g'), tabs: [], activeTabId: null };
        root = leaf;
    }

    // Repair per-leaf activeTabId.
    root = mapLeaves(root, leaf => {
        if (leaf.activeTabId !== null && leaf.tabs.some(t => t.id === leaf.activeTabId)) return leaf;
        const fallback = leaf.tabs.length > 0 ? leaf.tabs[leaf.tabs.length - 1].id : null;
        return fallback === leaf.activeTabId ? leaf : { ...leaf, activeTabId: fallback };
    });

    const leaves = allLeaves(root);
    const activeLeafId = leaves.some(l => l.id === layout.activeLeafId)
        ? layout.activeLeafId
        : leaves[0].id;

    if (root === layout.root && activeLeafId === layout.activeLeafId) return layout;
    return { ...layout, root, activeLeafId };
}

function sizesEqual(a: number[], b: number[]): boolean {
    return a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-9);
}

/** Enforce the MIN_LEAF_SHARE floor on a share vector: floor the undersized
 *  entries and scale the rest to absorb the difference, iterating because the
 *  scale-down can push new entries under the floor. The floored set is sticky
 *  across passes (an entry floored once stays at the floor and is never scaled
 *  again), so every pass fixes at least one new entry and the loop ends within
 *  n passes. Splits and same-direction merges can otherwise mint sub-minimum
 *  shares whose sash the resize handler refuses to move. Falls back to equal
 *  shares when the floor is infeasible (more children than 1/MIN_LEAF_SHARE
 *  allows). */
function clampShares(sizes: number[]): number[] {
    const n = sizes.length;
    if (n * MIN_LEAF_SHARE >= 1) return equalSizes(n);
    let shares = [...sizes];
    const fixed = new Array<boolean>(n).fill(false);
    for (;;) {
        let marked = false;
        shares.forEach((s, i) => {
            if (!fixed[i] && s < MIN_LEAF_SHARE) { fixed[i] = true; marked = true; }
        });
        if (!marked) break;
        const fc = fixed.filter(Boolean).length;
        const free = 1 - fc * MIN_LEAF_SHARE;
        const rest = shares.reduce((a, s, i) => a + (fixed[i] ? 0 : s), 0);
        shares = shares.map((s, i) =>
            fixed[i] ? MIN_LEAF_SHARE : (rest > 0 ? (s / rest) * free : free / (n - fc)));
    }
    return shares;
}

/** A layout must never be fully tab-less — the content pane would render
 *  nothing and offer nothing to click. Reseed a single `empty` tab in the
 *  first leaf. Shared by closeTab (the user can close the last tab directly)
 *  and reconcile (files can vanish under the layout). */
function ensureSomeTab(layout: CellLayout): CellLayout {
    if (allTabs(layout).length > 0) return layout;
    const first = allLeaves(layout.root)[0];
    const tab: CellTab = { id: mintId('t'), kind: 'empty' };
    const root = replaceLeaf(layout.root, first.id, { ...first, tabs: [tab], activeTabId: tab.id });
    return { ...layout, root, activeLeafId: first.id };
}

// ─── Core ops ───────────────────────────────────────────────────────────────

export function setActiveLeaf(layout: CellLayout, leafId: string): CellLayout {
    if (layout.activeLeafId === leafId || !findLeaf(layout.root, leafId)) return layout;
    return { ...layout, activeLeafId: leafId };
}

/** Activate a tab (and its leaf). */
export function activateTab(layout: CellLayout, tabId: string): CellLayout {
    const leaf = findTabLeaf(layout, tabId);
    if (!leaf) return layout;
    if (leaf.activeTabId === tabId && layout.activeLeafId === leaf.id) return layout;
    const root = mapLeaves(layout.root, l =>
        l.id === leaf.id && l.activeTabId !== tabId ? { ...l, activeTabId: tabId } : l);
    return { ...layout, root, activeLeafId: leaf.id };
}

/** Open (or focus) a file as a content tab. One file = at most one tab per
 *  cell: if the file is already open anywhere, focus it (applying
 *  `rendererId` — an "Open with" choice retargets the existing tab's renderer
 *  in place rather than opening a duplicate). Otherwise the file takes over
 *  the target leaf's active tab when that tab is non-pinned, or opens as a
 *  new tab right after a pinned active tab. `opts.newTab` (the file pane's
 *  double-click, the run overlay's rows) never takes a tab over: the file
 *  always opens as a NEW tab right after the active one, leaving it in
 *  place. `opts.background` opens that new tab WITHOUT activating it (and
 *  leaves an already-open tab unfocused). Omitting `rendererId` never clears
 *  an existing override — a plain re-open keeps the user's choice. */
export function openFileTab(
    layout: CellLayout,
    path: string,
    pinnedPath: string | null,
    rendererId?: string,
    opts?: { newTab?: boolean; background?: boolean },
): CellLayout {
    const existing = findFileTab(layout, path);
    if (existing) {
        const focused = opts?.background ? layout : activateTab(layout, existing.tab.id);
        return rendererId !== undefined ? setTabRenderer(focused, existing.tab.id, rendererId) : focused;
    }

    const target = activeLeaf(layout);
    const newTab: CellTab = rendererId !== undefined
        ? { id: mintId('t'), kind: 'file', path, rendererId }
        : { id: mintId('t'), kind: 'file', path };
    const current = target.tabs.find(t => t.id === target.activeTabId) ?? null;

    let tabs: CellTab[];
    const newTabOnly = opts?.newTab || opts?.background;
    if (current && !newTabOnly && !isTabPinned(current, pinnedPath)) {
        // Take over the visible non-pinned tab in place.
        tabs = target.tabs.map(t => (t.id === current.id ? newTab : t));
    } else {
        // Pinned (or no) active tab, or an explicit new-tab open — insert
        // right after the active tab.
        const at = current ? target.tabs.findIndex(t => t.id === current.id) + 1 : target.tabs.length;
        tabs = [...target.tabs.slice(0, at), newTab, ...target.tabs.slice(at)];
    }
    if (opts?.background && current) {
        const root = replaceLeaf(layout.root, target.id, { ...target, tabs });
        return { ...layout, root };
    }
    const root = replaceLeaf(layout.root, target.id, { ...target, tabs, activeTabId: newTab.id });
    return { ...layout, root, activeLeafId: target.id };
}

/** Show a file a RUN asked to reveal (SSE reveal_file, the run card on
 *  start) without stealing the user's focus: layout-only, never selects the
 *  cell. `activate` decides focus:
 *   - undefined (a run's own reveal): the tab is activated when the active
 *     tab is empty, not a file, or a file of the SAME run; the run's own
 *     card is taken over in place (a surface replaces its run card), an
 *     empty tab too. Otherwise the file opens beside the active tab in the
 *     background.
 *   - true: always activated (a run this client just started); only an
 *     empty active tab is taken over, never real content.
 *   - false: always opened (or left) in the background.
 *  `runLabel` is the revealing run's label; `lockOwnerOf` resolves the live
 *  owner label of the active tab's path. */
export function revealFileTab(
    layout: CellLayout,
    path: string,
    opts: {
        runLabel: string;
        pinnedPath: string | null;
        lockOwnerOf: (path: string) => string | null;
        activate?: boolean;
    },
): CellLayout {
    const current = activeTab(layout);
    const sameRun = current?.kind === 'file' && opts.lockOwnerOf(current.path) === opts.runLabel;
    const focus = opts.activate
        ?? (!current || current.kind !== 'file' || sameRun);
    if (!focus) return openFileTab(layout, path, opts.pinnedPath, undefined, { background: true });
    const takeOver = !current || current.kind === 'empty'
        || (opts.activate === undefined && sameRun && current.kind === 'file'
            && current.path.endsWith('.run') && !isTabPinned(current, opts.pinnedPath));
    return openFileTab(layout, path, opts.pinnedPath, undefined, takeOver ? undefined : { newTab: true });
}

/** A copy of a layout for ANOTHER cell (Run in new cell seeds the clone
 *  from its source): same splits and tabs, fresh ids, minus the tabs
 *  `drop` rejects and every history and compare tab (both are views of
 *  this cell). A leaf left without tabs is pruned; a layout left without
 *  any gets an empty tab. */
export function cloneLayout(layout: CellLayout, drop: (tab: CellTab) => boolean): CellLayout {
    let activeLeafId = '';
    const copy = (node: ContentNode): ContentNode => {
        if (node.type === 'split') {
            return { ...node, id: mintId('s'), children: node.children.map(copy), sizes: [...node.sizes] };
        }
        const id = mintId('g');
        if (node.id === layout.activeLeafId) activeLeafId = id;
        let activeTabId: string | null = null;
        const tabs: CellTab[] = [];
        for (const t of node.tabs) {
            if (t.kind === 'history' || t.kind === 'compare' || drop(t)) continue;
            const next = { ...t, id: mintId('t') } as CellTab;
            if (t.id === node.activeTabId) activeTabId = next.id;
            tabs.push(next);
        }
        return { type: 'leaf', id, tabs, activeTabId };
    };
    const root = copy(layout.root);
    return ensureSomeTab(normalize({
        ...layout, root, activeLeafId, filesPane: { ...layout.filesPane }, runsPane: { ...layout.runsPane },
    }));
}

/** Place `newTab` in the active leaf and focus it: an active empty tab is
 *  taken over; otherwise the tab is inserted after the active tab. */
function insertTabAtActive(layout: CellLayout, newTab: CellTab): CellLayout {
    const target = activeLeaf(layout);
    const current = target.tabs.find(t => t.id === target.activeTabId) ?? null;
    let tabs: CellTab[];
    if (current && current.kind === 'empty') {
        tabs = target.tabs.map(t => (t.id === current.id ? newTab : t));
    } else {
        const at = current ? target.tabs.findIndex(t => t.id === current.id) + 1 : target.tabs.length;
        tabs = [...target.tabs.slice(0, at), newTab, ...target.tabs.slice(at)];
    }
    const root = replaceLeaf(layout.root, target.id, { ...target, tabs, activeTabId: newTab.id });
    return { ...layout, root, activeLeafId: target.id };
}

/** Open (or focus) a history tab — a read-only view into the history
 *  rail's selected node, keyed by (category, path): one tab per artifact,
 *  like files. The body resolves content from the cellHistory slice at
 *  render time (tabs follow the scrub), so nothing else is stored here. */
export function openHistoryTab(
    layout: CellLayout,
    category: 'config' | 'file' | 'log',
    path: string,
    rendererId?: string,
): CellLayout {
    for (const leaf of allLeaves(layout.root)) {
        const tab = leaf.tabs.find(
            t => t.kind === 'history' && t.category === category && t.path === path);
        if (tab) {
            const focused = activateTab(layout, tab.id);
            return rendererId !== undefined ? setTabRenderer(focused, tab.id, rendererId) : focused;
        }
    }
    const newTab: CellTab = rendererId !== undefined
        ? { id: mintId('t'), kind: 'history', category, path, rendererId }
        : { id: mintId('t'), kind: 'history', category, path };
    return insertTabAtActive(layout, newTab);
}

/** Open a "Compare with…" tab, the cell's one temporary tab. An open
 *  compare tab is replaced IN PLACE (its slot, a fresh id so the body loads
 *  afresh) and focused; the same pair, already open, is only focused.
 *  Otherwise the tab lands in the active leaf: an active empty tab is taken
 *  over, else it opens after the active tab. */
export function openCompareTab(layout: CellLayout, req: CompareRequest): CellLayout {
    for (const leaf of allLeaves(layout.root)) {
        const open = leaf.tabs.find((t): t is Extract<CellTab, { kind: 'compare' }> => t.kind === 'compare');
        if (!open) continue;
        if (sameCompareSide(open.base, req.base) && sameCompareSide(open.current, req.current)) {
            return activateTab(layout, open.id);
        }
        const tab: CellTab = { id: mintId('t'), kind: 'compare', base: req.base, current: req.current };
        const root = replaceLeaf(layout.root, leaf.id, {
            ...leaf,
            tabs: leaf.tabs.map(t => (t.id === open.id ? tab : t)),
            activeTabId: tab.id,
        });
        return { ...layout, root, activeLeafId: leaf.id };
    }
    return insertTabAtActive(layout, { id: mintId('t'), kind: 'compare', base: req.base, current: req.current });
}

/** Exit-history sweep: close every history tab (nothing else changes —
 *  every other tab persists untouched). */
export function closeHistoryTabs(layout: CellLayout): CellLayout {
    const root = mapLeaves(layout.root, leaf => {
        const tabs = leaf.tabs.filter(t => t.kind !== 'history');
        return tabs.length === leaf.tabs.length ? leaf : { ...leaf, tabs };
    });
    if (root === layout.root) return layout;
    return ensureSomeTab(normalize({ ...layout, root }));
}

/** The "+" button: a fresh empty tab at the end of the given leaf. */
export function openEmptyTab(layout: CellLayout, leafId: string): CellLayout {
    const leaf = findLeaf(layout.root, leafId);
    if (!leaf) return layout;
    const newTab: CellTab = { id: mintId('t'), kind: 'empty' };
    const root = replaceLeaf(layout.root, leafId, {
        ...leaf,
        tabs: [...leaf.tabs, newTab],
        activeTabId: newTab.id,
    });
    return { ...layout, root, activeLeafId: leafId };
}

/** Pull a tab out of its leaf and repair the leaf's focus — the neighbor tab
 *  (previous index, else next) takes over when the closed tab was the active
 *  one — then normalize (an emptied leaf is pruned and its split collapses).
 *  WITHOUT the last-tab reseed, so callers that go on to add a tab of their
 *  own don't end up with a spurious empty tab beside it. Assumes the caller
 *  has already cleared the tab for closing (pinned-ness, existence). */
function detachTab(layout: CellLayout, leaf: ContentLeaf, tabId: string): CellLayout {
    const root = replaceLeaf(layout.root, leaf.id, leafWithout(leaf, tabId));
    return normalize({ ...layout, root });
}

/** The leaf with `tabId` removed and its focus repaired (previous index, else
 *  next, when the removed tab was active). Leaf-level and un-normalized, so
 *  moveTab/splitWithTab can keep editing the tree before they normalize. */
function leafWithout(leaf: ContentLeaf, tabId: string): ContentLeaf {
    const idx = leaf.tabs.findIndex(t => t.id === tabId);
    const tabs = leaf.tabs.filter(t => t.id !== tabId);
    const activeTabId = leaf.activeTabId === tabId
        ? (tabs[Math.max(0, idx - 1)]?.id ?? null)
        : leaf.activeTabId;
    return { ...leaf, tabs, activeTabId };
}

/** Close a tab. Pinned tabs refuse (unpin first — and unpinning then closes
 *  the tab through reconcile, see ctx.unpinnedPath); the last leaf standing is
 *  never removed. The neighbor tab (previous index, else next) is activated. */
export function closeTab(layout: CellLayout, tabId: string, pinnedPath: string | null): CellLayout {
    const leaf = findTabLeaf(layout, tabId);
    if (!leaf) return layout;
    const tab = leaf.tabs.find(t => t.id === tabId)!;
    if (isTabPinned(tab, pinnedPath)) return layout;

    // Reseed here, not in reconcile: reconcile only re-runs when the CELL
    // changes (files/pinned), and closing the last tab changes only the
    // layout — leaving the reseed to reconcile would leave the content pane
    // permanently blank.
    return ensureSomeTab(detachTab(layout, leaf, tabId));
}

/** Close every closable tab of the leaf except `keepTabId`. */
export function closeOtherTabs(layout: CellLayout, keepTabId: string, pinnedPath: string | null): CellLayout {
    const leaf = findTabLeaf(layout, keepTabId);
    if (!leaf) return layout;
    let next = layout;
    for (const t of leaf.tabs) {
        if (t.id !== keepTabId) next = closeTab(next, t.id, pinnedPath);
    }
    return activateTab(next, keepTabId);
}

/** Close every closable tab to the right of `tabId` within its leaf. */
export function closeTabsToRight(layout: CellLayout, tabId: string, pinnedPath: string | null): CellLayout {
    const leaf = findTabLeaf(layout, tabId);
    if (!leaf) return layout;
    const idx = leaf.tabs.findIndex(t => t.id === tabId);
    let next = layout;
    for (const t of leaf.tabs.slice(idx + 1)) next = closeTab(next, t.id, pinnedPath);
    return next;
}

/** Move a tab into another leaf (or reorder within its own) at `index`.
 *  The moved tab becomes active in the target leaf. */
export function moveTab(layout: CellLayout, tabId: string, targetLeafId: string, index: number): CellLayout {
    const source = findTabLeaf(layout, tabId);
    const target = findLeaf(layout.root, targetLeafId);
    if (!source || !target) return layout;
    const tab = source.tabs.find(t => t.id === tabId)!;

    if (source.id === target.id) {
        const from = source.tabs.findIndex(t => t.id === tabId);
        const without = source.tabs.filter(t => t.id !== tabId);
        const at = Math.max(0, Math.min(index > from ? index - 1 : index, without.length));
        if (at === from) return activateTab(layout, tabId);
        const tabs = [...without.slice(0, at), tab, ...without.slice(at)];
        const root = replaceLeaf(layout.root, source.id, { ...source, tabs, activeTabId: tabId });
        return { ...layout, root, activeLeafId: source.id };
    }

    const at = Math.max(0, Math.min(index, target.tabs.length));
    const tgtTabs = [...target.tabs.slice(0, at), tab, ...target.tabs.slice(at)];

    let root = replaceLeaf(layout.root, source.id, leafWithout(source, tabId));
    root = replaceLeaf(root, target.id, { ...target, tabs: tgtTabs, activeTabId: tabId });
    return normalize({ ...layout, root, activeLeafId: target.id });
}

/** Split the target leaf along an edge, moving `tabId` into the new leaf.
 *  No-op when the tab is the target leaf's only tab (the result would be the
 *  same content). */
export function splitWithTab(layout: CellLayout, targetLeafId: string, tabId: string, edge: SplitEdge): CellLayout {
    const source = findTabLeaf(layout, tabId);
    const target = findLeaf(layout.root, targetLeafId);
    if (!source || !target) return layout;
    if (source.id === target.id && source.tabs.length === 1) return layout;
    const tab = source.tabs.find(t => t.id === tabId)!;

    // Detach from the source leaf first.
    let root = replaceLeaf(layout.root, source.id, leafWithout(source, tabId));

    const newLeaf: ContentLeaf = { type: 'leaf', id: mintId('g'), tabs: [tab], activeTabId: tab.id };
    const dir: ContentSplit['dir'] = edge === 'left' || edge === 'right' ? 'row' : 'column';
    const before = edge === 'left' || edge === 'top';
    // The target may have shrunk (same-leaf split) — re-find it in the new root.
    const tgt = findLeaf(root, targetLeafId)!;
    const split: ContentSplit = {
        type: 'split',
        id: mintId('s'),
        dir,
        children: before ? [newLeaf, tgt] : [tgt, newLeaf],
        sizes: [0.5, 0.5],
    };
    root = replaceLeaf(root, targetLeafId, split);
    return normalize({ ...layout, root, activeLeafId: newLeaf.id });
}

/** Commit sash-drag sizes for one split node (fractions; clamped + renormed). */
export function setSplitSizes(layout: CellLayout, splitId: string, sizes: number[]): CellLayout {
    const apply = (node: ContentNode): ContentNode => {
        if (node.type === 'leaf') return node;
        if (node.id === splitId && sizes.length === node.children.length) {
            // clampShares, not clamp-then-renormalize: renorming after a plain
            // clamp scales the floored entries back BELOW the minimum, and
            // silently reflows children the caller never touched.
            const total = sizes.reduce((a, b) => a + b, 0);
            const normed = clampShares(total > 0 ? sizes.map(s => s / total) : equalSizes(sizes.length));
            return sizesEqual(normed, node.sizes) ? node : { ...node, sizes: normed };
        }
        let changed = false;
        const children = node.children.map(c => {
            const next = apply(c);
            if (next !== c) changed = true;
            return next;
        });
        return changed ? { ...node, children } : node;
    };
    const root = apply(layout.root);
    return root === layout.root ? layout : { ...layout, root };
}

export function setFilesPane(layout: CellLayout, patch: Partial<FilesPaneState>): CellLayout {
    const merged = { ...layout.filesPane, ...patch };
    const same = (Object.keys(patch) as (keyof FilesPaneState)[])
        .every(k => layout.filesPane[k] === merged[k]);
    return same ? layout : { ...layout, filesPane: merged };
}

/** Patch the cell's Files panel toggles through setFilesPane; the SAME
 *  layout when nothing changes. */
export function setFilesPanelFilters(layout: CellLayout, patch: Partial<FilesPanelFilters>): CellLayout {
    const cur = layout.filesPane.panelFilters;
    const same = (Object.keys(patch) as (keyof FilesPanelFilters)[])
        .every(k => cur[k] === patch[k]);
    return same ? layout : setFilesPane(layout, { panelFilters: { ...cur, ...patch } });
}

/** Patch the runs pane; the SAME layout when nothing changes. */
export function setRunsPane(layout: CellLayout, patch: Partial<RunsPaneState>): CellLayout {
    const merged = { ...layout.runsPane, ...patch };
    const same = (Object.keys(patch) as (keyof RunsPaneState)[])
        .every(k => layout.runsPane[k] === merged[k]);
    return same ? layout : { ...layout, runsPane: merged };
}

/** Dock the runs pane on its other side, leaving it open or closed. */
export function cycleRunsPaneSide(layout: CellLayout): CellLayout {
    return setRunsPane(layout, { side: nextRunsPaneSide(layout.runsPane.side) });
}

/** Dock the runs pane on `side` and open it (a drag-to-dock release). */
export function dockRunsPane(layout: CellLayout, side: RunsPaneSide): CellLayout {
    return setRunsPane(layout, { side, open: true });
}

/** Open the runs pane (what it shows is the store's `runsPaneView`). */
export function openRunsPane(layout: CellLayout): CellLayout {
    return setRunsPane(layout, { open: true });
}

/** Close the runs pane, keeping its side and sizes. */
export function closeRunsPane(layout: CellLayout): CellLayout {
    return setRunsPane(layout, { open: false });
}

/** Open the runs pane when closed, close it when open. */
export function toggleRunsPane(layout: CellLayout): CellLayout {
    return setRunsPane(layout, { open: !layout.runsPane.open });
}

// ─── Reconciliation against the cell row ────────────────────────────────────

/** The tab a leaf falls back to when its active tab vanishes: the nearest
 *  surviving tab, the previous one first, then the next, as closing a tab
 *  does. Null when none survives. */
function fallbackTab(tabs: CellTab[], kept: ReadonlySet<CellTab>, vanished: CellTab): CellTab | null {
    const at = tabs.indexOf(vanished);
    const order = [...tabs.slice(0, at).reverse(), ...tabs.slice(at + 1)];
    return order.find(t => kept.has(t)) ?? null;
}

/** Bring a layout in line with the cell: drop file tabs, and the compare
 *  tab, whose file vanished (the active one falls back to its nearest
 *  survivor, as a close does), close the tab the pin just moved off, ensure
 *  the pinned file's tab, and guarantee at least one tab exists. When the
 *  pinned tab APPEARS while the focused tab is an empty tab, focus moves to
 *  it (never steals focus from real content).
 *  Reference-stable when nothing changes — safe to call from a render-keyed
 *  effect. */
export function reconcile(layout: CellLayout, ctx: LayoutCellContext): CellLayout {
    const files = toSet(ctx.filePaths);
    let next = layout;

    // 1. Drop tabs for vanished files (a compare tab goes with the cell's
    //    side of the pair).
    const root = mapLeaves(next.root, leaf => {
        const tabs = leaf.tabs.filter(t => {
            if (t.kind === 'file') return files.has(t.path);
            if (t.kind === 'compare') return files.has(t.current.fileName);
            return true;
        });
        if (tabs.length === leaf.tabs.length) return leaf;
        const active = leaf.tabs.find(t => t.id === leaf.activeTabId);
        // The active tab survived, or the whole leaf emptied (normalize
        // prunes it): nothing to choose.
        if (!active || tabs.includes(active) || tabs.length === 0) {
            return { ...leaf, tabs, activeTabId: active && tabs.includes(active) ? active.id : null };
        }
        return { ...leaf, tabs, activeTabId: fallbackTab(leaf.tabs, new Set(tabs), active)?.id ?? null };
    });
    if (root !== next.root) next = normalize({ ...next, root });

    // 1b. The pin just moved OFF a file (the user's unpin or re-pin): close
    //     the tab it left behind. Runs BEFORE the ensure /
    //     focus steps so a re-pin reads as one tab replacing another — the
    //     old one is gone by the time the new one is ensured at the head
    //     and focused.
    //
    //     Deliberately routed through the same detachTab closeTab uses rather
    //     than a bespoke filter, because the tab may be the leaf's ACTIVE tab
    //     (focus falls to the previous-index neighbour — and step 2b then
    //     overrides that with the newly pinned tab when there is one) or the
    //     last tab of a split leaf (normalize prunes the leaf and collapses
    //     the split). It is closeTab minus the last-tab reseed on purpose:
    //     when this was the cell's only tab, step 2 is about to ensure the
    //     newly pinned one — reseeding first would leave a stray empty tab
    //     parked beside it — and step 3 still covers the unpin-to-nothing
    //     case, where nothing replaces it.
    //
    //     The `!== ctx.pinnedPath` guard keeps a caller that passes the same
    //     path as both (a no-op pin change) from closing the tab it just
    //     pinned. A file deleted in the same update needs no handling either:
    //     step 1 already dropped its tab, so findFileTab finds nothing here.
    const unpinned = ctx.unpinnedPath;
    if (unpinned !== undefined && unpinned !== null && unpinned !== ctx.pinnedPath) {
        // One file = at most one tab per cell (see openFileTab), so a single
        // detach is the whole sweep.
        const stale = findFileTab(next, unpinned);
        if (stale) next = detachTab(next, stale.leaf, stale.tab.id);
    }

    // 2. Ensure the pinned file's tab at the head of the first leaf.
    const hadFocusOnEmpty = activeTab(next)?.kind === 'empty' || activeTab(next) === null;
    let ensured: string | null = null;
    const ensureHeadTab = (make: () => CellTab, exists: boolean) => {
        if (exists) return;
        const first = allLeaves(next.root)[0];
        const tab = make();
        const root2 = replaceLeaf(next.root, first.id, {
            ...first,
            tabs: [tab, ...first.tabs],
            activeTabId: first.activeTabId ?? tab.id,
        });
        next = { ...next, root: root2 };
        ensured = tab.id;
    };
    if (ctx.pinnedPath !== null) {
        ensureHeadTab(
            () => ({ id: mintId('t'), kind: 'file', path: ctx.pinnedPath! }),
            findFileTab(next, ctx.pinnedPath) !== null,
        );
    }
    if (ensured && hadFocusOnEmpty) next = activateTab(next, ensured);

    // 2b. A pin CHANGE focuses the newly pinned tab. Gated on
    // ctx.focusPinned (the caller detects the change), so an unchanged pin
    // still never steals focus from real content.
    if (ctx.focusPinned && ctx.pinnedPath !== null) {
        const pinned = findFileTab(next, ctx.pinnedPath);
        if (pinned) next = activateTab(next, pinned.tab.id);
    }

    // 3. Never a fully tab-less tree.
    next = ensureSomeTab(next);

    return next === layout ? layout : normalize(next);
}

/** Set or clear a file/history tab's renderer override (the "Open with"
 *  choice). `undefined` restores the primary resolution for the file's
 *  extension. */
export function setTabRenderer(layout: CellLayout, tabId: string, rendererId: string | undefined): CellLayout {
    const leaf = findTabLeaf(layout, tabId);
    if (!leaf) return layout;
    const tab = leaf.tabs.find(t => t.id === tabId)!;
    if ((tab.kind !== 'file' && tab.kind !== 'history') || tab.rendererId === rendererId) return layout;
    const nextTab: CellTab = rendererId === undefined
        ? (tab.kind === 'file'
            ? { id: tab.id, kind: 'file', path: tab.path }
            : { id: tab.id, kind: 'history', category: tab.category, path: tab.path })
        : { ...tab, rendererId };
    const root = replaceLeaf(layout.root, leaf.id, {
        ...leaf,
        tabs: leaf.tabs.map(t => (t.id === tabId ? nextTab : t)),
    });
    return { ...layout, root };
}

// ─── Persistence projection ─────────────────────────────────────────────────

/** Retarget every file tab from one path to another — the rename/move flow.
 *  The tab (and its id, so React keeps the mount) survives; without this a
 *  rename reads as delete+create and reconcile closes the open editor. Also
 *  used with a folder prefix by the caller mapping each contained file. */
export function renameFileTab(layout: CellLayout, from: string, to: string): CellLayout {
    const root = mapLeaves(layout.root, leaf => {
        let changed = false;
        const tabs = leaf.tabs.map(t => {
            if (t.kind !== 'file' || t.path !== from) return t;
            changed = true;
            return { ...t, path: to };
        });
        return changed ? { ...leaf, tabs } : leaf;
    });
    return root === layout.root ? layout : { ...layout, root };
}
