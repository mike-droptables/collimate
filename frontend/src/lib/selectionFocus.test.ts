// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { deepActiveElement } from './blurForeignEditor';
import {
    ACTIVE_TAB_BODY_ATTR, editorHoldsKeys, focusCellEditor, focusEditorIn, focusIsFree, focusShadowAt, handFocusToSelectedCell, keyboardHolder,
    keyboardIsFree, keysInside, releaseForeignCellFocus,
} from './selectionFocus';
import { BARE_KEYS_OWNER_ATTR } from './keyboardOwner';
import '@/test/chromiumFrameFocus';

// jsdom has no CSS.escape; the ids here need no escaping.
if (!globalThis.CSS?.escape) (globalThis as unknown as { CSS: { escape: (s: string) => string } }).CSS = { escape: (s: string) => s };

// A cell as the canvas renders it: the StackItem shell carries the cell id,
// the card inside it, the focused tab's slot inside the card.
function cell(id: string) {
    const shell = document.createElement('div');
    shell.setAttribute('data-cell-id', id);
    const card = document.createElement('div');
    card.setAttribute('data-stack-card', '');
    const title = document.createElement('input');
    card.appendChild(title);
    const slot = document.createElement('div');
    slot.setAttribute(ACTIVE_TAB_BODY_ATTR, '');
    card.appendChild(slot);
    shell.appendChild(card);
    document.body.appendChild(shell);
    return { shell, card, title, slot };
}

/** A native renderer in the slot: NativeRenderer's wrapper and shadow host. */
function nativeEditor(slot: Element, fields = 1) {
    const wrapper = document.createElement('div');
    wrapper.setAttribute('data-native-renderer', '');
    const host = document.createElement('div');
    wrapper.appendChild(host);
    slot.appendChild(wrapper);
    const root = host.attachShadow({ mode: 'open' });
    const inputs = Array.from({ length: fields }, () => root.appendChild(document.createElement('textarea')));
    return { host, root, inputs };
}

/** The guest pool's world-space overlay and a pooled frame in it holding a
 *  textarea, mapped to `container` (null: parked). */
