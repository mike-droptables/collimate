// SPDX-License-Identifier: MIT
// Renderer contract types. SDK platform.tsx and the app hosts implement it.

import type { ComponentType } from 'react';

/** The skin's ground: 'dark' or 'light'. */
export type Scheme = 'light' | 'dark';

/**
 * Every skin token, by its CSS custom-property name. The host writes each
 * one on the renderer's `:root` (an iframe renderer's own document, or the
 * page a native shadow root inherits from), so `var(--sheet-2)` works in an
 * inline style with no subscription. `RendererTheme.vars` carries the same
 * names with their resolved values.
 */
export type ThemeVarName =
    | '--canvas' | '--sheet' | '--sheet-2' | '--sheet-3'
    | '--line' | '--line-2'
    | '--ink' | '--ink-2' | '--ink-3'
    | '--brass' | '--brass-soft'
    | '--teal' | '--teal-soft' | '--teal-sheet'
    | '--red' | '--red-soft' | '--green'
    | '--code-bg' | '--prose-ink'
    | '--shadow' | '--lift'
    | '--font-ui' | '--font-mono' | '--font-prose'
    | '--radius';

/** The skin's three font stacks, as CSS `font-family` values. */
export interface ThemeFonts {
    /** Interface text (`--font-ui`). */
    ui: string;
    /** Code, paths, terminals (`--font-mono`). */
    mono: string;
    /** Documents and long-form prose (`--font-prose`). */
    prose: string;
}

/** Snapshot returned by `api.getTheme()` and passed to `api.onThemeChange`. */
export interface RendererTheme {
    /** The active skin's id: 'night', 'night-cool', 'paper', or a user
     *  skin's 'user.<name>'. */
    themeId: string;
    /** Whether the skin paints on a dark or a light ground. */
    scheme: Scheme;
    /**
     * False only for the placeholder an iframe renderer sees before the
     * host's first theme arrives (`themeId` 'unknown', `scheme` 'dark',
     * empty `vars` and `fonts`). Every snapshot the host sends is true.
     */
    known: boolean;
    /** Every skin token's resolved value, keyed by CSS name
     *  (`vars['--sheet-2']`). Empty in the placeholder. */
    vars: Record<ThemeVarName, string>;
    /** The three font stacks (the values of `--font-ui`, `--font-mono`
     *  and `--font-prose`). Empty strings in the placeholder. */
    fonts: ThemeFonts;
}

/**
 * Snapshot returned by `api.getEditing()` and passed to
 * `api.onEditingChange`: the user's editing preferences (Settings → Editing).
 */
export interface RendererEditing {
    /** Underline misspelled words. The host sets it on the renderer's
     *  document (`<html spellcheck>`, which a native shadow root's editables
     *  inherit too), so an editable that leaves the attribute unset follows
     *  it with no code; an editor that sets its own, or runs in a frame of
     *  its own, applies it from here. */
    spellcheck: boolean;
}

/** Which of the skin's three font stacks a choice of family replaces. */
export type FontRole = 'ui' | 'mono' | 'prose';

/**
 * Snapshot returned by `api.getStyle()` and passed to `api.onStyleChange`:
 * the user's style choices for THIS renderer, made in its style control.
 * The host persists the record per channel under the renderer's id, so
 * every file the renderer shows in the channel shares it. A null field is
 * the renderer's default: the skin's look, the skin's font for the role,
 * the renderer's own text size.
 */
export interface RendererStyle {
    /** One of the theme ids the renderer offers; null = its default (the
     *  skin, or whatever the renderer's first choice means). */
    theme: string | null;
    /** A font family name from `getFontFamilies()` (`'Lora'`); null = the
     *  skin's stack for the role. */
    font: string | null;
    /** A text-size factor (1 = 100%); null = the renderer's own size. */
    textSize: number | null;
}

/** One family of the host's font catalogue (`api.getFontFamilies()`). */
export interface RendererFontFamily {
    /** The family name, what `RendererStyle.font` stores. */
    family: string;
    /** The name to show in a picker. */
    label: string;
    /** The stack it can stand in for. */
    role: FontRole;
    /** The CSS `font-family` value to apply. */
    stack: string;
}

