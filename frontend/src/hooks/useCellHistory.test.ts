import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { HistoryNode, HistoryResponse } from '@/lib/api';

// The hook's network is three fetchers; mocking the api module keeps axios
// out of the test. The selection lives in the store's `cellHistory` slice,
// so a tiny zustand store stands in for it and setState drives selection
// the way the rail's clicks would.
vi.mock('@/lib/api', async original => ({
    ...(await original<typeof import('@/lib/api')>()),
    fetchCellHistory: vi.fn(),
    fetchCellFilesTree: vi.fn(),
    fetchBlobText: vi.fn(),
}));
vi.mock('@/stores/useCollimateStore', async () => {
    const { create } = await import('zustand');
    return {
        useCollimateStore: create(() => ({
            cellHistory: {} as Record<string, { nodeId?: string | null; versionId?: string | null }>,
        })),
    };
});

import { useCellHistory } from './useCellHistory';
import { fileFetchCache } from '@/lib/fileFetchCache';
import { fetchBlobText, fetchCellFilesTree, fetchCellHistory } from '@/lib/api';
import { useCollimateStore } from '@/stores/useCollimateStore';

const store = useCollimateStore as unknown as {
    setState: (s: { cellHistory: Record<string, { nodeId?: string | null; versionId?: string | null }> }) => void;
};

// The hook's content caches are module-level and immutable by design, so
// every scenario mints its own ids: nothing one test cached leaks into the
// next one's expectations.
let seq = 0;
const uid = (p: string) => `${p}-${++seq}`;

const version = (cellId: string, index = 0) => ({
    cell_id: cellId, index, created_at: null, name: null, file_count: 1,
});
const node = (id: string, versionIds: string[]): HistoryNode => ({
    node_id: id,
    kind: 'run',
    run: null,
    versions: versionIds.map((v, i) => version(v, i)),
    config: [],
    logs: [],
});
const response = (nodes: HistoryNode[], current: string): HistoryResponse => ({
    nodes,
    total_versions: nodes.reduce((n, x) => n + x.versions.length, 0),
    current: { node_id: current, version_cell_id: 'tip' },
});
const tree = (files: { name: string; content: string; body_hash?: string | null }[]) => ({
    files: files.map(f => ({ language: 'markdown',file_revision:1,body_hash:'fixture',encoding:'utf-8',writer_id:'fixture',edit_seq:1,...f })),
});

beforeEach(() => {
    vi.clearAllMocks();
    store.setState({ cellHistory: {} });
});

afterEach(() => {
    vi.useRealTimers();
});

