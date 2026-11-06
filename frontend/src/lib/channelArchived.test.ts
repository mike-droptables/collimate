import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, requestJson } from './api/client';
import { putChannelSettings, ChannelReadOnlyError } from './api/settings';
import { isReadOnlyChannel, listedChannels, onArchivedRefusal, setReadOnlyChannel } from './channelArchived';

// Archived channels on the client (plan 1105 S6, P7): the refusal listener,
// the read-only mirror and the list filter.
afterEach(() => { vi.restoreAllMocks(); setReadOnlyChannel(null); });

const archivedAdapter = async () => {
    throw Object.assign(new Error('Request failed with status code 409'), {
        response: { status: 409, data: { detail: { code: 'channel_archived', message: 'This channel is archived and read-only.' } } },
    });
};

describe('the archived-channel refusal', () => {
    it('reaches the one listener through the API client, with the server\'s sentence as the error', async () => {
        const heard = vi.fn();
        const off = onArchivedRefusal(heard);
        await expect(requestJson('/stacks/c1/s1/remove', 'POST', undefined, { adapter: archivedAdapter }))
            .rejects.toThrow('This channel is archived and read-only.');
        expect(heard).toHaveBeenCalledExactlyOnceWith('This channel is archived and read-only.');
        off();
        await expect(requestJson('/x', 'POST', undefined, { adapter: archivedAdapter })).rejects.toThrow();
        expect(heard).toHaveBeenCalledOnce();
    });

    it('stays quiet for any other failure', async () => {
        const heard = vi.fn();
        const off = onArchivedRefusal(heard);
        const adapter = async () => { throw Object.assign(new Error('x'), { response: { status: 409, data: { detail: 'Name taken' } } }); };
        await expect(requestJson('/x', 'POST', undefined, { adapter })).rejects.toThrow('Name taken');
        expect(heard).not.toHaveBeenCalled();
        off();
    });
});

describe('the read-only mirror', () => {
    it('names only the archived active channel', () => {
        expect(isReadOnlyChannel('c1')).toBe(false);
        setReadOnlyChannel('c1');
        expect(isReadOnlyChannel('c1')).toBe(true);
        expect(isReadOnlyChannel('c2')).toBe(false);
        expect(isReadOnlyChannel(null)).toBe(false);
    });

    it('keeps settings writes to it in this window: no request, a ChannelReadOnlyError', async () => {
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: {} });
        setReadOnlyChannel('c1');
        await expect(putChannelSettings('c1', { editing: { spellcheck: false } })).rejects.toBeInstanceOf(ChannelReadOnlyError);
        expect(request).not.toHaveBeenCalled();
        await putChannelSettings('c2', { editing: { spellcheck: false } });
        expect(request).toHaveBeenCalledOnce();
    });
});

it('a channel list leaves archived channels out', () => {
    expect(listedChannels([{ id: 'a' }, { id: 'b', archived: true }, { id: 'c', archived: false }]).map(c => c.id)).toEqual(['a', 'c']);
});
