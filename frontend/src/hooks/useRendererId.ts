// Shared renderer-registry + dispatch hook.
//
// Owns the client's cache of which renderer handles which file extension
// (fetched once from /api/renderers, kept fresh via the store's
// _capabilitiesFingerprint subscription + a RENDERER_PREFS_CHANGED_EVENT
// event) and which files each is a default for (its manifest's
// `default_for`), plus the channel's extension → renderer preferences and the
// binary/text fallbacks. Resolves a filename → renderer
// record (id, embed mode, diff-support, etc.): FullFileEditor and
// CompareTabBody consume it via useRendererId to pick the right
// SandboxIframe / NativeRenderer; OpenWithSubmenu, TabContextMenu and the
// Files panel's Open with step use rendererCandidatesFor /
// resolveRendererWithOverride for "Open with" (the step lists allRenderers
// too); AddFileDialog and the Files panel's New file here list extensions
// via useSupportedExtensions.

import { useEffect, useState } from 'react';
import { getApiUrl, RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { BINARY_EXTS, IMAGE_EXTS, PDF_EXTS } from '@/lib/fileKinds';
import {handlerExtensions,rendererFilename,setRendererHandlers,type HandlerRendering} from '@/lib/rendererAliases';
import {caseFold} from '@/lib/unicodeCaseFold';

export interface RendererInfo {
    name: string;
    extensions: string[];
    /** The files this renderer is a default for (its manifest's
     *  `default_for`): an extension it claims, whose files it opens unless a
     *  preference says otherwise, or ANY_TEXT, every text file, whose "Open
     *  with" menu offers it. */
    defaultFor: string[];
    supportsDiff: boolean;
    /** The renderer refetches its own file (and siblings) on FILES_CHANGED
     *  and swaps content in place — hosts must not force-remount it to show
     *  an external write. */
    liveRefresh: boolean;
    embed: 'iframe' | 'native';
    contentHash: string;
}

export type ResolvedRenderer = RendererInfo & { id: string };

// Global renderer registry — fetched once on load, invalidated by push events.
let _rendererCache: Record<string, RendererInfo> = {};
let _rendererHandlers: HandlerRendering[] = [];
let _rendererPrefs: Record<string, string> = {}; // extension -> renderer id (from channel prefs)
const _rendererListeners: Set<() => void> = new Set();
// Coalesced refresh: a request that arrives mid-fetch queues one more pass
// instead of being dropped (its data may postdate what the in-flight pass read).
let _refreshInFlight: Promise<void> | null = null;
let _refreshAgain = false;
let _registryReady = false;
let _registryError: string | null = null;

/** The `default_for` entry for every text file. */
export const ANY_TEXT = '*';

// Precomputed lookup tables, rebuilt whenever the cache or prefs change.
// Prefs keep their original iteration order (a pref bound earlier wins);
// registry entries are ordered LONGEST EXTENSION FIRST so the most specific
// claim wins — `.slides.md` beats a `.md` renderer for `tour.slides.md` no
// matter which renderer the registry happened to list first. Among the
// claimants of one extension, a renderer that is the default for it
// (`default_for`) comes first; other ties fall back to registry order (the
// sort is stable), keeping resolution deterministic.
type ResolvedEntry = { ext: string; renderer: ResolvedRenderer | null; isDefault?: boolean };
let _prefEntries: ResolvedEntry[] = [];
let _extEntries: ResolvedEntry[] = [];
let _wildcardRenderer: ResolvedRenderer | null = null;
// The renderers that are a default for every text file, in registry order.
let _anyTextRenderers: ResolvedRenderer[] = [];

function findInCache(cache: Record<string, RendererInfo>, rendererId: string): ResolvedRenderer | null {
    const info = cache[rendererId];
    return info ? { id: rendererId, ...info } : null;
}

function rebuildResolveIndex() {
    _prefEntries = Object.entries(_rendererPrefs)
        .filter(([ext]) => ext !== '*')
        .map(([ext, rendererId]) => ({
            ext: caseFold(ext),
            renderer: findInCache(_rendererCache, rendererId),
        }));
    const extEntries: ResolvedEntry[] = [];
    for (const [id, info] of Object.entries(_rendererCache)) {
        const defaults = new Set(info.defaultFor.map(caseFold));
        for (const ext of info.extensions) {
            const folded = caseFold(ext);
            extEntries.push({ ext: folded, renderer: { id, ...info }, isDefault: defaults.has(folded) });
        }
    }
    _extEntries = extEntries.sort((a, b) => b.ext.length - a.ext.length || Number(!!b.isDefault) - Number(!!a.isDefault));
    _anyTextRenderers = Object.entries(_rendererCache)
        .filter(([, info]) => info.defaultFor.includes(ANY_TEXT))
        .map(([id, info]) => ({ id, ...info }));
    const wildcardId = _rendererPrefs['*'];
    _wildcardRenderer = wildcardId ? findInCache(_rendererCache, wildcardId) : null;
}

async function fetchRendererExtensions(): Promise<Record<string, RendererInfo>> {
    const base = getApiUrl().replace(/\/api\/?$/, '');
    const res = await fetch(`${base}/api/renderers`);
    if (!res.ok) throw new Error(`Renderer catalog: HTTP ${res.status}`);
    const manifests: Array<{ id: string; name: string; extensions: string[]; default_for?: string[]; supports_diff: boolean; live_refresh?: boolean; embed?: string; content_hash?: string }> = await res.json();
    if (!Array.isArray(manifests)) throw new Error('Invalid renderer catalog');
    const cache: Record<string, RendererInfo> = {};
    for (const m of manifests) {
        if (!m.id) continue;
        cache[m.id] = {
            name: m.name ?? m.id,
            extensions: Array.isArray(m.extensions) ? m.extensions.filter(ext=>typeof ext==='string') : [],
            defaultFor: Array.isArray(m.default_for) ? m.default_for.filter(entry=>typeof entry==='string') : [],
            supportsDiff: m.supports_diff ?? false,
            liveRefresh: m.live_refresh ?? false,
            embed: (m.embed === 'native' ? 'native' : 'iframe') as 'iframe' | 'native',
            contentHash: m.content_hash ?? '',
        };
    }
    return cache;
}

async function fetchPrefs(): Promise<Record<string, string>> {
    const base = getApiUrl().replace(/\/api\/?$/, '');
    const res = await fetch(`${base}/api/renderer_preferences`);
    if (!res.ok) throw new Error(`Renderer preferences: HTTP ${res.status}`);
    const body = await res.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('Invalid renderer preferences');
    return body;
}

async function fetchHandlers():Promise<HandlerRendering[]> {
    const base=getApiUrl().replace(/\/api\/?$/, '');
    const response=await fetch(`${base}/api/handlers`);
    if(!response.ok)throw new Error(`Renderer handlers: HTTP ${response.status}`);
    const value=await response.json();
    if(!Array.isArray(value))throw new Error('Invalid renderer handlers');
    return value;
}

export function refreshRendererCache(): Promise<void> {
    if (_refreshInFlight) {
        _refreshAgain = true;
        return _refreshInFlight;
    }
    _registryError = null;
    for (const cb of _rendererListeners) cb();
    _refreshInFlight = (async () => {
        try {
            do {
                _refreshAgain = false;
                const [fresh, prefs, handlers] = await Promise.all([fetchRendererExtensions(), fetchPrefs(), fetchHandlers()]);
                const oldJson = JSON.stringify(_rendererCache) + JSON.stringify(_rendererPrefs) + JSON.stringify(_rendererHandlers);
                const newJson = JSON.stringify(fresh) + JSON.stringify(prefs) + JSON.stringify(handlers);
                if (oldJson !== newJson) {
                    _rendererCache = fresh;
                    _rendererPrefs = prefs;
                    _rendererHandlers = handlers;
                    setRendererHandlers(handlers);
                    rebuildResolveIndex();
                }
            } while (_refreshAgain);
            _registryReady = true;
        } catch (error) {
            if (!_registryReady) _registryError = String((error as Error)?.message ?? error);
            console.error('Failed to refresh renderer registry', error);
        } finally {
            _refreshInFlight = null;
            for (const cb of _rendererListeners) cb();
        }
    })();
    return _refreshInFlight;
}

// Start resolution on module load. Views wait for its authoritative result;
// a fallback must not become editable before its registered hash is known.
refreshRendererCache();
// Re-fetch when a Capabilities cell changes.
useCollimateStore.subscribe(s => s._capabilitiesFingerprint, () => refreshRendererCache());
// Re-fetch when user saves renderer preferences.
if (typeof window !== 'undefined') {
    window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, () => refreshRendererCache());
}

