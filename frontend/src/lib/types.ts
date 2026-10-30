/** Metadata for one accepted current or immutable historical file. */
export interface VirtualFile {
    name: string;
    language: string;
    file_revision: number;
    body_hash: string | null;
    encoding: 'utf-8' | 'binary';
    writer_id: string;
    edit_seq: number;
    deleted?: boolean;
    /** Display metadata only; never compared to choose file bytes. */
    timestamp?: string;
}

/** Explicit hydrated/authoring input. Never stored as the current Cell wire. */
export interface FileBody extends VirtualFile {
    content: string;
}

export interface Cell {
    id: string;
    name: string;
    /** Optional glyph for a generator cell: a free-form lucide-style icon
     *  name, resolved via the ICON_REGISTRY and stored alongside the name.
     *  It shows on the cell's head, its overview card and the Commands
     *  pane's target. */
    icon?: string;
    files: VirtualFile[];
    created_at?: string;
    updated_at?: string;
    /** Flat {key: value} settings captured from the generator's settings
     *  schema form. */
    settings?: Record<string, unknown> | null;
    /** Immutable UUID of the containing channel when supplied. */
    channel?: string;
    /** File path (within this cell's files) the USER pinned as the cell's
     *  preview. Runs never pin. */
    pinned?: string;
    /** Plain-text chips in the user's order (lib/tags.ts holds the rules). */
    tags?: string[];
    /** A built-in card at the head of the Capabilities stack (server-set):
     *  its packages are the app's own build, so it is never removed, moved,
     *  renamed, duplicated or edited. Its handlers take added examples and
     *  regenerated skills from the Capabilities manage view. */
    builtin?: boolean;
}

/** One live run, keyed by run id in the store's `runs` slice. Set from the
 *  POST /runs response, the `run_started` broadcast and `/runs/active`;
 *  dropped when the run ends. Several runs may be live on one cell. */
export interface RunRecord {
    runId: string;
    /** The cell the run works in (every file under `folder` is its lock). */
    cellId: string;
    /** Immutable UUID of the channel the run belongs to. */
    channel: string;
    /** The saved runner's slug. */
    runnerId: string | null;
    /** The runner's display name. */
    runnerName: string | null;
    generatorName: string;
    /** `<runner id>-<run id[:8]>`: the run folder's name and the run's
     *  tag on its annotation messages. */
    label: string;
    /** `_runs/<label>`, cell-relative. Every file inside is locked. */
    folder: string;
    /** The run's registered inbox surface (its chat or terminal pointer). */
    surfaces?: { path: string; label: string }[];
}

/** Which panel a cell's pick session shows (one panel per cell, over its
 *  body): the runners panel (row P02), the file search panel (P03) or the
 *  transfer panel (Import / export, TR-11). */
export type PickKind = 'runners' | 'files' | 'transfer';

/** A cell's panel session: the state behind the in-cell panel the cell
 *  shows (components/cellPanel). Keyed by cell id in the store, one per
 *  cell; several cells can hold one at once. Persisted server-side in the
 *  channel DB's `ui_state` KV, which stores it verbatim (NOT in any
 *  `.coll*` bundle and never part of the DAG), so it survives zoom out/in,
 *  offscreen unmounts and browser refresh. Deleted when the panel closes
 *  or its run starts. */
export interface PickSession {
    kind: PickKind;
    /** The panel's search query. */
    query: string;
    /** Last-touch epoch ms; newest wins when hydration meets local edits. */
    at: number;
}

/** A standard stack holds the user's work. Each channel has exactly one
 *  Capabilities stack: every non-built-in cell on it is a capability cell
 *  whose generators, runners, renderers and handlers are in use, headed by
 *  the app's built-in cards. Nothing runs on it. */
export type StackType = 'standard' | 'capabilities';

export const CAPABILITIES_STACK_TYPE = 'capabilities' satisfies StackType;

/** The four definition kinds a Capabilities cell can hold. */
export type CapabilityKind = 'generator' | 'runner' | 'renderer' | 'handler';

export interface Stack {
    id: string;
    name: string;
    cells: Cell[];
    stack_type?: StackType;
    /** True for the client-minted fan columns of a fanned-out stack: one
     *  single-cell wrapper per cell of the real stack, id `fan-<cellId>`
     *  (lib/fanOut.ts fanColumnId). Never present on server stacks. */
    synthetic?: boolean;
    /** Fan column: the wrapped cell's id. */
    sourceCellId?: string;
    /** Fan column: the real (fanned) stack's id. */
    sourceStackId?: string;
    /** Fan column: the wrapped cell's index in the real stack. */
    fanIndex?: number;
    /** Fan column: the real stack's cell count (>= 2 while fanned). Gates the
     *  grab handle / remove button — never read the column's own cells.length (1). */
    fanCount?: number;
}

export interface Channel {
    id: string;
    name: string;

    /** Stacks hidden to the top bar: only the Capabilities stack, while hidden. */
    nav_stacks: Stack[];
    /** The stacks on the canvas, left to right. */
    main_stacks: Stack[];

    nav_selection_id: string | null;
    main_selection_id: string | null;
}

/** Layout routes carry cell metadata while existing bodies stay in the cache. */
export type ChannelLayout = Omit<Channel, 'nav_stacks' | 'main_stacks'> & {
    nav_stacks: (Omit<Stack, 'cells'> & { cells: Omit<Cell, 'files'>[] })[];
    main_stacks: (Omit<Stack, 'cells'> & { cells: Omit<Cell, 'files'>[] })[];
};

/** True for the channel's Capabilities stack (never for a fan column:
 *  Capabilities never fans out). */
export function isCapabilitiesStack(stack?: Pick<Stack, 'stack_type'> | null): boolean {
    return stack?.stack_type === CAPABILITIES_STACK_TYPE;
}

/** The channel's Capabilities stack and whether it is shown on the canvas
 *  (in main_stacks) or hidden to the top bar (in nav_stacks). Main first. */
export function findCapabilitiesStack(
    channel: Pick<Channel, 'main_stacks' | 'nav_stacks'> | null | undefined,
): { stack: Stack; shown: boolean } | null {
    if (!channel) return null;
    const shown = channel.main_stacks.find(isCapabilitiesStack);
    if (shown) return { stack: shown, shown: true };
    const hidden = channel.nav_stacks.find(isCapabilitiesStack);
    return hidden ? { stack: hidden, shown: false } : null;
}

export interface AppState {
    channels: Channel[];
    active_channel_id: string | null;
}

/** Wire snapshots carry one full channel and navigation metadata for the rest. */
export interface ScopedAppState {
    channels: (Channel | Pick<Channel, 'id' | 'name'>)[];
    active_channel_id: string | null;
    channel_scope: string | null;
}

/** A state_delta's keyed patches against the scoped snapshot (api state.py
 *  state_patches): a path step is a field name or a member picked by id/name. */
export type StatePathPart = string | { key: 'id' | 'name'; value: string };
export type StatePatch =
    | { op: 'set'; path: StatePathPart[]; value: unknown }
    | { op: 'remove'; path: StatePathPart[] }
    | { op: 'insert'; path: StatePathPart[]; index: number; value: unknown }
    | { op: 'sort'; path: StatePathPart[] }
    | { op: 'move'; path: StatePathPart[]; index: number };
