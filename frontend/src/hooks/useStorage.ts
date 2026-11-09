import { useCallback, useEffect, useRef, useState } from 'react';
import { describeApiError } from '@/lib/api/client';
import {
    fetchLocations, fetchManagedChannels, fetchStorageJob, fetchStorageOverview, moveChannel, moveEnded, refreshStorage,
    type ManagedChannel, type MoveJob, type StorageLocation, type StorageOverview,
} from '@/lib/api/storage';

/** The manage listing as the page last read it. `channels` is null until
 *  the first answer (or when it failed). */
export interface ManageListing {
    channels: ManagedChannel[] | null;
    error: string | null;
    loading: boolean;
}

/** The locations as the page last read them; `list` null until the first
 *  answer. */
export interface LocationListing {
    list: StorageLocation[] | null;
    error: string | null;
    loading: boolean;
}

/** The move the page started: its channel, its destination and the job's
 *  latest answer (null until the first poll), or why polling stopped. */
export interface MoveProgress {
    channelId: string;
    channelName: string;
    toLocationId: string;
    job: MoveJob | null;
    /** The job could not be read (gone, or the server went away). */
    error: string | null;
}

export interface Storage {
    manage: ManageListing;
    locations: LocationListing;
    /** Read the listing and the locations again (after an action, or the
     *  user's Refresh). */
    refresh: () => Promise<void>;
    /** The page's move job, polled every `MOVE_POLL_MS` until it ends. */
    move: MoveProgress | null;
    /** Start moving a channel: resolves once the server took the job (the
     *  poll goes on in the background), rejects with its refusal (nothing
     *  starts then). The lists are read again when the job ends. */
    startMove: (channel: Pick<ManagedChannel, 'id' | 'name'>, locationId: string) => Promise<void>;
    /** Forget an ended move (its dialog closed). */
    dismissMove: () => void;
}

/** The plan's poll interval for a move's progress. */
export const MOVE_POLL_MS = 500;

/**
 * The channels page's data from the window's server: the manage listing
 * (`GET /api/channels/manage`, every channel, archived ones included), the
 * locations (`GET /api/storage/locations`) and the page's move job. The
 * listing and the locations are read on mount, again whenever `revision`
 * changes (the app's channel list moved: a channel created, renamed or
 * switched elsewhere), and by `refresh()`. Only the latest answer of each
 * lands. The storage overview is the Storage tab's own read
 * (`useStorageOverview`): its first GET can start a survey of the machine.
 */
export function useStorage(revision?: unknown): Storage {
    const [manage, setManage] = useState<ManageListing>({ channels: null, error: null, loading: true });
    const [locations, setLocations] = useState<LocationListing>({ list: null, error: null, loading: true });
    const [move, setMove] = useState<MoveProgress | null>(null);
    const latest = useRef(0);
    const latestLocations = useRef(0);
    const alive = useRef(true);

    const readListing = useCallback(async () => {
        const seq = ++latest.current;
        setManage(prev => ({ ...prev, loading: true }));
        try {
            const listing = await fetchManagedChannels();
            if (seq === latest.current) setManage({ channels: listing.channels, error: null, loading: false });
        } catch (e) {
            if (seq === latest.current) setManage(prev => ({ channels: prev.channels, error: describeApiError(e), loading: false }));
        }
    }, []);

    const readLocations = useCallback(async () => {
        const seq = ++latestLocations.current;
        setLocations(prev => ({ ...prev, loading: true }));
        try {
            const answer = await fetchLocations();
            if (seq === latestLocations.current) setLocations({ list: answer.locations, error: null, loading: false });
        } catch (e) {
            if (seq === latestLocations.current) setLocations(prev => ({ list: prev.list, error: describeApiError(e), loading: false }));
        }
    }, []);

    const refresh = useCallback(async () => {
        await Promise.all([readListing(), readLocations()]);
    }, [readListing, readLocations]);

    useEffect(() => { void refresh(); }, [refresh, revision]);
    // A late answer after unmount must not land, nor a poll go on.
    useEffect(() => () => { alive.current = false; latest.current++; latestLocations.current++; }, []);

    /** Poll the job until it ends, then read the lists again. */
    const follow = useCallback(async (channelId: string, jobId: string) => {
        for (;;) {
            await new Promise(resolve => setTimeout(resolve, MOVE_POLL_MS));
            if (!alive.current) return;
            let job: MoveJob;
            try {
                job = await fetchStorageJob(jobId);
            } catch (e) {
                if (alive.current) setMove(prev => prev && prev.channelId === channelId ? { ...prev, error: describeApiError(e) } : prev);
                break;
            }
            if (!alive.current) return;
            setMove(prev => prev && prev.channelId === channelId ? { ...prev, job } : prev);
            if (moveEnded(job)) break;
        }
        if (alive.current) await refresh();
    }, [refresh]);

    const startMove = useCallback(async (channel: Pick<ManagedChannel, 'id' | 'name'>, locationId: string) => {
        const { job_id: jobId } = await moveChannel(channel.id, locationId);
        setMove({ channelId: channel.id, channelName: channel.name, toLocationId: locationId, job: null, error: null });
        // The channel's row shows Moving while the job runs.
        void readListing();
        void follow(channel.id, jobId);
    }, [readListing, follow]);

    const dismissMove = useCallback(() => setMove(prev => prev && (prev.error || (prev.job && moveEnded(prev.job))) ? null : prev), []);

    return { manage, locations, refresh, move, startMove, dismissMove };
}

/** The storage overview as the tab last read it. */
export interface OverviewState {
    overview: StorageOverview | null;
    error: string | null;
    loading: boolean;
}

/** How often the Storage tab reads the overview again while a survey runs. */
export const SURVEY_POLL_MS = 1500;

/**
 * The Storage tab's overview (`GET /api/storage`), read while `shown`, and
 * again every `SURVEY_POLL_MS` while a survey runs (its `pending` parts
 * land one by one). `refresh()` starts a survey (`POST /api/storage/
 * refresh`) and follows it; `reload()` only reads the cache again (after a
 * cleanup, whose re-measure runs on the server).
 */
export function useStorageOverview(shown: boolean): OverviewState & { refresh: () => Promise<void>; reload: () => Promise<void> } {
    const [state, setState] = useState<OverviewState>({ overview: null, error: null, loading: false });
    const seqRef = useRef(0);
    const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const shownRef = useRef(shown);
    shownRef.current = shown;

    const reload = useCallback(async () => {
        const seq = ++seqRef.current;
        if (timer.current) { clearTimeout(timer.current); timer.current = null; }
        setState(prev => ({ ...prev, loading: true }));
        try {
            const overview = await fetchStorageOverview();
            if (seq !== seqRef.current) return;
            setState({ overview, error: null, loading: false });
            if (overview.survey.state === 'running' && shownRef.current) {
                timer.current = setTimeout(() => { void reload(); }, SURVEY_POLL_MS);
            }
        } catch (e) {
            if (seq === seqRef.current) setState(prev => ({ overview: prev.overview, error: describeApiError(e), loading: false }));
        }
    }, []);

    const refresh = useCallback(async () => {
        try {
            const { survey } = await refreshStorage();
            setState(prev => prev.overview ? { ...prev, overview: { ...prev.overview, survey } } : prev);
        } catch (e) {
            setState(prev => ({ ...prev, error: describeApiError(e) }));
            return;
        }
        await reload();
    }, [reload]);

    useEffect(() => {
        if (shown) void reload();
        return () => {
            seqRef.current++;
            if (timer.current) { clearTimeout(timer.current); timer.current = null; }
        };
    }, [shown, reload]);

    return { ...state, refresh, reload };
}
