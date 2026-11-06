// Per-channel settings loader — the frontend face of the channel's
// settings document (GET/PUT /api/channels/{c}/settings).
//
// Responsibilities:
//   * load the active channel's doc on channel switch (subscription below)
//   * fan the loaded sections out to their consumer stores (keybindings,
//     editing preferences, renderer styles; ThemeProvider subscribes to `doc.theme` itself
//     via the hook)
//   * re-load after an external write (a settings JSON import replaces the
//     doc server-side behind the stores' backs)
//   * retry a failed load, with backoff, while its channel stays active
//
// SAVING is decentralized: each consumer store PUTs only its own section
// (putChannelSettings is section-wholesale) to `loadedFor`, so this store's
// `doc` is a load-time snapshot, not a live mirror of every section. A
// writer whose section is READ from the snapshot (the stack width default)
// merges it in with `patchDoc`, which fans nothing out: re-adopting the
// snapshot's other sections would revert every edit made since the load.

import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import { ChannelSettings, fetchChannelSettings } from '@/lib/api';
import { writeBootCache } from '@/lib/settingsCache';
import { useCollimateStore } from '@/stores/useCollimateStore';

interface ChannelSettingsState {
    /** The most recently loaded doc (null before the first load). */
    doc: ChannelSettings | null;
    /** Channel UUID `doc` belongs to — the channel every section writer
     *  saves to. Null before the first load and from the start of a switch
     *  until the new channel's load lands (a failing one retries): the
     *  writers then update only the boot cache, and `doc` keeps painting. */
    loadedFor: string | null;
    /** Fetch + adopt the given channel's settings. Idempotent per channel:
     *  while the active channel's load is in flight, a non-forced call for
     *  it joins that load. Pass `force` to re-fetch (external write). A
     *  failure for the active channel retries on its own (LOAD_RETRY_MS). */
    loadForChannel: (channelId: string, force?: boolean) => Promise<void>;
    /** Merge sections just saved to `channelId` into `doc`, when `doc` is
     *  that channel's. No fan-out, and every other section keeps its
     *  identity, so no consumer re-adopts the load-time snapshot. */
    patchDoc: (channelId: string, partial: Partial<ChannelSettings>) => void;
    /** Re-fetch the active channel's doc after a server-side write. */
    reloadAfterExternalWrite: () => Promise<void>;
}

/** Delays between retries of a failed load while its channel stays active.
 *  Past the last one, the next switch to the channel (or an external-write
 *  reload) tries again. */
export const LOAD_RETRY_MS = [1000, 2000, 4000, 8000, 16000] as const;

// At most one retry is pending: any load (a switch, a forced reload, the
// retry itself) supersedes it. Of the loads issued for the active channel
// only the latest applies its result, so an older GET landing late (a retry
// overtaken by a forced reload) cannot re-apply the doc it read. A
// non-forced load of the channel the latest load is still fetching joins it
// instead of superseding it (every editor mount nudges a load while a switch
// lands), so a later failing GET cannot throw away an earlier good one.
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let loadSeq = 0;
let latestLoad: { channel: string; seq: number } | null = null;
let inFlight: { channel: string; done: Promise<void> } | null = null;
function cancelRetry() {
    if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
    }
}

function fanOut(doc: ChannelSettings, isCurrent: () => boolean) {
    // Lazy imports keep this store free of load-order coupling; the consumer
    // stores reach back here (for `loadedFor`) through lazy imports of their
    // own.
    void import('@/lib/keybindings/store').then(({ useKeybindingsStore }) => {
        if (!isCurrent()) return;
        const keys = useKeybindingsStore.getState();
        keys.adoptOverrides(doc.keyboard?.overrides ?? {});
        keys.adoptCommandBindings(doc.keyboard?.commands ?? {});
    });
    void import('@/stores/useEditingStore').then(({ useEditingStore }) => {
        if (!isCurrent()) return;
        useEditingStore.getState().adoptEditing(doc.editing);
    });
    void import('@/stores/useRendererStylesStore').then(({ useRendererStylesStore }) => {
        if (!isCurrent()) return;
        useRendererStylesStore.getState().adoptRendererStyles(doc.renderer_styles);
    });
    // ThemeProvider reacts to `doc.theme` via useChannelSettingsStore.
    writeBootCache({
        theme: doc.theme, keyboard: doc.keyboard, editing: doc.editing, renderer_styles: doc.renderer_styles,
    });
}

