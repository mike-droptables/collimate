import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import {
    duplicateCell, fetchCellProvenance, fetchChannelOutline, fetchChannelTags, fetchJournal, fetchLineage, fetchRemovedCells, patchCellMeta,
    putRemovedCellTags, removeCellById, restoreRemovedCell,
} from './cells';

// The overview's clients against the routes of the W5 backend record.
describe('the overview clients', () => {
    afterEach(() => vi.restoreAllMocks());

    const capture = () => vi.spyOn(api, 'request').mockResolvedValue({ data: [] });

    it.each([
        ['the shelf', () => fetchRemovedCells('Q&A', { sort: 'created', dir: 'asc' }), 'GET', '/channels/Q%26A/cells/removed?sort=created&dir=asc', undefined],
        ['the shelf searched', () => fetchRemovedCells('c', { sort: 'edited', dir: 'desc', q: 'two words' }), 'GET', '/channels/c/cells/removed?sort=edited&dir=desc&q=two+words', undefined],
        ['restore', () => restoreRemovedCell('c', 'cell-1'), 'POST', '/channels/c/cells/cell-1/restore', undefined],
        ['remove by id', () => removeCellById('c', 'cell-1'), 'POST', '/channels/c/cells/cell-1/remove', undefined],
        ['duplicate by id', () => duplicateCell('c', 'cell-1', 'stack-2'), 'POST', '/channels/c/cells/cell-1/duplicate', { anchor_stack_id: 'stack-2' }],
        ['duplicate beside its own stack', () => duplicateCell('c', 'cell-1'), 'POST', '/channels/c/cells/cell-1/duplicate', { anchor_stack_id: null }],
        ['a cell\'s journal', () => fetchJournal('c', 'cell-1'), 'GET', '/channels/c/cells/cell-1/journal?limit=100', undefined],
        ['an older page', () => fetchJournal('c', 'cell-1', { before: '2026-10-06T14:05:00+00:00', limit: 50 }), 'GET', '/channels/c/cells/cell-1/journal?limit=50&before=2026-10-06T14%3A05%3A00%2B00%3A00', undefined],
        ['the All cells journal', () => fetchJournal('c', null), 'GET', '/channels/c/journal?limit=100', undefined],
        ['the lineage', () => fetchLineage('c'), 'GET', '/channels/c/lineage', undefined],
    ])('%s calls its route (O04)', async (_label, call, method, url, data) => {
        const request = capture();
        await call();
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method, url, data }));
    });
});

// Tags: the full list on the meta PATCH, the vocabulary route, the zone
// action on the remove body and the shelf's last-removed sort.
describe('the tag clients', () => {
    afterEach(() => vi.restoreAllMocks());

    const capture = () => vi.spyOn(api, 'request').mockResolvedValue({ data: [] });

    it.each([
        ['the full tag list on the meta patch', () => patchCellMeta('c', 'cell-1', { tags: ['a', 'B'] }), 'PATCH', '/channels/c/cells/cell-1/meta', { tags: ['a', 'B'] }],
        ['the vocabulary', () => fetchChannelTags('Q&A'), 'GET', '/channels/Q%26A/tags', undefined],
        ['a plain remove sends no body', () => removeCellById('c', 'cell-1'), 'POST', '/channels/c/cells/cell-1/remove', undefined],
        ['a remove that adds a tag', () => removeCellById('c', 'cell-1', { add_tags: ['done'] }), 'POST', '/channels/c/cells/cell-1/remove', { add_tags: ['done'] }],
        ['a remove that takes a tag off', () => removeCellById('c', 'cell-1', { remove_tags: ['todo'] }), 'POST', '/channels/c/cells/cell-1/remove', { remove_tags: ['todo'] }],
        ['the shelf by last removed', () => fetchRemovedCells('c', { sort: 'removed', dir: 'desc' }), 'GET', '/channels/c/cells/removed?sort=removed&dir=desc', undefined],
        ['a shelf cell\'s full tag list', () => putRemovedCellTags('Q&A', 'cell-1', ['a', 'B']), 'PUT', '/channels/Q%26A/cells/removed/cell-1/tags', { tags: ['a', 'B'] }],
    ])('%s calls its route (TG-02)', async (_label, call, method, url, data) => {
        const request = capture();
        await call();
        const sent = request.mock.calls[0][0];
        expect(sent).toEqual(expect.objectContaining({ method, url }));
        expect(sent.data).toEqual(data);
    });
});

// The new-channel page lists another channel's Capabilities cells and main
// stacks without freezing an export.
describe('the outline client', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads the channel outline route', async () => {
        const outline = {
            capabilities_cells: [{ id: 'g', name: 'Synth' }],
            main_stacks: [{ id: 's', name: 'S', cells: [] }],
        };
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: outline });
        expect(await fetchChannelOutline('Q&A')).toEqual(outline);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/channels/Q%26A/outline' }));
    });
});

describe('the provenance client', () => {
    afterEach(() => vi.restoreAllMocks());

    it('reads the cell’s provenance route', async () => {
        const provenance = { sdk_version: '0.1.0', records: [] };
        const request = vi.spyOn(api, 'request').mockResolvedValue({ data: provenance });
        expect(await fetchCellProvenance('Q&A', 'c 1')).toEqual(provenance);
        expect(request).toHaveBeenCalledWith(expect.objectContaining({ method: 'GET', url: '/channels/Q%26A/cells/c%201/provenance' }));
    });
});
