/**
 * Settings → This profile: in the desktop shell, the app's version and
 * build stamp, the automatic-updates switch, Check for updates through the
 * bridge and the interface size (RS-05); everywhere, the Editing group.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import { ProfileSection } from './ProfileSection';
import { expectButtonRowsWrap, expectFitsPaneColumn, renderInPaneColumn } from './paneColumn.testkit';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import type { BuildInfo, CollimateDesktop } from '@/lib/desktop';

const STAMPED: BuildInfo = {
    version: '0.6.0',
    commit: 'fedcba9876543210fedcba9876543210fedcba98',
    dirty: true,
    builtAt: '2026-11-04T10:00:00Z',
    serverEnvironmentHash: 'a1b2c3d4e5f6a7b8c9d0',
};

function setup(over: Partial<CollimateDesktop> = {}) {
    const stub = installDesktopShell(over);
    const { body } = renderInPaneColumn(<ProfileSection />);
    return { ...stub, body };
}

const updates = () => within(screen.getByRole('region', { name: 'Updates' }));

afterEach(() => { cleanup(); uninstallDesktopShell(); });

const groups = (root: ParentNode) =>
    [...root.querySelectorAll('[data-profile-section] > section')].map(section =>
        section.getAttribute('aria-label') ?? section.querySelector('h4')?.textContent);

describe('ProfileSection', () => {
    it('names the app version and its build stamp from buildInfo()', () => {
        const { body } = setup({ buildInfo: () => STAMPED });
        expectFitsPaneColumn(body);
        const app = within(screen.getByRole('region', { name: 'App' }));
        expect(app.getByText('Collimate 0.6.0')).toBeInTheDocument();
        expect(body.querySelector('[data-build-stamp]')?.textContent).toBe(
            `Build fedcba9876 · uncommitted changes · built ${new Date(STAMPED.builtAt!).toLocaleString()} · server environment a1b2c3d4e5f6`,
        );
    });

    it('builds the stamp line from what the stamp has, and says so when there is none', () => {
        const { body } = setup({ buildInfo: () => ({ ...STAMPED, dirty: false, serverEnvironmentHash: null }) });
        expect(body.querySelector('[data-build-stamp]')?.textContent)
            .toBe(`Build fedcba9876 · built ${new Date(STAMPED.builtAt!).toLocaleString()}`);
        cleanup();
        const dev = setup({ buildInfo: () => ({ version: '0.6.0', commit: null, dirty: null, builtAt: null, serverEnvironmentHash: null }) });
        expect(dev.body.querySelector('[data-build-stamp]')?.textContent).toBe('No build stamp (a development run)');
    });

    it('shows the automatic-updates setting and saves a change through the bridge', async () => {
        const { shell } = setup();
        const toggle = updates().getByRole('switch', { name: 'Automatic updates' });
        await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'));
        expect(toggle).not.toBeDisabled();

        fireEvent.click(toggle);
        await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
        expect(shell.setAutomaticUpdates).toHaveBeenCalledExactlyOnceWith(false);
    });

    it('reads an off setting as off, and keeps the switch disabled until the shell answers', async () => {
        let answer!: (on: boolean) => void;
        setup({ getAutomaticUpdates: vi.fn(() => new Promise<boolean>(resolve => { answer = resolve; })) });
        const toggle = updates().getByRole('switch', { name: 'Automatic updates' });
        expect(toggle).toBeDisabled();
        await act(async () => answer(false));
        expect(toggle).toHaveAttribute('aria-checked', 'false');
        expect(toggle).not.toBeDisabled();
    });

    it('Check for updates drives the shell\'s updater and says the answer comes as a dialog', async () => {
        const { shell, body } = setup();
        expectButtonRowsWrap(body);
        fireEvent.click(updates().getByRole('button', { name: 'Check for updates' }));
        expect(shell.checkForUpdates).toHaveBeenCalledOnce();
        expect(await updates().findByRole('status')).toHaveTextContent('Checking… the answer opens in a dialog.');
    });

    it('shows why there is nothing to check where the install has no update path (a dev run has no feed)', async () => {
        setup({ checkForUpdates: vi.fn(async () => ({ status: 'unavailable' as const, reason: 'no update feed (this package carries no app-update.yml and COLLIMATE_UPDATE_FEED is unset)' })) });
        fireEvent.click(updates().getByRole('button', { name: 'Check for updates' }));
        expect(await updates().findByRole('status')).toHaveTextContent(
            'This install does not update itself: no update feed (this package carries no app-update.yml and COLLIMATE_UPDATE_FEED is unset).',
        );
    });

    it('says a check is already running', async () => {
        setup({ checkForUpdates: vi.fn(async () => ({ status: 'busy' as const })) });
        fireEvent.click(updates().getByRole('button', { name: 'Check for updates' }));
        expect(await updates().findByRole('status')).toHaveTextContent('A check is already running');
    });

    it('shows a refusal from the shell as an alert', async () => {
        setup({ checkForUpdates: vi.fn(async () => { throw new Error('collimate-servers/not-app-window'); }) });
        fireEvent.click(updates().getByRole('button', { name: 'Check for updates' }));
        expect(await updates().findByRole('alert')).toHaveTextContent('collimate-servers/not-app-window');
    });

    it('holds this profile\'s interface size and follows page zoom (RS-05)', () => {
        let scale = 1.1;
        const setInterfaceScale = vi.fn((f: number) => { scale = f; });
        const stub = setup({ getInterfaceScale: () => scale, setInterfaceScale });
        act(() => stub.setZoomFactor(1.1));
        const group = screen.getByRole('group', { name: 'Interface size' });
        expect(group).toHaveClass('flex-wrap');
        const row = within(group);
        expect(row.getAllByRole('button').map(b => b.textContent)).toEqual(['70%', '80%', '90%', '100%', '110%', '125%', '150%']);
        expect(row.getByRole('button', { name: '110%' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(row.getByRole('button', { name: '125%' }));
        expect(setInterfaceScale).toHaveBeenCalledExactlyOnceWith(1.25);
        expect(row.getByRole('button', { name: '125%' })).toHaveAttribute('aria-pressed', 'true');
        // A change made elsewhere (the View menu) shows through the zoom signal.
        act(() => stub.setZoomFactor(0.9));
        expect(row.getByRole('button', { name: '90%' })).toHaveAttribute('aria-pressed', 'true');
    });

    it('shows only the Editing group in a browser', () => {
        const { container } = render(<ProfileSection />);
        expect(container.querySelector('[data-profile-section]')).not.toBeNull();
        expect(groups(container)).toEqual(['Editing']);
        expect(screen.getByRole('switch', { name: 'Spellcheck' })).toBeInTheDocument();
        expect(screen.getByRole('switch', { name: 'Open status when a run starts' })).toBeInTheDocument();
        expect(screen.queryByRole('region', { name: 'Updates' })).toBeNull();
        expect(container.querySelector('[data-build-stamp]')).toBeNull();
    });

    it('shows App, Updates, Interface size and Editing in the desktop shell', () => {
        const { body } = setup({ getInterfaceScale: () => 1, setInterfaceScale: vi.fn() });
        expect(groups(body)).toEqual(['App', 'Updates', 'Interface size', 'Editing']);
    });
});
