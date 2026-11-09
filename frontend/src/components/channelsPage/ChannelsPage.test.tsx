import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { ChannelsPage, type ChannelsPageProps } from './ChannelsPage';
import {
    archiveChannel, fetchChannelSize, fetchLocations, fetchManagedChannels, fetchServerInstance, fetchStorageJob, moveChannel, unarchiveChannel,
    type ManagedChannel, type MoveJob, type StorageLocation,
} from '@/lib/api/storage';
import { deleteChannel } from '@/lib/api/channels';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { retiredOr } from '@/test/retiredTokens';
import { useDefaultThemeSurface } from '@/contexts/ThemeContext';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { ACCOUNTS_DESCRIPTION } from '@/components/commandPane/settingsSections';
import type { CustomEntryDraft } from '@/lib/keystoreStructure';
import { newUserTheme, type UserTheme } from '@/lib/userThemes';

// The channels page (plan 1105 P7, P8) against mocked routes: its tabs and
// default tab, the header naming the window's server, the Channels and
// Archived tables (archived channels only on Archived, whose Open selects
// them), archive and unarchive, Move… with the job's progress on the row,
// Delete… with the size it frees, the location column and "Saved in" from
// the locations, the no-default notice, the Servers tab only in the
// desktop shell, and the Settings tab last with its docked settings panel
// (the theme context, the theme editor, the KeyManager and the profile body
// are their own packages', mocked here).

vi.mock('@/lib/api/storage', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/api/storage')>(),
    fetchManagedChannels: vi.fn(), fetchServerInstance: vi.fn(), archiveChannel: vi.fn(), unarchiveChannel: vi.fn(), fetchChannelSize: vi.fn(),
    fetchLocations: vi.fn(), moveChannel: vi.fn(), fetchStorageJob: vi.fn(),
}));
vi.mock('@/lib/api/channels', () => ({ deleteChannel: vi.fn() }));
vi.mock('./StorageTab', () => ({ StorageTab: () => <div>storage overview</div> }));
vi.mock('./NewTab', () => ({ NewTab: (props: {
    shown: boolean; onCancel?: () => void; channels: unknown[]; savedIn: string | null;
    onOpenKeys: (draft?: CustomEntryDraft) => void; accountsRevision: number;
}) =>
    <div data-testid="new-tab" data-shown={String(props.shown)} data-cancel={props.onCancel ? 'yes' : 'no'} data-saved-in={props.savedIn ?? ''}
        data-accounts-revision={props.accountsRevision}>
        <button type="button" onClick={() => props.onOpenKeys()}>New tab accounts</button>
        <button type="button" onClick={() => props.onOpenKeys({ name: 'Agent model', structure: {} } as unknown as CustomEntryDraft)}>New tab draft</button>
    </div> }));
vi.mock('./ServersTab', () => ({ ServersTab: () => <div>servers cards</div> }));
const theme = vi.hoisted(() => ({ defaultThemeId: 'paper', userThemes: [] as UserTheme[] }));
vi.mock('@/contexts/ThemeContext', () => ({ useDefaultThemeSurface: vi.fn(), useThemeContext: () => theme }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: ({ mode }: { mode: string }) => <div data-testid="theme-section" data-mode={mode} /> }));
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: ({ draft, onChanged }: { draft?: { name: string }; onChanged?: () => void }) =>
    <div data-testid="key-manager" data-draft={draft?.name ?? ''}>{draft && <input aria-label="Entry name" />}
        <button type="button" onClick={() => onChanged?.()}>Save account</button>
        <div data-owns-keyboard=""><button type="button">Service menu</button></div></div> }));
vi.mock('@/components/settings/ProfileSection', () => ({ ProfileSection: () => <div data-profile-section>profile body</div> }));

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
    useCollimateStore.setState({ commandPane: defaultCommandPane() });
    theme.defaultThemeId = 'paper';
    theme.userThemes = [];
});
afterEach(() => { cleanup(); uninstallDesktopShell(); });

