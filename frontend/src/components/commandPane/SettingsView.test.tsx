// The Commands pane's settings view on its own: the root's rows (the
// sections and the Settings file rows), a section's head and its lazily
// loaded body, and the settings JSON export / import.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SettingsView } from './SettingsView';
import { SETTINGS_SECTIONS, settingsSectionsFor } from './settingsSections';
import { fetchChannelSettings, putChannelSettings, type ChannelSettings } from '@/lib/api/settings';
import { RENDERER_PREFS_CHANGED_EVENT } from '@/lib/api/catalog';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';

const pending = vi.hoisted(() => {
    let release!: () => void;
    const ready = new Promise<void>(resolve => { release = resolve; });
    return { ready, release, started: vi.fn() };
});
vi.mock('@/lib/api/settings', () => ({ fetchChannelSettings: vi.fn(), putChannelSettings: vi.fn() }));
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys body</div> }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/components/settings/ServersSection', () => ({ ServersSection: () => <div>servers body</div> }));
vi.mock('@/components/settings/EditingSection', async () => {
    pending.started();
    await pending.ready;
    return { EditingSection: () => <input aria-label="Loaded editing field" /> };
});

const view = (over: Partial<Parameters<typeof SettingsView>[0]> = {}) => {
    const props = { section: null, desktop: false, from: null, onOpen: vi.fn(), onBack: vi.fn(), ...over };
    return { props, ...render(<SettingsView {...props} />) };
};

describe('SettingsView root', () => {
    it('lists the sections from the one table, the desktop-only ones only in the desktop shell (CP-3)', () => {
        expect(settingsSectionsFor(true)).toEqual(SETTINGS_SECTIONS);
        expect(settingsSectionsFor(false).map(s => s.id)).toEqual(['keys', 'editing', 'theme']);
        const { rerender, props } = view();
        expect(screen.queryByRole('button', { name: 'Servers' })).toBeNull();
        rerender(<SettingsView {...props} desktop />);
        expect(screen.getByRole('button', { name: 'Servers' })).toHaveAccessibleDescription('One server per window; the local server keeps running');
    });

    it('a row opens its section; the footer\'s esc back goes up a level (CP-3)', () => {
        const { props } = view();
        fireEvent.click(screen.getByRole('button', { name: 'Themes' }));
        expect(props.onOpen).toHaveBeenCalledWith('theme');
        fireEvent.click(screen.getByRole('button', { name: /back$/ }));
        expect(props.onBack).toHaveBeenCalledOnce();
    });

    it('the row the root came back from is its entry field (CP-3)', () => {
        view({ from: 'theme' });
        expect(screen.getByRole('button', { name: 'Themes' })).toHaveAttribute('data-pane-entry');
        expect(document.querySelectorAll('[data-pane-entry]')).toHaveLength(1);
    });

    it('paints its rows from the skin tokens (CP-3)', () => {
        view();
        expect(screen.getByRole('button', { name: 'Key manager' })).toHaveClass('h-11', 'rounded-md', 'text-ink-2', 'focus:bg-sheet-2');
        expect(screen.getByRole('button', { name: 'Export settings' })).toHaveClass('h-[1.875rem]', 'rounded-md', 'text-ink-2');
    });
});

