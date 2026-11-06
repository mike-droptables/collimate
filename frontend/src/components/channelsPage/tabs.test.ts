import { describe, expect, it } from 'vitest';
import { CHANNELS_TABS, channelsTabFromHash, channelsTabsFor, offeredTab } from './tabs';

describe('the channels page tabs (plan 1105 P7, P8)', () => {
    it('are New, Channels, Archived, Locations, Storage, then Servers in the desktop shell only', () => {
        expect(CHANNELS_TABS.map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage', 'servers']);
        expect(channelsTabsFor(true).map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage', 'servers']);
        expect(channelsTabsFor(false).map(tab => tab.id)).toEqual(['new', 'channels', 'archived', 'locations', 'storage']);
        expect(offeredTab('servers', false)).toBe('new');
        expect(offeredTab('servers', true)).toBe('servers');
        expect(offeredTab('archived', false)).toBe('archived');
        expect(offeredTab('storage', false)).toBe('storage');
    });

    it('route from #new-channel and #channels/<tab>; anything else is no request', () => {
        expect(channelsTabFromHash('#new-channel')).toBe('new');
        expect(channelsTabFromHash('#channels/archived')).toBe('archived');
        expect(channelsTabFromHash('#channels/servers')).toBe('servers');
        expect(channelsTabFromHash('#channels/storage')).toBe('storage');
        expect(channelsTabFromHash('#channels/locations')).toBe('locations');
        expect(channelsTabFromHash('#channels/trash')).toBeNull();
        expect(channelsTabFromHash('#channels/')).toBeNull();
        expect(channelsTabFromHash('')).toBeNull();
    });
});
