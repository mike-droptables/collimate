import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, describeApiError } from './client';
import {
    addLocation, archiveChannel, checkLocation, claimLocation, clearScratch, collectNow, deleteDockerImages, deleteRunEnvironments,
    fetchChannelSize, fetchLocations, fetchManagedChannels, fetchServerInstance, fetchStorageJob, fetchStorageOverview, moveChannel,
    previewDockerImages, previewRunEnvironments, pruneUvCache, refreshStorage, refusalCode, refusalDetail, removeLocation,
    setDefaultLocation, unarchiveChannel,
} from './storage';
import { archivedRefusalMessage } from '../channelArchived';

describe('the Locations and Storage routes (plan 1105 P8)', () => {
    afterEach(() => vi.restoreAllMocks());

    it('call each route with its method, path and body', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: {} });
        await fetchLocations();
        await checkLocation('/srv/ch');
        await addLocation({ path: '/srv/ch', claim: true, confirm_unchecked: true });
        await claimLocation('l 1');
        await claimLocation('l1', true);
        await setDefaultLocation('l1');
        await removeLocation('l1');
        await moveChannel('c1', 'l2');
        await fetchStorageJob('j1');
        await fetchStorageOverview();
        await refreshStorage();
        await clearScratch();
        await collectNow();
        await previewDockerImages({ mode: 'unused', days: 30 });
        await deleteDockerImages('p1');
        await previewRunEnvironments({ mode: 'all' });
        await deleteRunEnvironments('p2');
        await pruneUvCache();
        expect(request.mock.calls.map(([config]) => [config.method, config.url, config.data])).toEqual([
            ['GET', '/storage/locations', undefined],
            ['POST', '/storage/locations/check', { path: '/srv/ch' }],
            ['POST', '/storage/locations', { path: '/srv/ch', claim: true, confirm_unchecked: true }],
            ['POST', '/storage/locations/l%201/claim', { confirm_unchecked: false }],
            ['POST', '/storage/locations/l1/claim', { confirm_unchecked: true }],
            ['PUT', '/storage/locations/l1/default', undefined],
            ['DELETE', '/storage/locations/l1', undefined],
            ['POST', '/channels/c1/move', { location_id: 'l2' }],
            ['GET', '/storage/jobs/j1', undefined],
            ['GET', '/storage', undefined],
            ['POST', '/storage/refresh', undefined],
            ['POST', '/storage/scratch/clear', undefined],
            ['POST', '/storage/collect', undefined],
            ['POST', '/storage/docker/preview', { mode: 'unused', days: 30 }],
            ['POST', '/storage/docker/delete', { preview_id: 'p1' }],
            ['POST', '/storage/envs/preview', { mode: 'all' }],
            ['POST', '/storage/envs/delete', { preview_id: 'p2' }],
            ['POST', '/storage/uv/prune', undefined],
        ]);
    });

    it('read a coded refusal’s code and extra fields; a plain sentence has none', () => {
        const coded = Object.assign(new Error('x'), { response: { status: 409, data: { detail: { code: 'owner_running', message: 'runs', owner_data_dir: '/d' } } } });
        expect(refusalCode(coded)).toBe('owner_running');
        expect(refusalDetail(coded)).toMatchObject({ owner_data_dir: '/d' });
        expect(refusalCode(Object.assign(new Error('x'), { response: { status: 409, data: { detail: 'plain' } } }))).toBeNull();
        expect(refusalCode(new Error('offline'))).toBeNull();
    });
});

// The channels page's routes (plan 1105 API table), against the axios client.
describe('the channel management routes (plan 1105 P7)', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads the manage listing, the size and the server instance with GET', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: { channels: [] } });
        await fetchManagedChannels();
        await fetchChannelSize('a b');
        await fetchServerInstance();
        expect(request.mock.calls.map(([config]) => [config.method, config.url])).toEqual([
            ['GET', '/channels/manage'],
            ['GET', '/channels/a%20b/size'],
            ['GET', '/system/instance'],
        ]);
    });

    it('archives and unarchives with POST, answering the listing row', async () => {
        const row = { id: 'c1', name: 'One', archived: true, location_id: 'l', path: '/x/c1', state: 'ok', active: false };
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: row });
        expect(await archiveChannel('c1')).toEqual(row);
        await unarchiveChannel('c1');
        expect(request.mock.calls.map(([config]) => [config.method, config.url])).toEqual([
            ['POST', '/channels/c1/archive'],
            ['POST', '/channels/c1/unarchive'],
        ]);
    });
});

describe('the archived-channel refusal (plan 1105 S6)', () => {
    const refusal = (detail: unknown, status = 409) => Object.assign(new Error('x'), { response: { status, data: { detail } } });

    it('reads the coded 409 as its message; other refusals are not it', () => {
        const message = 'This channel is archived and read-only. Unarchive it to change it.';
        expect(archivedRefusalMessage(refusal({ code: 'channel_archived', message }))).toBe(message);
        expect(archivedRefusalMessage(refusal({ code: 'channel_moving', message: 'moving' }))).toBeNull();
        expect(archivedRefusalMessage(refusal({ code: 'channel_archived', message }, 400))).toBeNull();
        expect(archivedRefusalMessage(refusal('A channel named x exists'))).toBeNull();
        expect(archivedRefusalMessage(new Error('offline'))).toBeNull();
    });

    it('describes a coded detail by its message', () => {
        expect(describeApiError(refusal({ code: 'channel_archived', message: 'Read-only.' }))).toBe('Read-only.');
        expect(describeApiError(refusal('plain'))).toBe('plain');
    });
});