describe('useCellHistory', () => {
    it('reads immutable resources from the same bounded owner used by live diffs__F0792', async () => {
        const id = uid('shared-version');
        const cached = [{ name: 'cached.md', content: 'shared body', language: 'markdown', file_revision:1,body_hash:'fixture',encoding:'utf-8',writer_id:'fixture',edit_seq:1 }];
        fileFetchCache.put(`tree:ch:${id}`, cached);
        vi.mocked(fetchCellFilesTree).mockResolvedValue(tree([{ name: 'cached.md', content: 'shared body' }]) as never);
        vi.mocked(fetchCellHistory).mockResolvedValue(response([node('shared-node', [id])], 'shared-node'));
        const { result } = renderHook(() => useCellHistory('ch', uid('cell'), true, undefined));
        await waitFor(() => expect(result.current.selFiles).not.toBeNull());
        expect(fetchCellFilesTree).not.toHaveBeenCalled();
    });

    it('stays idle while history mode is off', () => {
        const cell = uid('cell');
        const { result } = renderHook(() => useCellHistory('ch', cell, false, undefined));
        expect(fetchCellHistory).not.toHaveBeenCalled();
        expect(result.current.history).toBeNull();
        expect(result.current.selNode).toBeNull();
        expect(result.current.selVersionId).toBeNull();
        expect(result.current.selFiles).toBeNull();
        expect(result.current.baseFiles).toEqual({});
        expect(fetchCellHistory).not.toHaveBeenCalled();
    });

    it('does not fetch without a channel', async () => {
        const cell = uid('cell');
        renderHook(() => useCellHistory(null, cell, true, undefined));
        await new Promise(r => setTimeout(r, 10));
        expect(fetchCellHistory).not.toHaveBeenCalled();
    });

    it('selects the current node, its last version, and loads the older end state as the base', async () => {
        const cell = uid('cell');
        const [v1a, v1b, v3] = [uid('v'), uid('v'), uid('v')];
        const hist = response([
            node('n1', [v1a, v1b]),
            node('n2', []),              // a zero-version run is skipped for the base
            node('n3', [v3]),
        ], 'n1');
        vi.mocked(fetchCellHistory).mockResolvedValue(hist);
        vi.mocked(fetchCellFilesTree).mockImplementation(async (_ch, id) => (
            id === v1b
                ? tree([{ name: 'a.md', content: 'new', body_hash: 'h-a' }])
                : tree([{ name: 'a.md', content: 'old' }, { name: 'gone.md', content: '' }])
        ) as never);

        const { result, rerender } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: 't0' } });
        expect(result.current.loading).toBe(true);
        await waitFor(() => expect(result.current.history).toBe(hist));
        expect(result.current.loading).toBe(false);
        expect(result.current.error).toBeNull();
        expect(result.current.selNode?.node_id).toBe('n1');
        expect(result.current.selVersionId).toBe(v1b);

        await waitFor(() => expect(result.current.selFiles).not.toBeNull());
        expect(result.current.selFiles).toEqual([
            { name: 'a.md', language: 'markdown', content: 'new', file_revision:1,encoding:'utf-8',writer_id:'fixture',edit_seq:1,body_hash:'h-a' },
        ]);
        await waitFor(() => expect(result.current.baseFiles).toEqual({ 'a.md': 'old', 'gone.md': '' }));
        expect(vi.mocked(fetchCellFilesTree).mock.calls.map(c => c[1]).sort()).toEqual([v1b, v3].sort());

        // Both trees are cached by version id: a re-render never refetches.
        const calls = vi.mocked(fetchCellFilesTree).mock.calls.length;
        act(() => rerender({ at: uid('refresh') }));
        await waitFor(() => expect(fetchCellHistory).toHaveBeenCalledTimes(2));
        expect(vi.mocked(fetchCellFilesTree).mock.calls.length).toBe(calls);
    });

    it('resolves the slice selection by identity: node, nested version, and fallbacks', async () => {
        const cell = uid('cell');
        const [va, vb, vc] = [uid('v'), uid('v'), uid('v')];
        const hist = response([node('cur', [va]), node('old', [vb, vc]), node('empty', [])], 'cur');
        vi.mocked(fetchCellHistory).mockResolvedValue(hist);
        vi.mocked(fetchCellFilesTree).mockResolvedValue(tree([{ name: 'x.md', content: 'x' }]) as never);

        const { result } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        await waitFor(() => expect(result.current.history).toBe(hist));
        await waitFor(() => expect(result.current.selFiles).not.toBeNull());

        act(() => store.setState({ cellHistory: { [cell]: { nodeId: 'old', versionId: vb } } }));
        expect(result.current.selNode?.node_id).toBe('old');
        expect(result.current.selVersionId).toBe(vb);            // the nested version
        await waitFor(() => expect(result.current.selFiles).not.toBeNull());
        await waitFor(() => expect(result.current.baseFiles).not.toBeNull());

        act(() => store.setState({ cellHistory: { [cell]: { nodeId: 'old', versionId: 'not-in-node' } } }));
        expect(result.current.selVersionId).toBe(vc);            // the node's last version
        await waitFor(() => expect(result.current.selFiles).not.toBeNull());

        act(() => store.setState({ cellHistory: { [cell]: { nodeId: 'empty' } } }));
        expect(result.current.selNode?.node_id).toBe('empty');
        expect(result.current.selVersionId).toBeNull();
        expect(result.current.selFiles).toEqual([]);             // zero versions, nothing to load
        expect(result.current.baseFiles).toEqual({});            // no older node

        act(() => store.setState({ cellHistory: { [cell]: { nodeId: 'vanished' } } }));
        expect(result.current.selNode?.node_id).toBe('cur');     // falls back to current
    });

    it('falls back to the first node when the current node is unknown, and to none for an empty history', async () => {
        const cell = uid('cell');
        const v = uid('v');
        vi.mocked(fetchCellHistory).mockResolvedValueOnce(response([node('only', [v])], 'elsewhere'));
        vi.mocked(fetchCellFilesTree).mockResolvedValue(tree([]) as never);
        const { result } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        await waitFor(() => expect(result.current.selNode?.node_id).toBe('only'));

        const empty = uid('cell');
        vi.mocked(fetchCellHistory).mockResolvedValueOnce(response([], 'x'));
        const { result: none } = renderHook(() => useCellHistory('ch', empty, true, undefined));
        await waitFor(() => expect(none.current.history).not.toBeNull());
        expect(none.current.selNode).toBeNull();
    });

    it('reports a failed history fetch', async () => {
        const cell = uid('cell');
        vi.mocked(fetchCellHistory).mockRejectedValue(new Error('boom'));
        const { result } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        await waitFor(() => expect(result.current.error).toBe('boom'));
        expect(result.current.loading).toBe(false);
        expect(result.current.history).toBeNull();
    });

    it('refetches, debounced, when the cell settles — and a stale answer never lands', async () => {
        const cell = uid('cell');
        const first = response([node('a', [])], 'a');
        const second = response([node('b', [])], 'b');
        let releaseStale: (v: HistoryResponse) => void = () => {};
        vi.mocked(fetchCellHistory)
            .mockResolvedValueOnce(first)
            .mockImplementationOnce(() => new Promise(r => { releaseStale = r; }))
            .mockResolvedValueOnce(second);

        const { result, rerender } = renderHook(
            ({ at }) => useCellHistory('ch', cell, true, at),
            { initialProps: { at: 't0' } });
        await waitFor(() => expect(result.current.history).toBe(first));

        rerender({ at: 't1' });                       // debounced (400 ms), not immediate
        expect(fetchCellHistory).toHaveBeenCalledTimes(1);
        await waitFor(() => expect(fetchCellHistory).toHaveBeenCalledTimes(2), { timeout: 2000 });
        expect(result.current.loading).toBe(false);   // a refresh keeps the old view

        act(() => rerender({ at: uid('refresh') }));          // a newer request supersedes the pending one
        await waitFor(() => expect(result.current.history).toBe(second));
        await act(async () => { releaseStale(response([node('stale', [])], 'stale')); });
        expect(result.current.history).toBe(second);
    });

    it('ignores a stale failure the same way', async () => {
        const cell = uid('cell');
        const good = response([node('g', [])], 'g');
        let failStale: (e: Error) => void = () => {};
        vi.mocked(fetchCellHistory)
            .mockImplementationOnce(() => new Promise((_r, rej) => { failStale = rej; }))
            .mockResolvedValueOnce(good);
        const { result, rerender } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        await waitFor(() => expect(fetchCellHistory).toHaveBeenCalledTimes(1));
        act(() => rerender({ at: uid('refresh') }));
        await waitFor(() => expect(result.current.history).toBe(good));
        await act(async () => { failStale(new Error('late')); });
        expect(result.current.error).toBeNull();
    });

    it('backs off after a failed tree fetch, then retries', async () => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        const cell = uid('cell');
        const v = uid('v');
        vi.mocked(fetchCellHistory).mockResolvedValue(response([node('n', [v])], 'n'));
        vi.mocked(fetchCellFilesTree)
            .mockRejectedValueOnce(new Error('503'))
            .mockResolvedValue(tree([{ name: 'r.md', content: 'retried' }]) as never);

        const { result, rerender } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        await waitFor(() => expect(fetchCellFilesTree).toHaveBeenCalledTimes(1));
        await act(async () => { await Promise.resolve(); });
        expect(result.current.selFiles).toBeNull();
        // Inside the back-off window a re-render does not refire.
        act(() => rerender({ at: uid('refresh') }));
        await act(async () => { await vi.advanceTimersByTimeAsync(100); });
        expect(fetchCellFilesTree).toHaveBeenCalledTimes(1);
        // The scheduled retry render fires after the window.
        await act(async () => { await vi.advanceTimersByTimeAsync(3300); });
        await waitFor(() => expect(result.current.selFiles?.[0]?.content).toBe('retried'));
        expect(fetchCellFilesTree).toHaveBeenCalledTimes(2);
    });

    it('re-renders every instance waiting on a shared in-flight fetch, not just the one that fired it', async () => {
        const [cellA, cellB] = [uid('cell'), uid('cell')];
        const v = uid('v');
        vi.mocked(fetchCellHistory).mockResolvedValue(response([node('n1', [v])], 'n1'));
        let resolveTree: (t: ReturnType<typeof tree>) => void = () => {};
        vi.mocked(fetchCellFilesTree).mockImplementation(() => new Promise(r => { resolveTree = r; }) as never);

        const a = renderHook(() => useCellHistory('ch', cellA, true, undefined));
        const b = renderHook(() => useCellHistory('ch', cellB, true, undefined));
        await waitFor(() => expect(a.result.current.selVersionId).toBe(v));
        await waitFor(() => expect(b.result.current.selVersionId).toBe(v));
        // One fetch for the shared version id; the other instance deduped.
        expect(fetchCellFilesTree).toHaveBeenCalledTimes(1);

        await act(async () => { resolveTree(tree([{ name: 'a.md', content: 'x' }])); });
        await waitFor(() => expect(a.result.current.selFiles).not.toBeNull());
        await waitFor(() => expect(b.result.current.selFiles).not.toBeNull());
    });

    it('fetches artifact text once per hash, caches it, and backs off on failure', async () => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        const cell = uid('cell');
        const [good, bad] = [uid('hash'), uid('hash')];
        vi.mocked(fetchCellHistory).mockResolvedValue(response([], 'x'));
        vi.mocked(fetchBlobText).mockImplementation(async (hash) => {
            if (hash === bad && vi.mocked(fetchBlobText).mock.calls.filter(c => c[0] === bad).length === 1) {
                throw new Error('404');
            }
            return `text of ${hash}`;
        });

        const { result } = renderHook(({ at }) => useCellHistory('ch', cell, true, at), { initialProps: { at: undefined as string | undefined } });
        expect(result.current.artifactText(good)).toBeUndefined();
        expect(result.current.artifactText(good)).toBeUndefined();   // in flight: not refired
        await waitFor(() => expect(result.current.artifactText(good)).toBe(`text of ${good}`));
        expect(vi.mocked(fetchBlobText).mock.calls.filter(c => c[0] === good)).toHaveLength(1);

        expect(result.current.artifactText(bad)).toBeUndefined();
        await act(async () => { await vi.advanceTimersByTimeAsync(10); });
        expect(result.current.artifactText(bad)).toBeUndefined();    // backing off
        expect(vi.mocked(fetchBlobText).mock.calls.filter(c => c[0] === bad)).toHaveLength(1);
        await act(async () => { await vi.advanceTimersByTimeAsync(3300); });
        expect(result.current.artifactText(bad)).toBeUndefined();    // the retry fires now
        await waitFor(() => expect(result.current.artifactText(bad)).toBe(`text of ${bad}`));
    });
});

