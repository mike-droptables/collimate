import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { LocationsTab, locationBytes, type LocationsTabProps } from './LocationsTab';
import {
    addLocation, checkLocation, claimLocation, removeLocation, setDefaultLocation,
    type LocationCheck, type ManagedChannel, type StorageLocation,
} from '@/lib/api/storage';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { retiredOr } from '@/test/retiredTokens';

// The Locations tab (plan 1105 P8) against mocked routes: the table (path,
// Default, channels, size from the listing, free / total, state, the
// foreign owner), Make default and Remove as the row offers them, Add
// location… with the typed path checked as typed (each refusal's sentence,
// creates, the network warning), the owned-folder claim (Claim and add, off
// while the owner runs, behind the explicit confirmation when it cannot be
// checked), Claim… on a foreign row, and the folder chooser only for the
// desktop's local server.

vi.mock('@/lib/api/storage', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/api/storage')>(),
    addLocation: vi.fn(), checkLocation: vi.fn(), claimLocation: vi.fn(), removeLocation: vi.fn(), setDefaultLocation: vi.fn(),
}));

const location = (id: string, path: string, over: Partial<StorageLocation> = {}): StorageLocation => ({
    id, path, is_default: false, state: 'ok', channel_count: 0, bytes: null, free_bytes: 2 * 2 ** 30, total_bytes: 8 * 2 ** 30, actions: [], ...over,
});
const LIST = [
    location('home', '/home/me/Collimate/Channels', { is_default: true, channel_count: 2 }),
    location('usb', '/media/usb/Channels', { actions: ['set_default', 'remove'] }),
    location('old', '/srv/old', { state: 'foreign', owner_data_dir: '/srv/other/.collimate', channel_count: 3, free_bytes: null, total_bytes: null, actions: ['claim'] }),
];
const channel = (id: string, locationId: string, bytes: number | null): ManagedChannel => ({
    id, name: id, archived: false, location_id: locationId, path: `/x/${id}`, state: 'ok', active: false, bytes,
});
const refusal = (status: number, detail: unknown) => Object.assign(new Error('refused'), { response: { status, data: { detail } } });
const checked = (over: Partial<LocationCheck>): LocationCheck => ({ ok: true, path: '/data/ch', creates: false, ...over });

const props = (over: Partial<LocationsTabProps> = {}): LocationsTabProps => ({
    locations: { list: LIST, error: null, loading: false },
    channels: [channel('a', 'home', 2048), channel('b', 'home', 1024), channel('c', 'usb', null)],
    onChanged: vi.fn(async () => {}),
    ...over,
});
const rowOf = (path: string) => screen.getByRole('rowheader', { name: new RegExp(path.replace(/[/.]/g, '\\$&')) }).closest('tr') as HTMLElement;
const addDialog = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Add location…' }));
    return screen.findByRole('dialog', { name: 'Add location' });
};
const typePath = (dialog: HTMLElement, path: string) =>
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Folder on the server' }), { target: { value: path } });

beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); uninstallDesktopShell(); });

