/**
 * RendererConcealContext — "hide your pooled renderer" signal.
 *
 * Renderer iframes live in the world-space overlay, outside the cell's DOM
 * tree, so cell-level opacity/scale animations (the remove-cell/remove-stack
 * burn, a drag-hidden front card) never affect them: the renderer would stay
 * painted at full opacity over a cell that is visually gone — or over a
 * stack whose entry is still held invisible (StackScene, lib/stackEntry).
 * Providers (StackScene, StackGroup, StackItem, CellEditor — for its pick
 * list — and ContentPane) flip this true for the duration of those states,
 * each OR-ing in its parent's value, so a conceal anywhere above a renderer
 * reaches it;
 * SandboxIframe consumes it and hides its pooled iframe without releasing it,
 * so a cancelled state (drag drop back) restores instantly; NativeRenderer
 * consumes it for its shadow-DOM content and its native guests.
 */

import { createContext, useContext } from 'react';

export const RendererConcealContext = createContext(false);

export function useRendererConcealed(): boolean {
    return useContext(RendererConcealContext);
}

/**
 * PooledIframeConcealContext — the narrower "hide your pooled IFRAME"
 * signal. A tab drag inside a cell (ContentPane) needs the cell's pooled
 * iframes out of the way so the in-cell drop highlight is visible, but it
 * must NOT conceal native guests: a concealed guest hides both its view
 * and its screenshot placeholder without capturing, so the pane goes blank
 * for the whole drag. Guests are handled by the occlusion registry instead
 * (lib/guestOcclusion): frozen with a fresh capture, placeholder dimmed
 * and click-through, so the page stays visible under the highlight and the
 * leaf beneath receives the drag. Only SandboxIframe consumes this; every
 * consumer ORs it with the full conceal above.
 */
export const PooledIframeConcealContext = createContext(false);

export function usePooledIframeConcealed(): boolean {
    return useContext(PooledIframeConcealContext);
}
