import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { FileConflict, FileDescriptor } from '@/lib/fileProtocol';
import type { Cell, VirtualFile } from '@/lib/types';

const h = vi.hoisted(() => ({
    requestJson: vi.fn(),
    docs: [] as { getSnapshot: () => { conflicts: FileConflict[] } }[],
    listeners: new Set<() => void>(),
    acquire: vi.fn(),
    note: vi.fn(),
}));
vi.mock('@/lib/api/client', () => ({ requestJson: h.requestJson }));
vi.mock('@/lib/fileDocuments', () => ({
    cellDocuments: () => h.docs,
    subscribeDocuments: (listener: () => void) => { h.listeners.add(listener); return () => h.listeners.delete(listener); },
    acquireFileDocument: h.acquire,
}));

import { useCellFileConflicts } from './useCellFileConflicts';
import { useCollimateStore } from '@/stores/useCollimateStore';

const conflict = (id: string, path: string, candidate = 2): FileConflict => ({
    conflict_id: id, path, base_revision: 1, current_revision: 1, candidate_revision: candidate,
    candidate_hash: null, writer_id: 'other', edit_seq: 1,
});
const vf = (name: string): VirtualFile => ({ name, language: 'plaintext', body_hash: name, file_revision: 1, encoding: 'utf-8', writer_id: 'me', edit_seq: 1 });
const descriptor = (path: string): FileDescriptor => ({ path, file_revision: 3, body_hash: 'h', deleted: false, writer_id: 'other', edit_seq: 3 } as FileDescriptor);
const CONFLICTS = '/channels/ch/cells/c1/files/conflicts';
const INVENTORY = '/channels/ch/cells/c1/files';
/** Routes requests by URL; an absent route never settles. */
const serve = (routes: Record<string, () => Promise<unknown>>) =>
    h.requestJson.mockImplementation((url: string) => routes[url]?.() ?? new Promise(() => {}));
const broadcast = (cellId: string) =>
    act(() => { window.dispatchEvent(new CustomEvent('collimate:files-changed-broadcast', { detail: { cellId } })); });

