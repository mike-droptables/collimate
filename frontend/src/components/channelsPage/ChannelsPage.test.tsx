import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ChannelsPage, type ChannelsPageProps } from './ChannelsPage';
import {
    archiveChannel, fetchChannelSize, fetchLocations, fetchManagedChannels, fetchServerInstance, fetchStorageJob, moveChannel, unarchiveChannel,
    type ManagedChannel, type MoveJob, type StorageLocation,
} from '@/lib/api/storage';
import { deleteChannel } from '@/lib/api/channels';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { retiredOr } from '@/test/retiredTokens';

// The channels page (plan 1105 P7, P8) against mocked routes: its tabs and
// default tab, the header naming the window's server, the Channels and
// Archived tables (archived channels only on Archived, whose Open selects
// them), archive and unarchive, Move… with the job's progress on the row,
// Delete… with the size it frees, the location column and "Saved in" from
// the locations, the no-default notice, and the Servers tab only in the
// desktop shell.

vi.mock('@/lib/api/storage', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/api/storage')>(),
    fetchManagedChannels: vi.fn(), fetchServerInstance: vi.fn(), archiveChannel: vi.fn(), unarchiveChannel: vi.fn(), fetchChannelSize: vi.fn(),
    fetchLocations: vi.fn(), moveChannel: vi.fn(), fetchStorageJob: vi.fn(),
}));
vi.mock('@/lib/api/channels', () => ({ deleteChannel: vi.fn() }));
vi.mock('./StorageTab', () => ({ StorageTab: () => <div>storage overview</div> }));
vi.mock('./NewTab', () => ({ NewTab: (props: { shown: boolean; onCancel?: () => void; channels: unknown[]; savedIn: string | null }) =>
    <div data-testid="new-tab" data-shown={String(props.shown)} data-cancel={props.onCancel ? 'yes' : 'no'} data-saved-in={props.savedIn ?? ''} /> }));
vi.mock('./ServersTab', () => ({ ServersTab: () => <div>servers cards</div> }));

const row = (id: string, name: string, over: Partial<ManagedChannel> = {}): ManagedChannel => ({
    id, name, archived: false, location_id: 'loc', path: `/home/me/Collimate/Channels/${id}`, state: 'ok', active: false, bytes: 2048, computed_at: '2026-11-05T10:00:00Z', ...over,
});
const LISTING = [
    row('a', 'Alpha', { active: true }),
    row('b', 'Bravo', { state: 'moving' }),
    row('z', 'Zulu', { archived: true, archived_at: '2026-11-04T09:00:00Z', bytes: null }),
];

const props = (over: Partial<ChannelsPageProps> = {}): ChannelsPageProps => ({
    tab: 'new', onTab: vi.fn(), onClose: vi.fn(), onCreate: vi.fn(), channels: [{ id: 'a', name: 'Alpha' }], sourceChannelId: 'a',
    onOpenChannel: vi.fn(async () => {}), onRenameChannel: vi.fn(async () => {}), onChannelsChanged: vi.fn(), ...over,
});
const page = () => screen.getByRole('dialog', { name: 'Channels' });
const table = (name: string) => screen.findByRole('table', { name });
const rowOf = (name: string) => screen.getByRole('rowheader', { name: new RegExp(name) }).closest('tr') as HTMLElement;

const location = (id: string, path: string, over: Partial<StorageLocation> = {}): StorageLocation => ({
    id, path, is_default: false, state: 'ok', channel_count: 0, bytes: null, free_bytes: 10 * 2 ** 30, total_bytes: 100 * 2 ** 30, actions: [], ...over,
});
const LOCATIONS = [
    location('loc', '/home/me/Collimate/Channels', { is_default: true, channel_count: 3 }),
    location('usb', '/media/usb/Channels', { actions: ['set_default', 'remove'] }),
];
const job = (over: Partial<MoveJob> = {}): MoveJob => ({
    id: 'j1', kind: 'move', channel_id: 'a', from_location_id: 'loc', to_location_id: 'usb', state: 'copying', method: 'copy',
    bytes_done: 0, bytes_total: 4096, files_done: 0, files_total: 8, started_at: '2026-11-05T10:00:00Z', finished_at: null, ...over,
});

beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: LISTING });
    vi.mocked(fetchLocations).mockResolvedValue({ locations: LOCATIONS });
    vi.mocked(fetchServerInstance).mockResolvedValue({ pid: 1, data_dir: '/srv/collimate', instance: 'i', version: '0.9.0', bridge: 8 });
});
afterEach(() => { cleanup(); uninstallDesktopShell(); });

