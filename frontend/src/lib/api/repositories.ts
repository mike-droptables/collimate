// A cell's rescued git history (api/collimate_server/routes/cells.py, `?path=<repo_path>`):
// what a run's harvest could not keep waits beside the channel's store until
// the user retries, exports or discards it. Unknown paths are 404
// `rescue_not_found`; while a run holds the cell's git each is 409
// `repository_busy`.
import { getApiUrl, requestJson } from './client';

const rescuePath = (channel: string, cellId: string) =>
    `/channels/${encodeURIComponent(channel)}/cells/${encodeURIComponent(cellId)}/repositories/rescue`;

/** Ingest the rescued `.git` again, as the harvest would have; the version
 *  it wrote. 503 `git_unavailable` without a usable git. */
export const retryRescue = (channel: string, cellId: string, repoPath: string): Promise<{ version_id: string }> =>
    requestJson(`${rescuePath(channel, cellId)}/retry?${new URLSearchParams({ path: repoPath })}`, 'POST');

/** The rescued `.git` as a streamed zip (an attachment): a download's href. */
export const rescueExportUrl = (channel: string, cellId: string, repoPath: string): string =>
    `${getApiUrl()}${rescuePath(channel, cellId)}/export?${new URLSearchParams({ path: repoPath })}`;

/** Delete the rescue. With a repository on the tip at the path, its rows go
 *  too, in a "Discard git history" version. */
export const discardRescue = async (channel: string, cellId: string, repoPath: string): Promise<void> => {
    await requestJson(`${rescuePath(channel, cellId)}?${new URLSearchParams({ path: repoPath })}`, 'DELETE');
};