it('hydrates selected and base files from their exact immutable versions__F0299', async () => {
    const cell = uid('cell');
    const selected = uid('selected');
    const base = uid('base');
    vi.mocked(fetchCellHistory).mockResolvedValue(response([node('current', [selected]), node('prior', [base])], 'current'));
    vi.mocked(fetchCellFilesTree).mockImplementation(async (_channel, versionId) => ({ cell_id: versionId, files: [
        {name:'large.txt',language:'plaintext',file_revision:1,body_hash:'history-body',encoding:'utf-8',writer_id:'fixture',edit_seq:1},
    ] }));
    const request = vi.fn(async (_input: RequestInfo | URL) => new Response('immutable old body'));
    vi.stubGlobal('fetch', request);
    const hook = renderHook(() => useCellHistory('ch', cell, true, undefined));
    await waitFor(() => expect(hook.result.current.selFiles?.[0].content).toBe('immutable old body'));
    await waitFor(() => expect(hook.result.current.baseFiles).toEqual({ 'large.txt': 'immutable old body' }));
    expect(request).toHaveBeenCalledTimes(2);
    expect(request.mock.calls.map(call=>String(call[0]))).toEqual(expect.arrayContaining([expect.stringContaining(`/versions/${base}/files/large.txt`),expect.stringContaining(`/versions/${selected}/files/large.txt`)]));
    hook.unmount();
    vi.unstubAllGlobals();
});