/**
 * What the Electron desktop shell can do that a browser tab cannot.
 * Read every flag as falsy-means-no: a browser tab has no capability map
 * at all, so test truthiness, never presence.
 */
export interface DesktopCapabilities {
    /** Host a real Chromium view (a native guest, not an iframe) for a cell. */
    nativeGuests: boolean;
    /** Capture that view's pixels, visible or not. */
    guestCapture: boolean;
    /** Give a view its own cookie/storage partition that survives restart. */
    persistentPartitions: boolean;
    /** Reach 127.0.0.1 services directly, unblocked by page CSP. */
    directLoopback: boolean;
    /** Open a URL in the user's real browser. */
    externalOpen: boolean;
    /** The app draws its own title bar — page chrome, not a renderer's business. */
    customTitleBar: boolean;
    /** …and its own minimise/maximise/close buttons; likewise page chrome. */
    customWindowControls: boolean;
}

/**
 * `api.desktop` — present only when the renderer runs inside the Electron
 * shell. Its absence is the feature check: a renderer that needs desktop
 * behaviour reads `useDesktop(api)` and falls back when it's undefined.
 */
export interface DesktopSurface {
    capabilities: Partial<DesktopCapabilities>;
    /** Open a URL in the user's real browser, outside the app. */
    openExternal(url: string): Promise<void>;
    /**
     * Mount a live web page inside `hostEl` and get a handle on it.
     *
     * Absent — not a function that fails — unless
     * `capabilities.nativeGuests` is true, so `api.desktop?.mountGuest`
     * is the whole feature test.
     */
    mountGuest?(hostEl: HTMLElement, options: GuestOptions): GuestHandle;
}

/** What a renderer may decide about the guest it mounts. */
export interface GuestOptions {
    /** Initial URL. http(s) only — the shell refuses anything else. */
    src: string;
    /**
     * Bare partition name, sanitised and prefixed before it reaches the
     * shell. Two mounts naming the same partition share cookies and
     * storage; the host picks a per-renderer default when this is absent.
     */
    partition?: string;
    /**
     * The device the guest presents as. `"phone"` and `"tablet"` give it
     * that device's user agent (composed by the shell from its own Chrome
     * version) and a mobile viewport of the host element's size; the
     * renderer letterboxes the host element to the device's width. Omitted
     * or `"desktop"`: the shell's own user agent, no emulation.
     */
    device?: 'desktop' | 'phone' | 'tablet';
    /**
     * Keep the guest alive when it is parked off-screen and exempt it from
     * the pool's LRU. Costs an OS process until the app window closes, a
     * mount names a different `guestSessionKey`, or the host deletes the
     * guest's `.webview` file — `destroy()` only releases the mount, never
     * the guest.
     */
    keepAlive?: boolean;
    /**
     * Whether the guest takes pointer input; defaults to true. A
     * non-interactive guest shows as a periodically-refreshed screenshot
     * that clicks fall straight through.
     */
    interactive?: boolean;
    /**
     * Names the guest's logical session.
     *
     * A mount reattaches a parked guest by pool key alone, which is what
     * lets a cell be closed and reopened mid-session without losing the
     * page. This is the other half: a mount whose key differs from the
     * parked guest's DESTROYS that guest and creates a fresh one on
     * `src`, so a second session in the same cell is not silently handed
     * its predecessor's page. Omitted on both sides reattaches, as
     * before; name whatever is unique per session (a run id, a proxy
     * base) when the renderer has one.
     */
    guestSessionKey?: string;
}

/** Per-kind payloads for `GuestHandle.on`. */
export interface GuestEventData {
    navigate: { url: string; inPage: boolean };
    title: { title: string };
    loading: { loading: boolean };
    crashed: { reason: string; exitCode: number };
    /** The guest gained or lost input focus — the DOM cannot see this. */
    focus: { focused: boolean };
    /** A main-frame load finished; the first one means "there is a page". */
    ready: { url: string };
}

/** A mounted guest. All operations run through the shell's single
 *  authorisation chokepoint against this mount's own guest. */