describe('SettingsView section', () => {
    it('shows the back button, the title and the description, and defers the body\'s import until opened (CP-3)', async () => {
        const { rerender, props } = view();
        expect(pending.started).not.toHaveBeenCalled();
        rerender(<SettingsView {...props} section="editing" />);
        expect(screen.getByRole('heading', { name: 'Editing' })).toBeInTheDocument();
        expect(screen.getByText("Spellcheck in this channel's fields, in the app and every renderer")).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Back to settings' })).toHaveAttribute('data-pane-entry');
        await waitFor(() => expect(pending.started).toHaveBeenCalledOnce());
        expect(screen.queryByLabelText('Loaded editing field')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Back to settings' }));
        expect(props.onBack).toHaveBeenCalledOnce();
        await act(async () => { pending.release(); await pending.ready; });
        expect(await screen.findByLabelText('Loaded editing field')).toBeInTheDocument();
        expect(document.querySelector('[data-settings-section="editing"]')).toHaveClass('flex-1', 'min-h-0', 'overflow-y-auto', 'px-3.5', 'py-3');
    });

    it('a section the page does not offer shows the root (CP-3)', () => {
        view({ section: 'servers' });
        expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    });
});

describe('SettingsView settings JSON (TR-12)', () => {
    const doc: ChannelSettings = {
        version: 1, kind: 'channel-settings', keyboard: { overrides: {}, commands: {} },
        theme: { activeThemeId: 'paper', userThemes: [] }, editing: { spellcheck: false },
        remove_zones: { zones: [] },
    };
    const setup = () => {
        vi.mocked(fetchChannelSettings).mockReset().mockResolvedValue(doc);
        vi.mocked(putChannelSettings).mockReset().mockResolvedValue(doc);
        useCollimateStore.setState({ activeChannel: { id: 'ch-1', name: 'Research' } as never });
        const reload = vi.fn(async () => {});
        useChannelSettingsStore.setState({ reloadAfterExternalWrite: reload });
        return reload;
    };
    const pick = (text: string) => {
        const input = screen.getByLabelText('Settings file') as HTMLInputElement;
        fireEvent.change(input, { target: { files: [new File([text], 'settings.json', { type: 'application/json' })] } });
    };

    it('offers Export settings and Import settings under Settings file, disabled with no channel (TR-12)', () => {
        const { unmount } = view();
        expect(screen.getByText('Settings file')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Export settings' })).toBeDisabled();
        unmount();
        setup();
        view();
        expect(screen.getByRole('button', { name: 'Export settings' })).toBeEnabled();
        expect(screen.getByRole('button', { name: 'Import settings' })).toBeEnabled();
    });

    it('exports the whole doc as <channel>-settings.json through the save dialog when present (TR-12)', async () => {
        setup();
        const write = vi.fn(async () => {});
        const close = vi.fn(async () => {});
        const picker = vi.fn(async () => ({ createWritable: async () => ({ write, close }) }));
        Object.assign(window, { showSaveFilePicker: picker });
        try {
            view();
            fireEvent.click(screen.getByRole('button', { name: 'Export settings' }));
            await waitFor(() => expect(close).toHaveBeenCalled());
            expect(fetchChannelSettings).toHaveBeenCalledWith('ch-1');
            expect(picker).toHaveBeenCalledWith(expect.objectContaining({ suggestedName: 'Research-settings.json' }));
            const blob = (write.mock.calls[0] as unknown as [Blob])[0];
            expect(JSON.parse(await blob.text())).toEqual(doc);
            expect(await screen.findByText('Settings exported.')).toBeDefined();
        } finally {
            delete (window as { showSaveFilePicker?: unknown }).showSaveFilePicker;
        }
    });

    it('falls back to a download link without the save dialog (TR-12)', async () => {
        setup();
        const createObjectURL = vi.fn(() => 'blob:settings');
        Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
        const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
            expect(this.download).toBe('Research-settings.json');
            expect(this.href).toBe('blob:settings');
        });
        view();
        fireEvent.click(screen.getByRole('button', { name: 'Export settings' }));
        await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
        click.mockRestore();
    });

    it('imports a channel-settings file: PUTs the whole doc, then reloads the settings store and the renderer prefs (TR-12)', async () => {
        const reload = setup();
        const announced = vi.fn();
        window.addEventListener(RENDERER_PREFS_CHANGED_EVENT, announced);
        try {
            view();
            pick(JSON.stringify(doc));
            await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
            expect(putChannelSettings).toHaveBeenCalledWith('ch-1', doc);
            expect(await screen.findByText('Settings imported.')).toBeDefined();
            expect(announced).toHaveBeenCalledTimes(1);
        } finally { window.removeEventListener(RENDERER_PREFS_CHANGED_EVENT, announced); }
    });

    it('refuses a file of another kind inline, writing nothing (TR-12)', async () => {
        const reload = setup();
        view();
        pick(JSON.stringify({ ...doc, kind: 'other' }));
        expect((await screen.findByRole('alert')).textContent).toMatch(/Not a channel settings file/);
        expect(putChannelSettings).not.toHaveBeenCalled();
        expect(reload).not.toHaveBeenCalled();
    });

    it('shows the server\'s refusal inline (TR-12)', async () => {
        const reload = setup();
        vi.mocked(putChannelSettings).mockRejectedValue(new Error('unknown settings section(s): extra'));
        view();
        pick(JSON.stringify(doc));
        expect((await screen.findByRole('alert')).textContent).toBe('unknown settings section(s): extra');
        expect(reload).not.toHaveBeenCalled();
    });
});
