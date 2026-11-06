import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import { useServerCatalog } from './useServerCatalog';
import { installDesktopShell, uninstallDesktopShell, LOCAL_SERVER_ENTRY } from '@/test/desktopShell';
import type { ServerEntry } from '@/lib/desktop';

const remote = (over: Partial<ServerEntry> = {}): ServerEntry => ({
    ...LOCAL_SERVER_ENTRY,
    id: 'lab', kind: 'remote', name: 'Lab', url: 'https://lab.test',
    hasToken: true, isWindowServer: false,
    channels: [{ id: 'c1', name: 'Research' }], activeChannelId: 'c1',
    ...over,
});

afterEach(() => { uninstallDesktopShell(); });

describe('useServerCatalog (MS-05)', () => {
    it('is [] in a browser and asks nothing (MS-05)', () => {
        const { result } = renderHook(() => useServerCatalog(true));
        expect(result.current).toEqual([]);
    });

    it('lists on mount, follows onServersChanged and unsubscribes on unmount (MS-05)', async () => {
        const stub = installDesktopShell();
        const { result, unmount } = renderHook(() => useServerCatalog());
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(1);
        await waitFor(() => expect(result.current).toEqual([LOCAL_SERVER_ENTRY]));
        expect(stub.serversSubscriberCount()).toBe(1);

        const next = [LOCAL_SERVER_ENTRY, remote()];
        act(() => stub.emitServersChanged(next));
        expect(result.current).toEqual(next);

        unmount();
        expect(stub.serversSubscriberCount()).toBe(0);
    });

    it('lists again each time the refresh flag turns true, as the channel menu opens (MS-05)', async () => {
        const stub = installDesktopShell();
        const { rerender } = renderHook(({ open }) => useServerCatalog(open), { initialProps: { open: false } });
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(1);
        rerender({ open: true });
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(2);
        rerender({ open: false });
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(2);
        rerender({ open: true });
        expect(stub.shell.servers.list).toHaveBeenCalledTimes(3);
    });

    it('a push supersedes a list still in flight (MS-05)', async () => {
        let answer: (entries: ServerEntry[]) => void = () => {};
        const stub = installDesktopShell();
        vi.mocked(stub.shell.servers.list).mockImplementation(() => new Promise(resolve => { answer = resolve; }));
        const { result } = renderHook(() => useServerCatalog());
        const pushed = [LOCAL_SERVER_ENTRY, remote({ status: 'unreachable', channels: null })];
        act(() => stub.emitServersChanged(pushed));
        await act(async () => { answer([LOCAL_SERVER_ENTRY]); });
        expect(result.current).toEqual(pushed);
    });
});
