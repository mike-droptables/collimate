// A live run's approval cards over its broadcast inbox: the pointer asked
// for until the run registers it (and at once on a phase change, even once
// it gave up), the socket's frames (only the card frames count; a replayed text delta leaves
// the cards alone), the answers, a gone socket's one refetch, the redial
// backoff, and the cleanup.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

const fetchRunInbox = vi.hoisted(() => vi.fn());
vi.mock('@/lib/api/annotations', async (importOriginal) => ({ ...(await importOriginal<object>()), fetchRunInbox }));

import { useRunInbox } from './useRunInbox';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { INBOX_RETRY_LIMIT, INBOX_RETRY_MS } from '@/lib/runInbox';

class FakeSocket {
    static OPEN = 1;
    static CLOSED = 3;
    static all: FakeSocket[] = [];
    readyState = 0;
    sent: string[] = [];
    closed = false;
    onopen: (() => void) | null = null;
    onmessage: ((e: { data: string }) => void) | null = null;
    onclose: ((e: { code: number }) => void) | null = null;
    constructor(readonly url: string) { FakeSocket.all.push(this); }
    send(data: string) { this.sent.push(data); }
    close() { this.closed = true; this.readyState = FakeSocket.CLOSED; }
    open() { this.readyState = FakeSocket.OPEN; this.onopen?.(); }
    frame(data: unknown) { this.onmessage?.({ data: typeof data === 'string' ? data : JSON.stringify(data) }); }
    drop(code: number) { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code }); }
}
const last = () => FakeSocket.all[FakeSocket.all.length - 1];
const POINTER = { chat_id: 'c', ws_path: '/api/chats/c', token: 't' };
const CARD = { id: 'p1', title: 'Run a command', kind: 'execute', options: [{ option_id: 'allow', name: 'Allow', kind: 'allow_once' }] };

/** Let the hook's awaited fetch settle. */
const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    vi.stubGlobal('WebSocket', FakeSocket);
    fetchRunInbox.mockReset();
    useCollimateStore.setState({ runPhases: {} });
});
afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
});

