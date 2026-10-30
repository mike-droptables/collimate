/**
 * The keyboard follows the selected cell.
 *
 * A selection change that comes with no press in the new cell (Ctrl+←/→,
 * Alt+↑/↓, the overview, a run or a reveal selecting a cell) never moves
 * DOM focus by itself. A pooled iframe in particular keeps the keys
 * wherever it is, even parked off-screen, so the next keys edited a cell
 * that was no longer selected (live: typing after Ctrl+→ from one .md cell
 * still landed in that .md).
 *
 * `handFocusToSelectedCell` is the one rule, run by `useSelectionFocus` a
 * frame after the selected cell changes (so a press that selected has
 * already moved focus itself):
 *
 * - Focus outside every cell (the nav column, the Commands pane, a dialog,
 *   a menu), on the body, inside a keyboard owner (a cell panel, a rename)
 *   or already in the selected cell is left alone. Nothing is stolen.
 * - Focus in ANOTHER cell (its card, or a pooled iframe positioned over it
 *   or parked) is taken out of that cell.
 * - When it was in that cell's file editor (a renderer: a pooled iframe or
 *   a native renderer's shadow root), the selected cell's focused tab's
 *   editor takes it (its own last focus, else its first editable), so
 *   typing carries on in the selected cell. Any other focus there (its
 *   title, a tab button) goes to the selected card, which keeps the scene's
 *   shortcuts on it, like the Commands pane's "to cell".
 *
 * `focusEditorAt` is the body click's half: the focus scrim takes the
 * press that selects a cell, so the scrims hand focus to the editable under
 * the press once the renderer takes pointer events.
 */

import { cardOf, deepActiveElement, type ContainerOf } from '@/lib/blurForeignEditor';
import { focusCellCard } from '@/lib/commandPane';
import { BARE_KEYS_OWNER_ATTR, KEYBOARD_OWNER_ATTR } from '@/lib/keyboardOwner';

/** On the slot of a cell's focused tab: the active tab of its active
 *  leaf, the one whose renderer takes the keyboard (ContentPane). */
export const ACTIVE_TAB_BODY_ATTR = 'data-active-tab-body';

/** The world-space overlay the guest pool parks and positions iframes in
 *  (StackScene). */
const SURFACE_OVERLAY_ID = 'collimate-surface-overlay';

const EDITABLE = [
    '[contenteditable]:not([contenteditable="false"])',
    'textarea:not([disabled]):not([readonly])',
    'input:not([disabled]):not([readonly]):not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"])',
].join(',');

/** A renderer element's last focused element, recorded per native
 *  renderer host by `useSelectionFocus` (a shadow root forgets its
 *  focused element once focus leaves it). */
export type LastFocused = (host: Element) => Element | null;

export type FocusHandOff = 'none' | 'editor' | 'card' | 'pending';

/** The card of a cell, when it is mounted on the canvas. */
export function cardOfCell(cellId: string, doc: Document = document): HTMLElement | null {
    return doc.querySelector<HTMLElement>(`[data-cell-id="${CSS.escape(cellId)}"] [data-stack-card]`);
}

/** Focus a keyboard owner (or a bare-keys owner: the manage card) or a
 *  modal holds: never taken from it. */
function heldByOwner(el: Element): boolean {
    let node: Element | null = el;
    while (node) {
        if (node.closest(`[${KEYBOARD_OWNER_ATTR}], [${BARE_KEYS_OWNER_ATTR}], [aria-modal="true"]`)) return true;
        const root = node.getRootNode();
        node = root instanceof ShadowRoot ? root.host : null;
    }
    return false;
}

function isPooledFrame(el: Element, doc: Document): boolean {
    return el.tagName === 'IFRAME' && !!doc.getElementById(SURFACE_OVERLAY_ID)?.contains(el);
}

function isEditable(el: Element): boolean {
    return el.matches(EDITABLE);
}

function visible(el: Element): boolean {
    return el.getClientRects().length > 0;
}

/** The first visible editable in a renderer's document or shadow root. */
function firstEditable(root: Document | ShadowRoot): HTMLElement | null {
    for (const el of Array.from(root.querySelectorAll<HTMLElement>(EDITABLE))) {
        if (visible(el)) return el;
    }
    return null;
}

