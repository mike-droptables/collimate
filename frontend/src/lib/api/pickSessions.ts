import { PickSession } from '@/lib/types';
import { requestJson } from './client';


// Per-cell panel sessions (kind runners / files / handlers) — channel-scoped UI state stored
// in the channel DB's `ui_state` KV. Survives browser refresh; never
// exported into any .coll* bundle and never part of the DAG. Keyed by the STABLE canvas
// cell id. The GET prunes entries whose cell no longer exists.
export const fetchPickSessions = async (
    channel: string,
): Promise<Record<string, PickSession>> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/pick_sessions`, 'GET');


export const savePickSession = async (
    channel: string,
    cellId: string,
    session: PickSession,
): Promise<void> => {
    await requestJson(`/channels/${encodeURIComponent(channel)}/pick_sessions/${encodeURIComponent(cellId)}`, 'PUT', session);
};


export const deletePickSession = async (
    channel: string,
    cellId: string,
): Promise<void> => {
    await requestJson(`/channels/${encodeURIComponent(channel)}/pick_sessions/${encodeURIComponent(cellId)}`, 'DELETE');
};
