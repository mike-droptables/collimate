import { requestJson } from './client';


/** A run start (POST /channels/{ch}/runs). `newCell` first clones the
 *  cell's exact file state (tip, in-memory files and pending drafts, minus
 *  `_runs/`) into a new stack right of the cell's stack, then runs there.
 *  `selectedFile` is the cell-relative path the run reads as
 *  `gen.selected_file` (only for a runner that takes one). Runs never start
 *  on a Capabilities cell. */
export interface RunRequest {
    runner: string;
    runnerRevision?: string;
    cellId: string;
    newCell?: boolean;
    selectedFile?: string;
}


/** POST /runs answer: the start was accepted (the process spawns after
 *  the answer). `cell_id` is the cell the run works in: the clone for a
 *  `new_cell` start (with its new `stack_id`), else the request's. `opens`
 *  lists the cell-relative surface files the host created at once, in the
 *  manifest's order; the first is the one to reveal. */
export interface RunStartResponse {
    run_id: string;
    status: 'queued';
    cell_id: string;
    stack_id?: string;
    runner_id: string;
    runner_name: string;
    label: string;
    folder: string;
    generator_name: string;
    opens: string[];
}


/** The server's refusal of a start as one readable sentence: FastAPI's
 *  `detail`, the missing-required-fields listing, a surface another run
 *  holds, a selected file gone from the cell, or a transport fallback. */
function describeRunStartFailure(status: number, body: unknown): string {
    if (body && typeof body === 'object') {
        const value = body as {
            detail?: unknown; code?: unknown; error?: unknown; message?: unknown;
            fields?: { label?: string; key?: string }[];
        };
        if (typeof value.detail === 'string') return value.detail;
        if (value.error === 'surface_taken' && typeof value.message === 'string') return value.message;
        if (value.error === 'selected_file_missing') {
            return 'The selected file is no longer in this cell — select another file and run again.';
        }
        if (value.code === 'missing_required' && Array.isArray(value.fields)) {
            const labels = value.fields.map(f => f.label || f.key).filter(Boolean);
            return `Cannot run — required fields are empty: ${labels.join(', ')}`;
        }
    }
    return status >= 500
        ? "Couldn't reach the server — check your connection."
        : `Failed to start run (HTTP ${status}).`;
}

/** Start a run. Throws an Error carrying the server's own sentence. */
export const postRun = async (channel: string, req: RunRequest): Promise<RunStartResponse> => {
    const body = {
        runner: req.runner,
        ...(req.runnerRevision ? { runner_revision: req.runnerRevision } : {}),
        cell_id: req.cellId,
        ...(req.newCell ? { new_cell: true } : {}),
        ...(req.selectedFile ? { selected_file: req.selectedFile } : {}),
    };
    try {
        return await requestJson<RunStartResponse>(`/channels/${encodeURIComponent(channel)}/runs`, 'POST', body);
    } catch (error) {
        const response = (error as { response?: { status: number; data: unknown } })?.response;
        throw new Error(response ? describeRunStartFailure(response.status, response.data)
            : "Couldn't reach the server — check your connection.", { cause: error });
    }
};


/** The surface a live run holds (its inbox pointer): `starting` from the
 *  run's acceptance until its generator registers it, then `live`. */
export type ActiveRunSurface = NonNullable<import('../types').RunRecord['surfaces']>[number] & {
    state: 'starting' | 'live';
};

/** One row of GET /runs/active. */
export interface ActiveRunInfo {
    run_id: string;
    cell_id: string;
    /** The cell's name on its canvas ('' once it left it). */
    cell_name: string;
    runner_id: string | null;
    runner_name: string | null;
    label: string;
    folder: string;
    generator_name: string;
    /** The run's start (its runs row), UTC ISO 8601; null without a row. */
    started_at: string | null;
    /** The run's live phase ('' before the first one) and its message —
     *  re-seeded on reload. */
    phase?: string;
    phase_message?: string;
    /** The run's end is under way (a stop was requested or its status is
     *  final); it stays listed until its run-end cleanup is done. */
    stopping?: boolean;
    /** The run reads its annotation context: it has polled it at least once. */
    listening?: boolean;
    surfaces?: ActiveRunSurface[];
    /** The grant the run runs under; null for a run no harness binds. */
    effective_grant?: ActiveRunGrant | null;
    /** The run's grant was raised to its harness's minimum (an Annotation
     *  run adopts `docker: full` for a harness that needs it). */
    adopted?: boolean;
    /** The harness the run's model drives, and its display label; null for
     *  a run no harness binds. */
    harness?: string | null;
    harness_label?: string | null;
    /** Where a CLI run's harness CLI runs: `host` (this machine, under the
     *  CLI's own sign-in) or `container`; null for any other run. */
    cli_locus?: 'host' | 'container' | null;
}

/** A run's effective grant, as its /runs/active row carries it. */
export interface ActiveRunGrant {
    docker: string;
    internet: string;
    commands: string;
    internet_allow?: string[];
    update_packages?: boolean;
}

export const fetchActiveRuns = async (channel: string): Promise<ActiveRunInfo[]> =>
    requestJson(`/channels/${encodeURIComponent(channel)}/runs/active`, 'GET');


/** One channel's entry in GET /runs/active (every channel). `channel` is
 *  the channel's UUID, `runs` its own /runs/active rows. */
export interface ChannelActiveRuns {
    channel: string;
    channel_name: string;
    runs: ActiveRunInfo[];
}

/** Every channel's live runs: the top bar's running count and runs menu. */
export const fetchActiveRunsEverywhere = async (): Promise<ChannelActiveRuns[]> =>
    requestJson('/runs/active', 'GET');


export const stopRun = async (channel: string, runId: string, force = false) =>
    requestJson(`/channels/${encodeURIComponent(channel)}/runs/${encodeURIComponent(runId)}${force ? '?force=true' : ''}`, 'DELETE');


/** Stop every running cell server-wide and tear down sandbox containers
 *  (the runs menu's Stop all). */
export const stopAllRuns = async () => requestJson('/system/stop_all', 'POST');