describe('the channels page (plan 1105 P7)', () => {
    it('offers New, Channels, Archived, Locations and Storage in a browser, on the tab asked for, under a header naming this server', async () => {
        render(<ChannelsPage {...props()} />);
        expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['New', 'Channels', 'Archived', 'Locations', 'Storage']);
        expect(screen.getByRole('tab', { name: 'New' })).toHaveAttribute('aria-selected', 'true');
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-shown', 'true');
        expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(window.location.host);
        await waitFor(() => expect(document.querySelector('[data-server-identity]')).toHaveTextContent('/srv/collimate · Collimate v0.9.0'));
        // The archived count rides on its tab.
        await waitFor(() => expect(screen.getByRole('tab', { name: /Archived/ })).toHaveTextContent('Archived 1'));
        expect(page()).toHaveAttribute('data-channel-create');
        expect(page()).toHaveClass('bg-canvas', 'text-ink');
    });

    it('adds Servers in the desktop shell, named by the shell; Servers in a browser shows New', async () => {
        installDesktopShell();
        const onTab = vi.fn();
        render(<ChannelsPage {...props({ tab: 'servers', onTab })} />);
        expect(screen.getAllByRole('tab').map(tab => tab.textContent?.replace(/\s\d+$/, ''))).toEqual(['New', 'Channels', 'Archived', 'Locations', 'Storage', 'Servers']);
        expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Local');
        expect(screen.getByText('servers cards')).toBeInTheDocument();
        // The New tab stays mounted (hidden) so a half-made channel survives.
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-shown', 'false');
        fireEvent.click(screen.getByRole('tab', { name: /Channels/ }));
        expect(onTab).toHaveBeenCalledWith('channels');
        cleanup();
        uninstallDesktopShell();
        render(<ChannelsPage {...props({ tab: 'servers' })} />);
        expect(screen.getByRole('tab', { name: 'New' })).toHaveAttribute('aria-selected', 'true');
        expect(screen.queryByText('servers cards')).toBeNull();
    });

    it('Channels lists every channel that is not archived, with server, location, size and state on each row', async () => {
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        const channels = await table('Channels');
        expect(within(channels).getAllByRole('rowheader').map(h => h.textContent)).toEqual(['AlphaOpen', 'Bravo']);
        expect(within(channels).queryByText('Zulu')).toBeNull();
        const alpha = rowOf('Alpha');
        expect(alpha).toHaveTextContent(window.location.host);
        expect(alpha).toHaveTextContent('/home/me/Collimate/Channels');
        expect(alpha).toHaveTextContent('2.0 KB');
        expect(alpha).toHaveTextContent('Ready');
        // A moving channel does not open or archive, and says why.
        const bravo = rowOf('Bravo');
        expect(bravo).toHaveTextContent('Moving');
        expect(within(bravo).getByRole('button', { name: 'Open Bravo' })).toBeDisabled();
        expect(within(bravo).getByRole('button', { name: 'Archive Bravo' })).toBeDisabled();
    });

    it('Move… moves to the chosen location and the row shows the job until it ends, polling every 500 ms', async () => {
        vi.mocked(moveChannel).mockResolvedValue({ job_id: 'j1' });
        vi.mocked(fetchStorageJob)
            .mockResolvedValueOnce(job({ bytes_done: 2048 }))
            .mockResolvedValue(job({ state: 'done', bytes_done: 4096, files_done: 8, finished_at: '2026-11-05T10:00:05Z' }));
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        await table('Channels');
        await waitFor(() => expect(fetchLocations).toHaveBeenCalled());
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Move Alpha' }));
        const dialog = await screen.findByRole('dialog', { name: 'Move Alpha' });
        // Its own location is not a destination; the other one is chosen.
        expect(within(dialog).getByRole('radio', { name: /Collimate\/Channels/ })).toBeDisabled();
        expect(within(dialog).getByRole('radio', { name: /media\/usb/ })).toBeChecked();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
        await waitFor(() => expect(moveChannel).toHaveBeenCalledExactlyOnceWith('a', 'usb'));
        await waitFor(() => expect(within(dialog).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50'), { timeout: 2000 });
        expect(rowOf('Alpha')).toHaveTextContent('Moving 50%');
        await waitFor(() => expect(within(dialog).getByRole('status')).toHaveTextContent('Moved to /media/usb/Channels'), { timeout: 2000 });
        // The ended job reads the lists again.
        await waitFor(() => expect(vi.mocked(fetchManagedChannels).mock.calls.length).toBeGreaterThanOrEqual(3));
        fireEvent.click(within(dialog).getAllByRole('button', { name: 'Close' }).at(-1)!);
        expect(screen.queryByRole('dialog', { name: 'Move Alpha' })).toBeNull();
    });

    it('a refused move says why in its dialog and starts nothing; a failed job says the channel stayed', async () => {
        const refusal = Object.assign(new Error('x'), { response: { status: 409, data: { detail: { code: 'runs_live', message: 'Stop the runs in Alpha first.' } } } });
        vi.mocked(moveChannel).mockRejectedValueOnce(refusal).mockResolvedValue({ job_id: 'j1' });
        vi.mocked(fetchStorageJob).mockResolvedValue(job({ state: 'failed', error: { code: 'insufficient_space', message: 'Not enough space.' } }));
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        await table('Channels');
        await waitFor(() => expect(fetchLocations).toHaveBeenCalled());
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Move Alpha' }));
        const dialog = await screen.findByRole('dialog', { name: 'Move Alpha' });
        fireEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
        const refused = await within(dialog).findByRole('alert');
        expect(refused).toHaveTextContent('Stop the runs in Alpha first.');
        expect(refused).toHaveAttribute('data-move-refusal', 'runs_live');
        expect(fetchStorageJob).not.toHaveBeenCalled();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Move' }));
        const failed = await waitFor(() => { const el = dialog.querySelector('[data-move-error]'); expect(el).not.toBeNull(); return el as HTMLElement; }, { timeout: 2000 });
        expect(failed).toHaveTextContent('Not enough space. The channel stays where it was.');
        expect(failed).toHaveAttribute('data-move-error', 'insufficient_space');
    });

    it('Delete… confirms by name with the size it frees, deletes, then reads the listing and the app again', async () => {
        vi.mocked(fetchChannelSize).mockResolvedValue({ bytes: 5 * 2 ** 20, files: 40, computed_at: '2026-11-05T10:00:00Z' });
        vi.mocked(deleteChannel).mockResolvedValue({});
        const p = props({ tab: 'channels' });
        render(<ChannelsPage {...p} />);
        await table('Channels');
        // Bravo is moving: it cannot be deleted.
        expect(within(rowOf('Bravo')).getByRole('button', { name: 'Delete Bravo' })).toBeDisabled();
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Delete Alpha' }));
        const dialog = await screen.findByRole('dialog', { name: 'Delete Alpha' });
        await waitFor(() => expect(dialog.querySelector('[data-delete-frees]')).toHaveTextContent('frees 5.0 MB'));
        fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'Alpha' } });
        fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
        await waitFor(() => expect(p.onChannelsChanged).toHaveBeenCalledOnce());
        expect(deleteChannel).toHaveBeenCalledExactlyOnceWith('a');
        expect(screen.queryByRole('dialog', { name: 'Delete Alpha' })).toBeNull();
    });

    it('names each row’s location by its id and the default location under New; no usable default links to Locations', async () => {
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('a', 'Alpha', { location_id: 'usb', path: '/media/usb/Channels/a' })] });
        render(<ChannelsPage {...props()} />);
        await waitFor(() => expect(screen.getByTestId('new-tab')).toHaveAttribute('data-saved-in', '/home/me/Collimate/Channels'));
        expect(document.querySelector('[data-no-default-location]')).toBeNull();
        cleanup();
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        await table('Channels');
        await waitFor(() => expect(rowOf('Alpha').querySelector('td[title="/media/usb/Channels/a"]')).toHaveTextContent('/media/usb/Channels'));
        cleanup();
        vi.mocked(fetchLocations).mockResolvedValue({ locations: [location('loc', '/gone', { is_default: true, state: 'missing' })] });
        const onTab = vi.fn();
        render(<ChannelsPage {...props({ onTab })} />);
        const notice = await waitFor(() => { const el = document.querySelector('[data-no-default-location]'); expect(el).not.toBeNull(); return el as HTMLElement; });
        expect(notice).toHaveTextContent('The default location /gone is missing');
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-saved-in', '');
        fireEvent.click(within(notice).getByRole('button', { name: 'Locations' }));
        expect(onTab).toHaveBeenCalledWith('locations');
    });

    it('Open on the open channel returns to it; on another selects it in this window', async () => {
        const p = props({ tab: 'channels' });
        render(<ChannelsPage {...p} />);
        await table('Channels');
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Open Alpha' }));
        expect(p.onClose).toHaveBeenCalledOnce();
        expect(p.onOpenChannel).not.toHaveBeenCalled();
    });

    it('Archive archives, reads the listing again and refreshes the app; a refusal shows above the table', async () => {
        vi.mocked(archiveChannel).mockRejectedValueOnce(new Error('A run is live in this channel')).mockResolvedValue(row('a', 'Alpha', { archived: true }));
        const p = props({ tab: 'channels' });
        render(<ChannelsPage {...p} />);
        await table('Channels');
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Archive Alpha' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('A run is live in this channel');
        expect(p.onChannelsChanged).not.toHaveBeenCalled();
        fireEvent.click(within(rowOf('Alpha')).getByRole('button', { name: 'Archive Alpha' }));
        await waitFor(() => expect(p.onChannelsChanged).toHaveBeenCalledOnce());
        expect(archiveChannel).toHaveBeenLastCalledWith('a');
        expect(fetchManagedChannels).toHaveBeenCalledTimes(2);
        expect(screen.queryByRole('alert')).toBeNull();
    });

    it('Rename… renames that channel through the rename dialog', async () => {
        const p = props({ tab: 'channels' });
        render(<ChannelsPage {...p} />);
        await table('Channels');
        fireEvent.click(within(rowOf('Bravo')).getByRole('button', { name: 'Rename Bravo' }));
        fireEvent.change(screen.getByRole('textbox', { name: 'New name' }), { target: { value: 'Bravissimo' } });
        fireEvent.click(screen.getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(p.onRenameChannel).toHaveBeenCalledExactlyOnceWith('b', 'Bravissimo'));
    });

    it('Archived lists only archived channels: Open read-only selects one, Unarchive unarchives it', async () => {
        vi.mocked(unarchiveChannel).mockResolvedValue(row('z', 'Zulu'));
        const p = props({ tab: 'archived' });
        render(<ChannelsPage {...p} />);
        const archived = await table('Archived channels');
        expect(within(archived).getAllByRole('rowheader').map(h => h.textContent)).toEqual(['Zulu']);
        expect(rowOf('Zulu')).toHaveTextContent('—');
        expect(within(rowOf('Zulu')).queryByRole('button', { name: 'Archive Zulu' })).toBeNull();
        // An archived channel moves, deletes and renames like any other.
        expect(within(rowOf('Zulu')).getByRole('button', { name: 'Move Zulu' })).toBeEnabled();
        expect(within(rowOf('Zulu')).getByRole('button', { name: 'Delete Zulu' })).toBeEnabled();
        expect(within(rowOf('Zulu')).getByRole('button', { name: 'Rename Zulu' })).toBeEnabled();
        fireEvent.click(within(rowOf('Zulu')).getByRole('button', { name: 'Open read-only Zulu' }));
        await waitFor(() => expect(p.onOpenChannel).toHaveBeenCalledExactlyOnceWith('z'));
        fireEvent.click(within(rowOf('Zulu')).getByRole('button', { name: 'Unarchive Zulu' }));
        await waitFor(() => expect(p.onChannelsChanged).toHaveBeenCalledOnce());
        expect(unarchiveChannel).toHaveBeenCalledWith('z');
    });

    it('with no open channel there is no Close, and New says how many channels are archived', async () => {
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('z', 'Zulu', { archived: true }), row('y', 'Yankee', { archived: true })] });
        const onTab = vi.fn();
        render(<ChannelsPage {...props({ onClose: undefined, onTab, channels: [], sourceChannelId: null })} />);
        expect(screen.queryByRole('button', { name: 'Close channels page' })).toBeNull();
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-cancel', 'no');
        const hint = await waitFor(() => { const el = document.querySelector('[data-archived-hint]'); expect(el).not.toBeNull(); return el as HTMLElement; });
        expect(hint).toHaveTextContent('This server has 2 archived channels.');
        fireEvent.click(within(hint).getByRole('button', { name: 'Show archived' }));
        expect(onTab).toHaveBeenCalledWith('archived');
    });

    it('a listing that cannot be read says so; the header stays', async () => {
        vi.mocked(fetchManagedChannels).mockRejectedValue(new Error('Request failed (404)'));
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        expect(await screen.findByRole('alert')).toHaveTextContent('Could not read this server’s channels: Request failed (404)');
        expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
    });

    it('paints from the skin tokens (S04)', async () => {
        render(<ChannelsPage {...props({ tab: 'channels' })} />);
        await table('Channels');
        const painted = [...document.body.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});
