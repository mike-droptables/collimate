import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { copyToChannel, reviewCopy, type CopyReview } from '@/lib/api/capabilityCells';
import { CellCopyView } from '../CellCopyView';
import { seedChannel } from './fixtures';

vi.mock('@/lib/api/capabilityCells', async original => ({
    ...(await original<Record<string, unknown>>()), reviewCopy: vi.fn(), copyToChannel: vi.fn(),
}));

const REVIEW: CopyReview = {
    source: { channel_id: 'ch', channel: 'ch', cell_id: 'tools' },
    target: { channel_id: 'other', channel: 'Other' },
    cells: [
        { name: 'Tools', brought: false, packages: [{ kind: 'generator', id: 'greeter', name: 'Greeter', folder: 'greeter', version: null,
            summary: { name: 'Greeter', adopts_harness_grant: true } }] },
        { name: 'Greetings', brought: true, packages: [{ kind: 'library', id: '_greetings', name: 'Greetings', folder: '_greetings', version: '1', summary: {} }] },
    ],
    libraries: [{ id: '_greetings', resolves: 'copy', cell: 'Greetings' }, { id: '_env_shared', resolves: 'app', cell: null }],
    skipped: [],
    token: 'tok',
};

beforeEach(() => {
    vi.clearAllMocks();
    const channel = seedChannel();
    useCollimateStore.setState({ state: { active_channel_id: 'ch', channels: [channel, { ...channel, id: 'other', name: 'Other' }] } as never });
    vi.mocked(reviewCopy).mockResolvedValue(REVIEW);
    vi.mocked(copyToChannel).mockResolvedValue({ cell_id: 'new', cell_ids: ['new', 'lib'], package_skips: [] });
});
afterEach(cleanup);

describe('Copy to channel', () => {
    it('reviews the copy on the other channel, then copies it as reviewed', async () => {
        const onDone = vi.fn();
        render(<CellCopyView cellId="tools" cellName="Tools" onBack={() => {}} onDone={onDone} />);
        expect(within(screen.getByRole('combobox', { name: 'Channel' })).getAllByRole('option').map(o => o.textContent)).toEqual(['Other']);
        fireEvent.click(screen.getByRole('button', { name: 'Review the copy' }));
        expect(await screen.findByRole('group', { name: 'Brought along: Greetings' })).toBeInTheDocument();
        expect(vi.mocked(reviewCopy)).toHaveBeenCalledWith('ch', 'tools', 'other');
        // Add to's summary keys, the privilege keys among them.
        expect(within(screen.getByRole('group', { name: 'Tools' })).getByText('adopts_harness_grant')).toBeInTheDocument();
        const libraries = screen.getByRole('group', { name: 'Libraries' });
        expect(libraries).toHaveTextContent('_greetings: comes with the copy, in ‘Greetings’');
        expect(libraries).toHaveTextContent('_env_shared: the app’s');
        fireEvent.click(screen.getByRole('button', { name: 'Copy to ‘Other’' }));
        await vi.waitFor(() => expect(onDone).toHaveBeenCalledWith('‘Tools’ is on ‘Other’’s Capabilities.'));
        expect(vi.mocked(copyToChannel)).toHaveBeenCalledWith('ch', 'tools', 'other', 'tok');
    });

    it('shows a refused review’s reason', async () => {
        vi.mocked(reviewCopy).mockRejectedValue(new Error('‘Tools’ names ‘_missing’ under shared:'));
        render(<CellCopyView cellId="tools" cellName="Tools" onBack={() => {}} onDone={() => {}} />);
        fireEvent.click(screen.getByRole('button', { name: 'Review the copy' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('‘_missing’');
        expect(screen.queryByRole('button', { name: /^Copy to/ })).toBeNull();
    });
});
