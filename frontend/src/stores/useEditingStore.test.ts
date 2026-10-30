/**
 * The editing preferences store: adopting a channel's `editing` section,
 * committing one (a PUT of only that section to the channel the doc was
 * adopted from, cache-only while none is), and the host following it —
 * <html spellcheck> and the collimate:editing-changed event the renderer
 * hosts re-send from.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { putChannelSettings } from '@/lib/api';
import { readBootCache } from '@/lib/settingsCache';
import { EDITING_CHANGED_EVENT, readEditingSnapshot } from '@/lib/editingSnapshot';
import { useEditingStore } from './useEditingStore';
import { useChannelSettingsStore } from './useChannelSettingsStore';

const putMock = vi.mocked(putChannelSettings);
const spellcheckAttr = () => document.documentElement.getAttribute('spellcheck');

describe('useEditingStore', () => {
    const heard: unknown[] = [];
    const listen = (e: Event) => heard.push((e as CustomEvent).detail);
    beforeEach(() => {
        useEditingStore.getState().adoptEditing({ spellcheck: true });
        useChannelSettingsStore.setState({ loadedFor: '11111111-1111-4111-8111-111111111111' });
        putMock.mockClear();
        localStorage.clear();
        heard.length = 0;
        window.addEventListener(EDITING_CHANGED_EVENT, listen);
    });
    afterEach(() => {
        window.removeEventListener(EDITING_CHANGED_EVENT, listen);
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    });

    it('applies the flag to <html> from its first value, so editables inherit it', () => {
        expect(spellcheckAttr()).toBe('true');
        expect(readEditingSnapshot()).toEqual({ spellcheck: true });
    });

    it('adopting a doc section flips <html spellcheck> and tells the hosts once, without saving', () => {
        useEditingStore.getState().adoptEditing({ spellcheck: false });
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false });
        expect(spellcheckAttr()).toBe('false');
        expect(heard).toEqual([{ spellcheck: false }]);
        // Re-adopting the same value re-sends nothing.
        useEditingStore.getState().adoptEditing({ spellcheck: false });
        expect(heard).toHaveLength(1);
        expect(putMock).not.toHaveBeenCalled();
    });

    it('a missing or non-boolean flag reads as on', () => {
        useEditingStore.getState().adoptEditing({ spellcheck: false });
        useEditingStore.getState().adoptEditing(undefined);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: true });
        useEditingStore.getState().adoptEditing({ spellcheck: 'no' } as never);
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: true });
    });

    it('commit PUTs only the editing section to the adopted channel and rewrites the boot cache', async () => {
        await useEditingStore.getState().commitEditing({ spellcheck: false });
        expect(putMock).toHaveBeenCalledExactlyOnceWith('11111111-1111-4111-8111-111111111111', { editing: { spellcheck: false } });
        expect(readBootCache().editing).toEqual({ spellcheck: false });
        expect(spellcheckAttr()).toBe('false');
        expect(heard).toEqual([{ spellcheck: false }]);
    });

    it('with no channel adopted (a switch still loading) the commit is cache-only', async () => {
        useChannelSettingsStore.setState({ loadedFor: null });
        await useEditingStore.getState().commitEditing({ spellcheck: false });
        expect(putMock).not.toHaveBeenCalled();
        expect(readBootCache().editing).toEqual({ spellcheck: false });
        expect(spellcheckAttr()).toBe('false');
    });

    it('a failed PUT rejects after the optimistic apply', async () => {
        putMock.mockRejectedValueOnce(new Error('HTTP 503'));
        await expect(useEditingStore.getState().commitEditing({ spellcheck: false })).rejects.toThrow('HTTP 503');
        expect(useEditingStore.getState().editing).toEqual({ spellcheck: false });
    });
});