describe('the Locations tab (plan 1105 P8)', () => {
    it('lists each location with Default, channels, size, free / total and state; a foreign one names its owner and offers Claim… only', () => {
        render(<LocationsTab {...props()} />);
        const home = rowOf('/home/me/Collimate/Channels');
        expect(home).toHaveTextContent('Default');
        expect(home).toHaveTextContent('3.0 KB');
        expect(home).toHaveTextContent('2.0 GB / 8.0 GB');
        expect(home).toHaveTextContent('Ready');
        expect(within(home).queryAllByRole('button')).toEqual([]);
        // Not sized yet.
        expect(rowOf('/media/usb/Channels')).toHaveTextContent('—');
        const old = rowOf('/srv/old');
        expect(old).toHaveTextContent('Owned by another installation');
        expect(old).toHaveTextContent('/srv/other/.collimate');
        expect(within(old).getAllByRole('button').map(b => b.textContent)).toEqual(['Claim…']);
        expect(locationBytes('usb', props().channels)).toBeNull();
    });

    it('Make default and Remove act on the row and read the lists again; a refusal shows', async () => {
        vi.mocked(setDefaultLocation).mockResolvedValue(location('usb', '/media/usb/Channels', { is_default: true }));
        vi.mocked(removeLocation).mockRejectedValueOnce(refusal(409, { code: 'location_not_empty', message: 'The location still holds channels.' }));
        const p = props();
        render(<LocationsTab {...p} />);
        fireEvent.click(screen.getByRole('button', { name: 'Make /media/usb/Channels the default' }));
        await waitFor(() => expect(p.onChanged).toHaveBeenCalledOnce());
        expect(setDefaultLocation).toHaveBeenCalledWith('usb');
        expect(await screen.findByRole('status')).toHaveTextContent('New channels now go to /media/usb/Channels.');
        fireEvent.click(screen.getByRole('button', { name: 'Remove /media/usb/Channels' }));
        expect(await screen.findByRole('alert')).toHaveTextContent('The location still holds channels.');
        expect(p.onChanged).toHaveBeenCalledOnce();
    });

    it('Add location… checks the typed path and says each refusal; an ok folder adds, as the default when asked', async () => {
        vi.mocked(checkLocation)
            .mockResolvedValueOnce(checked({ ok: false, path: 'rel', error: { code: 'not_absolute', message: 'Give the folder’s absolute path.' } }))
            .mockResolvedValueOnce(checked({ ok: false, path: '/home/me/Documents', error: { code: 'not_empty', message: '/home/me/Documents holds other files or folders.' } }))
            .mockResolvedValueOnce(checked({ path: '/mnt/nas/ch', creates: true, warning: { code: 'network_filesystem', message: 'It is on a network filesystem (nfs).' } }));
        vi.mocked(addLocation).mockResolvedValue({ location: location('nas', '/mnt/nas/ch', { is_default: true }) });
        const p = props();
        render(<LocationsTab {...p} />);
        const dialog = await addDialog();
        const add = within(dialog).getByRole('button', { name: 'Add location' });
        expect(add).toBeDisabled();
        typePath(dialog, 'rel');
        expect(await within(dialog).findByText('Give the folder’s absolute path.')).toHaveAttribute('data-check-error', 'not_absolute');
        expect(add).toBeDisabled();
        typePath(dialog, '/home/me/Documents');
        expect(await within(dialog).findByText(/holds other files/)).toHaveAttribute('data-check-error', 'not_empty');
        typePath(dialog, '/mnt/nas/ch');
        expect(await within(dialog).findByText(/Creates the folder/)).toHaveTextContent('Creates the folder /mnt/nas/ch.');
        expect(within(dialog).getByText(/network filesystem/)).toHaveAttribute('data-check-warning', 'network_filesystem');
        expect(checkLocation).toHaveBeenCalledTimes(3);
        fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Make it the default for new channels' }));
        fireEvent.click(add);
        await waitFor(() => expect(p.onChanged).toHaveBeenCalledOnce());
        expect(addLocation).toHaveBeenCalledExactlyOnceWith({ path: '/mnt/nas/ch', default: true });
        expect(screen.queryByRole('dialog', { name: 'Add location' })).toBeNull();
        expect(screen.getByRole('status')).toHaveTextContent('Added /mnt/nas/ch as the default.');
    });

    it('a folder another installation owns says so and offers Claim and add, off while its owner runs', async () => {
        const owner = { installation_id: 'i2', data_dir: '/srv/other/.collimate', channel_count: 4, owner_running: true };
        vi.mocked(checkLocation).mockResolvedValue(checked({
            ok: false, path: '/srv/shared', owned_by: owner, error: { code: 'location_owned', message: 'belongs to another' },
        }));
        render(<LocationsTab {...props()} />);
        const dialog = await addDialog();
        typePath(dialog, '/srv/shared');
        const statement = await waitFor(() => { const el = dialog.querySelector('[data-claim-statement]'); expect(el).not.toBeNull(); return el as HTMLElement; });
        expect(statement).toHaveTextContent('belongs to the Collimate installation at /srv/other/.collimate (4 channels). Claiming makes this server its owner and adopts those channels; the other installation will no longer open them.');
        expect(dialog.querySelector('[data-owner-running]')).toHaveTextContent('/srv/other/.collimate');
        expect(within(dialog).getByRole('button', { name: 'Claim and add' })).toBeDisabled();
        expect(addLocation).not.toHaveBeenCalled();
    });

    it('an owner that cannot be checked needs the explicit confirmation, then claims with it', async () => {
        const owner = { installation_id: 'i2', data_dir: '/gone/.collimate', channel_count: 1, owner_running: null };
        vi.mocked(checkLocation).mockResolvedValue(checked({ ok: false, path: '/srv/shared', owned_by: owner, error: { code: 'location_owned', message: 'x' } }));
        vi.mocked(addLocation).mockResolvedValue({ location: location('s', '/srv/shared'), claimed: { adopted: ['c1'], skipped: [] } });
        const p = props();
        render(<LocationsTab {...p} />);
        const dialog = await addDialog();
        typePath(dialog, '/srv/shared');
        const confirm = await within(dialog).findByRole('checkbox', { name: /cannot be checked/ });
        const claim = within(dialog).getByRole('button', { name: 'Claim and add' });
        expect(claim).toBeDisabled();
        fireEvent.click(confirm);
        expect(claim).toBeEnabled();
        fireEvent.click(claim);
        await waitFor(() => expect(p.onChanged).toHaveBeenCalledOnce());
        expect(addLocation).toHaveBeenCalledExactlyOnceWith({ path: '/srv/shared', default: false, claim: true, confirm_unchecked: true });
        expect(screen.getByRole('status')).toHaveTextContent('Added /srv/shared; adopted 1 channel.');
    });

    it('Claim… on a foreign row claims; owner_running turns it off, owner_unchecked asks to confirm and claims again', async () => {
        vi.mocked(claimLocation)
            .mockRejectedValueOnce(refusal(409, { code: 'owner_unchecked', message: 'cannot be checked', owner_data_dir: '/srv/other/.collimate' }))
            .mockResolvedValueOnce({ location: location('old', '/srv/old'), adopted: ['x', 'y'], skipped: [{ id: 'z', reason: 'duplicate_channel_id' }] });
        const p = props();
        render(<LocationsTab {...p} />);
        fireEvent.click(screen.getByRole('button', { name: 'Claim /srv/old' }));
        const dialog = await screen.findByRole('dialog', { name: 'Claim /srv/old' });
        expect(dialog.querySelector('[data-claim-statement]')).toHaveTextContent('(3 channels)');
        fireEvent.click(within(dialog).getByRole('button', { name: 'Claim' }));
        const confirm = await within(dialog).findByRole('checkbox', { name: /cannot be checked/ });
        expect(within(dialog).getByRole('button', { name: 'Claim' })).toBeDisabled();
        fireEvent.click(confirm);
        fireEvent.click(within(dialog).getByRole('button', { name: 'Claim' }));
        await waitFor(() => expect(p.onChanged).toHaveBeenCalledOnce());
        expect(vi.mocked(claimLocation).mock.calls).toEqual([['old', false], ['old', true]]);
        expect(screen.getByRole('status')).toHaveTextContent('Claimed /srv/old; adopted 2 channels, skipped 1 channel this server already has.');

        cleanup();
        vi.mocked(claimLocation).mockRejectedValueOnce(refusal(409, { code: 'owner_running', message: 'runs', owner_data_dir: '/srv/other/.collimate' }));
        render(<LocationsTab {...props()} />);
        fireEvent.click(screen.getByRole('button', { name: 'Claim /srv/old' }));
        const again = await screen.findByRole('dialog', { name: 'Claim /srv/old' });
        fireEvent.click(within(again).getByRole('button', { name: 'Claim' }));
        await waitFor(() => expect(again.querySelector('[data-owner-running]')).not.toBeNull());
        expect(within(again).getByRole('button', { name: 'Claim' })).toBeDisabled();
    });

    it('offers the folder chooser only for the desktop’s local server; its choice is checked like a typed path', async () => {
        vi.mocked(checkLocation).mockResolvedValue(checked({ path: '/picked' }));
        render(<LocationsTab {...props()} />);
        let dialog = await addDialog();
        expect(within(dialog).queryByRole('button', { name: 'Choose…' })).toBeNull();
        cleanup();

        const pickFolder = vi.fn(async () => '/picked');
        installDesktopShell({ pickFolder });
        render(<LocationsTab {...props()} />);
        dialog = await addDialog();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Choose…' }));
        await waitFor(() => expect(within(dialog).getByRole('textbox', { name: 'Folder on the server' })).toHaveValue('/picked'));
        expect(pickFolder).toHaveBeenCalledWith({ title: 'Choose a folder for channels', defaultPath: undefined });
        await waitFor(() => expect(checkLocation).toHaveBeenCalledWith('/picked'));
        cleanup();
        uninstallDesktopShell();

        const shell = installDesktopShell({ pickFolder });
        shell.shell.server.kind = 'remote';
        render(<LocationsTab {...props()} />);
        dialog = await addDialog();
        expect(within(dialog).queryByRole('button', { name: 'Choose…' })).toBeNull();
    });

    it('paints from the skin tokens (S04)', async () => {
        render(<LocationsTab {...props()} />);
        await addDialog();
        const painted = [...document.body.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('rgba?\\('));
    });
});