export interface GuestHandle {
    /** Resolves once the shell has created the guest. */
    readonly guestId: Promise<string>;
    loadURL(url: string): Promise<void>;
    /** Resolves false when there was nowhere to go. */
    goBack(): Promise<boolean>;
    goForward(): Promise<boolean>;
    reload(): Promise<void>;
    evaluate(js: string): Promise<unknown>;
    capturePng(): Promise<ArrayBuffer>;
    currentUrl(): Promise<string>;
    /** Subscribe to one kind of guest event. Returns an unsubscribe. */
    on<K extends keyof GuestEventData>(
        kind: K,
        handler: (data: GuestEventData[K]) => void,
    ): () => void;
    /** Let the guest take pointer input, or let clicks fall through to
     *  the canvas underneath (what a pan gesture needs). */
    setInteractive(interactive: boolean): void;
    /** Switch the device the guest presents as: the shell sets its user
     *  agent and viewport emulation, then reloads the page in place (same
     *  guest, same session — logins and history survive). A no-op where
     *  the guest is an iframe fallback. */
    setDevice(device: 'desktop' | 'phone' | 'tablet'): Promise<void>;
    /** Release the mount — the guest parks with its page state intact
     *  and a later mount in the same spot reattaches to it. Idempotent. */
    destroy(): void;
}

/**
 * The host API object available via `useCollimateFile().api`.
 *
 * Members marked optional exist in only one embed mode (native shadow
 * DOM vs iframe sandbox) — feature-detect before calling.
 */
export interface SurfaceSession {
    /**
     * `"starting"` while the host has accepted the run but its process has
     * not registered the surface yet: `chat_id`, `ws_path` and `token` are
     * empty. Show the surface and keep input open (queue what cannot be
     * sent); `onSurfaceChange` fires when it goes `"live"` with real
     * coordinates.
     */
    state: 'starting' | 'live';
    chat_id: string;
    ws_path: string;
    token: string;
    label: string;
    run_id: string;
}

/** A self-contained worker implementation, durably registered once by hash.
 * The worker accepts {type:'materialize',requestId,payload} and answers
 * {type:'materialized',requestId,body} or {type:'error',requestId,error}. */
export interface RendererSnapshotCodec {
    id: string;
    version: number;
    implementationHash: string;
    source: string;
}

/** Exact renderer state. The host assigns its edit sequence and owns recovery. */
export interface RendererRecoverySnapshot {
    codecHash: string;
    snapshotId: string;
    payload: unknown;
}

export interface RendererDocumentSnapshot {
    /** Last materialized bytes; current only when materializedSeq === editSeq. */
    content: string;
    recovery?: RendererRecoverySnapshot;
    editSeq: number;
    materializedSeq: number;
}

/** A host's request for context from this view (`api.onAnnotationRequest`):
 *  a user attaching part of a file to a message for a run. */
export interface AnnotationRequest {
    /** Host-minted id, unique per request. */
    id: string;
    /** 'pick': the user chooses something in the view now, at their own pace;
     *  'selection': answer at once from what is selected. */
    mode: 'pick' | 'selection';
    /** Short host text a pick prompt may show. */
    hint?: string;
}

/** What the view answers. The host stamps the file's path and the renderer
 *  id itself. */
export interface AnnotationContext {
    /** The chip the user sees, under 80 characters: the file's name and where. */
    label: string;
    /** Markdown the run reads (the host keeps at most 16 KiB). */
    text: string;
    /** A JSON object locating the answer in the view's own terms (at most 4 KiB). */
    anchor?: Record<string, unknown>;
}

