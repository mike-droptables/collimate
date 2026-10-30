import { useEffect } from 'react';
import { putChannelSettings, type ChannelSettings, type RemoveZone } from '@/lib/api';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { SHELF_HEIGHT_DEFAULT } from '@/stores/collimate/overviewSlice';
import { settledZones, unsettledZone } from '@/components/overview/removeZones';

type Layout = NonNullable<ChannelSettings['layout']>;

/** A remove-zones edit (a divider drag, a tag typed) saves this long after
 *  the last change. */
export const REMOVE_ZONES_SAVE_MS = 300;

const sameZones = (a: readonly RemoveZone[], b: readonly RemoveZone[]) =>
    JSON.stringify(a) === JSON.stringify(b);

/**
 * Keep the overview's per-channel settings in the channel's settings
 * document: the Removed shelf's height (`layout.removedShelfHeight`), the
 * tag toggle (`layout.overviewShowTags`) and the remove zones
 * (`remove_zones`). Adopt them when the channel's document loads; save each
 * new height (a resize's end) and each toggle back to that channel, and
 * the zones REMOVE_ZONES_SAVE_MS after their last change (a pending save
 * still goes to its channel when the channel switches). A tag zone still
 * waiting for its tag is not saved (the server would drop it): the zones
 * save once it has one, the editor drops it on closing, and a channel
 * switch saves the zones without it. Until the next channel's document
 * lands the overview has no zones, so a drop in between is a plain remove.
 * The layout section is replaced whole on PUT, so a height or toggle save
 * carries the snapshot's other layout keys with it. A forced reload of the
 * same channel (a settings import) adopts the zones and the tag toggle it
 * brings (the height stays as it is).
 */
export function useOverviewPersistence(): void {
    const loadedFor = useChannelSettingsStore(s => s.loadedFor);

    useEffect(() => {
        if (!loadedFor) {
            useCollimateStore.getState().adoptRemoveZones([]);
            return;
        }
        const doc = useChannelSettingsStore.getState().doc;
        const saved: Layout | undefined = doc?.layout;
        const overview = useCollimateStore.getState();
        overview.adoptShelfHeight(saved?.removedShelfHeight ?? SHELF_HEIGHT_DEFAULT);
        overview.adoptShowTags(saved?.overviewShowTags ?? false);
        overview.adoptRemoveZones(doc?.remove_zones.zones ?? []);

        // The doc snapshot is this channel's until another channel's load
        // lands (loadedFor is null in between).
        const stillOurs = () => {
            const current = useChannelSettingsStore.getState().loadedFor;
            return current === null || current === loadedFor;
        };

        const stopHeight = useCollimateStore.subscribe(s => s.overview.shelfHeight, height => {
            const settings = useChannelSettingsStore.getState();
            if (!stillOurs()) return;
            const current: Layout | undefined = settings.doc?.layout;
            if (current?.removedShelfHeight === height) return;
            const layout: Layout = { defaultStackWidth: null, ...current, removedShelfHeight: height };
            settings.patchDoc(loadedFor, { layout });
            void putChannelSettings(loadedFor, { layout }).catch(err => {
                console.warn('saving the Removed shelf height failed', err);
            });
        });

        const stopShowTags = useCollimateStore.subscribe(s => s.overview.showTags, showTags => {
            const settings = useChannelSettingsStore.getState();
            if (!stillOurs()) return;
            const current: Layout | undefined = settings.doc?.layout;
            if ((current?.overviewShowTags ?? false) === showTags) return;
            const layout: Layout = { defaultStackWidth: null, ...current, overviewShowTags: showTags };
            settings.patchDoc(loadedFor, { layout });
            void putChannelSettings(loadedFor, { layout }).catch(err => {
                console.warn('saving the overview tag toggle failed', err);
            });
        });
        // A reread of this channel's document brings its toggle (a save has
        // already patched the doc to match, so it adopts nothing).
        const stopDocShowTags = useChannelSettingsStore.subscribe(s => s.doc?.layout?.overviewShowTags, reread => {
            if (useChannelSettingsStore.getState().loadedFor !== loadedFor) return;
            const showTags = reread ?? false;
            if (useCollimateStore.getState().overview.showTags === showTags) return;
            useCollimateStore.getState().adoptShowTags(showTags);
        });

        /** This channel's zones as last saved or read. */
        let savedZones: readonly RemoveZone[] = doc?.remove_zones.zones ?? [];
        let pending: RemoveZone[] | null = null;
        let timer: ReturnType<typeof setTimeout> | null = null;
        /** Save the pending zones; `leaving` (the channel switches): as the
         *  server would keep them, a tag zone with no tag dropped. */
        const saveZones = (leaving = false) => {
            if (timer !== null) clearTimeout(timer);
            timer = null;
            if (!pending || (!leaving && pending.some(unsettledZone))) return;
            const zones = settledZones(pending);
            pending = null;
            if (sameZones(savedZones, zones)) return;
            savedZones = zones;
            const settings = useChannelSettingsStore.getState();
            const section = { zones };
            settings.patchDoc(loadedFor, { remove_zones: section });
            void putChannelSettings(loadedFor, { remove_zones: section }).catch(err => {
                console.warn('saving the remove zones failed', err);
            });
        };
        const stopZones = useCollimateStore.subscribe(s => s.overview.removeZones, zones => {
            if (!stillOurs()) return;
            pending = zones;
            if (timer !== null) clearTimeout(timer);
            timer = setTimeout(() => saveZones(), REMOVE_ZONES_SAVE_MS);
        });
        // A reread of this channel's document brings its zones.
        const stopDoc = useChannelSettingsStore.subscribe(s => s.doc?.remove_zones.zones, reread => {
            if (useChannelSettingsStore.getState().loadedFor !== loadedFor) return;
            const zones = reread ?? [];
            savedZones = zones;
            if (sameZones(useCollimateStore.getState().overview.removeZones, zones)) return;
            if (timer !== null) clearTimeout(timer);
            timer = null;
            pending = null;
            useCollimateStore.getState().adoptRemoveZones(zones);
        });

        return () => {
            stopHeight();
            stopShowTags();
            stopDocShowTags();
            stopZones();
            stopDoc();
            saveZones(true);
        };
    }, [loadedFor]);
}