describe('the channels page (plan 1105 P7)', () => {
    it('offers New, Channels, Archived, Locations, Storage and Settings in a browser, on the tab asked for, under a header naming this server', async () => {
        render(<ChannelsPage {...props()} />);
        expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['New', 'Channels', 'Archived', 'Locations', 'Storage', 'Settings']);
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
        expect(screen.getAllByRole('tab').map(tab => tab.textContent?.replace(/\s\d+$/, ''))).toEqual(['New', 'Channels', 'Archived', 'Locations', 'Storage', 'Servers', 'Settings']);
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

describe('the channels page\'s Settings tab and its docked panel', () => {
    const panel = () => document.querySelector<HTMLElement>('[data-settings-panel]');
    const scroller = () => document.querySelector<HTMLElement>('[data-channels-scroll]')!;
    const entry = (name: string) => screen.getByRole('button', { name });

    it('shows the page in the default theme while it is mounted', () => {
        render(<ChannelsPage {...props()} />);
        expect(useDefaultThemeSurface).toHaveBeenCalled();
    });

    it('lists Accounts on this machine, Default theme with the default\'s name, then This profile inline', () => {
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(screen.getByRole('tab', { name: 'Settings' })).toHaveAttribute('aria-selected', 'true');
        const rows = [...document.querySelectorAll('[data-settings-entry]')].map(row => row.getAttribute('data-settings-entry'));
        expect(rows).toEqual(['keys', 'theme']);
        expect(entry('Accounts on this machine')).toHaveTextContent(ACCOUNTS_DESCRIPTION);
        expect(entry('Default theme')).toHaveTextContent('The theme of this page and of every channel that does not override it. Themes are shared by every channel on this server.');
        expect(entry('Default theme').querySelector('[data-settings-value]')).toHaveTextContent('Paper');
        // Named by the title alone, described by the current default and the words.
        expect(screen.getByRole('button', { name: 'Default theme' })).toHaveAccessibleDescription(
            'Paper The theme of this page and of every channel that does not override it. Themes are shared by every channel on this server.');
        expect(screen.getByRole('button', { name: 'Accounts on this machine' })).toHaveAccessibleDescription(ACCOUNTS_DESCRIPTION);
        const profile = screen.getByRole('region', { name: 'This profile' });
        expect(within(profile).getByText('This browser, for this server')).toBeInTheDocument();
        expect(within(profile).getByText('profile body')).toBeInTheDocument();
        expect(entry('Accounts on this machine').compareDocumentPosition(profile) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(panel()).toBeNull();
    });

    it('names a library theme by its label, and a default the library lost by the built-in it falls back to', () => {
        theme.defaultThemeId = 'user.ink';
        theme.userThemes = [{ ...newUserTheme('paper', 'Ink well'), id: 'user.ink' }];
        const { unmount } = render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(entry('Default theme').querySelector('[data-settings-value]')).toHaveTextContent('Ink well');
        unmount();
        theme.userThemes = [];
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(entry('Default theme').querySelector('[data-settings-value]')).toHaveTextContent('Graphite');
    });

    it('names the desktop profile the shell runs in, whatever its build stamp says; a shell that names none gets no name', () => {
        installDesktopShell();
        const first = render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(screen.getByText('This app on this computer: the Collimate profile')).toBeInTheDocument();
        expect(screen.getByText('profile body')).toBeInTheDocument();
        first.unmount();
        uninstallDesktopShell();
        // A stamped build forced onto the dev profile (COLLIMATE_APP_PROFILE).
        installDesktopShell({ getProfileName: () => 'Collimate Dev' });
        const second = render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(screen.getByText('This app on this computer: the Collimate Dev profile')).toBeInTheDocument();
        second.unmount();
        uninstallDesktopShell();
        installDesktopShell({ getProfileName: undefined });
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(screen.getByText('This app on this computer')).toBeInTheDocument();
    });

    it('Accounts on this machine opens a docked 352px panel with an X, taking the keyboard; Esc closes it and the row gets it back', async () => {
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        const row = entry('Accounts on this machine');
        row.focus();
        fireEvent.click(row);
        const column = panel()!;
        expect(column).toHaveAttribute('data-settings-panel', 'keys');
        expect(column).toHaveAttribute('data-pane-column');
        expect(column.style.width).toBe('352px');
        expect(screen.getByRole('complementary', { name: 'Accounts on this machine' })).toBe(column);
        expect(column).toHaveClass('absolute', 'left-0');
        // Below the blurred stand-in top bar's 36px band, which lies over the page.
        expect(column).toHaveClass('top-9');
        expect(column).not.toHaveClass('top-0');
        expect(within(column).getByRole('heading', { name: 'Accounts on this machine' })).toBeInTheDocument();
        expect(within(column).getByText(ACCOUNTS_DESCRIPTION)).toBeInTheDocument();
        expect(within(column).getByTestId('key-manager')).toBeInTheDocument();
        const close = within(column).getByRole('button', { name: 'Close Accounts on this machine' });
        expect(close).toHaveAttribute('title', 'Close (Esc)');
        expect(document.activeElement).toBe(close);
        // Not modal: the page stays as it was.
        expect(page()).toHaveAttribute('aria-modal', 'true');
        fireEvent.keyDown(close, { key: 'Escape' });
        expect(panel()).toBeNull();
        expect(document.activeElement).toBe(entry('Accounts on this machine'));
    });

    it('the X closes it; Esc inside something that owns the keyboard is that owner\'s', () => {
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        fireEvent.click(entry('Accounts on this machine'));
        fireEvent.keyDown(screen.getByRole('button', { name: 'Service menu' }), { key: 'Escape' });
        expect(panel()).not.toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Close Accounts on this machine' }));
        expect(panel()).toBeNull();
    });

    it('Default theme opens the theme editor in its default mode, the footer\'s esc closing it', async () => {
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        fireEvent.click(entry('Default theme'));
        expect(panel()).toHaveAttribute('data-settings-panel', 'theme');
        expect(screen.getByRole('complementary', { name: 'Default theme' })).toBe(panel());
        expect(await screen.findByTestId('theme-section')).toHaveAttribute('data-mode', 'default');
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close Default theme' }));
        fireEvent.click(within(panel()!).getByRole('button', { name: /close$/ }));
        expect(panel()).toBeNull();
    });

    it('stays open across a tab switch, and a press elsewhere on the page leaves it open', () => {
        const { rerender } = render(<ChannelsPage {...props({ tab: 'settings' })} />);
        fireEvent.click(entry('Default theme'));
        fireEvent.mouseDown(screen.getByRole('heading', { level: 1 }));
        rerender(<ChannelsPage {...props({ tab: 'channels' })} />);
        expect(panel()).toHaveAttribute('data-settings-panel', 'theme');
        rerender(<ChannelsPage {...props({ tab: 'new' })} />);
        expect(panel()).toHaveAttribute('data-settings-panel', 'theme');
    });

    it('docks on the Commands pane\'s side, and the scroller narrows by its width there, its scrollbar beside the panel', () => {
        render(<ChannelsPage {...props({ tab: 'settings' })} />);
        expect(scroller().style.marginLeft).toBe('');
        expect(scroller().style.marginRight).toBe('');
        fireEvent.click(entry('Accounts on this machine'));
        expect(panel()).toHaveClass('left-0');
        expect(scroller().style.marginLeft).toBe('352px');
        expect(scroller().style.marginRight).toBe('');
        act(() => useCollimateStore.getState().setCommandPaneSide('right'));
        expect(panel()).toHaveClass('right-0');
        // A margin, never padding: padding sits inside the scrollbar, which
        // would then lie under the panel.
        expect(scroller().style.marginRight).toBe('352px');
        expect(scroller().style.marginLeft).toBe('');
        expect(scroller().style.paddingRight).toBe('');
        expect(scroller().style.paddingLeft).toBe('');
        expect(page()).toHaveClass('absolute', 'inset-0', 'flex');
        expect(scroller()).toHaveClass('flex-1', 'min-w-0', 'overflow-y-auto', '[scrollbar-gutter:stable]');
    });

    it('the New tab\'s accounts open the same panel, with a draft on its first field; each account change bumps the revision the New tab reads accounts on, while the panel is open', async () => {
        const { rerender } = render(<ChannelsPage {...props()} />);
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-accounts-revision', '0');
        const opener = screen.getByRole('button', { name: 'New tab draft' });
        opener.focus();
        fireEvent.click(opener);
        expect(panel()).toHaveAttribute('data-settings-panel', 'keys');
        expect(screen.getByTestId('key-manager')).toHaveAttribute('data-draft', 'Agent model');
        await waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText('Entry name')));
        // A save in the panel: the New tab, still in view beside it, reads again.
        fireEvent.click(within(panel()!).getByRole('button', { name: 'Save account' }));
        expect(panel()).toHaveAttribute('data-settings-panel', 'keys');
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-accounts-revision', '1');
        fireEvent.keyDown(screen.getByLabelText('Entry name'), { key: 'Escape' });
        expect(panel()).toBeNull();
        // Closing changed nothing, so nothing is read again.
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-accounts-revision', '1');
        expect(document.activeElement).toBe(opener);
        fireEvent.click(screen.getByRole('button', { name: 'New tab accounts' }));
        expect(screen.getByTestId('key-manager')).toHaveAttribute('data-draft', '');
        rerender(<ChannelsPage {...props({ tab: 'settings' })} />);
        fireEvent.click(entry('Default theme'));
        expect(panel()).toHaveAttribute('data-settings-panel', 'theme');
        expect(screen.getByTestId('new-tab')).toHaveAttribute('data-accounts-revision', '1');
    });

    it('on close the keyboard goes back to the last opener while it is on show, else to the current tab\'s button, never the body', () => {
        // Opened from a Settings row, closed on another tab: the row is gone.
        const { rerender } = render(<ChannelsPage {...props({ tab: 'settings' })} />);
        entry('Accounts on this machine').focus();
        fireEvent.click(entry('Accounts on this machine'));
        rerender(<ChannelsPage {...props({ tab: 'channels' })} />);
        fireEvent.click(within(panel()!).getByRole('button', { name: /^Close / }));
        expect(panel()).toBeNull();
        expect(document.activeElement).toBe(document.getElementById('channels-tab-channels'));
        // Opened from the New tab, closed on Settings: the button is hidden.
        rerender(<ChannelsPage {...props({ tab: 'new' })} />);
        const newTabButton = screen.getByRole('button', { name: 'New tab accounts' });
        newTabButton.focus();
        fireEvent.click(newTabButton);
        rerender(<ChannelsPage {...props({ tab: 'settings' })} />);
        fireEvent.click(within(panel()!).getByRole('button', { name: /^Close / }));
        expect(document.activeElement).toBe(document.getElementById('channels-tab-settings'));
        // Opened again from elsewhere while open: that later opener is the one.
        entry('Default theme').focus();
        fireEvent.click(entry('Default theme'));
        entry('Accounts on this machine').focus();
        fireEvent.click(entry('Accounts on this machine'));
        fireEvent.click(within(panel()!).getByRole('button', { name: /^Close / }));
        expect(document.activeElement).toBe(entry('Accounts on this machine'));
    });
});