export interface RendererAPI {
    // Content access
    /** Wait for the current edit's materialized bytes, including recovered edits. */
    getContent(): Promise<string>;
    /** Read exact recovery state without waiting for background serialization. */
    getDocumentSnapshot(): Promise<RendererDocumentSnapshot>;
    /** Register and durably cache a codec before enabling snapshot edits. */
    registerSnapshotCodec(codec: RendererSnapshotCodec): Promise<void>;
    /** Publish an immutable exact snapshot immediately on every edit. */
    saveSnapshot(snapshot: RendererRecoverySnapshot): void;
    /** Owner state pushes. A renderer must not reapply its own snapshotId. */
    onDocumentSnapshot(cb: (snapshot: RendererDocumentSnapshot) => void): () => void;
    getDiffContent(): Promise<string | null>;
    /**
     * Host names for the diff's two sides (e.g. "v3"/"v4" when the
     * version-history slider drives the diff). null when the host has no
     * meaningful names — fall back to generic labels ("Before"/"After").
     * Host-optional: call it as `api.getDiffLabels?.()` and read an
     * absent method exactly like a null result.
     */
    getDiffLabels?(): { base: string; current: string } | null;
    getFilename(): string;
    getCellId(): string;
    /** Live coordinates for this mounted file; null when offline or in History.
     * A surface the host claimed for a starting run answers `state: "starting"`
     * with empty coordinates until the run registers it.
     * Network/server failures reject rather than masquerading as release. */
    getSurface(): Promise<SurfaceSession | null>;
    /** Claim, registration (starting → live) and release of this mounted
     * file. Returns unsubscribe. */
    onSurfaceChange(cb: () => void): () => void;
    /** Body-free own-cell membership; History returns its exact immutable version. */
    getFilePaths(): Promise<string[]>;
    /** The preview primitives (the renderer example's live preview): compile
     * exact virtual files in the pinned local worker, without executing or
     * approving code. Bounds: 5000 files / 64 MiB; failed requests reject.
     * Runtime texts and apiOrigin describe the host's served preview
     * environment, not authority. Present in every host. */
    preview: {
        /** The explicit online action: fetch the modules `compile` needs.
         * Every fetched module stays inert bytes. */
        prepareDependencies(files: Record<string, Uint8Array>, entrypoint: string, signal?: AbortSignal): Promise<void>;
        compile(files: Record<string, Uint8Array>, entrypoint: string): Promise<{
            source_hash: string; code: string; bundle_hash: string;
            runtime: {react: string; session: string}; apiOrigin: string;
            imports: Record<string, string>; artifact_hash: string;
        }>;
    };

    // Sibling files (other files in the same cell). `path` is always the
    // file's FULL cell-relative path — the same addressing for reads and
    // writes, so `docs/notes.md` is not reachable as `notes.md`.
    // HTTP failures reject with numeric err.status (404 means absent).
    // Current reads include this window's durable document recovery. History
    // reads the exact selected snapshot, including the root version.
    getSiblingFile(path: string): Promise<string>;
    /**
     * Conditionally write text into a sibling of the renderer's OWN cell.
     * The host journals the edit before sending; getSiblingFile exposes its
     * local recovery too. True means exact server acceptance. Checkpoints
     * separately group accepted revisions into history.
     * Creates the file when absent, replaces it in
     * place when present; `path` is cell-relative, identical addressing to
     * `getSiblingFile`. Available in every host.
     *
     * NEVER rejects: a missing cell, an unavailable host, a mid-run refusal
     * and a transport timeout all resolve `false`, so callers branch on the
     * boolean and never need a catch.
     */
    saveSiblingFile(path: string, content: string): Promise<boolean>;
    /**
     * Direct URL for a file in the renderer's OWN cell — for `<img src>`,
     * `<video>`, fetch. Binary-safe (unlike `getSiblingFile`, which decodes
     * as text) and pinned to the accepted revision, independently of dirty drafts.
     * In History it reads the exact selected snapshot. The host encodes
     * each path segment and preserves slashes; pass an unencoded path.
     * Synchronous in every host.
     */
    getFileUrl(path: string): string;

    // Editing
    /** Report the exact serialized body on every document change. The host
     * journals it locally and coalesces remote writes; do not debounce it. */
    reportChange(newContent: string): void;
    /** Primary document editing is disabled for run-owned UI, history and review.
     * Never offer a user override or emit file saves while this is true. */
    isReadOnly(): boolean;
    /** Run actions and annotation requests are available. False in history,
     * review, diff and pending structural operations; hosts send no annotation
     * request then. A true result never grants permission to edit run-owned
     * primary file bytes. */
    canInteract(): boolean;
    /** Answer the host's annotation requests for this view. Resolve the
     *  context, null when the user abandons the pick in the view (Esc), or
     *  reject; reject with `annotationUnavailable(message)` when this view
     *  cannot answer. Abort the work when `signal` aborts (the host cancelled).
     *  One handler per view: a second subscription replaces the first.
     *  Returns unsubscribe. Absent in a sealed preview. */
    onAnnotationRequest?(handler: (request: AnnotationRequest, signal: AbortSignal) => Promise<AnnotationContext | null>): () => void;

