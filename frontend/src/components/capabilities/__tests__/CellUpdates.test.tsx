import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { CapabilitiesManageBody } from '../CapabilitiesManageBody';
import { fetchGenerators, fetchRendererPreferences, fetchRenderersWithCell, fetchState } from '@/lib/api';
import { fetchCapabilityLibraries, fetchCapabilityProblems } from '@/lib/api/capabilitiesStack';
import { fetchCapabilityUpdates, replaceFromUpstream, reviewReplace, type CellUpstream, type ReplaceReview } from '@/lib/api/capabilityCells';
import { fetchHandlers } from '@/lib/definitionApi';
import { fetchRunnerGenerators } from '@/lib/runnerApi';
import { seedChannel } from './fixtures';

vi.mock('@/lib/api', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchGenerators: vi.fn(), fetchRenderersWithCell: vi.fn(), fetchRendererPreferences: vi.fn(), fetchState: vi.fn(),
}));
vi.mock('@/lib/runnerApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchRunnerGenerators: vi.fn(), fetchRunner: vi.fn() }));
vi.mock('@/lib/definitionApi', async original => ({ ...(await original<Record<string, unknown>>()), fetchHandlers: vi.fn() }));
vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), fetchCapabilityProblems: vi.fn(), fetchCapabilityLibraries: vi.fn() }));
vi.mock('@/lib/api/capabilityCells', async original => ({
    ...(await original<Record<string, unknown>>()), fetchCapabilityUpdates: vi.fn(), reviewReplace: vi.fn(), replaceFromUpstream: vi.fn(),
}));
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: [], busy: false, error: null, refresh: vi.fn() }) }));

const follow = (cellId: string, over: Partial<CellUpstream> = {}): CellUpstream => ({
    cell_id: cellId, upstream: { kind: 'app', id: '', label: 'the app' }, status: 'current', edited: false, ...over,
});
const REVIEW: ReplaceReview = {
    cell: { id: 'views', name: 'Views' }, upstream: { kind: 'app', id: '', label: 'the app' }, edited: true,
    packages: [
        { kind: 'library', id: '_views', name: 'Views lib', folder: '_views', version: null, summary: {}, change: 'changed' },
        { kind: 'renderer', id: 'old-view', name: 'Old', folder: 'old', version: null, summary: {}, change: 'removed' },
    ],
    runners: ['Nightly'], libraries: [{ id: '_env_shared', resolves: 'app' }],
    named_by: [{ id: '_views', cells: ['Tools'] }], token: 'tok',
};

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel({ applyServerState: vi.fn() });
    vi.mocked(fetchGenerators).mockResolvedValue([]);
    vi.mocked(fetchRunnerGenerators).mockResolvedValue([]);
    vi.mocked(fetchRenderersWithCell).mockResolvedValue([]);
    vi.mocked(fetchRendererPreferences).mockResolvedValue({});
    vi.mocked(fetchHandlers).mockResolvedValue([]);
    vi.mocked(fetchCapabilityProblems).mockResolvedValue([]);
    vi.mocked(fetchCapabilityLibraries).mockResolvedValue([]);
    vi.mocked(fetchState).mockResolvedValue({} as never);
    vi.mocked(fetchCapabilityUpdates).mockResolvedValue([follow('tools'), follow('views', { status: 'update', edited: true })]);
    vi.mocked(reviewReplace).mockResolvedValue(REVIEW);
    vi.mocked(replaceFromUpstream).mockResolvedValue({ cell_id: 'views', version_id: 'v2' });
});
afterEach(cleanup);

describe('the update path on Capabilities', () => {
    it('a cell’s Manage tab says what it follows and offers Review and replace when its upstream changed', async () => {
        render(<CapabilitiesManageBody scope={{ cellId: 'views' }} />);
        const strip = await screen.findByRole('region', { name: 'This cell' });
        expect(await within(strip).findByText('Update available')).toBeTruthy();
        expect(strip).toHaveTextContent('Follows the app.');
        expect(strip).toHaveTextContent('Edited here since it was added.');
        fireEvent.click(within(strip).getByRole('button', { name: 'Review and replace…' }));
        expect(await screen.findByRole('note')).toHaveTextContent('replacing discards those edits');
        expect(screen.getByRole('group', { name: 'Named by' })).toHaveTextContent('_views is named by ‘Tools’');
        expect(screen.getByRole('group', { name: 'Runners kept' })).toHaveTextContent('Nightly');
        expect(document.querySelector('[data-change="removed"]')).toHaveTextContent('goes');
        fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
        expect(await screen.findByRole('status')).toHaveTextContent('‘Views’ holds the current copy from the app.');
        expect(vi.mocked(replaceFromUpstream)).toHaveBeenCalledWith('ch', 'views', 'tok');
    });

    it('a current cell says only what it follows; a built-in card has no strip', async () => {
        const { unmount } = render(<CapabilitiesManageBody scope={{ cellId: 'tools' }} />);
        const strip = await screen.findByRole('region', { name: 'This cell' });
        expect(await within(strip).findByText('Follows the app.')).toBeTruthy();
        expect(within(strip).queryByText('Update available')).toBeNull();
        expect(within(strip).getByRole('button', { name: 'Copy to channel…' })).toBeTruthy();
        expect(within(strip).getByRole('button', { name: 'Publish to catalog…' })).toBeTruthy();
        unmount();
        render(<CapabilitiesManageBody scope={{ cellId: 'defs' }} />);
        await vi.waitFor(() => expect(vi.mocked(fetchCapabilityUpdates)).toHaveBeenCalled());
        expect(screen.queryByRole('region', { name: 'This cell' })).toBeNull();
    });

    it('the master face lists the cells with an update', async () => {
        render(<CapabilitiesManageBody active={false} rootRef={{ current: null }} onShowCell={() => {}} />);
        const strip = await screen.findByRole('region', { name: 'Updates available' });
        expect(strip).toHaveTextContent('An update is available');
        expect(within(strip).getAllByRole('listitem').map(item => item.getAttribute('data-update'))).toEqual(['views']);
        fireEvent.click(within(strip).getByRole('button', { name: 'Review and replace…' }));
        expect(await screen.findByText('Review and replace ‘Views’')).toBeTruthy();
    });
});
