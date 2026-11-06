// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSelectionFocus } from './useSelectionFocus';
import { deepActiveElement } from '@/lib/blurForeignEditor';
import { ACTIVE_TAB_BODY_ATTR } from '@/lib/selectionFocus';

if (!globalThis.CSS?.escape) (globalThis as unknown as { CSS: { escape: (s: string) => string } }).CSS = { escape: (s: string) => s };

/** A cell's card with a native renderer (one shadow textarea) in its
 *  focused tab, or an empty tab. */
function cell(id: string, withEditor = true) {
    const shell = document.createElement('div');
    shell.setAttribute('data-cell-id', id);
    const card = shell.appendChild(document.createElement('div'));
    card.setAttribute('data-stack-card', '');
    const slot = card.appendChild(document.createElement('div'));
    slot.setAttribute(ACTIVE_TAB_BODY_ATTR, '');
    document.body.appendChild(shell);
    if (!withEditor) return { shell, card, editor: null };
    const wrapper = slot.appendChild(document.createElement('div'));
    wrapper.setAttribute('data-native-renderer', '');
    const host = wrapper.appendChild(document.createElement('div'));
    const editor = host.attachShadow({ mode: 'open' }).appendChild(document.createElement('textarea'));
    return { shell, card, editor };
}

describe('useSelectionFocus', () => {
    let frames: FrameRequestCallback[] = [];
    const flushFrames = (n = 3) => act(() => {
        for (let i = 0; i < n; i++) {
            const due = frames;
            frames = [];
            due.forEach(cb => cb(0));
        }
    });
    beforeEach(() => {
        frames = [];
        vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length; });
        vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
        vi.spyOn(document, 'hasFocus').mockReturnValue(true);
        vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    });
    afterEach(() => {
        (deepActiveElement(document) as HTMLElement | null)?.blur?.();
        document.body.innerHTML = '';
        vi.restoreAllMocks();
    });

    it('a frame after the selection moves, the keys leave the previous cell\'s editor for the selected one\'s', () => {
        const a = cell('A'), b = cell('B');
        const { rerender } = renderHook(({ id }) => useSelectionFocus(id, false), { initialProps: { id: 'A' } });
        a.editor!.focus();
        rerender({ id: 'B' });
        // Nothing moves before the frame: a press that selected has its say.
        expect(deepActiveElement(document)).toBe(a.editor);
        flushFrames(1);
        expect(deepActiveElement(document)).toBe(b.editor);
    });

    it('leaves the keys where a press put them by the frame (a title, a pane)', () => {
        const a = cell('A');
        cell('B');
        const { rerender } = renderHook(({ id }) => useSelectionFocus(id, false), { initialProps: { id: 'A' } });
        a.editor!.focus();
        rerender({ id: 'B' });
        const pane = document.body.appendChild(document.createElement('input'));
        pane.focus();
        flushFrames();
        expect(document.activeElement).toBe(pane);
    });

    it('finishes the hand-off when the editor dropped the keys before the frame (a parked frame)', () => {
        const a = cell('A'), b = cell('B');
        const { rerender } = renderHook(({ id }) => useSelectionFocus(id, false), { initialProps: { id: 'A' } });
        a.editor!.focus();
        rerender({ id: 'B' });
        a.editor!.blur();
        flushFrames(1);
        expect(deepActiveElement(document)).toBe(b.editor);
    });

    it('carries the keys through the overview to the cell picked there', () => {
        const a = cell('A'), b = cell('B');
        const { rerender } = renderHook(({ id, zoomed }) => useSelectionFocus(id, zoomed), { initialProps: { id: 'A', zoomed: false } });
        a.editor!.focus();
        rerender({ id: 'A', zoomed: true });
        // The overview mounts no cards (and hides every frame): the editor
        // drops the keys.
        a.editor!.blur();
        a.shell.remove();
        b.shell.remove();
        flushFrames();
        rerender({ id: 'B', zoomed: true });
        flushFrames();
        // Nothing holds the keys (jsdom reports no active element at all).
        expect([null, document.body]).toContain(deepActiveElement(document));
        document.body.append(a.shell, b.shell);
        rerender({ id: 'B', zoomed: false });
        flushFrames(1);
        expect(deepActiveElement(document)).toBe(b.editor);
    });

    it('waits for the selected cell\'s editor, then settles on its card', () => {
        const a = cell('A'), b = cell('B', false);
        const { rerender } = renderHook(({ id }) => useSelectionFocus(id, false), { initialProps: { id: 'A' } });
        a.editor!.focus();
        rerender({ id: 'B' });
        flushFrames(5);
        expect(deepActiveElement(document)).toBe(document.body);
        flushFrames(80);
        expect(document.activeElement).toBe(b.card);
    });

    it('does nothing when the keys were not in a cell', () => {
        cell('A');
        cell('B');
        const nav = document.body.appendChild(document.createElement('input'));
        const { rerender } = renderHook(({ id }) => useSelectionFocus(id, false), { initialProps: { id: 'A' } });
        nav.focus();
        rerender({ id: 'B' });
        flushFrames(80);
        expect(document.activeElement).toBe(nav);
    });
});
