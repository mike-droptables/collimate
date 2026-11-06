import { requestJson } from './client';

// The window's server's channel management routes (plan 1105, the API
// table): the manage listing that holds every channel, archived ones too,
// archive and unarchive, and a channel's exact size for the delete
// confirmation; and the server's identity for the channels page header.
// Sizes are bytes, times ISO-8601 UTC. P8 adds the Locations tab's routes
// (list, check, add, claim, default, remove), the channel move and its job,
// and the Storage tab's (the overview, refresh, Docker images, run
// environments, uv's cache, scratch and collection). The shapes are the
// server's (routes/storage.py, routes/channels.py, storage_survey.py).

/** Where a channel's folder stands: usable, being copied to another
 *  location, or unreadable (its location missing or unwritable). */
export type ManagedChannelState = 'ok' | 'moving' | 'unavailable';

/** One row of `GET /api/channels/manage`, and the archive and unarchive
 *  answers (one builder, `archive.manage_entry`). `bytes`, `files` and
 *  `computed_at` are null until the storage survey has sized the folder;
 *  `state` is `moving` while a move job holds the channel. */
export interface ManagedChannel {
    id: string;
    name: string;
    archived: boolean;
    archived_at?: string | null;
    location_id: string;
    /** The channel's folder, absolute on the server. */
    path: string;
    state: ManagedChannelState;
    reason?: string | null;
    bytes?: number | null;
    files?: number | null;
    computed_at?: string | null;
    /** The window's open channel. */
    active: boolean;
}

export interface ManagedChannels { channels: ManagedChannel[] }

/** A channel folder's size by part (`storage_survey.channel_size`). */
export interface ChannelParts {
    channel_db: number;
    git: number;
    objects: number;
    objects_trash: number;
    cell_state: number;
    other: number;
}

/** `GET /api/channels/{id}/size`: measured now (the delete confirmation). */
export interface ChannelSize { bytes: number; files: number; parts?: ChannelParts; computed_at: string }

/** `GET /api/system/instance`: who the window's server is. */
export interface ServerInstance {
    pid: number;
    data_dir: string;
    instance: string;
    version: string;
    bridge: number;
}

const channelPath = (id: string) => `/channels/${encodeURIComponent(id)}`;

/** Every channel of the window's server, archived ones included. */
export const fetchManagedChannels = async (): Promise<ManagedChannels> =>
    requestJson('/channels/manage', 'GET');

/** Archive a channel: it leaves every channel list and opens read-only.
 *  Refused (409) while runs are live on it or it is moving. Answers the
 *  channel's manage-listing row. */
export const archiveChannel = async (id: string): Promise<ManagedChannel> =>
    requestJson(`${channelPath(id)}/archive`, 'POST');

/** Unarchive a channel: it is listed and writable again. */
export const unarchiveChannel = async (id: string): Promise<ManagedChannel> =>
    requestJson(`${channelPath(id)}/unarchive`, 'POST');

/** A channel folder's exact size, for the delete confirmation. */
export const fetchChannelSize = async (id: string): Promise<ChannelSize> =>
    requestJson(`${channelPath(id)}/size`, 'GET');

/** The window's server: its data dir and version, for the page header. */
export const fetchServerInstance = async (): Promise<ServerInstance> =>
    requestJson('/system/instance', 'GET');

// ---------------------------------------------------------------------------
// Refusals
// ---------------------------------------------------------------------------

/** A coded refusal or warning: `{code, message}` (the storage routes' 400
 *  and 409 `detail`, a check's `error`, a job's `error`). */
export interface CodedMessage { code: string; message: string }

/** The refusal's whole `detail` object when it is coded (with its extra
 *  fields: `owned_by`, `owner_data_dir`, `channel_count`), else null (a
 *  plain-sentence detail, a network failure). */
export function refusalDetail(error: unknown): (CodedMessage & Record<string, unknown>) | null {
    const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
    return detail && typeof detail === 'object' && typeof (detail as { code?: unknown }).code === 'string'
        ? detail as CodedMessage & Record<string, unknown>
        : null;
}

/** The `code` of a coded refusal, or null. */
export const refusalCode = (error: unknown): string | null => refusalDetail(error)?.code ?? null;

// ---------------------------------------------------------------------------
// Locations (plan 1105 S2a, P5)
// ---------------------------------------------------------------------------

/** `ok`; `missing` (the folder is gone, its channels unlisted);
 *  `unwritable`; `foreign` (another installation claimed it). */
