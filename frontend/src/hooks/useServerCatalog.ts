import { useCallback, useEffect, useRef, useState } from 'react';
import { desktopBridge, type ServerEntry } from '@/lib/desktop';

const NONE: ServerEntry[] = [];

/**
 * The shell's server registry (the local server and the saved remotes, each
 * with its status and channel catalog), kept current: listed on mount and
 * again whenever `refresh` changes to a truthy value (the channel menu
 * opening, a Servers tab action landing), and replaced by every
 * `onServersChanged` push. Asking the shell to list also makes it poll at
 * once, so an open menu shows fresh statuses.
 *
 * `null` until the shell has answered once: a list waits for that poll,
 * which an unresponsive remote holds until its timeout, so a caller can
 * show that it is loading rather than an empty registry. A later list
 * leaves the last answer standing until the new one lands.
 *
 * Multi-server is desktop-only: in a browser this is always `[]`.
 */
export function useServerCatalog(refresh?: unknown): ServerEntry[] | null {
    const [entries, setEntries] = useState<ServerEntry[] | null>(() => (desktopBridge()?.servers ? null : NONE));
    // Only the latest answer lands: a slow list must not overwrite a newer
    // list or push.
    const latest = useRef(0);

    const list = useCallback(() => {
        const servers = desktopBridge()?.servers;
        if (!servers) return;
        const seq = ++latest.current;
        servers.list().then(
            next => { if (seq === latest.current) setEntries(next); },
            // The registry is unavailable for the moment; the last list (or
            // the next push) stands.
            () => {},
        );
    }, []);

    useEffect(() => {
        const shell = desktopBridge();
        list();
        const off = shell?.onServersChanged(next => {
            latest.current++;
            setEntries(next);
        });
        return off;
    }, [list]);

    useEffect(() => {
        if (refresh) list();
    }, [refresh, list]);

    return entries;
}
