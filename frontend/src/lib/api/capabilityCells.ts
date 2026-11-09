// One Capabilities cell as a whole (api/collimate_server/routes/capability_cells.py):
// Copy to channel, and where its packages came from with the update path.
import type { CapabilityKind } from '@/lib/types';
import type { PackageSkip } from '../packageReports';
import { requestJson } from './client';

const cellPath = (channel: string, cellId: string) =>
    `/channels/${encodeURIComponent(channel)}/capabilities/cells/${encodeURIComponent(cellId)}`;

/** One package of a cell as the cell-wide reviews list it; `summary` is Add
 *  to's (the manifest keys a review shows, the privilege keys among them). */
export interface PackageRow {
    kind: CapabilityKind;
    id: string;
    name: string;
    folder: string;
    version: string | null;
    summary: Record<string, unknown>;
}

/** Where one library the copy names resolves on the target: the copy
 *  itself (`cell` names the cell holding it, the copied one or one brought
 *  along), the target's Capabilities, or the app; null resolves nowhere. */
export interface LibraryResolution {
    id: string;
    resolves: 'copy' | 'capabilities' | 'app' | null;
    cell: string | null;
}

/** POST …/copy/review: what the copy lands on the target. The first cell
 *  is the copied one; `brought` marks a library cell the copy brings. */
export interface CopyReview {
    source: { channel_id: string; channel: string; cell_id: string };
    target: { channel_id: string; channel: string };
    cells: { name: string; brought: boolean; packages: PackageRow[] }[];
    libraries: LibraryResolution[];
    skipped: (PackageSkip & { cell: string })[];
    token: string;
}

export interface CopyResult { cell_id: string; cell_ids: string[]; package_skips: PackageSkip[] }

export const reviewCopy = (channel: string, cellId: string, targetChannelId: string): Promise<CopyReview> =>
    requestJson(`${cellPath(channel, cellId)}/copy/review`, 'POST', { target_channel_id: targetChannelId });

export const copyToChannel = (channel: string, cellId: string, targetChannelId: string, token: string): Promise<CopyResult> =>
    requestJson(`${cellPath(channel, cellId)}/copy`, 'POST', { target_channel_id: targetChannelId, token });

/** What a Capabilities cell follows (GET …/capabilities/updates): its
 *  upstream (`label` names it: 'the app', an entry, a channel's cell),
 *  `status` (`update`: Update available; `missing`: the upstream no longer
 *  holds it; `mixed`: its packages come from more than one upstream) and
 *  whether its own packages were edited since they were added. */
export interface CellUpstream {
    cell_id: string;
    upstream: { kind: 'app' | 'catalog' | 'channel'; id: string; label: string | null };
    status: 'current' | 'update' | 'missing' | 'mixed';
    edited: boolean;
}

/** POST …/replace/review: what Review and replace makes of the cell. */
export interface ReplaceReview {
    cell: { id: string; name: string };
    upstream: CellUpstream['upstream'];
    edited: boolean;
    packages: (PackageRow & { change: 'added' | 'changed' | 'same' | 'removed' })[];
    runners: string[];
    libraries: { id: string; resolves: 'cell' | 'capabilities' | 'app' | null }[];
    named_by: { id: string; cells: string[] }[];
    token: string;
}

export const fetchCapabilityUpdates = async (channel: string): Promise<CellUpstream[]> =>
    (await requestJson<{ cells: CellUpstream[] }>(`/channels/${encodeURIComponent(channel)}/capabilities/updates`, 'GET')).cells;

export const reviewReplace = (channel: string, cellId: string): Promise<ReplaceReview> =>
    requestJson(`${cellPath(channel, cellId)}/replace/review`, 'POST');

export const replaceFromUpstream = (channel: string, cellId: string, token: string): Promise<{ cell_id: string; version_id: string | null }> =>
    requestJson(`${cellPath(channel, cellId)}/replace`, 'POST', { token });