it('exposes only the production history view surface__F0791', () => {
    const hook = renderHook(() => useCellHistory(null, uid('cell'), false, undefined));
    expect(Object.keys(hook.result.current)).toEqual([
        'history','loading','error','selNode','selVersionId','selFiles','baseFiles','artifactText',
    ]);
});

it('keeps mounted history resources across accelerator eviction without refetching__F0762', async () => {
    const cell = uid('byte-cell');
    const selected = uid('selected-version');
    const base = uid('base-version');
    const selectedBody = 'selected body';
    vi.mocked(fetchCellHistory).mockResolvedValue(response([node('selected-node', [selected]), node('base-node', [base])], 'selected-node'));
    vi.mocked(fetchCellFilesTree).mockImplementation(async (_channel, id) => tree([{name:'body.txt',content:id === selected ? selectedBody : 'base body'}]) as never);
    const hook = renderHook(() => useCellHistory('ch', cell, true, undefined));
    await waitFor(() => expect(hook.result.current.baseFiles).toEqual({'body.txt':'base body'}));
    expect(hook.result.current.selFiles?.[0].content).toBe(selectedBody);
    expect(fetchCellFilesTree).toHaveBeenCalledTimes(2);
    for (let index = 0; index < 257; index++) fileFetchCache.put(`eviction:${uid('key')}`, 'other');
    hook.rerender();
    expect(hook.result.current.selFiles?.[0].content).toBe(selectedBody);
    expect(hook.result.current.baseFiles).toEqual({'body.txt':'base body'});
    expect(fetchCellFilesTree).toHaveBeenCalledTimes(2);
});

