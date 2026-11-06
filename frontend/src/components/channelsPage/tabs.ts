// The channels page's tabs (plan 1105 "The page"), in the recommended
// order: creating first, the tables, the locations, cleanup on its own tab
// (Storage), the Servers list last. Every reader below picks a tab up from
// this table; its body is ChannelsPage's.

/** A tab of the channels page. */
export type ChannelsTab = 'new' | 'channels' | 'archived' | 'locations' | 'storage' | 'servers';

export interface ChannelsTabDef {
    id: ChannelsTab;
    label: string;
    /** Offered only inside the desktop shell (the Servers list is the
     *  shell's registry). */
    desktopOnly?: boolean;
}

export const CHANNELS_TABS: readonly ChannelsTabDef[] = [
    { id: 'new', label: 'New' },
    { id: 'channels', label: 'Channels' },
    { id: 'archived', label: 'Archived' },
    { id: 'locations', label: 'Locations' },
    { id: 'storage', label: 'Storage' },
    { id: 'servers', label: 'Servers', desktopOnly: true },
];

/** The tabs a page offers: the desktop-only ones only with `desktop`. */
export function channelsTabsFor(desktop: boolean): ChannelsTabDef[] {
    return CHANNELS_TABS.filter(tab => desktop || !tab.desktopOnly);
}

/** The tab a page shows for a request: the one asked for when the page
 *  offers it, else New. */
export function offeredTab(tab: ChannelsTab, desktop: boolean): ChannelsTab {
    return channelsTabsFor(desktop).some(t => t.id === tab) ? tab : 'new';
}

/** The launch hash `#new-channel` (the shell's handoff) opens New;
 *  `#channels/<tab>` opens that tab. Anything else is no request. */
export function channelsTabFromHash(hash: string): ChannelsTab | null {
    if (hash === '#new-channel') return 'new';
    const match = /^#channels\/([a-z]+)$/.exec(hash);
    if (!match) return null;
    return CHANNELS_TABS.find(tab => tab.id === match[1])?.id ?? null;
}
