import { routeApiToFetchStub } from '@/test/apiTransport';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { checkpointCell, postRun } from './api';
import * as api from './api';

// Channel names are user-chosen display names: '&', '#', '?' and spaces are
// legal, so every channel path segment must be percent-encoded or the name
// gets cut at the '#' (fragment) or '?' (query string).
const NAME = 'Q&A #2';
const ENCODED = 'Q%26A%20%232';

describe('channel names in request URLs', () => {
    afterEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
    });

    it('postRun encodes the channel', async () => {
        const restore = routeApiToFetchStub();
        const fetchMock = vi.fn().mockResolvedValue(new Response('{"run_id":"r"}', { status: 200 }));
        vi.stubGlobal('fetch', fetchMock);
        try { await postRun(NAME, { runner: 'r', cellId: 'x' }); } finally { restore(); }
        expect(String(fetchMock.mock.calls[0][0])).toMatch(new RegExp(`/channels/${ENCODED}/runs$`));
    });

    const encodes = async (call: () => Promise<unknown>) => {
        const urls: string[] = [];
        vi.spyOn(XMLHttpRequest.prototype, 'open').mockImplementation(function (
            this: XMLHttpRequest, _method: string, url: string | URL,
        ) {
            urls.push(String(url));
            throw new Error('blocked');
        });
        await call().catch(() => undefined);
        expect(urls).toHaveLength(1);
        expect(urls[0]).toContain(`channels/${ENCODED}/`);
    };

    it('checkpointCell encodes the channel', () => encodes(() => checkpointCell(NAME, 'cell-1')));

    it.each([
        ['fetchRemovedCells', () => api.fetchRemovedCells(NAME, { sort: 'edited', dir: 'desc' })],
        ['restoreRemovedCell', () => api.restoreRemovedCell(NAME, 'cell-1')],
        ['removeCellById', () => api.removeCellById(NAME, 'cell-1')],
        ['duplicateCell', () => api.duplicateCell(NAME, 'cell-1')],
        ['fetchJournal', () => api.fetchJournal(NAME, 'cell-1')],
        ['fetchJournal (All cells)', () => api.fetchJournal(NAME, null)],
        ['fetchLineage', () => api.fetchLineage(NAME)],
    ])('the overview client %s encodes the channel (O04)', (_label, call) => encodes(call));
});


describe('the retired clients', () => {
    it('the DAG, verdict and add-to-main clients are gone with their routes (O02)', () => {
        for (const name of ['fetchCellDag', 'setCellVerdict', 'addCellToMain']) expect(api).not.toHaveProperty(name);
    });

    it('the open, duplicate-from-history and index-addressed remove clients are gone, replaced by the id-addressed ones (O04)', () => {
        for (const name of ['openChannelCell', 'duplicateFromHistory', 'removeCell']) expect(api).not.toHaveProperty(name);
        for (const name of ['restoreRemovedCell', 'duplicateCell', 'removeCellById']) expect(api).toHaveProperty(name);
    });
});

describe('explicit channel stack URLs (F0287)', () => {
    afterEach(() => vi.restoreAllMocks());

    it.each([
        ['remove', () => api.removeStack(NAME, 'stack')],
        ['minimize', () => api.minimizeStack(NAME, 'stack')],
        ['restore_minimized', () => api.restoreMinimizedStack(NAME, 'stack')],
        ['duplicate_cell_in_stack/2', () => api.duplicateCellInStack(NAME, 'stack', 2)],
        ['duplicate_stack', () => api.duplicateStack(NAME, 'stack')],
        ['move_cell_to/2/stack?cell_id=cell-1&tgt_cell_index=0', () => api.moveCellTo(NAME, 'stack', 2, 'stack', 'cell-1', 0)],
        ['move_stack_to/2/insert', () => api.moveStackTo(NAME, 'stack', 2, 'insert')],
        ['rename?name=Title', () => api.renameStack(NAME, 'stack', 'Title')],
        ['insert_blank_cell', () => api.insertBlankCell(NAME, 'stack')],
    ])('%s addresses the named channel (F0287)', async (suffix, call) => {
        const urls: string[] = [];
        vi.spyOn(XMLHttpRequest.prototype, 'open').mockImplementation(function (
            this: XMLHttpRequest, _method: string, url: string | URL,
        ) { urls.push(String(url)); throw new Error('blocked'); });
        await call().catch(() => undefined);
        expect(urls).toHaveLength(1);
        expect(urls[0]).toContain(`/stacks/${ENCODED}/stack/${suffix}`);
    });

    it.each([
        [`stacks/${ENCODED}/create`, () => api.createStack(NAME)],
    ])('%s needs no stack id (F0287)', async (path, call) => {
        const urls: string[] = [];
        vi.spyOn(XMLHttpRequest.prototype, 'open').mockImplementation(function (
            this: XMLHttpRequest, _method: string, url: string | URL,
        ) { urls.push(String(url)); throw new Error('blocked'); });
        await call().catch(() => undefined);
        expect(urls).toHaveLength(1);
        expect(urls[0]).toContain(`/${path}`);
    });
});