/** The pooled iframe positioned over `body` (it lives in the overlay, not
 *  in the cell's DOM). */
function pooledFrameOver(body: Element, containerOf: ContainerOf, doc: Document): HTMLIFrameElement | null {
    const overlay = doc.getElementById(SURFACE_OVERLAY_ID);
    if (!overlay) return null;
    for (const frame of Array.from(overlay.querySelectorAll('iframe'))) {
        const container = containerOf(frame);
        if (container && body.contains(container)) return frame;
    }
    return null;
}

function frameDocument(frame: HTMLIFrameElement): Document | null {
    try {
        return frame.contentDocument;
    } catch {
        return null;
    }
}

/** Give a pooled iframe the keys: its own last focus comes back with the
 *  frame; a frame that never held one focuses its first editable. An
 *  opaque frame just holds the keys. False while the frame cannot take
 *  focus. */
function focusFrame(frame: HTMLIFrameElement): boolean {
    frame.focus({ preventScroll: true });
    // A frame the pool still hides (loading, revealing) cannot take focus
    // yet: report it, so the hand-off tries again next frame.
    if (frame.ownerDocument.activeElement !== frame) return false;
    const doc = frameDocument(frame);
    if (!doc) return true;
    const inner = doc.activeElement;
    if (inner && inner !== doc.body && inner !== doc.documentElement) {
        (inner as HTMLElement).focus?.({ preventScroll: true });
        return true;
    }
    firstEditable(doc)?.focus({ preventScroll: true });
    return true;
}

/** Give a native renderer's shadow root the keys: its last focused
 *  element, else its first editable. False when it has nothing to focus. */
function focusShadow(root: ShadowRoot, remembered: Element | null): boolean {
    const target = remembered && remembered.isConnected && root.contains(remembered)
        ? remembered as HTMLElement
        : firstEditable(root);
    if (!target) return false;
    target.focus({ preventScroll: true });
    return root.activeElement !== null;
}

/** The native renderer host in a tab slot (NativeRenderer's shadow host). */
function nativeHostIn(body: Element): HTMLElement | null {
    for (const el of Array.from(body.querySelectorAll<HTMLElement>('[data-native-renderer] > *'))) {
        if (el.shadowRoot) return el;
    }
    return null;
}

/** Put the keyboard in the file editor of `card`'s focused tab. False
 *  when the tab has no renderer to take it (an empty tab, a loading one). */
export function focusCellEditor(card: Element, containerOf: ContainerOf, lastFocused?: LastFocused): boolean {
    const doc = card.ownerDocument;
    const body = card.querySelector(`[${ACTIVE_TAB_BODY_ATTR}]`);
    if (!body) return false;
    const host = nativeHostIn(body);
    if (host?.shadowRoot) return focusShadow(host.shadowRoot, lastFocused?.(host) ?? null);
    const frame = pooledFrameOver(body, containerOf, doc);
    return frame ? focusFrame(frame) : false;
}

/** Put the keyboard in the renderer mounted inside `box` (a native host's
 *  shadow root, else the pooled frame positioned over it): the status
 *  pane's composer editor. False when there is nothing to focus yet. */
export function focusEditorIn(box: Element, containerOf: ContainerOf): boolean {
    const host = nativeHostIn(box);
    if (host?.shadowRoot) return focusShadow(host.shadowRoot, null);
    const frame = pooledFrameOver(box, containerOf, box.ownerDocument);
    return frame ? focusFrame(frame) : false;
}

/** Whether the keyboard holder sits inside `within` (an element, or any
 *  element matching a selector): a pooled frame by the slot it is
 *  positioned over (`containerOf`), any other holder by its own ancestors,
 *  climbing out of shadow roots. A key a pooled frame forwards is
 *  dispatched on the window, so its path never crosses the page; this is
 *  how its owner is found. */
