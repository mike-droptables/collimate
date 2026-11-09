import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { CellRepository } from '@/lib/types';

vi.mock('@/lib/api/repositories', () => ({
    retryRescue: vi.fn(async () => ({ version_id: 'v2' })),
    discardRescue: vi.fn(async () => {}),
    rescueExportUrl: (channel: string, cellId: string, path: string) => `/api/channels/${channel}/cells/${cellId}/repositories/rescue/export?path=${path}`,
}));
vi.mock('@/lib/transfer/exportSinks', async original => ({
    ...await original<typeof import('@/lib/transfer/exportSinks')>(),
    saveCheckedZip: vi.fn(async () => 'saved'),
}));
vi.mock('@/lib/repositoryBusy', async original => ({
    ...await original<typeof import('@/lib/repositoryBusy')>(),
    offerDuplicate: vi.fn(async () => {}),
}));

import { discardRescue, retryRescue } from '@/lib/api/repositories';
import { offerDuplicate } from '@/lib/repositoryBusy';
import { saveCheckedZip } from '@/lib/transfer/exportSinks';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { RepositoryChip } from './RepositoryChip';

const repo = (patch: Partial<CellRepository> = {}): CellRepository =>
    ({ path: '', branch: 'main', upstream: 'origin/main', ahead: 0, behind: 0, detached: false, ...patch });
const RESCUE = { reason: 'cell changed since run abcd1234', rescued: true };
const mount = (r: CellRepository, extra: { readOnly?: boolean } = {}) =>
    render(<RepositoryChip repo={r} cellId="c1" channel="ch" {...extra} />);
const chip = () => screen.getByTestId('repo-chip');

const initial = useCollimateStore.getState();
beforeEach(() => { useCollimateStore.setState(initial, true); vi.clearAllMocks(); });
afterEach(cleanup);

// The branch chip (D5, §11): `main`, `main ↑2 ↓1` or `(detached)`, its hover
// the upstream or git's refusal; a rescue offers Retry, Export and Discard.
describe('the repository chip', () => {
    it('shows the branch with its upstream on hover, and no counts when even', () => {
        mount(repo());
        expect(chip().textContent).toBe('main');
        expect(chip().getAttribute('title')).toContain('main tracks origin/main');
        expect(screen.queryByTestId('repo-chip-ahead')).toBeNull();
        expect(screen.queryByTestId('repo-chip-behind')).toBeNull();
        expect(screen.queryByTestId('repo-chip-error')).toBeNull();
    });

    it('shows how far the branch is ahead of and behind its upstream', () => {
        mount(repo({ ahead: 2, behind: 1 }));
        expect(chip()).toHaveTextContent('main↑2↓1');
        expect(screen.getByTestId('repo-chip-ahead')).toHaveTextContent('↑2');
        expect(screen.getByTestId('repo-chip-behind')).toHaveTextContent('↓1');
    });

    it('a detached HEAD reads (detached)', () => {
        mount(repo({ branch: null, detached: true, upstream: null, ahead: null, behind: null }));
        expect(chip()).toHaveTextContent('(detached)');
        expect(chip().getAttribute('title')).toContain('HEAD is detached');
    });

    it('git’s refusal marks the chip and is its hover; it offers nothing', () => {
        mount(repo({ error: { reason: 'materialize failed at refresh: index.lock exists', rescued: false } }));
        expect(screen.getByTestId('repo-chip-error')).toBeInTheDocument();
        expect(chip().getAttribute('title')).toContain('materialize failed at refresh: index.lock exists');
        expect(chip().tagName).toBe('SPAN');
    });

    it('a rescue says the history was not saved and offers Retry, Export and Discard', async () => {
        mount(repo({ error: RESCUE }));
        expect(chip().getAttribute('title')).toContain('history not saved: cell changed since run abcd1234');
        fireEvent.click(chip());
        expect(await screen.findByTestId('repo-rescue-reason')).toHaveTextContent('history not saved: cell changed since run abcd1234');
        expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
        expect(screen.getByRole('button', { name: 'Export' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Discard…' })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(retryRescue).toHaveBeenCalledWith('ch', 'c1', ''));
    });

    it('a read-only view shows the rescue only', () => {
        mount(repo({ error: RESCUE }), { readOnly: true });
        expect(chip().tagName).toBe('SPAN');
    });

    it('Export saves the rescued .git from the export route', async () => {
        mount(repo({ path: 'vendor/lib', error: RESCUE }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Export' }));
        await waitFor(() => expect(saveCheckedZip).toHaveBeenCalledWith(
            '/api/channels/ch/cells/c1/repositories/rescue/export?path=vendor/lib', 'lib git history.zip'));
    });

    it('a refused Export says what the host said', async () => {
        const openAlert = vi.spyOn(useCollimateStore.getState(), 'openAlert').mockResolvedValue(true);
        vi.mocked(saveCheckedZip).mockRejectedValueOnce(Object.assign(new Error('404'), {
            response: { status: 404, data: { error: 'rescue_not_found', message: 'no rescued git history at .' } } }));
        mount(repo({ error: RESCUE }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Export' }));
        await waitFor(() => expect(openAlert).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Export failed', body: 'no rescued git history at .' })));
    });

    it('Discard confirms that a root repository’s history leaves the cell, then calls the route', async () => {
        const openConfirm = vi.spyOn(useCollimateStore.getState(), 'openConfirm').mockResolvedValue(true);
        mount(repo({ error: RESCUE }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Discard…' }));
        await waitFor(() => expect(discardRescue).toHaveBeenCalledWith('ch', 'c1', ''));
        expect(openConfirm).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Discard git history', destructive: true,
            body: expect.stringContaining('The history of the cell root’s repository (its branches, stash and reflogs) is removed from the cell too'),
        }));
    });

    it('Discard of a rescue with no repository on the tip only deletes the rescue; a cancel keeps it', async () => {
        const openConfirm = vi.spyOn(useCollimateStore.getState(), 'openConfirm').mockResolvedValue(false);
        mount(repo({ path: 'new', branch: null, upstream: null, ahead: null, behind: null, error: RESCUE }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Discard…' }));
        await waitFor(() => expect(openConfirm).toHaveBeenCalled());
        expect(openConfirm.mock.calls[0][0].body).toBe('The git history a run could not save is deleted. It cannot be recovered.');
        expect(discardRescue).not.toHaveBeenCalled();
    });

    it('a route refused while another run holds the git offers Duplicate; other refusals say the host’s words', async () => {
        const message = 'git is in use by run abcd1234 on this cell; Duplicate the cell for a second working copy';
        vi.mocked(retryRescue).mockRejectedValueOnce(Object.assign(new Error('Request failed (409)'), { response: { status: 409, data: { error: 'repository_busy', owner: 'x', run_id: 'abcd1234', message } } }));
        mount(repo({ error: RESCUE }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(offerDuplicate).toHaveBeenCalledWith('c1', message));
        const openAlert = vi.spyOn(useCollimateStore.getState(), 'openAlert').mockResolvedValue(true);
        vi.mocked(retryRescue).mockRejectedValueOnce(Object.assign(new Error('Request failed (503)'), { response: { status: 503, data: { error: 'git_unavailable', message: 'git on this machine is missing' } } }));
        fireEvent.click(chip());
        fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
        await waitFor(() => expect(openAlert).toHaveBeenCalledWith({ cellId: 'c1', title: 'Retry failed', body: 'git on this machine is missing' }));
    });
});