    // File change notifications; returns unsubscribe
    onFilesChange(cb: (detail: { cellId: string; changedFiles: string[] }) => void): () => void;

    /**
     * The document owner supplies an accepted or explicitly resolved body.
     * Apply in place without emitting reportChange. Present in every host;
     * primary files must not refetch themselves on onFilesChange.
     */
    onSetContent(cb: (content: string) => void): () => void;

    /**
     * Host requests a serialization barrier (Save, Run or user close). The callback should push its latest value
     * through reportChange synchronously, or return a promise the host
     * awaits. Returns unsubscribe. Optional — unsupported editors are
     * covered by the host's bounded flush wait.
     */
    onFlush?(cb: () => void | Promise<void>): () => void;

    /**
     * Whether the renderer's cell is on screen. Native mode only — an
     * iframe renderer lacks it and reads as always visible. Read it
     * through `useVisibility(api)`.
     */
    getVisible?(): boolean;
    /** Subscribe to visibility flips; fires immediately on subscribe.
     *  Returns unsubscribe. Native mode only. */
    onVisibleChange?(cb: (visible: boolean) => void): () => void;

    /**
     * Whether the renderer's cell holds the canvas focus. Native mode
     * only — an iframe renderer lacks it and reads as always focused.
     * Gate window-level input listeners on it via `useHostFocus(api)`.
     */
    getFocused?(): boolean;
    /** Subscribe to focus flips; fires immediately on subscribe.
     *  Returns unsubscribe. Native mode only. */
    onFocusChange?(cb: (focused: boolean) => void): () => void;

    /**
     * The host's current skin. Synchronous in every host; an iframe
     * renderer reads the `known: false` placeholder until the host's first
     * theme arrives (after the renderer signals ready).
     */
    getTheme(): RendererTheme;
    /**
     * Subscribe to skin changes. Fires immediately on subscribe with the
     * current snapshot and again on every change. Returns unsubscribe.
     */
    onThemeChange(cb: (t: RendererTheme) => void): () => void;

    /**
     * The user's editing preferences. Synchronous in every host; an iframe
     * renderer reads the default (spellcheck on) until the host's first
     * preferences arrive (with the theme, after the renderer loads).
     */
    getEditing(): RendererEditing;
    /**
     * Subscribe to editing-preference changes. Fires immediately on
     * subscribe with the current record and again on every change. Returns
     * unsubscribe.
     */
    onEditingChange(cb: (e: RendererEditing) => void): () => void;

    /**
     * The user's style choices for this renderer (theme, font, text size).
     * The host persists them per channel under this renderer's id; a null
     * field is the renderer's default. Synchronous in every host; an iframe
     * renderer reads all-null until the host's first record arrives.
     */
    getStyle(): RendererStyle;
    /**
     * Subscribe to this renderer's style. Fires immediately on subscribe
     * with the current record and again on every change for this
     * renderer's id. Returns unsubscribe.
     */
    onStyleChange(cb: (s: RendererStyle) => void): () => void;
    /**
     * Change some fields of this renderer's style (null puts one back to
     * the default). The host persists the merged record per channel under
     * this renderer's id and echoes it through `onStyleChange`; a sealed
     * preview applies it locally and persists nothing. Allowed in a
     * read-only view: it styles the view, not the file.
     */
    setStyle(patch: Partial<RendererStyle>): void;
    /**
     * The host's font catalogue, every family with its label, role and
     * CSS stack. `RendererStyle.font` stores a family name from it.
     */
    getFontFamilies(): ReadonlyArray<RendererFontFamily>;