export function keysInside(within: string | Element, containerOf: ContainerOf, doc: Document = document): boolean {
    const inside = (el: Element) => (typeof within === 'string' ? !!el.closest(within) : within.contains(el));
    const holder = keyboardHolder(doc);
    if (!holder || holder === doc.body || holder === doc.documentElement) return false;
    if (isPooledFrame(holder, doc)) {
        const slot = containerOf(holder as HTMLIFrameElement);
        return !!slot && inside(slot);
    }
    let node: Element | null = holder;
    while (node) {
        if (inside(node)) return true;
        const root = node.getRootNode();
        node = root instanceof ShadowRoot ? root.host : null;
    }
    return false;
}

/** A pooled iframe whose document holds the keys while the top
 *  document's active element is only the body: a frame the pool hid
 *  (parked by a stack rotation, every frame in the overview) drops out of
 *  `activeElement`, yet Chromium keeps it the focused frame, so typing
 *  still edits it. Same-origin pool frames only. */
function silentlyFocusedFrame(doc: Document): HTMLIFrameElement | null {
    const overlay = doc.getElementById(SURFACE_OVERLAY_ID);
    if (!overlay) return null;
    for (const frame of Array.from(overlay.querySelectorAll('iframe'))) {
        if (frameDocument(frame)?.hasFocus()) return frame;
    }
    return null;
}

/** What holds the keyboard: the innermost active element, or a pooled
 *  frame that silently kept it (see above). Null or the body when
 *  nothing does. */
export function keyboardHolder(doc: Document = document): Element | null {
    const active = deepActiveElement(doc);
    if (active && active !== doc.body && active !== doc.documentElement) return active;
    return silentlyFocusedFrame(doc) ?? active;
}

/** A file editor holds the keys: a pooled frame, or an element in a
 *  renderer's shadow root. */
function isEditorHolder(el: Element, doc: Document): boolean {
    return isPooledFrame(el, doc) || el.getRootNode() instanceof ShadowRoot;
}

/** Whether a file editor holds the keyboard right now. Read when the
 *  selection changes, before the hand-off's frame: an editor whose
 *  renderer the pool hides by then (a stack rotation parks the old front
 *  cell's frame, the overview hides every frame) drops the keys on its
 *  own, and the hand-off must still know they were in an editor. */
export function editorHoldsKeys(doc: Document = document): boolean {
    const holder = keyboardHolder(doc);
    return !!holder && holder !== doc.body && holder !== doc.documentElement && isEditorHolder(holder, doc);
}

/** Take the keys out of `el` (the holder): blur it, and for a frame let
 *  the top document take them back (a frame keeps them until it does;
 *  its own focused element is blurred too, for a frame that still kept
 *  focus after that). */
function release(el: Element, doc: Document): void {
    (el as HTMLElement).blur?.();
    if (el.tagName !== 'IFRAME') return;
    const inner = frameDocument(el as HTMLIFrameElement)?.activeElement as HTMLElement | null | undefined;
    doc.defaultView?.focus();
    if (inner && frameDocument(el as HTMLIFrameElement)?.hasFocus()) inner.blur?.();
}

/** Nothing holds the keyboard: no element, or only the body. A card that
 *  owns its own focus (the Capabilities manage card, which no selected
 *  cell hands the keys to) takes them only then. */
export function keyboardIsFree(doc: Document = document): boolean {
    const holder = keyboardHolder(doc);
    return !holder || holder === doc.body || holder === doc.documentElement;
}

/** Nothing holds the keyboard: the body, or the card of `cellId` itself.
 *  The only state in which a deferred hand-off may still act. */
export function focusIsFree(cellId: string, doc: Document = document): boolean {
    return keyboardIsFree(doc) || keyboardHolder(doc) === cardOfCell(cellId, doc);
}

/** The rule (see the file header). Returns what it did; `pending` means it
 *  took the keys out of another cell's editor while the selected cell has
 *  no editor to take them yet (no card in the overview, a renderer still
 *  mounting), so the caller finishes the hand-off with `focusCellEditor`
 *  once there is one, or `focusCellCard`. */
