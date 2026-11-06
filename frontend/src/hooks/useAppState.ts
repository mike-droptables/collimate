import { useEffect, useCallback } from 'react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { Channel, ChannelLayout } from '@/lib/types';
import { fetchPickSessions, postClientHandshake } from '@/lib/api';
import { describeClient } from '@/lib/desktop';

export const useAppState = () => {
    const state = useCollimateStore((s) => s.state);
    const activeChannel = useCollimateStore((s) => s.activeChannel);
    const hasInitialized = useCollimateStore((s) => s.hasInitialized);
    const apiError = useCollimateStore((s) => s.apiError);
    const fetchState = useCollimateStore((s) => s.fetchState);
    const applyOptimisticChannel = useCollimateStore((s) => s.applyOptimisticChannel);
    const applyOptimisticLayout = useCollimateStore((s) => s.applyOptimisticLayout);

    const refresh = useCallback(async () => {
        await fetchState();
    }, [fetchState]);

    useEffect(() => {
        // Single initial fetch — SSE handles all subsequent updates
        fetchState();
    }, [fetchState]);

    // Tell the server what kind of client this is, once per page load.
    // Fire-and-forget: nothing in the UI waits on it, and a failure just
    // leaves the server on its COLLIMATE_CLIENT env seed. Browser tabs post
    // too — a tab opened against a shell-spawned server has to correct the
    // seed, or generators will be told they have a native guest view.
    useEffect(() => {
        postClientHandshake(describeClient()).catch(() => { /* best effort */ });
    }, []);

    // Cell panel sessions (runners, files, handlers) are channel-scoped
    // server state (the channel DB's ui_state KV) — hydrate the map
    // whenever the active channel becomes known. The store dropped its
    // copy on channel switch; hydration merges by `at` so a session the
    // user opened before this lands isn't clobbered. The immutable UUID
    // captured here is also checked by hydration before adopting the result.
    useEffect(() => {
        const channel = activeChannel?.id;
        if (!channel || !hasInitialized) return;
        let cancelled = false;
        fetchPickSessions(channel)
            .then((sessions) => {
                if (!cancelled) useCollimateStore.getState().hydratePickSessions(channel, sessions ?? {});
            })
            .catch(() => { /* best effort — an empty map is a fine start */ });
        return () => { cancelled = true; };
    }, [activeChannel?.id, hasInitialized]);

    useEffect(() => {
        if(activeChannel?.id && hasInitialized)void useCollimateStore.getState().hydrateCellDisplays();
    }, [activeChannel?.id, hasInitialized]);

    const performAction = useCallback(async (action: Promise<unknown>) => {
        try {
            const result = await action;

            // Fast Path: If result is a Channel, apply optimistically
            // The SSE stream will deliver the authoritative state shortly after
            if (result && typeof result === 'object' && 'id' in result && 'main_stacks' in result) {
                const channel = result as Channel | ChannelLayout;
                const hasLayoutCells = [...channel.main_stacks, ...channel.nav_stacks]
                    .some(stack => stack.cells.some(cell => !('files' in cell)));
                if (hasLayoutCells) applyOptimisticLayout(channel);
                else applyOptimisticChannel(channel as Channel);
            }
            // No fallback fetch needed — SSE will push the update
            return result;
        } catch (e: unknown) {
            console.error("Action failed", e);
            // On error, do a one-time fetch to resync
            await fetchState();
            return null;
        }
    }, [applyOptimisticChannel, applyOptimisticLayout, fetchState]);

    return {
        state,
        activeChannel,
        refresh,
        performAction,
        hasInitialized,
        apiError,
    };
};