it('completes an oversized active history tree without retaining it in the shared accelerator__F0762', async () => {
    const selected = uid('oversized-tree');
    const files = Array.from({length: 2000}, (_, index) => ({name:`part-${index}.txt`,content:'x'.repeat(17000)}));
    vi.mocked(fetchCellHistory).mockResolvedValue(response([node('oversized-node', [selected])], 'oversized-node'));
    vi.mocked(fetchCellFilesTree).mockResolvedValue(tree(files) as never);
    const cell = uid('cell');
    const hook = renderHook(() => useCellHistory('ch', cell, true, undefined));
    await waitFor(() => expect(hook.result.current.selFiles).toHaveLength(2000));
    expect(fileFetchCache.get(`tree:ch:${selected}`)).toBeUndefined();
    hook.rerender();
    expect(hook.result.current.selFiles?.[1999].content).toBe(files[1999].content);
    expect(fetchCellFilesTree).toHaveBeenCalledOnce();
});

it('adopts warm artifact bodies into the mounted history working set__F0762', () => {
    const hash = uid('warm-artifact');
    vi.mocked(fetchBlobText).mockResolvedValue('refetched body');
    const cell = uid('warm-cell');
    fileFetchCache.put(`blob:${hash}`, 'retained working body');
    const hook = renderHook(() => useCellHistory('ch', cell, false, undefined));
    expect(hook.result.current.artifactText(hash)).toBe('retained working body');
    for (let index = 0; index < 257; index++) fileFetchCache.put(`warm-eviction:${uid('key')}`, 'other');
    expect(fileFetchCache.get(`blob:${hash}`)).toBeUndefined();
    hook.rerender();
    expect(hook.result.current.artifactText(hash)).toBe('retained working body');
    expect(fetchBlobText).not.toHaveBeenCalled();
    hook.unmount();
    const replacement = renderHook(() => useCellHistory('ch', cell, false, undefined));
    expect(replacement.result.current.artifactText(hash)).toBeUndefined();
    expect(fetchBlobText).toHaveBeenCalledWith(hash);
});