function subscribeRenderers(cb: () => void): () => void {
    _rendererListeners.add(cb);
    return () => { _rendererListeners.delete(cb); };
}

// Image and PDF files no renderer is the default for, and other binary
// files, open in the app's own binary view ('Cannot display filetype'); a
// text file no renderer claims opens in the built-in text renderer, always
// registered once the registry loads. A shared frozen object, so fallback
// resolutions are referentially stable.
const BINARY_RENDERER: ResolvedRenderer = Object.freeze({ id: 'binary', name: 'binary', extensions: [], defaultFor: [], supportsDiff: false, liveRefresh: false, embed: 'iframe', contentHash: '' });

export const DEFINITION_RENDERERS: ResolvedRenderer[] = [
    ['renderer', 'Renderer'], ['generator', 'Generator'], ['runner', 'Runner'], ['handler', 'Handler'], ['options', 'Options'],
].map(([kind, name]) => Object.freeze({ id: `definition-${kind}`, name: `${name} definition`, extensions: [kind === 'options' ? '.options_stack' : `.${kind}`], defaultFor: [], supportsDiff: false, liveRefresh: true, embed: 'native' as const, contentHash: '' }));

/** The renderer that is the default for the file's longest claimed
 *  extension (`default_for`), or null. */
function defaultClaimant(lower: string): ResolvedRenderer | null {
    return _extEntries.find(e => e.isDefault && e.renderer && lower.endsWith(e.ext))?.renderer ?? null;
}

