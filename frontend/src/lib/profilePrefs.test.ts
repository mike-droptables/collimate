// @vitest-environment jsdom
/**
 * The profile's editing preferences at their boundary: sanitising (a
 * missing or junk flag is on), the mapping between the bridge's
 * {spellcheck, statusOnRun} and the store's {spellcheck, status_on_run},
 * and the two sources: the desktop shell's members (localStorage never
 * used) and, in a browser or a shell without them, localStorage under
 * `collimate.profile.v1` with the `storage` event.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
    DEFAULT_EDITING,
    PROFILE_PREFS_KEY,
    onEditingPreferencesChanged,
    readEditingPreferences,
    sanitizeEditing,
    writeEditingPreferences,
} from './profilePrefs';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';

const stored = () => JSON.parse(localStorage.getItem(PROFILE_PREFS_KEY) ?? 'null');

beforeEach(() => localStorage.clear());
afterEach(() => {
    uninstallDesktopShell();
    localStorage.clear();
    vi.restoreAllMocks();
});

describe('sanitizeEditing', () => {
    it('keeps real booleans and reads anything else as the default, each on its own', () => {
        expect(DEFAULT_EDITING).toEqual({ spellcheck: true, status_on_run: true });
        expect(Object.isFrozen(DEFAULT_EDITING)).toBe(true);
        expect(sanitizeEditing({ spellcheck: false, status_on_run: false })).toEqual({ spellcheck: false, status_on_run: false });
        expect(sanitizeEditing({ spellcheck: false })).toEqual({ spellcheck: false, status_on_run: true });
        expect(sanitizeEditing({ spellcheck: 'no', status_on_run: 0 })).toEqual(DEFAULT_EDITING);
        for (const junk of [undefined, null, 'x', 3, []]) expect(sanitizeEditing(junk)).toEqual(DEFAULT_EDITING);
        // Only the two flags come out.
        expect(Object.keys(sanitizeEditing({ spellcheck: true, extra: 1 })).sort()).toEqual(['spellcheck', 'status_on_run']);
    });
});

describe('in a browser', () => {
    it('reads localStorage, the defaults when it is missing, unreadable or not the document', () => {
        expect(readEditingPreferences()).toEqual(DEFAULT_EDITING);
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ editing: { spellcheck: false, status_on_run: true } }));
        expect(readEditingPreferences()).toEqual({ spellcheck: false, status_on_run: true });
        for (const junk of ['{ not json', '[]', '"x"', JSON.stringify({ editing: 'off' })]) {
            localStorage.setItem(PROFILE_PREFS_KEY, junk);
            expect(readEditingPreferences()).toEqual(DEFAULT_EDITING);
        }
        vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError'); });
        expect(readEditingPreferences()).toEqual(DEFAULT_EDITING);
    });

    it('writes the sanitised preferences under the profile key, keeping the document\'s other keys', async () => {
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ other: 1 }));
        await expect(writeEditingPreferences({ spellcheck: false, status_on_run: 'x' } as never))
            .resolves.toEqual({ spellcheck: false, status_on_run: true });
        expect(stored()).toEqual({ other: 1, editing: { spellcheck: false, status_on_run: true } });
    });

    it('rejects when storage refuses the write', async () => {
        vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError'); });
        await expect(writeEditingPreferences(DEFAULT_EDITING)).rejects.toThrow('QuotaExceededError');
    });

    it('hears another tab through the storage event for its key (and a clear), until unsubscribed', () => {
        const heard = vi.fn();
        const off = onEditingPreferencesChanged(heard);
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ editing: { spellcheck: false, status_on_run: false } }));
        window.dispatchEvent(new StorageEvent('storage', { key: 'unrelated' }));
        expect(heard).not.toHaveBeenCalled();
        window.dispatchEvent(new StorageEvent('storage', { key: PROFILE_PREFS_KEY }));
        expect(heard).toHaveBeenLastCalledWith({ spellcheck: false, status_on_run: false });
        localStorage.clear();
        window.dispatchEvent(new StorageEvent('storage', { key: null }));
        expect(heard).toHaveBeenLastCalledWith(DEFAULT_EDITING);
        off();
        window.dispatchEvent(new StorageEvent('storage', { key: PROFILE_PREFS_KEY }));
        expect(heard).toHaveBeenCalledTimes(2);
    });

    it('falls back to localStorage in a shell without the editing members', async () => {
        installDesktopShell({ getEditingPrefs: undefined, setEditingPrefs: undefined, onEditingPrefsChanged: undefined });
        await writeEditingPreferences({ spellcheck: false, status_on_run: true });
        expect(stored()).toEqual({ editing: { spellcheck: false, status_on_run: true } });
        expect(readEditingPreferences()).toEqual({ spellcheck: false, status_on_run: true });
    });
});

describe('in the desktop shell', () => {
    it('reads the bridge synchronously and maps statusOnRun to status_on_run', () => {
        installDesktopShell({ getEditingPrefs: () => ({ spellcheck: true, statusOnRun: false }) });
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ editing: { spellcheck: false, status_on_run: true } }));
        expect(readEditingPreferences()).toEqual({ spellcheck: true, status_on_run: false });
    });

    it('writes through setEditingPrefs in the bridge\'s shape, resolves with what the shell saved, and never touches localStorage', async () => {
        const { shell } = installDesktopShell();
        await expect(writeEditingPreferences({ spellcheck: false, status_on_run: false }))
            .resolves.toEqual({ spellcheck: false, status_on_run: false });
        expect(shell.setEditingPrefs).toHaveBeenCalledExactlyOnceWith({ spellcheck: false, statusOnRun: false });
        expect(localStorage.getItem(PROFILE_PREFS_KEY)).toBeNull();
        expect(readEditingPreferences()).toEqual({ spellcheck: false, status_on_run: false });
    });

    it('passes the shell\'s pushes on in the store\'s shape, and ignores the storage event', () => {
        const stub = installDesktopShell();
        const heard = vi.fn();
        const off = onEditingPreferencesChanged(heard);
        stub.emitEditingPrefs({ spellcheck: false, statusOnRun: true });
        expect(heard).toHaveBeenCalledExactlyOnceWith({ spellcheck: false, status_on_run: true });
        window.dispatchEvent(new StorageEvent('storage', { key: PROFILE_PREFS_KEY }));
        expect(heard).toHaveBeenCalledOnce();
        off();
        stub.emitEditingPrefs({ spellcheck: true, statusOnRun: true });
        expect(heard).toHaveBeenCalledOnce();
    });
});
