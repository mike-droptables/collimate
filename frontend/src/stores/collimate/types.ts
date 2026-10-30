// Shared types for the Collimate store. Extracted so the store itself
// (useCollimateStore.ts) can focus on state + actions, and other modules
// can import the types without pulling in the entire store.

import { Channel, VirtualFile, findCapabilitiesStack, type RunRecord } from '@/lib/types';
import type { CompareSide } from '@/lib/compare';
import type { AnnotationContextItem, AnnotationMode } from '@/lib/annotations';

// ─── Per-cell editor layout (see CellEditor.tsx + lib/cellLayout.ts) ────────
// A cell body is a collapsible Files pane beside a content pane of tabs, where
// tabs can be split recursively (VSCode-style). All structural mutations go
// through the pure ops in lib/cellLayout.ts. The explicit CellDisplay codec
// persists tabs, split topology and pane presentation in the channel KV.

/** File-manager view for the Files pane: Tree or Folder. */
export type FileViewMode = 'tree' | 'folder';

/** Which section of a history node a history tab came from. */
export type HistoryCategory = 'config' | 'file' | 'log';

/** One content-pane tab. Pinned-ness is DERIVED, never stored: a tab is pinned
 *  iff kind 'file' && path === cell.pinned. `cell.pinned` stays the single
 *  server-persisted (user-only) preview pointer. */
export type CellTab =
    /** `rendererId` is the "Open with" override — absent means the primary
     *  resolution for the file's extension (see hooks/useRendererId). */
    | { id: string; kind: 'file'; path: string; rendererId?: string }
    | { id: string; kind: 'empty' }
    /** A read-only view into the history rail's selected node. Deliberately
     *  carries NO frozen blob/version reference: the body resolves
     *  (nodeId, versionId, path) from the cellHistory slice at render time,
     *  so open history tabs FOLLOW the scrub (a file absent in the new node
     *  renders a placeholder rather than closing). Exempt from reconcile,
     *  dirty tracking and checkpoints by construction — every
     *  one of those filters on kind === 'file'. Swept closed on history
     *  exit. */
    | { id: string; kind: 'history'; category: HistoryCategory; path: string; rendererId?: string }
    /** "Compare with…": the cell's one temporary tab, a read-only diff of
     *  `current` (the right-clicked file, always this cell's own) against
     *  `base` (the picked target). One per cell — the next Compare with…
     *  replaces it in place (lib/cellLayout openCompareTab) — and its name
     *  reads in italic. Persisted as UI coordinates, never cloned, and
     *  dropped by reconcile when `current.fileName` leaves the cell. */
    | { id: string; kind: 'compare'; base: CompareSide; current: CompareSide };

export interface ContentLeaf {
    type: 'leaf';
    id: string;
    tabs: CellTab[];
    activeTabId: string | null;
}
export interface ContentSplit {
    type: 'split';
    id: string;
    /** 'row' = children side by side (vertical sashes); 'column' = stacked. */
    dir: 'row' | 'column';
    children: ContentNode[];
    /** Fractions summing to 1, one per child. */
    sizes: number[];
}
export type ContentNode = ContentLeaf | ContentSplit;

/** The Files panel's Runs and Recent toggles (components/cellPanel/
 *  FilesPanel), per cell; they outlive the panel, so reopening it keeps
 *  them. Its query lives in the cell's panel session. */
export interface FilesPanelFilters {
    /** Only files a live run locks. */
    runsOnly: boolean;
    /** Files newest first. */
    sortByRecency: boolean;
}

/** Where the Files pane docks in its cell: a column on the left edge, or a
 *  band across the top. A right-click on its Hide / Show icon switches the
 *  side; dragging that icon to an edge docks it there (lib/cellLayout). */
export type FilesPaneSide = 'left' | 'top';

export interface FilesPaneState {
    /** Persisted in the CellDisplay descriptor. */
    side: FilesPaneSide;
    /** px, the width the user dragged the left column to; null = the
     *  default (FILES_PANE_WIDTH in lib/cellLayout). Persisted; held between
     *  the minimum and two thirds of the cell at render. */
    width: number | null;
    /** px, the height of the top band; null = the default
     *  (FILES_PANE_HEIGHT). Persisted and clamped like the width, and kept
     *  apart from it, so the column keeps the width and the band the
     *  height. */
    height: number | null;
    /** The user's explicit show/hide choice. `null` = never touched and
     *  defaults to hidden. Any explicit choice wins. */
    collapsed: boolean | null;
    /** Undefined = the default ('tree'). */
    viewMode?: FileViewMode;
    /** Published folder-mode path ('' = cell root). The transfer panel's
     *  Import reads it as its target; lives here (not component state) so it
     *  survives collapse and CellEditor remounts. Session-only. */
    currentFolder: string;
    /** Published selection (null = nothing selected); in tree mode the
     *  import target folder derives from it. Session-only. */
    selection: { nodePath: string; isFolder: boolean } | null;
    /** The Files panel's toggles, persisted in the CellDisplay descriptor. */
    panelFilters: FilesPanelFilters;
}