export function resolveRenderer(filename: string): ResolvedRenderer {
    const real = caseFold(filename);
    const presentation = rendererFilename(filename);
    const lower = caseFold(presentation);
    // An explicit real-extension preference wins before alias resolution.
    if (presentation !== filename) {
        for (const e of _prefEntries) if (e.renderer && real.endsWith(e.ext)) return e.renderer;
    }
    const definition = DEFINITION_RENDERERS.find(r => r.extensions.some(ext => lower.endsWith(ext)));
    if (definition) return definition;
    // An image or a PDF has no text to fall back to: it opens in the renderer
    // that is the default for its extension, before any preference, and in
    // the binary view when the channel has none.
    if (IMAGE_EXTS.some(ext => lower.endsWith(ext)) || PDF_EXTS.some(ext => lower.endsWith(ext))) {
        return defaultClaimant(lower) ?? BINARY_RENDERER;
    }
    if (BINARY_EXTS.some(ext => lower.endsWith(ext))) return BINARY_RENDERER;
    for (const e of _prefEntries) {
        if (e.renderer && lower.endsWith(e.ext)) return e.renderer;
    }
    for (const e of _extEntries) {
        if (e.renderer && lower.endsWith(e.ext)) return e.renderer;
    }
    if (_wildcardRenderer) return _wildcardRenderer;
    // The floor: the app's built-in text renderer (builtins/text).
    return findInCache(_rendererCache, 'text') ?? BINARY_RENDERER;
}

/** Every renderer that can open this file, primary resolution FIRST: the
 *  registry renderers whose extensions match, then every renderer that is a
 *  default for any text file (`default_for: ['*']`, registry order: a
 *  channel's code editor before the built-in text renderer). Those are
 *  withheld from image/binary files — they have no text content to hand an
 *  editor. Powers the "Open with" menus (a menu is only worth showing when
 *  this returns more than one entry) and the Files panel's Open with step. */
export function rendererCandidatesFor(filename: string): ResolvedRenderer[] {
    const presentation=rendererFilename(filename);
    const lower = caseFold(presentation);
    const out: ResolvedRenderer[] = [resolveRenderer(filename)];
    const seen = new Set(out.map(r => r.id));
    const push = (r: ResolvedRenderer | null) => {
        if (r && !seen.has(r.id)) { seen.add(r.id); out.push(r); }
    };
    for (const renderer of DEFINITION_RENDERERS) if (renderer.extensions.some(ext => lower.endsWith(ext))) push(renderer);
    for (const e of _extEntries) {
        if (e.renderer && lower.endsWith(e.ext)) push(e.renderer);
    }
    const binaryish = IMAGE_EXTS.some(ext => lower.endsWith(ext))
        || PDF_EXTS.some(ext => lower.endsWith(ext))
        || BINARY_EXTS.some(ext => lower.endsWith(ext));
    if (!binaryish) {
        // The built-in plain text renderer is one of these, last: a broken
        // fancy renderer never leaves the user without a way to see the bytes.
        for (const renderer of _anyTextRenderers) push(renderer);
    }
    return out;
}

/** Every renderer in the registry, by name then id: the Files panel's Open
 *  with step offers those that do not claim the file too. The app's own
 *  definition and binary views are no registry entries; they appear only
 *  where rendererCandidatesFor lists them. */
