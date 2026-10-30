// The new-channel page's state, owned by ChannelCreateView and handed to
// its two tabs, so switching tabs never discards work or refetches. Each
// fetch the page makes lands here once, keyed by what it was for.

import type { ChannelOutline, ChannelTag } from '@/lib/api/cells';
import type { MainZip, PackagesZip } from '@/lib/api/channels';
import type { ChannelSettings } from '@/lib/api/settings';
import type { ImportCell } from '@/lib/api/transfer';
import type { SuggestedCatalog, SuggestedRunner } from '@/lib/api/suggested';

export type CreateTab = 'suggested' | 'config';

/** A fetch the page made, answered or failed; absent while under way. */
export type Fetched<T> =
    | { state: 'ready'; value: T }
    | { state: 'failed'; error: string };

export interface ChannelChoice { id: string; name: string }

export interface SuggestedState {
    /** The catalog is being read (from the page's open until the tab's
     *  first showing answers). */
    loading: boolean;
    catalog: SuggestedCatalog | null;
    /** The catalog request itself failed (the server, not GitHub). */
    requestError: string;
    pickedId: string;
    tutorial: boolean;
    drafts: SuggestedRunner[];
    /** The draft whose Delete awaits its confirmation. */
    confirming: number | null;
    /** The local-folder row, toggled by holding Refresh. */
    folderOpen: boolean;
    folderPath: string;
    folderBusy: boolean;
    folderError: string;
}

export type SourceMode = 'none' | 'channel' | 'file';

/** A file a Config section reads: under way, accepted, or refused. */
export type PickedFile<T> =
    | { status: 'reading'; name: string }
    | { status: 'ready'; name: string; value: T; summary: string }
    | { status: 'refused'; name: string; error: string };

export interface SectionState<T> {
    mode: SourceMode;
    channelId: string;
    file: PickedFile<T> | null;
}

/** A tag zip's cells and its root folder's name (the tag it was exported
 *  under, safe-named). */
export interface HistoryFile { root: string; cells: ImportCell[] }

export interface ConfigState {
    packages: SectionState<PackagesZip> & { expanded: boolean };
    settings: SectionState<ChannelSettings>;
    main: SectionState<MainZip>;
    /** `tag` is the picked tag of the From channel mode; `fileTag` the
     *  editable tag a tag zip's cells get. */
    history: SectionState<HistoryFile> & { tag: string; fileTag: string };
    outlines: Record<string, Fetched<ChannelOutline>>;
    tags: Record<string, Fetched<ChannelTag[]>>;
    settingsDocs: Record<string, Fetched<ChannelSettings>>;
}

export type ConfigSectionKey = 'packages' | 'settings' | 'main' | 'history';

export const SECTION_LABELS: Record<ConfigSectionKey, string> = {
    packages: 'Capabilities',
    settings: 'Settings',
    main: 'Main cells',
    history: 'History cells',
};

/** The tab a page opens on: Config when channels exist to copy from. */
export const initialTab = (channels: readonly ChannelChoice[]): CreateTab => channels.length ? 'config' : 'suggested';

export const initialSuggested = (): SuggestedState => ({
    loading: true, catalog: null, requestError: '', pickedId: '', tutorial: true,
    drafts: [], confirming: null, folderOpen: false, folderPath: '', folderBusy: false, folderError: '',
});

/** The Config tab's defaults: Capabilities and settings from the source
 *  channel (the one open, else the first) when channels exist;
 *  main and history cells none. */
export function initialConfig(channels: readonly ChannelChoice[], sourceChannelId: string | null): ConfigState {
    const source = channels.find(channel => channel.id === sourceChannelId)?.id ?? channels[0]?.id ?? '';
    const copied: SourceMode = source ? 'channel' : 'none';
    return {
        packages: { mode: copied, channelId: source, file: null, expanded: false },
        settings: { mode: copied, channelId: source, file: null },
        main: { mode: 'none', channelId: source, file: null },
        history: { mode: 'none', channelId: source, file: null, tag: '', fileTag: '' },
        outlines: {}, tags: {}, settingsDocs: {},
    };
}
