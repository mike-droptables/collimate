// The Commands pane's settings view on its own: the root's rows (Accounts
// on this machine, and inside a channel its theme override), a section's
// head and its lazily loaded body, and the section chrome the channels
// page's docked panel shares.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsSectionFrame, SettingsView } from './SettingsView';
import { ACCOUNTS_DESCRIPTION, SETTINGS_SECTIONS, settingsSectionsFor } from './settingsSections';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const pending = vi.hoisted(() => {
    let release!: () => void;
    const ready = new Promise<void>(resolve => { release = resolve; });
    return { ready, release, started: vi.fn() };
});
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys body</div> }));
vi.mock('@/components/settings/ThemeSection', async () => {
    pending.started();
    await pending.ready;
    return { ThemeSection: ({ mode }: { mode: string }) => <input aria-label={`Loaded theme field (${mode})`} /> };
});

const view = (over: Partial<Parameters<typeof SettingsView>[0]> = {}) => {
    const props = { section: null, channel: true, from: null, onOpen: vi.fn(), onBack: vi.fn(), ...over };
    return { props, ...render(<SettingsView {...props} />) };
};

afterEach(() => uninstallDesktopShell());

describe('SettingsView root', () => {
    it('lists Accounts on this machine always and the theme override only inside a channel; nothing else is the pane\'s', () => {
        expect(settingsSectionsFor(true)).toEqual(SETTINGS_SECTIONS);
        expect(settingsSectionsFor(true).map(s => s.id)).toEqual(['keys', 'theme']);
        expect(settingsSectionsFor(false).map(s => s.id)).toEqual(['keys']);
        const { rerender, props } = view({ channel: false });
        expect(screen.getByRole('button', { name: 'Accounts on this machine' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Override theme for this channel' })).toBeNull();
        rerender(<SettingsView {...props} channel />);
        const rows = [...document.querySelectorAll('[data-settings-row]')].map(row => row.getAttribute('data-settings-row'));
        expect(rows).toEqual(['keys', 'theme']);
        for (const gone of ['Editing', 'Themes', 'This device', 'This profile', 'Export settings', 'Import settings', 'Servers']) {
            expect(screen.queryByRole('button', { name: gone })).toBeNull();
        }
        expect(screen.queryByText('Settings file')).toBeNull();
    });

    it('a row opens its section; the footer\'s esc back goes up a level (CP-3)', () => {
        const { props } = view();
        fireEvent.click(screen.getByRole('button', { name: 'Override theme for this channel' }));
        expect(props.onOpen).toHaveBeenCalledWith('theme');
        fireEvent.click(screen.getByRole('button', { name: /back$/ }));
        expect(props.onBack).toHaveBeenCalledOnce();
    });

    it('the row the root came back from is its entry field (CP-3)', () => {
        view({ from: 'theme' });
        expect(screen.getByRole('button', { name: 'Override theme for this channel' })).toHaveAttribute('data-pane-entry');
        expect(document.querySelectorAll('[data-pane-entry]')).toHaveLength(1);
    });

    it('paints its rows from the skin tokens (CP-3)', () => {
        view();
        expect(screen.getByRole('button', { name: 'Accounts on this machine' })).toHaveClass('h-11', 'rounded-md', 'text-ink-2', 'focus:bg-sheet-2');
    });
});

describe('SettingsView section', () => {
    it('shows the back button, the title and the description, and defers the body\'s import until opened; the theme body is the override mode (CP-3)', async () => {
        const { rerender, props } = view();
        expect(pending.started).not.toHaveBeenCalled();
        rerender(<SettingsView {...props} section="theme" />);
        expect(screen.getByRole('heading', { name: 'Override theme for this channel' })).toBeInTheDocument();
        expect(screen.getByText("This channel's theme; the default and the themes themselves are this server's, shared by every channel.")).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Back to settings' })).toHaveAttribute('data-pane-entry');
        await waitFor(() => expect(pending.started).toHaveBeenCalledOnce());
        expect(screen.queryByLabelText('Loaded theme field (override)')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Back to settings' }));
        expect(props.onBack).toHaveBeenCalledOnce();
        await act(async () => { pending.release(); await pending.ready; });
        expect(await screen.findByLabelText('Loaded theme field (override)')).toBeInTheDocument();
        expect(document.querySelector('[data-settings-section="theme"]')).toHaveClass('flex-1', 'min-h-0', 'overflow-y-auto', 'px-3.5', 'py-3');
    });

    it('the Accounts section says whose keyring it is', async () => {
        view({ section: 'keys' });
        expect(screen.getByRole('heading', { name: 'Accounts on this machine' })).toBeInTheDocument();
        expect(screen.getByText(ACCOUNTS_DESCRIPTION)).toBeInTheDocument();
        expect(await screen.findByText('keys body')).toBeInTheDocument();
    });

    it('in a window on a remote server, the Accounts description names that server\'s computer', () => {
        installDesktopShell({ server: { id: 'lab', kind: 'remote', name: 'Lab box', url: 'https://lab.example', port: 443, dataDir: null, serverVersion: null } });
        view({ section: 'keys' });
        expect(screen.getByText(`${ACCOUNTS_DESCRIPTION} That is Lab box’s computer, not this one.`)).toBeInTheDocument();
    });

    it('a section the pane does not offer shows the root: the theme override without a channel (CP-3)', () => {
        view({ section: 'theme', channel: false });
        expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
        expect(screen.queryByRole('heading', { name: 'Override theme for this channel' })).toBeNull();
    });
});

describe('SettingsSectionFrame', () => {
    it('draws the head it is given, the description, the padded body and the footer\'s esc', () => {
        const onEsc = vi.fn();
        render(<SettingsSectionFrame id="theme" description="One line." escLabel="close" onEsc={onEsc}
            head={<h2>Default theme</h2>}><p>body</p></SettingsSectionFrame>);
        expect(screen.getByRole('heading', { name: 'Default theme' })).toBeInTheDocument();
        expect(screen.getByText('One line.')).toHaveClass('px-3.5', 'py-2', 'border-b');
        const body = document.querySelector('[data-settings-section="theme"]')!;
        expect(body).toHaveClass('px-3.5', 'py-3', 'overflow-y-auto');
        expect(body).toHaveTextContent('body');
        fireEvent.click(screen.getByRole('button', { name: /close$/ }));
        expect(onEsc).toHaveBeenCalledOnce();
    });
});