export type LocationState = 'ok' | 'missing' | 'unwritable' | 'foreign';
/** What a location row offers: `claim` for a foreign one (its only
 *  action), `set_default` and `remove` as they apply. */
export type LocationAction = 'claim' | 'set_default' | 'remove';

/** One row of `GET /api/storage/locations`, re-read from disk. A foreign
 *  location reports its owner and channel count, nothing else. `bytes` is
 *  always null here (the storage overview sizes folders); free and total
 *  are null for a foreign or missing one. */
export interface StorageLocation {
    id: string;
    path: string;
    is_default: boolean;
    state: LocationState;
    owner_data_dir?: string | null;
    channel_count: number;
    bytes: number | null;
    free_bytes: number | null;
    total_bytes: number | null;
    /** `network_filesystem`: channels there are slower. */
    warning?: CodedMessage;
    actions: LocationAction[];
}

export interface StorageLocations { locations: StorageLocation[] }

/** The installation a folder's marker names (`check`'s `owned_by`, and the
 *  `location_owned` refusal's). `owner_running` is null when the owner
 *  cannot be checked (its data dir gone or on another machine): a claim
 *  then needs `confirm_unchecked` (D18). */
export interface LocationOwner {
    installation_id: string | null;
    data_dir: string | null;
    channel_count: number;
    owner_running: boolean | null;
}

/** `POST /api/storage/locations/check`. `error.code` is one of
 *  `not_absolute`, `not_directory`, `not_writable`, `inside_data_dir`,
 *  `overlaps_location`, `not_empty`, `location_owned` (with `owned_by`).
 *  `creates`: the folder does not exist yet and adding it makes it. */
export interface LocationCheck {
    ok: boolean;
    path: string;
    creates: boolean;
    warning?: CodedMessage;
    owned_by?: LocationOwner;
    error?: CodedMessage;
}

/** A channel a claim or an add did not adopt (its id is already here). */
export interface SkippedChannel { id: string; reason: string }

/** `POST /api/storage/locations`: `claimed` when a claim (or a folder of
 *  ours from before) brought channels. */
export interface AddedLocation {
    location: StorageLocation;
    claimed?: { adopted: string[]; skipped: SkippedChannel[] };
}

/** `POST /api/storage/locations/{id}/claim`. */
export interface ClaimedLocation { location: StorageLocation; adopted: string[]; skipped: SkippedChannel[] }

export interface AddLocationRequest {
    path: string;
    /** Take ownership of a folder another installation owns. */
    claim?: boolean;
    /** Make it the default for new channels. */
    default?: boolean;
    /** With `claim`: claim although the owner cannot be checked. */
    confirm_unchecked?: boolean;
}

const locationPath = (id: string) => `/storage/locations/${encodeURIComponent(id)}`;

export const fetchLocations = async (): Promise<StorageLocations> =>
    requestJson('/storage/locations', 'GET');

/** Validate a folder before adding it; a refusal is the answer's `error`,
 *  never a failed request. */
export const checkLocation = async (path: string): Promise<LocationCheck> =>
    requestJson('/storage/locations/check', 'POST', { path });

/** 201; 400 with a validation code; 409 `location_owned` without `claim`,
 *  `owner_running`, `owner_unchecked`. */
export const addLocation = async (body: AddLocationRequest): Promise<AddedLocation> =>
    requestJson('/storage/locations', 'POST', body);

/** Claim a listed foreign location. 409 `owner_running`, `owner_unchecked`
 *  (send `confirmUnchecked` once the user confirms), `not_foreign`. */
export const claimLocation = async (id: string, confirmUnchecked = false): Promise<ClaimedLocation> =>
    requestJson(`${locationPath(id)}/claim`, 'POST', { confirm_unchecked: confirmUnchecked });

/** New channels go here; no channel moves. 409 `location_foreign`,
 *  `location_missing`, `location_unwritable`. */
export const setDefaultLocation = async (id: string): Promise<StorageLocation> =>
    requestJson(`${locationPath(id)}/default`, 'PUT');

/** 204. 409 `location_is_default`, `location_not_empty`. A foreign or
 *  missing entry is just dropped from the list. */
export const removeLocation = async (id: string): Promise<void> => {
    await requestJson(locationPath(id), 'DELETE');
};

// ---------------------------------------------------------------------------
// The channel move (plan 1105 S7, P5)
// ---------------------------------------------------------------------------