describe('useRunInbox', () => {
    it('is off with no cards and no request while the run is not live', async () => {
        const { result } = renderHook(() => useRunInbox('ch', 'r1', false));
        await settle();
        expect(result.current.status).toBe('off');
        expect(result.current.cards).toEqual([]);
        expect(fetchRunInbox).not.toHaveBeenCalled();
        expect(result.current.answer('p1', 'allow')).toBe(false);
    });

    it('asks again every 2 s while the run has no inbox, at once on a phase change, and ends after the limit', async () => {
        fetchRunInbox.mockResolvedValue(null);
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        await settle();
        expect(result.current.status).toBe('pending');
        expect(fetchRunInbox).toHaveBeenCalledWith('ch', 'r1');
        await act(async () => { vi.advanceTimersByTime(INBOX_RETRY_MS); });
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(2);
        act(() => useCollimateStore.setState({ runPhases: { r1: { phase: 'running' } } }));
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(3);
        fetchRunInbox.mockRejectedValueOnce(new Error('down'));
        for (let i = 3; i < INBOX_RETRY_LIMIT; i++) {
            await act(async () => { vi.advanceTimersByTime(INBOX_RETRY_MS); });
            await settle();
        }
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT);
        expect(result.current.status).toBe('ended');
        await act(async () => { vi.advanceTimersByTime(INBOX_RETRY_MS * 3); });
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT);
    });

    it('ended, it still asks once on each phase change: an inbox registered late shows its cards', async () => {
        fetchRunInbox.mockResolvedValue(null);
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        for (let i = 0; i < INBOX_RETRY_LIMIT; i++) {
            await settle();
            await act(async () => { vi.advanceTimersByTime(INBOX_RETRY_MS); });
        }
        expect(result.current.status).toBe('ended');
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT);
        // Still none: one ask, no new round of retries.
        act(() => useCollimateStore.setState({ runPhases: { r1: { phase: 'running' } } }));
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT + 1);
        await act(async () => { vi.advanceTimersByTime(INBOX_RETRY_MS * 3); });
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT + 1);
        expect(result.current.status).toBe('ended');
        // The run asks for approval with its inbox up now: it dials and takes the cards.
        fetchRunInbox.mockResolvedValue(POINTER);
        act(() => useCollimateStore.setState({ runPhases: { r1: { phase: 'awaiting_approval' } } }));
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT + 2);
        act(() => { last().open(); last().frame({ t: 'history', pending_requests: [CARD] }); });
        expect(result.current.status).toBe('open');
        expect(result.current.cards.map(c => c.id)).toEqual(['p1']);
        // Connected, a phase change asks nothing.
        act(() => useCollimateStore.setState({ runPhases: { r1: { phase: 'running' } } }));
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(INBOX_RETRY_LIMIT + 2);
    });

    it('dials the inbox, takes its cards from the card frames only, and answers on the open socket', async () => {
        fetchRunInbox.mockResolvedValue(POINTER);
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        await settle();
        expect(result.current.status).toBe('connecting');
        expect(last().url).toMatch(/\/api\/chats\/c\?token=t$/);
        expect(result.current.answer('p1', 'allow')).toBe(false);
        act(() => last().open());
        expect(result.current.status).toBe('open');
        act(() => last().frame({ t: 'history', text: 'streamed so far', pending_requests: [CARD] }));
        expect(result.current.cards.map(c => c.id)).toEqual(['p1']);
        const cards = result.current.cards;
        // A replayed text delta, a malformed frame and a turn bracket leave the cards as they are.
        act(() => { last().frame({ t: 'delta', turn_id: 't', text: 'hello' }); last().frame('{oops'); last().frame({ t: 'turn_end' }); });
        expect(result.current.cards).toBe(cards);
        act(() => last().frame({ t: 'permission_request', id: 'p2', title: 'Edit a file' }));
        expect(result.current.cards.map(c => c.id)).toEqual(['p1', 'p2']);
        act(() => last().frame({ t: 'permission_resolved', id: 'p2' }));
        expect(result.current.cards.map(c => c.id)).toEqual(['p1']);
        let ok = false;
        act(() => { ok = result.current.answer('p1', 'allow'); });
        expect(ok).toBe(true);
        expect(JSON.parse(last().sent[0])).toEqual({ t: 'permission_answer', id: 'p1', option_id: 'allow' });
        expect(result.current.cards).toEqual([]);
        act(() => last().frame({ t: 'permission_request', ...CARD }));
        act(() => { result.current.answer('p1', null); });
        expect(JSON.parse(last().sent[1])).toEqual({ t: 'permission_answer', id: 'p1', cancelled: true });
    });

    it('a socket gone (4404) fetches the pointer once: a new one dials, none (or gone again) ends it', async () => {
        fetchRunInbox.mockResolvedValueOnce(POINTER).mockResolvedValueOnce({ ...POINTER, token: 'u' });
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        await settle();
        act(() => { last().open(); last().frame({ t: 'history', pending_requests: [CARD] }); });
        act(() => last().drop(4404));
        await settle();
        expect(fetchRunInbox).toHaveBeenCalledTimes(2);
        expect(FakeSocket.all).toHaveLength(2);
        expect(last().url).toMatch(/token=u$/);
        act(() => last().drop(4401));
        await settle();
        expect(result.current.status).toBe('ended');
        expect(result.current.cards).toEqual([]);
        expect(fetchRunInbox).toHaveBeenCalledTimes(2);
    });

    it('a gone socket whose refetch finds no inbox ends it', async () => {
        fetchRunInbox.mockResolvedValueOnce(POINTER).mockResolvedValueOnce(null);
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        await settle();
        act(() => last().drop(4404));
        await settle();
        expect(result.current.status).toBe('ended');
    });

    it('any other close redials after a growing wait; a frame received resets the wait', async () => {
        fetchRunInbox.mockResolvedValue(POINTER);
        const { result } = renderHook(() => useRunInbox('ch', 'r1', true));
        await settle();
        act(() => last().drop(1006));
        expect(result.current.status).toBe('connecting');
        act(() => { vi.advanceTimersByTime(499); });
        expect(FakeSocket.all).toHaveLength(1);
        act(() => { vi.advanceTimersByTime(1); });
        expect(FakeSocket.all).toHaveLength(2);
        act(() => last().drop(1006));
        act(() => { vi.advanceTimersByTime(999); });
        expect(FakeSocket.all).toHaveLength(2);
        act(() => { vi.advanceTimersByTime(1); });
        expect(FakeSocket.all).toHaveLength(3);
        act(() => { last().open(); last().frame({ t: 'turn_start' }); last().drop(1006); });
        act(() => { vi.advanceTimersByTime(500); });
        expect(FakeSocket.all).toHaveLength(4);
        expect(fetchRunInbox).toHaveBeenCalledTimes(1);
    });

    it('turning not live closes the socket; an unmount closes it and stops the timers; late events are ignored', async () => {
        fetchRunInbox.mockResolvedValue(POINTER);
        const { result, rerender, unmount } = renderHook(({ live }) => useRunInbox('ch', 'r1', live), { initialProps: { live: true } });
        await settle();
        const first = last();
        act(() => first.open());
        rerender({ live: false });
        expect(first.closed).toBe(true);
        expect(result.current.status).toBe('off');
        act(() => { first.onopen?.(); first.frame({ t: 'history', pending_requests: [CARD] }); first.drop(1006); });
        expect(result.current.cards).toEqual([]);
        rerender({ live: true });
        await settle();
        const second = last();
        expect(second).not.toBe(first);
        act(() => second.drop(1006));
        unmount();
        act(() => { vi.advanceTimersByTime(10_000); });
        expect(FakeSocket.all).toHaveLength(2);
    });

    it('an unmount while the pointer is on its way dials nothing', async () => {
        let resolve!: (v: unknown) => void;
        fetchRunInbox.mockReturnValue(new Promise(r => { resolve = r; }));
        const { unmount } = renderHook(() => useRunInbox('ch', 'r1', true));
        unmount();
        await act(async () => { resolve(POINTER); });
        expect(FakeSocket.all).toHaveLength(0);
    });
});