export const useChannelSettingsStore = create<ChannelSettingsState>()(
    subscribeWithSelector((set, get) => {
        const isActive = (channelId: string) => {
            const activeNow = useCollimateStore.getState().activeChannel?.id;
            return activeNow === channelId;
        };

        const load = (channelId: string, force: boolean, attempt: number): Promise<void> => {
            if (!channelId) return Promise.resolve();
            if (!force && get().loadedFor === channelId) return Promise.resolve();
            if (!force && inFlight?.channel === channelId) return inFlight.done;
            const seq = ++loadSeq;
            const active = isActive(channelId);
            if (active) {
                cancelRetry();
                latestLoad = { channel: channelId, seq };
                // The previous channel's doc must not take this channel's
                // edits while its load is in flight or failing: drop
                // `loadedFor` (writers go cache-only, `doc` stays for the
                // paint). A forced reload of the loaded channel keeps it:
                // `doc` is that channel's.
                if (get().loadedFor !== channelId) set({ loadedFor: null });
            }
            const done = fetchAndApply(channelId, force, attempt, seq);
            if (active) {
                const entry = { channel: channelId, done };
                inFlight = entry;
                const settle = () => { if (inFlight === entry) inFlight = null; };
                void done.then(settle, settle);
            }
            return done;
        };

        const fetchAndApply = async (channelId: string, force: boolean, attempt: number, seq: number): Promise<void> => {
            const superseded = () => latestLoad?.channel !== channelId || latestLoad.seq !== seq;
            let doc: ChannelSettings;
            try {
                doc = await fetchChannelSettings(channelId);
            } catch {
                // Transient failure, or the channel just went away. Nothing
                // to do when a newer load was issued or the user already
                // left; otherwise retry while the channel stays active.
                if (superseded() || !isActive(channelId)) return;
                const delay = LOAD_RETRY_MS[attempt];
                if (delay === undefined) return;
                cancelRetry();
                retryTimer = setTimeout(() => {
                    retryTimer = null;
                    if (useCollimateStore.getState().activeChannel?.id !== channelId) return;
                    void load(channelId, force, attempt + 1);
                }, delay);
                return;
            }
            // A newer load supersedes this one, and a slower response for a
            // channel the user already switched away from must not clobber
            // the newer channel's settings.
            if (superseded() || !isActive(channelId)) return;
            set({ doc, loadedFor: channelId });
            fanOut(doc, () => !superseded() && isActive(channelId));
        };

        return {
            doc: null,
            loadedFor: null,

            loadForChannel: (channelId, force = false) => load(channelId, force, 0),

            patchDoc: (channelId, partial) => {
                const { doc, loadedFor } = get();
                if (!doc || loadedFor !== channelId) return;
                set({ doc: { ...doc, ...partial } });
            },

            reloadAfterExternalWrite: async () => {
                const channelId = useCollimateStore.getState().activeChannel?.id;
                if (channelId) await get().loadForChannel(channelId, true);
            },
        };
    }),
);

// Load on channel switch (and once at boot when a channel is already
// active). Module-level so every consumer shares one subscription.
if (typeof window !== 'undefined') {
    useCollimateStore.subscribe(
        (s) => s.activeChannel?.id,
        (channelId) => {
            cancelRetry();
            latestLoad = null;
            inFlight = null;
            useChannelSettingsStore.setState({ loadedFor: null });
            if (channelId) void useChannelSettingsStore.getState().loadForChannel(channelId);
        },
        { fireImmediately: true },
    );
}
