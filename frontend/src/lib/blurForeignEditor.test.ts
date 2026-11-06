// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { blurForeignEditor, deepActiveElement } from './blurForeignEditor';
import '@/test/chromiumFrameFocus';

const card = () => {
    const el = document.createElement('div');
    el.setAttribute('data-stack-card', '');
    document.body.appendChild(el);
    return el;
};
/** A renderer host in `parent` whose shadow root holds a textarea. */
const shadowTextarea = (parent: Element) => {
    const host = document.createElement('div');
    parent.appendChild(host);
    const textarea = document.createElement('textarea');
    host.attachShadow({ mode: 'open' }).appendChild(textarea);
    return textarea;
};
const noPool = () => null;

describe('blurForeignEditor', () => {
    afterEach(() => {
        (document.activeElement as HTMLElement | null)?.blur?.();
        document.body.innerHTML = '';
        vi.restoreAllMocks();
    });

    it('reads the focus through shadow roots', () => {
        const textarea = shadowTextarea(card());
        textarea.focus();
        expect(document.activeElement?.tagName).toBe('DIV');
        expect(deepActiveElement(document)).toBe(textarea);
    });

    it('blurs an editor focused in another card\'s shadow root', () => {
        const other = card();
        const selecting = card();
        const textarea = shadowTextarea(other);
        textarea.focus();
        blurForeignEditor(selecting, noPool);
        expect(deepActiveElement(document)).toBe(document.body);
    });

    it('blurs a light-DOM input in another card (the cell title)', () => {
        const other = card();
        const input = document.createElement('input');
        other.appendChild(input);
        input.focus();
        blurForeignEditor(card(), noPool);
        expect(document.activeElement).toBe(document.body);
    });

    it('leaves an editor in the card being selected focused', () => {
        const selecting = card();
        const textarea = shadowTextarea(selecting);
        textarea.focus();
        blurForeignEditor(selecting, noPool);
        expect(deepActiveElement(document)).toBe(textarea);
    });

    it('leaves focus outside every card alone', () => {
        const input = document.createElement('input');
        document.body.appendChild(input);
        input.focus();
        blurForeignEditor(card(), noPool);
        expect(document.activeElement).toBe(input);
    });

    it('maps a pooled iframe back to its cell, blurs it and gives the keys to the top document', () => {
        const other = card();
        const container = document.createElement('div');
        other.appendChild(container);
        const overlay = document.createElement('div');
        document.body.appendChild(overlay);
        const frame = document.createElement('iframe');
        overlay.appendChild(frame);
        frame.focus();
        expect(document.activeElement).toBe(frame);
        vi.spyOn(document, 'hasFocus').mockReturnValue(true);
        const focusTop = vi.spyOn(window, 'focus').mockImplementation(() => {});
        blurForeignEditor(card(), el => (el === frame ? container : null));
        expect(document.activeElement).toBe(document.body);
        expect(focusTop).toHaveBeenCalledTimes(1);
    });

    it('leaves the selected cell\'s own pooled iframe focused', () => {
        const selecting = card();
        const container = document.createElement('div');
        selecting.appendChild(container);
        const frame = document.createElement('iframe');
        document.body.appendChild(frame);
        frame.focus();
        const focusTop = vi.spyOn(window, 'focus').mockImplementation(() => {});
        blurForeignEditor(selecting, el => (el === frame ? container : null));
        expect(document.activeElement).toBe(frame);
        expect(focusTop).not.toHaveBeenCalled();
    });

    it('does not pull the window\'s focus while the page lacks it (a desktop guest may hold it)', () => {
        const other = card();
        const frame = document.createElement('iframe');
        other.appendChild(frame);
        frame.focus();
        vi.spyOn(document, 'hasFocus').mockReturnValue(false);
        const focusTop = vi.spyOn(window, 'focus').mockImplementation(() => {});
        blurForeignEditor(card(), noPool);
        expect(document.activeElement).toBe(document.body);
        expect(focusTop).not.toHaveBeenCalled();
    });
});