    // Styling (native/shadow-DOM renderers)
    injectCss(css: string): void;
    /** Native mode resolves the promise once the stylesheet loads. */
    loadCss(href: string): void | Promise<void>;

    /** Reveal the renderer; only call with `deferReady: true`. */
    ready?(): void;
    /** Emit a tagged action to the generator (`gen.wait_for_action` /
     * `gen.wait_for_action_event`). The host stamps this renderer's file as
     * the action's `source`; `payload` optionally carries a structured
     * answer value (agent-v2 §4.3.4). Native-embed renderers only. */
    emitAction?(actionId: string, payload?: Record<string, unknown>): Promise<boolean>;

    /**
     * Stage bytes as a DRAFT on the renderer's OWN cell. Drafts-first
     * getSiblingFile/getFileUrl reads see them immediately; use getFileUrl
     * for binary bytes. Checkpoint/run preparation promotes the draft to
     * committed history. Use this for uploads a generator will pick up.
     * Native only. Never throws; resolves false on failure.
     */
    stageDraftFile?(path: string, bytes: ArrayBuffer | Uint8Array | Blob | string): Promise<boolean>;

    /**
     * Reveal a file in the renderer's OWN cell: centre the cell and open
     * that file in its own tab under its default renderer, or focus the tab
     * already showing it. The file takes over a non-pinned active tab in
     * place; `newTab: true` opens a fresh tab beside it instead, so the
     * renderer that asked keeps its own. Same cell-relative addressing as
     * `getSiblingFile`; strictly same-cell — there is no cross-cell reach.
     * Fire-and-forget: an unresolvable cell or path is a silent no-op, so
     * renderers can wire it straight to user clicks (e.g. a markdown link
     * to a neighboring file). Available in every host.
     */
    revealFile(path: string, options?: { newTab?: boolean }): void;

    /**
     * Desktop-shell surface. Present only when the app runs inside
     * Electron AND the renderer is `embed: "native"` — sandboxed iframe
     * renderers never receive it, because handing untrusted iframe
     * content a shell handle would cross the sandbox boundary. Read it
     * through `useDesktop(api)`.
     */
    desktop?: DesktopSurface;
}

/** `api.preview`, the preview primitives. */
export type RendererPreview = RendererAPI['preview'];

/** Context value returned by `useCollimateFile()`. */
export interface CollimateFile {
    /** File content when the renderer first mounted (never changes). */
    initialContent: string;
    /** Current synced content (updates on external changes). */
    content: string;
    /** Publish every serialized edit immediately to the host document owner. */
    saveContent: (newContent: string) => void;
    isReadOnly: boolean;
    /** Name of the file being rendered, e.g. "output.md". */
    fileName: string;
    api: RendererAPI;
    /** Previous version content — present in diff mode only. */
    diffContent?: string | null;
    /** Host names for the diff's two sides (e.g. "v3"/"v4") — present in
     *  diff mode only, and null when the host has no meaningful names
     *  (use generic "Before"/"After" labels then). */
    diffLabels?: { base: string; current: string } | null;
}

/** Return shape of `useSiblingFile(path)`. */
export interface SiblingFileState {
    /** Blob URL for the sibling's content, or null while loading / on error. */
    url: string | null;
    loading: boolean;
}

/** Config accepted by `defineRenderer()`. */
export interface DefineRendererConfig {
    /** Main view. Reads file state via useCollimateFile(). */
    component: ComponentType;
    /** Optional diff view; receives diffContent via context. */
    diffComponent?: ComponentType;
    /**
     * Opt out of auto-ready after first commit; the renderer must call
     * `api.ready()` itself once fully painted (monaco/crepe-style async
     * engines).
     */
    deferReady?: boolean;
    /** The component restores getDocumentSnapshot().recovery before enabling
     * editing. Mount does not wait for Markdown/text materialization. */
    recoverySnapshots?: boolean;
}

/**
 * Opaque mount/unmount lifecycle produced by `defineRenderer()` and
 * consumed by the host. Renderers only ever `export default` it.
 */
export interface RendererDefinition {
    mount(container: Element, api: RendererAPI): Promise<() => void>;
    mountDiff?(container: Element, api: RendererAPI): Promise<() => void>;
}
