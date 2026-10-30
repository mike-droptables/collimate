/**
 * A wheel-select (StackItem's card `onWheel`) moves the selection with no
 * pointerdown, so the browser never moves DOM focus. Without this the caret
 * stayed in the previously focused cell's editor (a renderer's shadow
 * textarea, a pooled milkdown iframe, the cell title's input), and the next
 * keys edited a cell that was no longer selected.
 *
 * Before the card selects, `blurForeignEditor` blurs whatever holds focus in
 * ANOTHER card. It never focuses anything in the card being selected: the
 * wheel only hands the keyboard to the scene (focusArea `main`), as a click
 * on the cell's header would, and a live desktop guest keeps its own focus
 * rule. Focus outside every card (the nav column, a pane) is left alone.
 */

const CARD = '[data-stack-card]';

/** Maps a pooled element back to the container it covers (the guest pool's
 *  `containerOf`). */
export type ContainerOf = (element: Element) => HTMLElement | null;

/** The focused element, followed into open shadow roots to the innermost
 *  one. A focused iframe is itself the answer: the top document sees only
 *  the frame. Blurring a shadow host is not enough by the spec (only
 *  Chromium honours it), so the innermost element is the one to blur. */
export function deepActiveElement(doc: Document): Element | null {
    let active = doc.activeElement;
    while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
    return active;
}

/** The card `el` sits in, climbing out through its shadow hosts. */
function enclosingCard(el: Element): Element | null {
    let node: Element | null = el;
    while (node) {
        const card = node.closest(CARD);
        if (card) return card;
        const root = node.getRootNode();
        node = root instanceof ShadowRoot ? root.host : null;
    }
    return null;
}

/** The card `el` belongs to. A pooled iframe lives in the world-space
 *  overlay, outside every card, so it is mapped back through the container
 *  it covers. */
export function cardOf(el: Element, containerOf: ContainerOf): Element | null {
    const card = enclosingCard(el);
    if (card || el.tagName !== 'IFRAME') return card;
    const container = containerOf(el);
    return container ? enclosingCard(container) : null;
}

/** Blur the focused element when it sits in a card other than `card`. */
export function blurForeignEditor(card: Element, containerOf: ContainerOf): void {
    const doc = card.ownerDocument;
    const active = deepActiveElement(doc);
    if (!active) return;
    const owner = cardOf(active, containerOf);
    if (!owner || owner === card) return;
    const pageHasFocus = doc.hasFocus();
    (active as HTMLElement).blur?.();
    // A frame keeps the keys until the top document takes them back
    // (Chromium's iframe.blur() already does that, and window.focus() is the
    // spec's way). Only while the page holds focus: otherwise a live desktop
    // guest (another web contents) may hold it, and that must not be taken.
    if (active.tagName === 'IFRAME' && pageHasFocus) doc.defaultView?.focus();
}
