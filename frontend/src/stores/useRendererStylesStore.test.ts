/**
 * The renderer styles store (RS-02): adopting a channel's `renderer_styles`
 * section, committing one renderer's patch (the whole section PUT to the
 * channel the doc was adopted from, cache-only while none is), and the host
 * following it — lib/rendererStyles' record per renderer id and the
 * collimate:renderer-styles-changed event the renderer hosts re-send from.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { putChannelSettings } from '@/lib/api';
import { readBootCache } from '@/lib/settingsCache';
import {
    RENDERER_STYLES_CHANGED_EVENT, readRendererStyle, rendererFontFamilies, sanitizeRendererStyle,
    sanitizeRendererStyles,
} from '@/lib/rendererStyles';
import { FONT_CATALOGUE } from '@/lib/fonts';
import { useRendererStylesStore } from './useRendererStylesStore';
import { useChannelSettingsStore } from './useChannelSettingsStore';

const putMock = vi.mocked(putChannelSettings);
const CHANNEL = '11111111-1111-4111-8111-111111111111';
const NONE = { theme: null, font: null, textSize: null };

describe('useRendererStylesStore (RS-02)', () => {
    const heard: unknown[] = [];
    const listen = (e: Event) => heard.push((e as CustomEvent).detail);
    beforeEach(() => {
        useRendererStylesStore.getState().adoptRendererStyles({});
        useChannelSettingsStore.setState({ loadedFor: CHANNEL });
        putMock.mockClear();
        localStorage.clear();
        heard.length = 0;
        window.addEventListener(RENDERER_STYLES_CHANGED_EVENT, listen);
    });
    afterEach(() => {
        window.removeEventListener(RENDERER_STYLES_CHANGED_EVENT, listen);
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
    });

    it('adopting a section applies each record and tells the hosts once per changed id, without saving (RS-02)', () => {
        useRendererStylesStore.getState().adoptRendererStyles({
            milkdown: { theme: 'nord', font: 'Lora', textSize: 1.2 },
            html: { theme: 'darkened', font: null, textSize: null },
        });
        expect(readRendererStyle('milkdown')).toEqual({ theme: 'nord', font: 'Lora', textSize: 1.2 });
        expect(readRendererStyle('html')).toEqual({ theme: 'darkened', font: null, textSize: null });
        expect(readRendererStyle('code-editor')).toEqual(NONE);
        expect(heard).toEqual([{ rendererId: 'milkdown' }, { rendererId: 'html' }]);
        // Re-adopting the same section re-sends nothing; a switch to a channel
        // without html's record tells html only.
        heard.length = 0;
        useRendererStylesStore.getState().adoptRendererStyles({
            milkdown: { theme: 'nord', font: 'Lora', textSize: 1.2 },
            html: { theme: 'darkened', font: null, textSize: null },
        });
        expect(heard).toEqual([]);
        useRendererStylesStore.getState().adoptRendererStyles({ milkdown: { theme: 'nord', font: 'Lora', textSize: 1.2 } });
        expect(heard).toEqual([{ rendererId: 'html' }]);
        expect(readRendererStyle('html')).toEqual(NONE);
        expect(putMock).not.toHaveBeenCalled();
    });

    it('a malformed section reads as the server keeps it (RS-02)', () => {
        useRendererStylesStore.getState().adoptRendererStyles({
            'bad id': { theme: 'nord' },
            ok: { theme: ' frame ', font: 'f'.repeat(121), textSize: 9 },
            empty: { theme: '', font: null, textSize: Number.NaN },
        } as never);
        expect(useRendererStylesStore.getState().styles).toEqual({ ok: { theme: 'frame', font: null, textSize: 3 } });
        useRendererStylesStore.getState().adoptRendererStyles(undefined);
        expect(useRendererStylesStore.getState().styles).toEqual({});
    });

    it('commit merges the patch, PUTs the whole section to the adopted channel and rewrites the boot cache (RS-02)', async () => {
        useRendererStylesStore.getState().adoptRendererStyles({ html: { theme: 'authored', font: null, textSize: null } });
        heard.length = 0;
        await useRendererStylesStore.getState().commitStyle('milkdown', { textSize: 1.234 });
        await useRendererStylesStore.getState().commitStyle('milkdown', { font: 'Lora' });
        const section = {
            html: { theme: 'authored', font: null, textSize: null },
            milkdown: { theme: null, font: 'Lora', textSize: 1.23 },
        };
        expect(putMock).toHaveBeenLastCalledWith(CHANNEL, { renderer_styles: section });
        expect(putMock).toHaveBeenCalledTimes(2);
        expect(readBootCache().renderer_styles).toEqual(section);
        expect(readRendererStyle('milkdown')).toEqual(section.milkdown);
        expect(heard).toEqual([{ rendererId: 'milkdown' }, { rendererId: 'milkdown' }]);
    });

    it('a record set back to all null is dropped from the section (RS-02)', async () => {
        await useRendererStylesStore.getState().commitStyle('code-editor', { theme: 'vs-dark' });
        await useRendererStylesStore.getState().commitStyle('code-editor', { theme: null });
        expect(putMock).toHaveBeenLastCalledWith(CHANNEL, { renderer_styles: {} });
        expect(readRendererStyle('code-editor')).toEqual(NONE);
    });

    it('refuses an id the server would drop (RS-02)', async () => {
        await useRendererStylesStore.getState().commitStyle('../other', { theme: 'x' });
        expect(putMock).not.toHaveBeenCalled();
        expect(useRendererStylesStore.getState().styles).toEqual({});
    });

    it('with no channel adopted (a switch still loading) the commit is cache-only (RS-02)', async () => {
        useChannelSettingsStore.setState({ loadedFor: null });
        await useRendererStylesStore.getState().commitStyle('excalidraw', { theme: 'excalidraw' });
        expect(putMock).not.toHaveBeenCalled();
        expect(readBootCache().renderer_styles).toEqual({ excalidraw: { theme: 'excalidraw', font: null, textSize: null } });
        expect(readRendererStyle('excalidraw').theme).toBe('excalidraw');
    });

    it('a failed PUT rejects after the optimistic apply (RS-02)', async () => {
        putMock.mockRejectedValueOnce(new Error('HTTP 503'));
        await expect(useRendererStylesStore.getState().commitStyle('milkdown', { theme: 'frame' })).rejects.toThrow('HTTP 503');
        expect(readRendererStyle('milkdown').theme).toBe('frame');
    });
});

describe('lib/rendererStyles (RS-02)', () => {
    it('sanitizes a record like the server: trims, caps, clamps and rounds (RS-02)', () => {
        expect(sanitizeRendererStyle({ theme: 't'.repeat(64), font: ' Lora ', textSize: 0.1 }))
            .toEqual({ theme: 't'.repeat(64), font: 'Lora', textSize: 0.5 });
        expect(sanitizeRendererStyle({ theme: 't'.repeat(65), font: 3, textSize: Infinity })).toEqual(NONE);
        expect(sanitizeRendererStyle(null)).toEqual(NONE);
        const many = Object.fromEntries(Array.from({ length: 300 }, (_, i) => [`r${i}`, { textSize: 1 }]));
        expect(Object.keys(sanitizeRendererStyles(many))).toHaveLength(256);
    });

    it('projects the whole font catalogue as {family, label, role, stack} (RS-02)', () => {
        const fonts = rendererFontFamilies();
        const count = FONT_CATALOGUE.ui.length + FONT_CATALOGUE.prose.length + FONT_CATALOGUE.mono.length;
        expect(fonts).toHaveLength(count);
        expect(fonts.find((f) => f.family === 'Lora')).toEqual({
            family: 'Lora', label: 'Lora', role: 'prose', stack: '"Lora", Georgia, "Times New Roman", serif',
        });
        expect(fonts.find((f) => f.family === 'NV Jost')).toMatchObject({ label: 'Jost', role: 'ui' });
        for (const f of fonts) expect(Object.keys(f).sort()).toEqual(['family', 'label', 'role', 'stack']);
    });
});

describe('the iframe bridge (RS-02)', () => {
    beforeEach(() => {
        useRendererStylesStore.getState().adoptRendererStyles({});
        useChannelSettingsStore.setState({ loadedFor: CHANNEL });
        putMock.mockClear();
    });
    afterEach(() => useChannelSettingsStore.setState({ doc: null, loadedFor: null }));

    const frame = () => ({ postMessage: vi.fn() }) as unknown as Window & { postMessage: ReturnType<typeof vi.fn> };
    const context = (target: Window, readOnly = false) => ({
        target, isCurrent: () => true, ready: vi.fn(), rendererId: 'milkdown', fileName: 'a.md', cellId: 'c1', readOnly,
    });
    const message = (target: Window, data: unknown) =>
        ({ data, source: target, origin: window.location.origin }) as MessageEvent;

    it('pushes SET_STYLE with this renderer record and the font catalogue (RS-02)', async () => {
        const { pushSandboxStyle } = await import('@/lib/sandboxBridge');
        useRendererStylesStore.getState().adoptRendererStyles({ milkdown: { theme: 'frame', font: 'Lora', textSize: 1.3 } });
        const target = frame();
        pushSandboxStyle(target, 'milkdown');
        pushSandboxStyle(target, 'html');
        expect(target.postMessage.mock.calls).toEqual([
            [{ type: 'SET_STYLE', style: { theme: 'frame', font: 'Lora', textSize: 1.3 }, fonts: rendererFontFamilies() }, window.location.origin],
            [{ type: 'SET_STYLE', style: NONE, fonts: rendererFontFamilies() }, window.location.origin],
        ]);
    });

    it('commits SET_STYLE_PATCH under the frame own renderer id only, read-only views included (RS-02)', async () => {
        const { handleSandboxHostMessage } = await import('@/lib/sandboxBridge');
        const target = frame();
        handleSandboxHostMessage(message(target, {
            type: 'SET_STYLE_PATCH', rendererId: 'html', patch: { theme: 'nord', textSize: 1.4, rendererId: 'html', extra: 1 },
        }), context(target, true));
        await vi.waitFor(() => expect(putMock).toHaveBeenCalledOnce());
        expect(putMock).toHaveBeenCalledWith(CHANNEL, {
            renderer_styles: { milkdown: { theme: 'nord', font: null, textSize: 1.4 } },
        });
        expect(readRendererStyle('html')).toEqual(NONE);
        // A malformed patch, or one from another window, commits nothing.
        handleSandboxHostMessage(message(target, { type: 'SET_STYLE_PATCH', patch: 'nord' }), context(target));
        handleSandboxHostMessage(message(frame(), { type: 'SET_STYLE_PATCH', patch: { theme: 'x' } }), context(target));
        expect(putMock).toHaveBeenCalledOnce();
    });
});