/** Where the cell's runs pane docks: a column on the right edge, or a band across the bottom. */
export type RunsPaneSide = 'right' | 'bottom';

/** The cell's status pane (the runs pane), persisted in the CellDisplay
 *  descriptor: whether it shows, where it docks and its two sizes. What it
 *  shows (the status view, a run's drilldown or All) is session state, the
 *  store's `runsPaneView`. */
export interface RunsPaneState {
    /** Shown or hidden. */
    open: boolean;
    side: RunsPaneSide;
    /** px, the width of the right column; null = the default
     *  (RUNS_PANE_WIDTH in lib/cellLayout). */
    width: number | null;
    /** px, the height of the bottom band; null = the default
     *  (RUNS_PANE_HEIGHT). Kept apart from the width, as the Files pane's. */
    height: number | null;
}

/** What the status view's drilldown and the All view show: the messages
 *  with the files touched (Default), the same about the file last viewed
 *  in the cell (Current file), or the log. */
export type RunsPaneOption = 'default' | 'file' | 'log';

/** What the cell's status pane shows (session-only, per cell): the cell's
 *  runs (status), one run's drilldown, or every run and the cell's own
 *  notes (all). */
export type RunsPaneView =
    | { view: 'status' }
    | { view: 'run'; runId: string; option: RunsPaneOption }
    | { view: 'all'; option: RunsPaneOption };

/** A run that ended this session, kept for the status view's finished
 *  rows (dimmed, below the live ones) until Clear finished. */
export interface FinishedRun extends Omit<RunRecord, 'surfaces'> {
    status: string;
    failureReason?: string;
    /** ms. */
    endedAt: number;
}

/** A composer's draft, per drilldown run or the All view
 *  (lib/annotationDraft draftKey). Session-only. */
export interface ComposerDraft {
    /** The editor's text, mirrored (the composer reads the editor itself
     *  to send). */
    text: string;
    contextItems: AnnotationContextItem[];
    mode: AnnotationMode;
    /** With the queue bar on: post below it (held). */
    below: boolean;
    replyTo: { id: string; contextId: string; author: string; excerpt: string } | null;
    editing: { id: string; contextId: string; revision: number } | null;
    /** The composer fills the pane. */
    expanded: boolean;
    /** The POST's idempotency key: kept across a failed send, so a retry
     *  replays rather than posting twice. */
    operationId: string;
    /** The request (JSON, without its key) a failed POST made under
     *  operationId: only the same request reuses the key; any other takes a
     *  new one (the server refuses a key reused for another request). */
    failedRequest: string | null;
    /** Bumped when the editor must load new text (edit, reply): it remounts. */
    generation: number;
    notice: string | null;
    sending: boolean;
}

/** An Add annotation in flight: the draft it attaches to and the file the
 *  renderer was asked about. */
export interface PendingAnnotationRequest {
    draftKey: string;
    path: string;
    controller: AbortController;
}

/** A request for the status pane's view to take the keyboard (`seq` grows). */
export interface RunsPaneFocusRequest {
    target: 'view' | 'composer';
    seq: number;
    /** ms. */
    at: number;
}

/** A status pane to open once its cell is reachable (after a channel
 *  switch from the top bar's runs menu). Dropped after 15 s. */
export interface PendingRunsPaneOpen {
    channel: string;
    cellId: string;
    view: RunsPaneView;
    focus: 'view' | 'composer';
    at: number;
}

export interface CellLayout {
    root: ContentNode;
    activeLeafId: string;
    filesPane: FilesPaneState;
    runsPane: RunsPaneState;
    /** The pinned file tab shows only its pin, no label. Toggled by a
     *  click on the pin of the ACTIVE pinned tab (TabStrip); the pin of an
     *  inactive pinned tab only activates it. Per cell, persisted in CellDisplay. */
    pinnedTabCompact?: boolean;
}

/** In-flight content-tab drag. The payload lives in the store — dataTransfer
 *  can't be read during dragover — and only the marker MIME
 *  'application/x-collimate-tab' rides on the drag event. Cell-scoped: drops
 *  onto a different cell are ignored. */
export interface TabDragState {
    cellId: string;
    leafId: string;
    tabId: string;
}

/** A cell pane a dock drag moves (hooks/useDockDrag). */
export type DockPane = 'files' | 'runs';

/** In-flight drag of a cell pane's dock icon to a cell edge
 *  (hooks/useDockDrag). Session-only, like TabDragState. `side` is the edge
 *  a release would dock on, one of the pane's own sides; null while the
 *  pointer is outside the drag's region. */
export interface DockDragState {
    cellId: string;
    pane: DockPane;
    side: string | null;
}

// File operations use pinned bodies and conditional durable receipts.

/** One file riding in the file clipboard / drag payload. `relPath` is the
 *  path RELATIVE to the transfer root (the parent of the grabbed node), so
 *  the grabbed node's own top segment is preserved when rebased onto the
 *  paste/drop target folder. */
export interface FileTransferEntry {
    relPath: string;
    file: VirtualFile;
}

