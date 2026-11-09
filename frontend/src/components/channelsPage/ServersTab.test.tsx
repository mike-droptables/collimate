import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react';

import { ServersTab } from './ServersTab';
import { expectButtonRowsWrap, expectFitsPaneColumn, expectStackedFields, renderInPaneColumn } from '@/components/settings/paneColumn.testkit';
import { SettingsView } from '@/components/commandPane/SettingsView';
import { SettingsTab } from './SettingsTab';
import { installDesktopShell, uninstallDesktopShell, LOCAL_SERVER_ENTRY } from '@/test/desktopShell';
import type { CollimateDesktop, ServerEntry } from '@/lib/desktop';
import { registerRendererFlusher } from '@/lib/rendererFlush';

vi.mock('@/lib/guestOcclusion', () => ({ useGuestOccluder: vi.fn() }));
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys</div> }));
vi.mock('@/components/settings/ProfileSection', () => ({ ProfileSection: () => <div data-profile-section>profile body</div> }));
vi.mock('@/contexts/ThemeContext', () => ({ useThemeContext: () => ({ defaultThemeId: 'graphite', userThemes: [] }) }));

const LOCAL: ServerEntry = { ...LOCAL_SERVER_ENTRY, channels: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }], activeChannelId: 'a' };
const LAB: ServerEntry = {
    ...LOCAL_SERVER_ENTRY,
    id: 'lab', kind: 'remote', name: 'Lab', url: 'https://lab.test', status: 'unauthorized',
    serverVersion: '0.5.0', hasToken: true, channels: null, activeChannelId: null,
    isWindowServer: false, error: 'the token was refused',
};

function setup(over: Partial<CollimateDesktop> = {}, entries: ServerEntry[] = [LOCAL, LAB]) {
    const stub = installDesktopShell(over);
    vi.mocked(stub.shell.servers.list).mockResolvedValue(entries);
    const { body } = renderInPaneColumn(<ServersTab />);
    return Object.assign(stub.shell.servers, { body });
}

const KEEP = 'Keep local server running when the app quits';

const row = async (id: string) => {
    await waitFor(() => expect(document.querySelector(`[data-server-id="${id}"]`)).not.toBeNull());
    return document.querySelector(`[data-server-id="${id}"]`) as HTMLElement;
};

afterEach(() => { cleanup(); uninstallDesktopShell(); });

