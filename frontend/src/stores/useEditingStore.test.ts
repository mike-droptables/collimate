/**
 * The editing preferences store, per profile: its first value (the shell's
 * bootstrap, else this browser's localStorage, else the defaults), a commit
 * (localStorage `collimate.profile.v1` in a browser, the bridge's
 * setEditingPrefs in the shell, never a channel PUT), a change made
 * elsewhere (the shell's push, another tab's `storage` event), and the host
 * following it: <html spellcheck> and the collimate:editing-changed event
 * the renderer hosts re-send from. Renderers see spellcheck only:
 * status_on_run never reaches the snapshot, the event or SET_EDITING.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { putChannelSettings } from '@/lib/api';
import { EDITING_CHANGED_EVENT, readEditingSnapshot } from '@/lib/editingSnapshot';
import { PROFILE_PREFS_KEY } from '@/lib/profilePrefs';
import { pushSandboxEditing } from '@/lib/sandboxBridge';
import { installDesktopShell, uninstallDesktopShell } from '@/test/desktopShell';
import { useEditingStore } from './useEditingStore';

const putMock = vi.mocked(putChannelSettings);
const spellcheckAttr = () => document.documentElement.getAttribute('spellcheck');
const stored = () => JSON.parse(localStorage.getItem(PROFILE_PREFS_KEY) ?? 'null');

/** A fresh copy of the store module, so its first value and its module-level
 *  subscription see the profile as the test set it up. */
async function freshStore() {
    vi.resetModules();
    return (await import('./useEditingStore')).useEditingStore;
}