export interface FileTransferSource {
    /** Registered channel coordinate; mutation commands resolve its immutable UUID. */
    channel: string;
    cellId: string;
    /** The node that was grabbed (e.g. 'src/components' or 'src/a.ts'). Used
     *  to compute cut-source removal and same-cell self/descendant guards. */
    nodePath: string;
}

export interface FileClipboard {
    op: 'copy' | 'cut';
    source: FileTransferSource;
    entries: FileTransferEntry[];
    /** Human label for the paste menu item, e.g. "Paste 'components'". */
    label: string;
    /** The text the copy or cut wrote to the OS clipboard (the copied path):
     *  a paste whose text/plain equals it is this clipboard's paste. */
    osText: string;
}

/** Live drag payload — same shape as the clipboard minus `op` (copy vs move
 *  is decided at DROP time from the modifier key + same-cell rule). Set on
 *  dragstart, cleared on dragend. */
export interface FileDragState {
    source: FileTransferSource;
    entries: FileTransferEntry[];
    /** True if the drag originated from a read-only/locked browser — the drop
     *  target forces a copy (a move would mutate the read-only source). */
    sourceReadOnly?: boolean;
}

/** Per-cell history mode (the files-pane run-node rail). The presence of a
 *  cell's id in the `cellHistory` record means that cell's Files pane is in
 *  history mode — the content pane stays live. Display-owned, so
 *  it survives editor open/close and CellEditor remounts (zoom-out /
 *  offscreen culling).
 *
 *  Selection keys by IDENTITY, never by index: node ids are stable across
 *  refetches ('run:<run_id>:<first version id>' / 'files:<newest version
 *  cell id>'), so a head insert (live run, idle checkpoint) never re-points
 *  the user's position — only an explicit scrub moves it. */
export interface CellHistoryState {
    /** Selected rail node, or null = "the current node" (resolved from the
     *  fetched history's `current` once loaded, tracking new heads until the
     *  user scrubs). */
    nodeId: string | null;
    /** Nested file-version scrubbing within the node: a version cell id, or
     *  null = the node's last version (its end state). Non-null IS nested
     *  mode. */
    versionId: string | null;
    /** Collapsible section state. Files open, Config + Logs collapsed by
     *  default — config folders (staged skill trees) can be large. */
    sectionsOpen: { config: boolean; files: boolean; logs: boolean };
}

export const DEFAULT_HISTORY_SECTIONS: CellHistoryState['sectionsOpen'] = { config: false, files: true, logs: false };

/** A fresh history view: tracking the current node, default sections. */
export const defaultCellHistory = (): CellHistoryState => ({
    nodeId: null,
    versionId: null,
    sectionsOpen: { ...DEFAULT_HISTORY_SECTIONS },
});

/** The channel's id, then each Capabilities cell's id and stamp (built-in
 *  cards included, shown or hidden). It changes whenever a capability cell
 *  is added, moved on or off, renamed or edited (updated_at), so the
 *  renderer inventory, the runner catalog and the manage view refetch. The
 *  channel id leads, so two channels never share one. */
export function capabilitiesSourceFingerprint(channel: Channel | null): string {
    if (!channel) return '';
    const parts: string[] = [channel.id];
    const found = findCapabilitiesStack(channel);
    for (const cell of found?.stack.cells ?? []) parts.push(`${cell.id}:${cell.updated_at ?? ''}`);
    return parts.join('|');
}

// ─── Run slices (see useCollimateStore + lib/runningCell.ts) ────────────────

/** A cell's last finished run, set when a run on the cell ends (an unseen
 *  failure is kept over a later non-failure) and cleared when a run starts
 *  there. Drives the idle run light (red while an unseen failure) and the
 *  live light's notch. Persisted (localStorage). */
export interface LastFinishedRun {
    runId: string;
    label: string;
    /** Terminal status: 'completed' | 'failed' | 'stopped' (or the stop
     *  type a client that saw the stop records: 'force_stopped' |
     *  'gracefully_stopped') | 'timed_out' | 'interrupted' | … */
    status: string;
    /** failure_reason, when the run failed with one. */
    reason?: string;
    /** The user has seen it (opened the cell's status view, or the
     *  run's drilldown). Born true for anything that is not a failure. */
    seen: boolean;
    /** Finish time, ms — the persistence prune key. */
    at: number;
}

/** An ended run, snapshotted at its end: while the entry lasts
 *  (ENDED_RUN_LOG_MS, session-only) the run keeps its in-memory log and its
 *  failure reason. */
export interface EndedRun {
    runId: string;
    cellId: string;
    label: string;
    status: string;
    failureReason?: string;
    endedAt: number;
}

/** A file a run asked the client to show (SSE reveal_file, or the run card
 *  on start), waiting until the path lands in the cell's files: state
 *  arrives in a coalesced push AFTER the reveal. Expires after
 *  REVEAL_TTL_MS or when its run ends. */
export interface PendingReveal {
    path: string;
    runId: string;
    /** Queue time, ms. */
    at: number;
    /** undefined = the run-reveal focus rule (lib/cellLayout revealFileTab);
     *  true = activate (a run this client started); false = background. */
    activate?: boolean;
}