describe('ServersTab (MS-05, plan 1105 P7)', () => {
    it('fits a narrow column: no heading of its own, cards stacked, the add form\'s fields stacked, every button row wraps (CP-6)', async () => {
        const { body } = setup({}, [LOCAL, { ...LAB, status: 'running', isWindowServer: false }]);
        await row('lab');
        // No heading or description of its own: the channels page's tab
        // strip names it.
        expect(screen.queryByRole('heading')).toBeNull();
        expect(screen.queryByText(/one server/i)).toBeNull();
        expect(screen.getByRole('list', { name: 'Servers' })).toHaveClass('space-y-2');
        expect(body.querySelector('.grid')).toBeNull();
        // The local card's actions (Stop, the keep-alive switch on a line of
        // its own) and the remote's (Open, Rename, Change token, Remove).
        expect(within(await row('local')).getByRole('switch').closest('label')).toHaveClass('basis-full');
        expect(within(await row('lab')).getByRole('switch', { name: 'Check and connect' }).closest('label')).toHaveClass('basis-full');
        expect(expectButtonRowsWrap(body)).toBeGreaterThan(0);
        fireEvent.click(within(await row('lab')).getByRole('button', { name: 'Rename' }));
        expect(within(await row('lab')).getByLabelText('Name for Lab').closest('form')).toHaveClass('flex-wrap');
        fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
        const form = screen.getByRole('form', { name: 'Add server' });
        expect(expectStackedFields(form)).toBe(3);
        expect(expectButtonRowsWrap(form)).toBe(1);
        expectFitsPaneColumn(body);
        expectButtonRowsWrap(body);
    });

    it('reads Loading servers… until the shell answers, which an unresponsive remote holds back, then shows the cards', async () => {
        let answer: (entries: ServerEntry[]) => void = () => {};
        const stub = installDesktopShell();
        vi.mocked(stub.shell.servers.list).mockImplementation(() => new Promise(resolve => { answer = resolve; }));
        renderInPaneColumn(<ServersTab />);
        const loading = screen.getByRole('status');
        expect(loading.textContent).toBe('Loading servers…');
        expect(loading).toHaveAttribute('data-servers-loading');
        expect(screen.queryByRole('list', { name: 'Servers' })).toBeNull();
        // Adding a server waits for nothing.
        expect(screen.getByRole('button', { name: 'Add server' })).toBeEnabled();
        await act(async () => { answer([LOCAL, { ...LAB, status: 'unreachable', error: 'timed out' }]); });
        expect(screen.queryByRole('status')).toBeNull();
        expect((await row('lab')).textContent).toContain('Unreachable');
        expect(screen.getByRole('list', { name: 'Servers' })).toBeInTheDocument();
    });

    it('lists the local server and the saved remotes with status, version and channel count (MS-05)', async () => {
        setup();
        const local = await row('local');
        expect(local.textContent).toContain('Local');
        expect(local.textContent).toContain('This window');
        expect(local.textContent).toContain('Running');
        expect(local.textContent).toContain('v0.4.1');
        expect(local.textContent).toContain('2 channels');
        const lab = await row('lab');
        expect(lab.textContent).toContain('Token refused');
        expect(lab.textContent).toContain('v0.5.0');
        expect(lab.textContent).toContain('Token saved');
        expect(lab.textContent).toContain('the token was refused');
        // The window's own server has nothing to open; an unauthorized one cannot be opened.
        expect(within(local).queryByRole('button', { name: 'Open' })).toBeNull();
        expect(within(lab).queryByRole('button', { name: 'Open' })).toBeNull();
    });

    it('names each server\'s version beside this app\'s; mixed versions with one contract are not marked (C5)', async () => {
        setup({ buildInfo: () => ({ version: '0.6.0', commit: null, dirty: null, builtAt: null, serverEnvironmentHash: null }) }, [
            LOCAL, { ...LAB, status: 'running', error: undefined, serverVersion: '0.5.0' },
            { ...LAB, id: 'fresh', name: 'Fresh', status: 'starting', serverVersion: null, contract: null, error: undefined },
        ]);
        const versions = async (id: string) => (await row(id)).querySelector('[data-versions]')?.textContent;
        expect(await versions('local')).toBe('Server v0.4.1 · this app v0.6.0');
        expect(await versions('lab')).toBe('Server v0.5.0 · this app v0.6.0');
        expect(await versions('fresh')).toBe('Server version unknown · this app v0.6.0');
        const lab = await row('lab');
        expect(lab.querySelector('[data-incompatible]')).toBeNull();
        expect(lab.textContent).toContain('Running');
        expect(within(lab).getByRole('button', { name: 'Open' })).toBeInTheDocument();
    });

    it('marks a server whose API contract this app cannot speak, with the reason, and offers no Open (C5)', async () => {
        const reason = "Collimate 0.4.0 at https://lab.test speaks an older API contract 0000000000000000 than this app's "
            + '(Collimate 0.6.0, bd3891a14f796340). Update that server on its own machine.';
        setup({}, [LOCAL, {
            ...LAB, status: 'incompatible', serverVersion: '0.4.0', contract: '0000000000000000',
            incompatible: reason, error: undefined, channels: null,
        }]);
        const lab = await row('lab');
        expect(lab.textContent).toContain('Incompatible');
        const marker = lab.querySelector('[data-incompatible]') as HTMLElement;
        expect(marker.textContent).toBe(`This app cannot speak this server's API. ${reason}`);
        expect(marker).toHaveClass('text-red');
        expect(lab.querySelector('[data-versions]')?.textContent).toBe('Server v0.4.0 · this app v0.4.1');
        expect(within(lab).queryByRole('button', { name: 'Open' })).toBeNull();
        // Still managed: it can be renamed, re-tokened or removed.
        expect(within(lab).getByRole('button', { name: 'Remove' })).toBeInTheDocument();
    });

    it('marks the local server too when its contract differs, keeping its status (C5)', async () => {
        setup({}, [{ ...LOCAL, contract: 'ffffffffffffffff', incompatible: 'http://127.0.0.1:8765 speaks API contract ffffffffffffffff, not this app\'s' }, LAB]);
        const local = await row('local');
        expect(local.textContent).toContain('Running');
        expect(local.querySelector('[data-incompatible]')?.textContent).toContain('API contract ffffffffffffffff');
    });

    it('disables Stop while this window uses local and no remote is running', async () => {
        const servers = setup();
        const stop = within(await row('local')).getByRole('button', { name: 'Stop' });
        expect(stop).toBeDisabled();
        expect(stop).toHaveAttribute('title', 'Add a running remote server before stopping the server this window uses');
        fireEvent.click(stop);
        expect(servers.stopLocal).not.toHaveBeenCalled();
    });

    it('allows Stop with a running remote, and keeps it available on a remote window', async () => {
        const servers = setup({}, [LOCAL, { ...LAB, status: 'running', channels: [] }]);
        const stop = within(await row('local')).getByRole('button', { name: 'Stop' });
        expect(stop).toBeEnabled();
        fireEvent.click(stop);
        await waitFor(() => expect(servers.stopLocal).toHaveBeenCalledOnce());
    });

    it('adds a server: name, URL and token go to the shell once, then the list reloads (MS-05)', async () => {
        const servers = setup();
        await row('local');
        fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
        const form = screen.getByRole('form', { name: 'Add server' });
        fireEvent.change(within(form).getByLabelText('Name (optional)'), { target: { value: ' Lab 2 ' } });
        fireEvent.change(within(form).getByLabelText('URL'), { target: { value: ' https://lab2.test ' } });
        fireEvent.change(within(form).getByLabelText('Token (optional)'), { target: { value: 's3cret' } });
        const listed = vi.mocked(servers.list).mock.calls.length;
        fireEvent.click(within(form).getByRole('button', { name: 'Add' }));
        await waitFor(() => expect(screen.queryByRole('form', { name: 'Add server' })).toBeNull());
        expect(servers.add).toHaveBeenCalledExactlyOnceWith({ name: 'Lab 2', url: 'https://lab2.test', token: 's3cret' });
        await waitFor(() => expect(vi.mocked(servers.list).mock.calls.length).toBeGreaterThan(listed));
    });

    it('Test probes the typed URL and token and shows the refusal\'s text (MS-05)', async () => {
        const servers = setup();
        vi.mocked(servers.probe).mockResolvedValueOnce({ ok: false, message: 'The server refused the token (401).' });
        await row('local');
        fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
        const form = screen.getByRole('form', { name: 'Add server' });
        fireEvent.change(within(form).getByLabelText('URL'), { target: { value: 'https://lab2.test' } });
        fireEvent.change(within(form).getByLabelText('Token (optional)'), { target: { value: 'bad' } });
        fireEvent.click(within(form).getByRole('button', { name: 'Test' }));
        expect((await within(form).findByRole('alert')).textContent).toBe('The server refused the token (401).');
        expect(servers.probe).toHaveBeenCalledExactlyOnceWith({ url: 'https://lab2.test', token: 'bad' });
        expect(servers.add).not.toHaveBeenCalled();

        vi.mocked(servers.probe).mockResolvedValueOnce({ ok: true, serverVersion: '0.5.0' });
        fireEvent.click(within(form).getByRole('button', { name: 'Test' }));
        expect((await within(form).findByRole('status')).textContent).toBe('Reachable · Collimate 0.5.0');
        expect(within(form).queryByRole('alert')).toBeNull();
    });

    it('a refused add keeps the form and says why (MS-05)', async () => {
        const servers = setup();
        vi.mocked(servers.add).mockRejectedValueOnce(new Error('That server is already saved.'));
        await row('local');
        fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
        const form = screen.getByRole('form', { name: 'Add server' });
        fireEvent.change(within(form).getByLabelText('URL'), { target: { value: 'https://lab.test' } });
        fireEvent.click(within(form).getByRole('button', { name: 'Add' }));
        expect((await within(form).findByRole('alert')).textContent).toBe('That server is already saved.');
    });

    it('Remove asks the shell, which confirms natively (MS-05)', async () => {
        const servers = setup();
        fireEvent.click(within(await row('lab')).getByRole('button', { name: 'Remove' }));
        await waitFor(() => expect(servers.remove).toHaveBeenCalledExactlyOnceWith('lab'));
    });

    it('Remove is unavailable on the server the window is on (MS-05)', async () => {
        setup({}, [{ ...LOCAL, isWindowServer: false }, { ...LAB, status: 'running', isWindowServer: true }]);
        expect(within(await row('lab')).getByRole('button', { name: 'Remove' })).toBeDisabled();
        // The local server, not the window's, can be opened.
        expect(within(await row('local')).getByRole('button', { name: 'Open' })).toBeEnabled();
    });

    it('Rename and Change token send one update; the token field is a password and is never prefilled (MS-05)', async () => {
        const servers = setup();
        const lab = await row('lab');
        fireEvent.click(within(lab).getByRole('button', { name: 'Change token' }));
        const token = within(lab).getByLabelText('New token for Lab') as HTMLInputElement;
        expect(token.type).toBe('password');
        expect(token.value).toBe('');
        fireEvent.change(token, { target: { value: 'fresh' } });
        fireEvent.click(within(lab).getByRole('button', { name: 'Save' }));
        await waitFor(() => expect(servers.update).toHaveBeenCalledExactlyOnceWith({ id: 'lab', token: 'fresh' }));

        await waitFor(() => expect(within(lab).getByRole('button', { name: 'Rename' })).toBeEnabled());
        fireEvent.click(within(lab).getByRole('button', { name: 'Rename' }));
        const name = within(lab).getByLabelText('Name for Lab') as HTMLInputElement;
        expect(name.value).toBe('Lab');
        fireEvent.change(name, { target: { value: 'Lab B' } });
        fireEvent.submit(name.closest('form')!);
        await waitFor(() => expect(servers.update).toHaveBeenLastCalledWith({ id: 'lab', name: 'Lab B' }));
    });

    it('Open, Stop and Start call the shell; a refusal shows above the list (MS-05)', async () => {
        const servers = setup({}, [{ ...LOCAL, isWindowServer: false }, { ...LAB, status: 'running', isWindowServer: true, channels: [] }]);
        const local = await row('local');
        fireEvent.click(within(local).getByRole('button', { name: 'Open' }));
        await waitFor(() => expect(servers.openServer).toHaveBeenCalledExactlyOnceWith('local'));
        vi.mocked(servers.stopLocal).mockRejectedValueOnce(new Error('The local server did not stop.'));
        fireEvent.click(within(local).getByRole('button', { name: 'Stop' }));
        expect((await screen.findByRole('alert')).textContent).toBe('The local server did not stop.');
        expect(servers.stopLocal).toHaveBeenCalledOnce();
    });

    it('Open, and Stop on the window\'s own local server, save the renderer editors before the shell replaces the page (MS-05)', async () => {
        const order: string[] = [];
        const unregister = registerRendererFlusher('cell', 'notes.md', () => { order.push('flush'); });
        try {
            const servers = setup({}, [LOCAL, { ...LAB, status: 'running', channels: [] }]);
            vi.mocked(servers.openServer).mockImplementation(async () => { order.push('openServer'); });
            vi.mocked(servers.stopLocal).mockImplementation(async () => { order.push('stopLocal'); });
            fireEvent.click(within(await row('lab')).getByRole('button', { name: 'Open' }));
            await waitFor(() => expect(order).toEqual(['flush', 'openServer']));
            const local = await row('local');
            await waitFor(() => expect(within(local).getByRole('button', { name: 'Stop' })).toBeEnabled());
            fireEvent.click(within(local).getByRole('button', { name: 'Stop' }));
            await waitFor(() => expect(order).toEqual(['flush', 'openServer', 'flush', 'stopLocal']));
        } finally {
            unregister();
        }
    });

    it('a stopped local server offers Start (MS-05)', async () => {
        const servers = setup({}, [{ ...LOCAL, status: 'stopped', isWindowServer: false, channels: null }]);
        const local = await row('local');
        expect(within(local).queryByRole('button', { name: 'Stop' })).toBeNull();
        fireEvent.click(within(local).getByRole('button', { name: 'Start' }));
        await waitFor(() => expect(servers.startLocal).toHaveBeenCalledOnce());
    });

    it('the keep-alive switch sits in the running local server\'s card and reads and writes the shell prefs (MS-05)', async () => {
        const servers = setup();
        const local = await row('local');
        const toggle = within(local).getByRole('switch', { name: 'Keep local server running when the app quits' });
        expect(local.textContent).toContain('Keep local server running when the app quits');
        expect(toggle.closest('label')).toHaveAttribute(
            'title', 'The next launch reuses it when its version matches, and restarts it otherwise.');
        await waitFor(() => expect(toggle).toBeEnabled());
        expect(toggle).toHaveAttribute('aria-checked', 'false');
        fireEvent.click(toggle);
        await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'));
        expect(servers.setPrefs).toHaveBeenCalledExactlyOnceWith({ keepLocalRunning: true });
        // Only the local card carries it: nothing on a remote, nothing below the list.
        expect(within(await row('lab')).queryByRole('switch', { name: KEEP })).toBeNull();
        expect(screen.getAllByRole('switch').filter(s => !s.closest('[data-server-id]'))).toEqual([]);
    });

    it('the keep-alive switch is not offered while the local server is starting, stopped, failed or unreachable (MS-05)', async () => {
        setup({}, [
            { ...LOCAL, id: 'local-starting', isWindowServer: false, status: 'starting', channels: null },
            { ...LOCAL, id: 'local-stopped', isWindowServer: false, status: 'stopped', channels: null },
            { ...LOCAL, id: 'local-failed', isWindowServer: false, status: 'failed', channels: null },
            { ...LOCAL, id: 'local-unreachable', isWindowServer: false, status: 'unreachable', channels: null },
            { ...LAB, status: 'running', isWindowServer: true, channels: [] },
        ]);
        expect(within(await row('local-starting')).getByRole('button', { name: 'Stop' })).toBeInTheDocument();
        expect(within(await row('local-stopped')).getByRole('button', { name: 'Start' })).toBeInTheDocument();
        await row('local-failed');
        await row('local-unreachable');
        await row('lab');
        expect(screen.queryByRole('switch', { name: KEEP })).toBeNull();
    });

    it('Windows has no local server: no Start, Stop or keep-alive (MS-05)', async () => {
        // A local entry in each state that would otherwise offer Stop or Start.
        setup({ platform: 'win32' }, [
            { ...LOCAL, isWindowServer: false, status: 'running' },
            { ...LOCAL, id: 'local-stopped', isWindowServer: false, status: 'stopped', channels: null },
            { ...LAB, status: 'running', isWindowServer: true, channels: [] },
        ]);
        await row('lab');
        await row('local');
        await row('local-stopped');
        expect(screen.queryByRole('switch', { name: KEEP })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
        expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
    });

    it('an off remote keeps its record on the card: Off, Remote, URL, token; no version, channels or Open (Servers switch)', async () => {
        setup({}, [LOCAL, {
            ...LAB, status: 'off', enabled: false, serverVersion: null, contract: null,
            channels: null, error: undefined,
        }]);
        const lab = await row('lab');
        expect(lab.textContent).toContain('Lab');
        expect(lab.textContent).toContain('Remote');
        expect(lab.textContent).toContain('https://lab.test');
        expect(lab.textContent).toContain('Token saved');
        const label = within(lab).getByText('Off');
        expect(label).toHaveClass('text-ink-3');
        expect(lab.querySelector('[data-versions]')).toBeNull();
        expect(lab.textContent).not.toMatch(/channel/);
        expect(within(lab).queryByRole('button', { name: 'Open' })).toBeNull();
        expect(within(lab).getByRole('switch', { name: 'Check and connect' })).toHaveAttribute('aria-checked', 'false');
        for (const name of ['Rename', 'Change token', 'Remove']) {
            expect(within(lab).getByRole('button', { name })).toBeEnabled();
        }
        // The local server has no such switch.
        expect(within(await row('local')).queryByRole('switch', { name: 'Check and connect' })).toBeNull();
    });

    it('the Check and connect switch turns a remote off and on through update, then the list reloads (Servers switch)', async () => {
        const servers = setup({}, [LOCAL, { ...LAB, status: 'running', error: undefined, channels: [] }]);
        const toggle = within(await row('lab')).getByRole('switch', { name: 'Check and connect' });
        expect(toggle).toHaveAttribute('aria-checked', 'true');
        expect(toggle).toBeEnabled();
        const listed = vi.mocked(servers.list).mock.calls.length;
        fireEvent.click(toggle);
        await waitFor(() => expect(servers.update).toHaveBeenCalledExactlyOnceWith({ id: 'lab', enabled: false }));
        await waitFor(() => expect(vi.mocked(servers.list).mock.calls.length).toBeGreaterThan(listed));

        cleanup();
        uninstallDesktopShell();
        const again = setup({}, [LOCAL, { ...LAB, status: 'off', enabled: false, channels: null, error: undefined }]);
        fireEvent.click(within(await row('lab')).getByRole('switch', { name: 'Check and connect' }));
        await waitFor(() => expect(again.update).toHaveBeenCalledExactlyOnceWith({ id: 'lab', enabled: true }));
    });

    it('the window\'s own server cannot be turned off, like Remove (Servers switch)', async () => {
        const servers = setup({}, [{ ...LOCAL, isWindowServer: false }, { ...LAB, status: 'running', isWindowServer: true, channels: [] }]);
        const toggle = within(await row('lab')).getByRole('switch', { name: 'Check and connect' });
        expect(toggle).toBeDisabled();
        expect(toggle.closest('label')).toHaveAttribute('title', 'Open another server before turning this one off');
        fireEvent.click(toggle);
        expect(servers.update).not.toHaveBeenCalled();
    });

    it('carries no build line: the app stamp lives on the Settings tab, under This profile', async () => {
        const { body } = setup();
        await row('lab');
        expect(body.querySelector('[data-local-build]')).toBeNull();
        expect(body.textContent).not.toContain('Local build');
        expect(body.textContent).not.toContain('0123456789');
    });

    it('is absent in a browser, and neither the pane\'s settings nor the Settings tab offers Servers (MS-05, plan 1105 P7)', () => {
        const { container } = render(<ServersTab />);
        expect(container.innerHTML).toBe('');
        cleanup();
        render(<SettingsView section={null} channel from={null} onOpen={vi.fn()} onBack={vi.fn()} />);
        expect(screen.getByRole('button', { name: 'Accounts on this machine' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Servers' })).toBeNull();
        cleanup();
        installDesktopShell();
        render(<SettingsTab onOpen={vi.fn()} />);
        const profile = screen.getByRole('region', { name: 'This profile' });
        expect(within(profile).getByText('This app on this computer: the Collimate profile')).toBeInTheDocument();
        expect(profile.querySelector('[data-profile-section]')).not.toBeNull();
        expect(screen.queryByRole('button', { name: 'Servers' })).toBeNull();
    });
});