export function handFocusToSelectedCell(
    cellId: string,
    containerOf: ContainerOf,
    lastFocused?: LastFocused,
    doc: Document = document,
): FocusHandOff {
    // Another web contents (a live desktop guest, another window) holds
    // the OS focus: never take it.
    if (!doc.hasFocus()) return 'none';
    const active = keyboardHolder(doc);
    if (!active || active === doc.body || active === doc.documentElement) return 'none';
    if (heldByOwner(active)) return 'none';
    const pooled = isPooledFrame(active, doc);
    const owner = cardOf(active, containerOf);
    // Outside every cell (the nav column, a pane, a dialog), or a frame the
    // pool does not own.
    if (!owner && !pooled) return 'none';
    const target = cardOfCell(cellId, doc);
    if (owner && owner === target) return 'none';
    const fromEditor = isEditorHolder(active, doc);
    release(active, doc);
    if (fromEditor) return target && focusCellEditor(target, containerOf, lastFocused) ? 'editor' : 'pending';
    focusCellCard(cellId);
    return 'card';
}

/** No cell is selected (the Capabilities manage card is in front of the
 *  selected stack): take the keys out of a cell, or a pooled frame, that
 *  still holds them, with the same guards as `handFocusToSelectedCell`
 *  (the page has focus; never from an owner, a modal, or outside every
 *  cell). True when it released them. */
export function releaseForeignCellFocus(containerOf: ContainerOf, doc: Document = document): boolean {
    if (!doc.hasFocus()) return false;
    const active = keyboardHolder(doc);
    if (!active || active === doc.body || active === doc.documentElement) return false;
    if (heldByOwner(active)) return false;
    if (!cardOf(active, containerOf) && !isPooledFrame(active, doc)) return false;
    release(active, doc);
    return true;
}

/** Focus the editable under a press at client (x, y) in a renderer root
 *  whose scrim took the press, placing the caret there in a document's
 *  contenteditable. `doc` is the root's document; the point is in its
 *  coordinates. False when nothing editable is there. */
export function focusEditableAt(root: Document | ShadowRoot, doc: Document, x: number, y: number): boolean {
    const hit = root.elementFromPoint(x, y);
    const editable = hit ? hit.closest(EDITABLE) as HTMLElement | null : null;
    if (!editable || !isEditable(editable)) return false;
    editable.focus({ preventScroll: true });
    if (editable.isContentEditable && root === doc) {
        const range = doc.caretRangeFromPoint?.(x, y);
        const selection = doc.getSelection();
        if (range && selection && editable.contains(range.startContainer)) {
            selection.removeAllRanges();
            selection.addRange(range);
        }
    }
    return true;
}

/** The body click on a pooled iframe's scrim: focus the frame and the
 *  editable under the press (client coordinates, mapped through the
 *  camera's scale), else the frame's own last focus or first editable. */
export function focusFrameAt(frame: HTMLIFrameElement, clientX: number, clientY: number): void {
    const doc = frameDocument(frame);
    if (!doc) {
        frame.focus({ preventScroll: true });
        return;
    }
    const r = frame.getBoundingClientRect();
    const sx = frame.offsetWidth ? r.width / frame.offsetWidth : 1;
    const sy = frame.offsetHeight ? r.height / frame.offsetHeight : 1;
    frame.focus({ preventScroll: true });
    if (focusEditableAt(doc, doc, (clientX - r.left) / sx, (clientY - r.top) / sy)) return;
    focusFrame(frame);
}

/** The body click on a native renderer's scrim: the editable under the
 *  press, else the renderer's first editable. */
export function focusShadowAt(root: ShadowRoot, clientX: number, clientY: number): void {
    if (!focusEditableAt(root, root.ownerDocument, clientX, clientY)) focusShadow(root, null);
}

/** A scrim's press selected its cell: two frames on (the selection's
 *  commit, then the paint that lets the renderer take pointer events),
 *  `focus` the editor at the press, unless something took the keyboard
 *  meanwhile (a title, the Commands pane) or the page lost it. */
export function afterScrimPress(x: number, y: number, focus: (x: number, y: number) => void): void {
    requestAnimationFrame(() => requestAnimationFrame(() => {
        if (!document.hasFocus()) return;
        const active = deepActiveElement(document);
        if (active && active !== document.body && active !== document.documentElement && !active.hasAttribute('data-stack-card')) return;
        focus(x, y);
    }));
}
