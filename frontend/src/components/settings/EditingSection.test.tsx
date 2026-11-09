// The Editing section: two labelled switches, Spellcheck and Open status
// when a run starts, bound to this profile's editing preferences (This
// profile's Editing group), in the Commands pane's column
// (renderInPaneColumn, PANE_COLUMN_PX wide). It shows the adopted value, a
// toggle saves the profile's preferences (this browser's localStorage, or
// the shell's setEditingPrefs), never a channel, and a failed save rolls the
// switch back and says why.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { putChannelSettings } from '@/lib/api';
import { PROFILE_PREFS_KEY } from '@/lib/profilePrefs';
import { EditingSection } from './EditingSection';
import { expectButtonRowsWrap, expectFitsPaneColumn, renderInPaneColumn } from './paneColumn.testkit';
import { useEditingStore } from '@/stores/useEditingStore';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { retiredOr } from '@/test/retiredTokens';

const putMock = vi.mocked(putChannelSettings);
const spellcheck = () => screen.getByRole('switch', { name: 'Spellcheck' });
const statusOnRun = () => screen.getByRole('switch', { name: 'Open status when a run starts' });
const stored = () => JSON.parse(localStorage.getItem(PROFILE_PREFS_KEY) ?? 'null');

beforeEach(() => {
    localStorage.clear();
    useEditingStore.getState().adoptEditing({ spellcheck: true, status_on_run: true });
    putMock.mockClear();
});
afterEach(() => {
    cleanup();
    uninstallDesktopShell();
    localStorage.clear();
    vi.restoreAllMocks();
});

describe('EditingSection', () => {
    it('fits the pane\'s column with no heading or description of its own: This profile\'s Editing group carries the heading (CP-6)', () => {
        const { body } = renderInPaneColumn(<EditingSection />);
        expect(screen.queryByRole('heading')).toBeNull();
        expect(screen.queryByText(/every renderer/)).toBeNull();
        expect(body.querySelector('.grid')).toBeNull();
        expectFitsPaneColumn(body);
        expectButtonRowsWrap(body);
        expect(spellcheck().closest('.border-line')).toHaveTextContent('Underline misspelled words');
    });

    it('reflects the adopted section and paints the switch from the skin tokens (CP-6)', () => {
        const { container } = renderInPaneColumn(<EditingSection />);
        expect(spellcheck()).toHaveAttribute('aria-checked', 'true');
        expect(spellcheck()).toHaveClass('bg-ink');
        act(() => useEditingStore.getState().adoptEditing({ spellcheck: false, status_on_run: true }));
        expect(spellcheck()).toHaveAttribute('aria-checked', 'false');
        expect(spellcheck()).toHaveClass('bg-line-2');
        const painted = [...container.querySelectorAll('*')]
            .map(el => `${el.getAttribute('class') ?? ''} ${el.getAttribute('style') ?? ''}`).join('\n');
        expect(painted).not.toMatch(retiredOr('var\\(--', 'rgba?\\('));
    });

    it('a toggle saves the profile\'s preferences, never a channel, and turns <html spellcheck> off (CP-6)', async () => {
        renderInPaneColumn(<EditingSection />);
        await act(async () => { fireEvent.click(spellcheck()); });
        expect(stored()).toEqual({ editing: { spellcheck: false, status_on_run: true } });
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false, status_on_run: true });
        expect(putMock).not.toHaveBeenCalled();
        expect(spellcheck()).toHaveAttribute('aria-checked', 'false');
        expect(document.documentElement.getAttribute('spellcheck')).toBe('false');
        expect(screen.getByText('Saved')).toBeDefined();
    });

    it('Open status when a run starts sits under Spellcheck, on by default, and saves the whole section without touching <html spellcheck>', async () => {
        renderInPaneColumn(<EditingSection />);
        const switches = screen.getAllByRole('switch');
        expect(switches.indexOf(statusOnRun())).toBe(switches.indexOf(spellcheck()) + 1);
        expect(statusOnRun()).toHaveAttribute('aria-checked', 'true');
        expect(statusOnRun().closest('.border-line')).toHaveTextContent("Show the cell's status section, with the new run's row expanded, each time a run starts here.");
        await act(async () => { fireEvent.click(statusOnRun()); });
        expect(stored()).toEqual({ editing: { spellcheck: true, status_on_run: false } });
        expect(statusOnRun()).toHaveAttribute('aria-checked', 'false');
        expect(useEditingStore.getState().editing.status_on_run).toBe(false);
        expect(document.documentElement.getAttribute('spellcheck')).toBe('true');
    });

    it('a failed save rolls the switch back and shows the error (CP-6)', async () => {
        vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError'); });
        renderInPaneColumn(<EditingSection />);
        await act(async () => { fireEvent.click(spellcheck()); });
        expect(spellcheck()).toHaveAttribute('aria-checked', 'true');
        expect(document.documentElement.getAttribute('spellcheck')).toBe('true');
        expect(screen.getByRole('alert').textContent).toBe('QuotaExceededError');
    });

    it('in the desktop shell a toggle goes to setEditingPrefs, and a refusal rolls back', async () => {
        const { shell } = installDesktopShell();
        renderInPaneColumn(<EditingSection />);
        await act(async () => { fireEvent.click(statusOnRun()); });
        expect(shell.setEditingPrefs).toHaveBeenCalledExactlyOnceWith({ spellcheck: true, statusOnRun: false });
        expect(statusOnRun()).toHaveAttribute('aria-checked', 'false');
        expect(localStorage.getItem(PROFILE_PREFS_KEY)).toBeNull();

        vi.mocked(shell.setEditingPrefs!).mockRejectedValueOnce(new Error('collimate-servers/not-app-window'));
        await act(async () => { fireEvent.click(spellcheck()); });
        expect(spellcheck()).toHaveAttribute('aria-checked', 'true');
        expect(screen.getByRole('alert').textContent).toBe('collimate-servers/not-app-window');
        expect(putMock).not.toHaveBeenCalled();
    });
});
