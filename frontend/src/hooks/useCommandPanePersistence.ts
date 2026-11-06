import { useEffect } from 'react';
import { putChannelSettings } from '@/lib/api';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane, persistedCommandPane, type PersistedCommandPane } from '@/stores/collimate/commandPaneSlice';

/** Typing in the query saves once the keys pause. */
export const COMMAND_PANE_SAVE_MS = 400;

const samePane = (a: PersistedCommandPane | undefined, b: PersistedCommandPane) =>
    !!a && a.open === b.open && a.side === b.side && a.query === b.query
    && a.recents.length === b.recents.length && a.recents.every((k, i) => k === b.recents[i]);

/**
 * Keep the Commands pane's state in the channel's settings document
 * (`layout.commandPane`), per channel: adopt the saved state when the
 * channel's document loads, and save every change back to that channel.
 * The view is session state: a save carries `persistedCommandPane` (no
 * view), and a change of view alone writes nothing.
 * The layout section is replaced whole on PUT, so a save carries the
 * snapshot's other layout keys (the stack width default) with it.
 */
export function useCommandPanePersistence(): void {
    const loadedFor = useChannelSettingsStore(s => s.loadedFor);

    useEffect(() => {
        if (!loadedFor) return;
        const saved = useChannelSettingsStore.getState().doc?.layout?.commandPane;
        useCollimateStore.getState().adoptCommandPane(saved ?? defaultCommandPane());

        let timer: ReturnType<typeof setTimeout> | null = null;
        let pending: PersistedCommandPane | null = null;
        const save = () => {
            timer = null;
            const pane = pending;
            pending = null;
            if (!pane) return;
            const settings = useChannelSettingsStore.getState();
            // The doc snapshot is this channel's until another channel's
            // load lands (loadedFor is null in between).
            if (settings.loadedFor !== null && settings.loadedFor !== loadedFor) return;
            if (samePane(settings.doc?.layout?.commandPane, pane)) return;
            const layout = { defaultStackWidth: null, ...settings.doc?.layout, commandPane: pane };
            settings.patchDoc(loadedFor, { layout });
            void putChannelSettings(loadedFor, { layout }).catch(err => {
                console.warn('saving the Commands pane failed', err);
            });
        };
        const unsubscribe = useCollimateStore.subscribe(s => s.commandPane, pane => {
            pending = persistedCommandPane(pane);
            if (timer !== null) clearTimeout(timer);
            timer = setTimeout(save, COMMAND_PANE_SAVE_MS);
        });
        return () => {
            unsubscribe();
            if (timer !== null) {
                clearTimeout(timer);
                save();
            }
        };
    }, [loadedFor]);
}