export type MoveJobState = 'copying' | 'verifying' | 'switching' | 'cleaning' | 'done' | 'failed';

/** `GET /api/storage/jobs/{id}`: a move's progress. `method` is null until
 *  the job knows whether it renames (one filesystem) or copies. A finished
 *  job stays readable for ten minutes, then 404s. */
export interface MoveJob {
    id: string;
    kind: 'move';
    channel_id: string;
    from_location_id: string;
    to_location_id: string;
    state: MoveJobState;
    method: 'rename' | 'copy' | null;
    bytes_done: number;
    bytes_total: number;
    files_done: number;
    files_total: number;
    started_at: string;
    finished_at: string | null;
    error?: CodedMessage;
    /** Done, but the source's removal failed (left for the next start). */
    warning?: string;
}

/** Whether a job has ended (done or failed). */
export const moveEnded = (job: Pick<MoveJob, 'state'>): boolean => job.state === 'done' || job.state === 'failed';

/** 202 `{job_id}`. 409 with a code: `runs_live`, `move_in_progress`,
 *  `channel_busy`, `location_foreign`, `location_missing`,
 *  `location_unwritable`, `same_location`, `destination_exists`,
 *  `pins_held`, `collection_in_flight`, `insufficient_space`,
 *  `channel_moving`; 404 `location_not_found`. */
export const moveChannel = async (id: string, locationId: string): Promise<{ job_id: string }> =>
    requestJson(`${channelPath(id)}/move`, 'POST', { location_id: locationId });

export const fetchStorageJob = async (jobId: string): Promise<MoveJob> =>
    requestJson(`/storage/jobs/${encodeURIComponent(jobId)}`, 'GET');

// ---------------------------------------------------------------------------
// The storage overview (plan 1105 S8, P6)
// ---------------------------------------------------------------------------

/** Every part of the overview carries the time it was measured. */
interface Measured { computed_at?: string | null }

/** `idle`, `running` (with `pending`: the parts still to measure, the
 *  machine-wide ones last) or `failed` (with `error`). */
export interface StorageSurvey {
    state: 'idle' | 'running' | 'failed';
    computed_at: string | null;
    started_at?: string;
    pending?: string[];
    error?: string;
}

/** A channel inside an overview location, largest first. */
export interface OverviewChannel {
    id: string;
    name: string;
    archived: boolean;
    bytes: number | null;
    files: number | null;
    parts: ChannelParts | null;
    computed_at: string | null;
}

/** A location in the overview. A foreign one carries only its id, path,
 *  state, owner and (once surveyed) its channel count. `bytes` is its
 *  channels plus `other_bytes` (its marker, `.orphaned/`, a `.moving`
 *  copy); free and total only for an `ok` one. */
export interface OverviewLocation {
    id: string;
    path: string;
    is_default: boolean;
    state: LocationState;
    owner_data_dir?: string | null;
    channel_count?: number;
    bytes?: number;
    other_bytes?: number | null;
    channels?: OverviewChannel[];
    free_bytes?: number;
    total_bytes?: number;
}

export interface BytesPart extends Measured { bytes: number }
export interface StorePart extends Measured { bytes: number; trash_bytes: number }
export interface EnvsPart extends Measured { count: number; bytes: number; reclaimable_bytes: number }
export interface ScratchPart extends Measured { tmp_bytes: number; tmp_live_bytes: number; pycache_bytes: number; live_runs: number }
export interface PathPart extends Measured { path: string | null; bytes: number; reason?: string }
export interface PreviousLayoutPart extends Measured { paths: string[]; bytes?: number; note: string }
/** The machine's Docker images: `{available: false, reason}` when the
 *  daemon cannot be read. `exact` false: `images_bytes` is at least that
 *  (the containerd store); the same for `unused_*`. */
export type DockerPart = Measured & ({ available: false; reason: string } | {
    available: true;
    count: number;
    images_bytes: number;
    exact: boolean;
    unused_days: number;
    unused_count: number;
    unused_bytes: number;
    unused_exact: boolean;
    build_cache_bytes: number;
});

/** `GET /api/storage`: what the window's server stores and where. A part
 *  not measured yet is absent. `totals` counts the locations and the data
 *  dir, never uv's cache, the adapters or Docker (every server on the
 *  machine shares them). */
