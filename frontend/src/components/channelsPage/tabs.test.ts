import { describe, expect, it } from 'vitest';
import { CHANNELS_TABS, channelsTabFromHash, channelsTabsFor, offeredTab } from './tabs';

describe('the channels page tabs (plan 1105 P7, P8)', () => {
    it('are New, Channels, Archived, Locations, Storage, Servers in the desktop shell only, then Settings last', () => {
        expect(CHANNELS_TABS.map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage', 'servers', 'settings']);
        expect(channelsTabsFor(true).map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage', 'servers', 'settings']);
        expect(channelsTabsFor(false).map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage', 'settings']);
        expect(CHANNELS_TABS.at(-1)).toEqual({ id: 'settings', label: 'Settings' });
        expect(offeredTab('servers', false)).toBe('new');
        expect(offeredTab('servers', true)).toBe('servers');
        expect(offeredTab('archived', false)).toBe('archived');
        expect(offeredTab('storage', false)).toBe('storage');
        expect(offeredTab('settings', false)).toBe('settings');
        expect(offeredTab('settings', true)).toBe('settings');
    });

    it('route from #new-channel and #channels/<tab>; anything else is no request', () => {
        expect(channelsTabFromHash('#new-channel')).toBe('new');
        expect(channelsTabFromHash('#channels/archived')).toBe('archived');
        expect(channelsTabFromHash('#channels/servers')).toBe('servers');
        expect(channelsTabFromHash('#channels/storage')).toBe('storage');
        expect(channelsTabFromHash('#channels/locations')).toBe('locations');
        expect(channelsTabFromHash('#channels/settings')).toBe('settings');
        expect(channelsTabFromHash('#channels/trash')).toBeNull();
        expect(channelsTabFromHash('#channels/')).toBeNull();
        expect(channelsTabFromHash('')).toBeNull();
    });
});
