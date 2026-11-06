import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { fetchCapabilityCatalog } from '@/lib/api/capabilitiesStack';
import {
    addCatalogEntry, fetchCatalogEntries, fetchSavedSuggestions, publishToCatalog, removeCatalogEntry, reviewPublish, saveSuggestion,
    type ChannelCatalogEntry, type PublishReview,
} from '@/lib/api/userCatalog';
import type { RunnerEntry } from '@/lib/runnerApi';
import { CatalogAddView } from '../CatalogAddView';
import { CellPublishView } from '../CellPublishView';
import { runner, seedChannel } from './fixtures';

vi.mock('@/lib/api/capabilitiesStack', async original => ({ ...(await original<Record<string, unknown>>()), fetchCapabilityCatalog: vi.fn(), addFromCatalog: vi.fn() }));
vi.mock('@/lib/api/userCatalog', async original => ({
    ...(await original<Record<string, unknown>>()),
    fetchCatalogEntries: vi.fn(), fetchSavedSuggestions: vi.fn(), addCatalogEntry: vi.fn(), removeCatalogEntry: vi.fn(),
    reviewPublish: vi.fn(), publishToCatalog: vi.fn(), saveSuggestion: vi.fn(), deleteSuggestion: vi.fn(),
}));
const runnerMocks = vi.hoisted(() => ({ rows: [] as RunnerEntry[] }));
vi.mock('@/stores/useRunnerStore', () => ({ useRunnerCatalog: () => ({ data: runnerMocks.rows, busy: false, error: null, refresh: vi.fn() }) }));

const ENTRY: ChannelCatalogEntry = {
    id: 'e1', name: 'Greeter', section: 'generator', libraries: ['_greetings'], revision: 2, published_at: 'now', app_version: '0.1.0',
    packages: [{ kind: 'generator', id: 'greeter', version: null }], held: false,
};

beforeEach(() => {
    vi.clearAllMocks();
    seedChannel();
    runnerMocks.rows = [runner()];
    vi.mocked(fetchCapabilityCatalog).mockResolvedValue([{ id: 'standard', name: 'Standard', description: '',
        cells: [{ folder: 'generators/web', name: 'Web', section: 'generator', held: false }] }]);
    vi.mocked(fetchCatalogEntries).mockResolvedValue([ENTRY]);
    vi.mocked(fetchSavedSuggestions).mockResolvedValue([]);
});
afterEach(cleanup);

describe('the user catalog in Add from catalog', () => {
    it('lists your catalog before the app’s cells, adds an entry and removes one', async () => {
        vi.mocked(addCatalogEntry).mockResolvedValue({ cell_id: 'new', cell_ids: ['new', 'lib'], package_skips: [] } as never);
        vi.mocked(removeCatalogEntry).mockResolvedValue();
        const onAdded = vi.fn();
        render(<CatalogAddView onBack={() => {}} onAdded={onAdded} />);
        const own = await screen.findByRole('list', { name: 'Your catalog' });
        expect(within(own).getByText('Greeter')).toBeTruthy();
        expect(within(own).getByText('names _greetings')).toBeTruthy();
        expect(await screen.findByRole('list', { name: 'Catalog cells' })).toBeTruthy();
        fireEvent.click(within(own).getByRole('button', { name: 'Add ‘Greeter’' }));
        await vi.waitFor(() => expect(onAdded).toHaveBeenCalledWith('new', 'Greeter'));
        expect(vi.mocked(addCatalogEntry)).toHaveBeenCalledWith('ch', 'e1');
        fireEvent.click(within(own).getByRole('button', { name: 'Remove ‘Greeter’ from the catalog' }));
        expect(await screen.findByRole('status')).toHaveTextContent('‘Greeter’ is out of the catalog.');
    });

    it('saves a suggestion of catalog cells and this channel’s runners', async () => {
        vi.mocked(saveSuggestion).mockResolvedValue({ id: 's1', added: ['Greetings'] });
        render(<CatalogAddView onBack={() => {}} onAdded={() => {}} />);
        await screen.findByRole('list', { name: 'Your catalog' });
        await screen.findByRole('list', { name: 'Catalog cells' });
        fireEvent.click(screen.getByRole('button', { name: 'Save a suggestion…' }));
        const form = screen.getByRole('form', { name: 'Saved suggestion' });
        fireEvent.change(within(form).getByLabelText('Name'), { target: { value: 'Mine' } });
        fireEvent.click(within(form).getByRole('checkbox', { name: /Greeter/ }));
        fireEvent.click(within(form).getByRole('checkbox', { name: /^Web/ }));
        fireEvent.click(within(form).getByRole('checkbox', { name: /Synth run/ }));
        fireEvent.click(within(form).getByRole('button', { name: 'Save suggestion' }));
        expect(await screen.findByRole('status')).toHaveTextContent('‘Mine’ is saved: the New channel page offers it. It lists ‘Greetings’ too');
        expect(vi.mocked(saveSuggestion)).toHaveBeenCalledWith({
            name: 'Mine', description: '', channel_id: 'ch', runner_ids: [runner().id],
            items: [{ source: 'catalog', id: 'e1' }, { source: 'app', section: 'generator', folder: 'generators/web', name: 'Web' }],
        }, undefined);
    });
});

describe('Publish to catalog', () => {
    const REVIEW: PublishReview = {
        name: 'Greeter', entry: null, unchanged: false, token: 'tok',
        packages: [{ kind: 'generator', id: 'greeter', name: 'Greeter', folder: 'greeter', version: null, summary: { enforces: ['commands'] } }],
        libraries: [{ id: '_greetings', resolves: null, entry: null, cell: 'Greetings' }],
    };

    it('says where each library resolves for an adder, then publishes', async () => {
        vi.mocked(reviewPublish).mockResolvedValue(REVIEW);
        vi.mocked(publishToCatalog).mockResolvedValue({ ...ENTRY, revision: 1 });
        const onDone = vi.fn();
        render(<CellPublishView cellId="tools" cellName="Tools" onBack={() => {}} onDone={onDone} />);
        expect(await screen.findByRole('group', { name: 'Libraries' })).toHaveTextContent(
            '_greetings: not in the catalog: publish ‘Greetings’ too, or a channel without it cannot add this');
        expect(within(screen.getByRole('group', { name: 'Packages' })).getByText('enforces')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Publish' }));
        await vi.waitFor(() => expect(onDone).toHaveBeenCalledWith('‘Greeter’ is in the catalog.'));
        expect(vi.mocked(publishToCatalog)).toHaveBeenCalledWith('ch', 'tools', 'tok');
    });

    it('republishes an entry and says when the catalog already holds the cell', async () => {
        vi.mocked(reviewPublish).mockResolvedValue({ ...REVIEW, entry: ENTRY, unchanged: true });
        render(<CellPublishView cellId="tools" cellName="Tools" onBack={() => {}} onDone={() => {}} />);
        expect(await screen.findByText(/^Republishes ‘Greeter’ \(revision 2\)/)).toBeTruthy();
        expect(screen.getByText('The catalog already holds this cell as it is.')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Republish' })).toBeNull();
    });
});