export function allRenderers(): ResolvedRenderer[] {
    return Object.entries(_rendererCache)
        .map(([id, info]) => ({ id, ...info }))
        .sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

/** Resolve with an explicit "Open with" override riding on a tab. Falls back
 *  to the primary resolution when the override no longer exists (its cell
 *  moved off Capabilities, or the renderer was deleted) — the tab keeps
 *  rendering rather than erroring. */
export function resolveRendererWithOverride(filename: string, overrideId?: string): ResolvedRenderer {
    const primary = resolveRenderer(filename);
    if (!overrideId || overrideId === primary.id) return primary;
    return findInCache(_rendererCache, overrideId) ?? primary;
}

/** Distinct file extensions known to the installed renderers, each annotated
 *  with the renderer names that handle it. Sorted. Powers the add-file
 *  dialog's extension picker. */
export function getSupportedExtensions(): { ext: string; renderers: string[] }[] {
    const map = new Map<string, Set<string>>();
    for (const info of Object.values(_rendererCache)) {
        for (const ext of info.extensions) {
            const e = caseFold(ext);
            if (!map.has(e)) map.set(e, new Set());
            map.get(e)!.add(info.name);
        }
    }
    for (const renderer of DEFINITION_RENDERERS) for (const ext of renderer.extensions) {
        if (!map.has(ext)) map.set(ext,new Set());
        map.get(ext)!.add(renderer.name);
    }
    for (const ext of handlerExtensions()) {
        if (!map.has(ext)) map.set(ext,new Set());
        map.get(ext)!.add(resolveRenderer('file'+ext).name);
    }
    return [...map.entries()]
        .map(([ext, rs]) => ({ ext, renderers: [...rs].sort() }))
        .sort((a, b) => a.ext.localeCompare(b.ext));
}

/** Reactive variant of getSupportedExtensions — refreshes when the renderer
 *  cache changes (e.g. a Capabilities cell's renderer is edited). */
export function useSupportedExtensions(): { ext: string; renderers: string[] }[] {
    const [exts, setExts] = useState(() => getSupportedExtensions());
    useEffect(() => {
        const update = () => setExts(getSupportedExtensions());
        update();
        return subscribeRenderers(update);
    }, []);
    return exts;
}

function sameRenderer(a: ResolvedRenderer, b: ResolvedRenderer): boolean {
    return a.id === b.id && a.name === b.name && a.embed === b.embed && a.contentHash === b.contentHash
        && a.supportsDiff === b.supportsDiff && a.liveRefresh === b.liveRefresh;
}

/** Resolve the renderer for a filename, kept in sync with cache + pref
 *  invalidations so a user mid-session switching the .md default from one
 *  renderer to another is reflected immediately in any open editor.
 *  `overrideId` is a tab's "Open with" choice; see resolveRendererWithOverride. */
export interface RendererResolution {
    renderer: ResolvedRenderer | null;
    error: string | null;
}

/** The registry's answer for a name now (module state, read synchronously). */
function resolveNow(filename: string, overrideId?: string): RendererResolution {
    return { renderer: _registryReady ? resolveRendererWithOverride(filename, overrideId) : null, error: _registryError };
}

/** `next`, or `prev` when it says the same: a registry refresh that changes
 *  nothing keeps the caller's renderer object. */
function settled(prev: RendererResolution, next: RendererResolution): RendererResolution {
    const equal = prev.renderer === next.renderer || (prev.renderer && next.renderer && sameRenderer(prev.renderer, next.renderer));
    return equal && prev.error === next.error ? prev : next;
}

export function useRendererId(filename: string, overrideId?: string): RendererResolution {
    const [held, setHeld] = useState(() => ({ filename, overrideId, resolution: resolveNow(filename, overrideId) }));
    // A new name resolves as this renders, never from an effect: an effect's
    // second render for each keystroke of a typed name (a new file's) piles
    // up behind fast typing until React stops it as an update loop.
    let current = held;
    if (held.filename !== filename || held.overrideId !== overrideId) {
        current = { filename, overrideId, resolution: settled(held.resolution, resolveNow(filename, overrideId)) };
        setHeld(current);
    }
    // A registry change re-resolves the name held (one that landed before
    // this subscribed too).
    useEffect(() => {
        const update = () => setHeld(prev => {
            const resolution = settled(prev.resolution, resolveNow(prev.filename, prev.overrideId));
            return resolution === prev.resolution ? prev : { ...prev, resolution };
        });
        update();
        return subscribeRenderers(update);
    }, []);
    return current.resolution;
}