function overlay() {
    const el = document.createElement('div');
    el.id = 'collimate-surface-overlay';
    document.body.appendChild(el);
    return el;
}
function pooledFrame(over: Element) {
    const frame = document.createElement('iframe');
    over.appendChild(frame);
    // The frame's own realm: its elements count as visible too.
    const realm = frame.contentWindow as unknown as { Element: typeof Element };
    vi.spyOn(realm.Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    const textarea = frame.contentDocument!.createElement('textarea');
    frame.contentDocument!.body.appendChild(textarea);
    return { frame, textarea };
}

describe('handFocusToSelectedCell', () => {
    beforeEach(() => {
        vi.spyOn(document, 'hasFocus').mockReturnValue(true);
        // jsdom lays nothing out: every element counts as visible.
        vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    });
    afterEach(() => {
        (deepActiveElement(document) as HTMLElement | null)?.blur?.();
        (document.activeElement as HTMLElement | null)?.blur?.();
        document.body.innerHTML = '';
        vi.restoreAllMocks();
    });
    const noPool = () => null;

    it('moves the keys from another cell\'s native editor to the selected cell\'s editor', () => {
        const a = cell('A'), b = cell('B');
        const editorA = nativeEditor(a.slot), editorB = nativeEditor(b.slot);
        editorA.inputs[0].focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('editor');
        expect(deepActiveElement(document)).toBe(editorB.inputs[0]);
    });

    it('moves the keys out of another cell\'s pooled frame into the selected cell\'s frame', () => {
        const a = cell('A'), b = cell('B');
        const over = overlay();
        const frameA = pooledFrame(over), frameB = pooledFrame(over);
        const containerA = a.slot.appendChild(document.createElement('div'));
        const containerB = b.slot.appendChild(document.createElement('div'));
        const containerOf = (el: Element) => (el === frameA.frame ? containerA : el === frameB.frame ? containerB : null);
        const focusTop = vi.spyOn(window, 'focus').mockImplementation(() => {});
        frameA.frame.focus();
        expect(handFocusToSelectedCell('B', containerOf)).toBe('editor');
        expect(focusTop).toHaveBeenCalled();
        expect(document.activeElement).toBe(frameB.frame);
        // A frame that never held focus gives it to its first editable.
        expect(frameB.frame.contentDocument!.activeElement).toBe(frameB.textarea);
    });

    it('treats a parked pooled frame (the old front cell of a rotated stack) as another cell', () => {
        const b = cell('B');
        const editorB = nativeEditor(b.slot);
        const parked = pooledFrame(overlay());
        vi.spyOn(window, 'focus').mockImplementation(() => {});
        parked.frame.focus();
        expect(handFocusToSelectedCell('B', () => null)).toBe('editor');
        expect(deepActiveElement(document)).toBe(editorB.inputs[0]);
    });

    it('finds a hidden pooled frame that kept the keys while the body is the active element', () => {
        const b = cell('B');
        const editorB = nativeEditor(b.slot);
        const parked = pooledFrame(overlay());
        vi.spyOn(parked.frame.contentDocument!, 'hasFocus').mockReturnValue(true);
        expect(document.activeElement).toBe(document.body);
        expect(keyboardHolder(document)).toBe(parked.frame);
        expect(editorHoldsKeys(document)).toBe(true);
        vi.spyOn(window, 'focus').mockImplementation(() => {});
        expect(handFocusToSelectedCell('B', () => null)).toBe('editor');
        expect(deepActiveElement(document)).toBe(editorB.inputs[0]);
    });

    it('puts the keys on the selected card when they were in another cell\'s chrome (its title)', () => {
        const a = cell('A'), b = cell('B');
        nativeEditor(b.slot);
        a.title.focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('card');
        expect(document.activeElement).toBe(b.card);
    });

    it('restores the selected renderer\'s last focused field', () => {
        const a = cell('A'), b = cell('B');
        const editorA = nativeEditor(a.slot), editorB = nativeEditor(b.slot, 2);
        editorA.inputs[0].focus();
        const remembered = (host: Element) => (host === editorB.host ? editorB.inputs[1] : null);
        expect(handFocusToSelectedCell('B', noPool, remembered)).toBe('editor');
        expect(deepActiveElement(document)).toBe(editorB.inputs[1]);
    });

    it('owes the hand-off when the selected cell has no editor yet (an empty tab, the overview)', () => {
        const a = cell('A');
        cell('B');
        const editorA = nativeEditor(a.slot);
        editorA.inputs[0].focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('pending');
        expect(deepActiveElement(document)).toBe(document.body);
        expect(focusIsFree('B')).toBe(true);
        editorA.inputs[0].focus();
        expect(handFocusToSelectedCell('Z', noPool)).toBe('pending');
    });

    it('leaves focus outside every cell, in a keyboard owner, in the selected cell or in another window', () => {
        const a = cell('A'), b = cell('B');
        const editorB = nativeEditor(b.slot);
        const nav = document.body.appendChild(document.createElement('input'));
        nav.focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('none');
        expect(document.activeElement).toBe(nav);

        const panel = a.card.appendChild(document.createElement('div'));
        panel.setAttribute('data-owns-keyboard', '');
        const search = panel.appendChild(document.createElement('input'));
        search.focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('none');
        expect(document.activeElement).toBe(search);

        editorB.inputs[0].focus();
        expect(handFocusToSelectedCell('B', noPool)).toBe('none');
        expect(deepActiveElement(document)).toBe(editorB.inputs[0]);

        a.title.focus();
        vi.mocked(document.hasFocus).mockReturnValue(false);
        expect(handFocusToSelectedCell('B', noPool)).toBe('none');
        expect(document.activeElement).toBe(a.title);
    });

    it('focusCellEditor reports a tab with nothing to focus', () => {
        const b = cell('B');
        expect(focusCellEditor(b.card, noPool)).toBe(false);
        const readOnly = nativeEditor(b.slot);
        readOnly.inputs[0].readOnly = true;
        expect(focusCellEditor(b.card, noPool)).toBe(false);
    });

    it('focusShadowAt focuses the editable under the press, else the first one', () => {
        const b = cell('B');
        const editor = nativeEditor(b.slot, 2);
        (editor.root as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint = () => editor.inputs[1];
        focusShadowAt(editor.root, 10, 10);
        expect(editor.root.activeElement).toBe(editor.inputs[1]);
        editor.inputs[1].blur();
        (editor.root as unknown as { elementFromPoint: (x: number, y: number) => Element | null }).elementFromPoint = () => null;
        focusShadowAt(editor.root, 10, 10);
        expect(editor.root.activeElement).toBe(editor.inputs[0]);
    });

    it('keyboardIsFree holds only while nothing but the body has the keys', () => {
        const b = cell('B');
        expect(keyboardIsFree(document)).toBe(true);
        b.title.focus();
        expect(keyboardIsFree(document)).toBe(false);
        // The card itself frees the hand-off for its own cell, never the keyboard.
        b.title.blur();
        b.card.tabIndex = -1;
        b.card.focus();
        expect(keyboardIsFree(document)).toBe(false);
        expect(focusIsFree('B')).toBe(true);
        b.card.blur();
        expect(keyboardIsFree(document)).toBe(true);
    });

    it('releaseForeignCellFocus takes the keys out of a cell or a pooled frame, never from the manage card, an owner, outside or another window', () => {
        const a = cell('A');
        const editorA = nativeEditor(a.slot);
        editorA.inputs[0].focus();
        expect(releaseForeignCellFocus(noPool)).toBe(true);
        expect(keyboardIsFree(document)).toBe(true);

        const over = overlay();
        const { frame } = pooledFrame(over);
        frame.focus();
        expect(releaseForeignCellFocus(noPool)).toBe(true);
        expect(document.activeElement).not.toBe(frame);

        // The manage card owns its bare keys and is a stack card: its search keeps them.
        const manage = document.body.appendChild(document.createElement('div'));
        manage.setAttribute('data-stack-card', '');
        manage.setAttribute(BARE_KEYS_OWNER_ATTR, '');
        const search = manage.appendChild(document.createElement('input'));
        search.focus();
        expect(releaseForeignCellFocus(noPool)).toBe(false);
        expect(document.activeElement).toBe(search);

        const panel = a.card.appendChild(document.createElement('div'));
        panel.setAttribute('data-owns-keyboard', '');
        const field = panel.appendChild(document.createElement('input'));
        field.focus();
        expect(releaseForeignCellFocus(noPool)).toBe(false);
        expect(document.activeElement).toBe(field);

        const nav = document.body.appendChild(document.createElement('input'));
        nav.focus();
        expect(releaseForeignCellFocus(noPool)).toBe(false);
        expect(document.activeElement).toBe(nav);

        a.title.focus();
        vi.mocked(document.hasFocus).mockReturnValue(false);
        expect(releaseForeignCellFocus(noPool)).toBe(false);
        expect(document.activeElement).toBe(a.title);
    });
});

describe('the status pane composer: focusEditorIn and keysInside', () => {
    beforeEach(() => {
        vi.spyOn(document, 'hasFocus').mockReturnValue(true);
        vi.spyOn(Element.prototype, 'getClientRects').mockReturnValue([{}] as unknown as DOMRectList);
    });
    afterEach(() => {
        (deepActiveElement(document) as HTMLElement | null)?.blur?.();
        (document.activeElement as HTMLElement | null)?.blur?.();
        document.body.innerHTML = '';
        vi.restoreAllMocks();
    });

    it('focusEditorIn focuses a native host\'s first editable in the box', () => {
        const box = document.body.appendChild(document.createElement('div'));
        const editor = nativeEditor(box);
        expect(focusEditorIn(box, () => null)).toBe(true);
        expect(deepActiveElement(document)).toBe(editor.inputs[0]);
    });

    it('focusEditorIn focuses the pooled frame positioned over the box, and answers false with nothing there', () => {
        const box = document.body.appendChild(document.createElement('div'));
        const slot = box.appendChild(document.createElement('div'));
        const { frame, textarea } = pooledFrame(overlay());
        expect(focusEditorIn(box, () => null)).toBe(false);
        expect(focusEditorIn(box, el => (el === frame ? slot : null))).toBe(true);
        expect(document.activeElement).toBe(frame);
        expect(frame.contentDocument!.activeElement).toBe(textarea);
    });

    it('keysInside follows a pooled frame holder to its slot, a shadow holder out of its root, and says no outside', () => {
        const pane = document.body.appendChild(document.createElement('aside'));
        pane.setAttribute('data-runs-pane', '');
        const slot = pane.appendChild(document.createElement('div'));
        const { frame } = pooledFrame(overlay());
        frame.focus();
        expect(keysInside('[data-runs-pane]', el => (el === frame ? slot : null))).toBe(true);
        expect(keysInside('[data-runs-pane]', () => document.body)).toBe(false);

        const editor = nativeEditor(pane);
        editor.inputs[0].focus();
        expect(keysInside('[data-runs-pane]', () => null)).toBe(true);

        const outside = document.body.appendChild(document.createElement('input'));
        outside.focus();
        expect(keysInside('[data-runs-pane]', () => null)).toBe(false);
        outside.blur();
        expect(keysInside('[data-runs-pane]', () => null)).toBe(false);
    });

    it('keysInside scoped to one element answers for that element only, its frame slot or its shadow holder', () => {
        const mine = document.body.appendChild(document.createElement('div'));
        const theirs = document.body.appendChild(document.createElement('div'));
        const slot = mine.appendChild(document.createElement('div'));
        const { frame } = pooledFrame(overlay());
        frame.focus();
        expect(keysInside(mine, el => (el === frame ? slot : null))).toBe(true);
        expect(keysInside(theirs, el => (el === frame ? slot : null))).toBe(false);
        expect(keysInside(mine, () => null)).toBe(false);

        const editor = nativeEditor(theirs);
        editor.inputs[0].focus();
        expect(keysInside(theirs, () => null)).toBe(true);
        expect(keysInside(mine, () => null)).toBe(false);
    });
});