describe('useEditingStore', () => {
    const heard: unknown[] = [];
    const listen = (e: Event) => heard.push((e as CustomEvent).detail);
    beforeEach(() => {
        localStorage.clear();
        useEditingStore.getState().adoptEditing({ spellcheck: true, status_on_run: true });
        putMock.mockClear();
        heard.length = 0;
        window.addEventListener(EDITING_CHANGED_EVENT, listen);
    });
    afterEach(() => {
        window.removeEventListener(EDITING_CHANGED_EVENT, listen);
        uninstallDesktopShell();
        localStorage.clear();
        vi.restoreAllMocks();
    });

    it('applies the flag to <html> from its first value, so editables inherit it', () => {
        expect(spellcheckAttr()).toBe('true');
        expect(readEditingSnapshot()).toEqual({ spellcheck: true });
    });

    it('adopting flips <html spellcheck> and tells the hosts once, without saving', () => {
        useEditingStore.getState().adoptEditing({ spellcheck: false, status_on_run: true });
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false, status_on_run: true });
        expect(spellcheckAttr()).toBe('false');
        expect(heard).toEqual([{ spellcheck: false }]);
        // Re-adopting the same value re-sends nothing.
        useEditingStore.getState().adoptEditing({ spellcheck: false, status_on_run: true });
        expect(heard).toHaveLength(1);
        expect(localStorage.getItem(PROFILE_PREFS_KEY)).toBeNull();
        expect(putMock).not.toHaveBeenCalled();
    });

    it('a missing or non-boolean flag reads as on, each on its own', () => {
        useEditingStore.getState().adoptEditing({ spellcheck: false, status_on_run: false });
        useEditingStore.getState().adoptEditing(undefined);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: true, status_on_run: true });
        useEditingStore.getState().adoptEditing({ spellcheck: 'no', status_on_run: 1 } as never);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: true, status_on_run: true });
        useEditingStore.getState().adoptEditing({ spellcheck: false } as never);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false, status_on_run: true });
        useEditingStore.getState().adoptEditing({ status_on_run: false } as never);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: true, status_on_run: false });
    });

    it('status_on_run never reaches the renderers: the snapshot, the event and SET_EDITING carry exactly {spellcheck}', () => {
        useEditingStore.getState().adoptEditing({ spellcheck: true, status_on_run: false });
        // The app-only flag changes nothing a renderer sees.
        expect(heard).toEqual([]);
        expect(readEditingSnapshot()).toStrictEqual({ spellcheck: true });
        useEditingStore.getState().adoptEditing({ spellcheck: false, status_on_run: false });
        expect(heard).toStrictEqual([{ spellcheck: false }]);
        const postMessage = vi.fn();
        pushSandboxEditing({ postMessage } as unknown as Window);
        expect(postMessage).toHaveBeenCalledExactlyOnceWith({ type: 'SET_EDITING', spellcheck: false }, window.location.origin);
        expect(Object.keys(postMessage.mock.calls[0][0]).sort()).toEqual(['spellcheck', 'type']);
    });

    it('in a browser, a commit writes this profile\'s localStorage and never PUTs a channel', async () => {
        await useEditingStore.getState().commitEditing({ spellcheck: false, status_on_run: false });
        expect(stored()).toEqual({ editing: { spellcheck: false, status_on_run: false } });
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false, status_on_run: false });
        expect(spellcheckAttr()).toBe('false');
        expect(heard).toEqual([{ spellcheck: false }]);
        expect(putMock).not.toHaveBeenCalled();
    });

    it('in the desktop shell, a commit goes to setEditingPrefs and adopts what was saved, with localStorage untouched', async () => {
        const { shell } = installDesktopShell();
        const store = await freshStore();
        await store.getState().commitEditing({ spellcheck: true, status_on_run: false });
        expect(shell.setEditingPrefs).toHaveBeenCalledExactlyOnceWith({ spellcheck: true, statusOnRun: false });
        expect(store.getState().editing).toEqual({ spellcheck: true, status_on_run: false });
        expect(localStorage.getItem(PROFILE_PREFS_KEY)).toBeNull();
        expect(putMock).not.toHaveBeenCalled();
    });

    it('a failed save rejects after the optimistic apply (a browser that refuses storage)', async () => {
        vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('QuotaExceededError'); });
        await expect(useEditingStore.getState().commitEditing({ spellcheck: false, status_on_run: true })).rejects.toThrow('QuotaExceededError');
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false, status_on_run: true });
    });

    it('a failed save rejects in the shell too (the shell refused it)', async () => {
        installDesktopShell({ setEditingPrefs: vi.fn(async () => { throw new Error('collimate-servers/not-app-window'); }) });
        const store = await freshStore();
        await expect(store.getState().commitEditing({ spellcheck: false, status_on_run: true })).rejects.toThrow('not-app-window');
    });

    it('starts from the shell\'s bootstrap value, else localStorage, else the defaults', async () => {
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ editing: { spellcheck: true, status_on_run: false } }));
        installDesktopShell({ getEditingPrefs: () => ({ spellcheck: false, statusOnRun: true }) });
        expect((await freshStore()).getState().editing).toEqual({ spellcheck: false, status_on_run: true });
        expect(spellcheckAttr()).toBe('false');

        uninstallDesktopShell();
        expect((await freshStore()).getState().editing).toEqual({ spellcheck: true, status_on_run: false });
        expect(spellcheckAttr()).toBe('true');

        localStorage.clear();
        expect((await freshStore()).getState().editing).toEqual({ spellcheck: true, status_on_run: true });
        localStorage.setItem(PROFILE_PREFS_KEY, '{ not json');
        expect((await freshStore()).getState().editing).toEqual({ spellcheck: true, status_on_run: true });
    });

    it('adopts the shell\'s push (another load, a set elsewhere in the same profile)', async () => {
        const stub = installDesktopShell();
        const store = await freshStore();
        stub.emitEditingPrefs({ spellcheck: false, statusOnRun: false });
        expect(store.getState().editing).toEqual({ spellcheck: false, status_on_run: false });
        expect(spellcheckAttr()).toBe('false');
    });

    it('adopts another tab\'s change through the storage event', async () => {
        const store = await freshStore();
        localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ editing: { spellcheck: false, status_on_run: true } }));
        window.dispatchEvent(new StorageEvent('storage', { key: 'something.else' }));
        expect(store.getState().editing).toEqual({ spellcheck: true, status_on_run: true });
        window.dispatchEvent(new StorageEvent('storage', { key: PROFILE_PREFS_KEY }));
        expect(store.getState().editing).toEqual({ spellcheck: false, status_on_run: true });
    });
});