describe('useCellFileConflicts', () => {
    const { locateCell } = useCollimateStore.getState();
    beforeEach(() => {
        h.requestJson.mockReset(); h.acquire.mockReset(); h.note.mockReset();
        h.docs = []; h.listeners.clear();
        h.acquire.mockResolvedValue({ noteConflicts: h.note });
        useCollimateStore.setState({ locateCell, cellCache: { c1: { id: 'c1', name: 'c1', files: [vf('a.txt')] } as Cell } });
    });

    it('stays empty and asks nothing while disabled or without a cell', () => {
        const off = renderHook(() => useCellFileConflicts('ch', 'c1', false));
        const none = renderHook(() => useCellFileConflicts('ch', ''));
        expect(off.result.current).toEqual([]);
        expect(none.result.current).toEqual([]);
        expect(h.requestJson).not.toHaveBeenCalled();
        expect(h.listeners.size).toBe(0);
    });

    it('lists server conflicts on an open file without fetching the inventory', async () => {
        serve({ [CONFLICTS]: async () => ({ conflicts: [conflict('k1', 'a.txt')] }) });
        const { result } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(result.current.map(c => c.conflict_id)).toEqual(['k1']));
        expect(h.requestJson).toHaveBeenCalledTimes(1);
        expect(h.acquire).not.toHaveBeenCalled();
    });

    it('merges document conflicts over the server list by id, and refreshes when documents change', async () => {
        h.docs = [{ getSnapshot: () => ({ conflicts: [conflict('k1', 'a.txt', 9)] }) }];
        serve({ [CONFLICTS]: async () => ({ conflicts: [conflict('k1', 'a.txt'), conflict('k2', 'a.txt')] }) });
        const { result } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(result.current).toHaveLength(2));
        expect(result.current.find(c => c.conflict_id === 'k1')?.candidate_revision).toBe(9);
        h.docs = [...h.docs, { getSnapshot: () => ({ conflicts: [conflict('k3', 'a.txt')] }) }];
        act(() => { h.listeners.forEach(listener => listener()); });
        expect(result.current.map(c => c.conflict_id)).toEqual(['k1', 'k2', 'k3']);
    });

    it('opens a document for a conflicted file the cell no longer lists, skipping paths absent from the inventory', async () => {
        const gone = conflict('k1', 'gone.txt');
        serve({
            [CONFLICTS]: async () => ({ conflicts: [gone, conflict('k2', 'vanished.txt')] }),
            [INVENTORY]: async () => ({ files: [descriptor('gone.txt')] }),
        });
        renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(h.note).toHaveBeenCalledTimes(1));
        expect(h.acquire).toHaveBeenCalledTimes(1);
        const { path: _path, ...metadata } = descriptor('gone.txt');
        expect(h.acquire).toHaveBeenCalledWith('ch', 'c1', { name: 'gone.txt', ...metadata });
        expect(h.note).toHaveBeenCalledWith([gone], descriptor('gone.txt'));
    });

    it('reads the cell from the open channel when it is not cached', async () => {
        useCollimateStore.setState({ cellCache: {}, locateCell: () => ({ channel: 'ch', stackId: 's', cellIndex: 0, cell: { id: 'c1', name: 'c1', files: [vf('a.txt')] } as Cell }) });
        serve({ [CONFLICTS]: async () => ({ conflicts: [conflict('k1', 'a.txt')] }) });
        const { result } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(result.current).toHaveLength(1));
        expect(h.requestJson).toHaveBeenCalledTimes(1);
    });

    it('reloads on a files-changed broadcast for its own cell only', async () => {
        let round = 0;
        serve({ [CONFLICTS]: async () => ({ conflicts: round++ ? [conflict('k2', 'a.txt')] : [] }) });
        const { result } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(h.requestJson).toHaveBeenCalledTimes(1));
        broadcast('other');
        expect(h.requestJson).toHaveBeenCalledTimes(1);
        broadcast('c1');
        await waitFor(() => expect(result.current.map(c => c.conflict_id)).toEqual(['k2']));
        expect(h.requestJson).toHaveBeenCalledTimes(2);
    });

    it('keeps the list it has when a request fails', async () => {
        let fail = false;
        serve({ [CONFLICTS]: async () => { if (fail) throw new Error('offline'); return { conflicts: [conflict('k1', 'a.txt')] }; } });
        const { result } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(result.current).toHaveLength(1));
        fail = true;
        broadcast('c1');
        await waitFor(() => expect(h.requestJson).toHaveBeenCalledTimes(2));
        await act(async () => {});
        expect(result.current.map(c => c.conflict_id)).toEqual(['k1']);
    });

    it('ignores a response that lands after unmount and detaches its listeners', async () => {
        let answer: (value: unknown) => void = () => {};
        serve({ [CONFLICTS]: () => new Promise(resolve => { answer = resolve; }) });
        const { result, unmount } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        unmount();
        expect(h.listeners.size).toBe(0);
        await act(async () => { answer({ conflicts: [conflict('k1', 'gone.txt')] }); });
        expect(result.current).toEqual([]);
        expect(h.requestJson).toHaveBeenCalledTimes(1);
        broadcast('c1');
        expect(h.requestJson).toHaveBeenCalledTimes(1);
    });

    it('stops opening documents once unmounted mid-inventory', async () => {
        let inventory: (value: unknown) => void = () => {};
        serve({
            [CONFLICTS]: async () => ({ conflicts: [conflict('k1', 'gone.txt')] }),
            [INVENTORY]: () => new Promise(resolve => { inventory = resolve; }),
        });
        const { unmount } = renderHook(() => useCellFileConflicts('ch', 'c1'));
        await waitFor(() => expect(h.requestJson).toHaveBeenCalledWith(INVENTORY, 'GET'));
        unmount();
        await act(async () => { inventory({ files: [descriptor('gone.txt')] }); });
        expect(h.acquire).not.toHaveBeenCalled();
    });
});