export interface StorageOverview {
    server: { data_dir: string; instance: string; installation_id: string; version: string };
    survey: StorageSurvey;
    totals: { bytes: number; computed_at: string | null };
    locations: OverviewLocation[];
    app_db?: BytesPart;
    app_store?: StorePart;
    staged_store?: StorePart;
    envs?: EnvsPart;
    scratch?: ScratchPart;
    other?: BytesPart;
    uv_cache?: PathPart;
    adapters?: PathPart;
    docker?: DockerPart;
    previous_layout?: PreviousLayoutPart;
    last_collection_at: string | null;
}

/** With nothing measured yet the GET starts a survey (`survey.state`
 *  running). */
export const fetchStorageOverview = async (): Promise<StorageOverview> =>
    requestJson('/storage', 'GET');

/** 202: start a survey, or join the running one. */
export const refreshStorage = async (): Promise<{ survey: StorageSurvey }> =>
    requestJson('/storage/refresh', 'POST');

/** `POST /api/storage/scratch/clear`; 409 (a plain sentence) while a run
 *  is starting, live or finishing, or an export is in progress. */
export interface ScratchCleared { bytes_freed: number; removed: number; kept: { name: string; reason: string }[] }
export const clearScratch = async (): Promise<ScratchCleared> =>
    requestJson('/storage/scratch/clear', 'POST');

/** `POST /api/storage/collect`: every body store, then the git stores. */
export interface CollectionResult {
    channels: { id: string; freed_bytes?: number; [figure: string]: unknown }[];
    app_store: Record<string, unknown>;
    staging: Record<string, unknown>;
    git: unknown[];
    freed_bytes: number;
    last_collection_at: string | null;
}
export const collectNow = async (): Promise<CollectionResult> =>
    requestJson('/storage/collect', 'POST');

// ---------------------------------------------------------------------------
// Docker images and run environments (plan 1105 S4, S5; P2, P3)
// ---------------------------------------------------------------------------

/** Every cleanup selects `all`, or those `unused` in the last `days`. */
export interface CleanupSelection { mode: 'all' | 'unused'; days?: number }

/** A Collimate-built image. `last_used_at` is this server's last recorded
 *  use (absent: never recorded, so it counts as unused). */
export interface DockerImage {
    id: string;
    refs: string[];
    other_refs: string[];
    size: number;
    shared_size: number;
    unique_size: number;
    containers: number;
    created_at: string;
    last_used_at?: string;
}

/** `POST /api/storage/docker/preview`: `bytes` exact when `exact`, else a
 *  lower bound. Valid once, within ten minutes. 503 (a sentence) when
 *  Docker is not reachable. */
export interface DockerPreview { preview_id: string; count: number; bytes: number; exact: boolean; expires_at: string; images: DockerImage[] }
export interface DockerDeleted { removed: string[]; failed: { ref: string; reason: string }[]; bytes_freed_estimate: number; exact: boolean }

export const previewDockerImages = async (selection: CleanupSelection): Promise<DockerPreview> =>
    requestJson('/storage/docker/preview', 'POST', selection);
/** 404 for an unknown, used or expired preview; 503 without Docker. */
export const deleteDockerImages = async (previewId: string): Promise<DockerDeleted> =>
    requestJson('/storage/docker/delete', 'POST', { preview_id: previewId });

/** A run environment: a generator version's stable tree and its uv
 *  environment once known. `reclaimable_bytes` is what deleting it frees;
 *  a `live` one (a run uses it) is never selected. */
export interface RunEnvironment {
    key: string;
    generator_id: string;
    tree: string;
    uv_env?: string;
    bytes: number;
    reclaimable_bytes: number;
    last_used_at: string;
    live: boolean;
}

export interface EnvsPreview { preview_id: string; count: number; bytes: number; exact: boolean; envs: RunEnvironment[] }
export interface EnvsDeleted { removed: string[]; failed: { key: string; reason: string }[]; bytes_freed_estimate: number }

export const previewRunEnvironments = async (selection: CleanupSelection): Promise<EnvsPreview> =>
    requestJson('/storage/envs/preview', 'POST', selection);
/** 404 for an unknown, used or expired preview. */
export const deleteRunEnvironments = async (previewId: string): Promise<EnvsDeleted> =>
    requestJson('/storage/envs/delete', 'POST', { preview_id: previewId });

/** `uv cache prune` for the whole machine; 409 (a sentence) while any run
 *  on this server is live. */
export const pruneUvCache = async (): Promise<{ ok: boolean; output: string }> =>
    requestJson('/storage/uv/prune', 'POST');
