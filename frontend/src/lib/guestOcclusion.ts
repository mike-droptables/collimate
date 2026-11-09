/**
 * The guest occlusion registry.
 *
 * A native guest view is painted by the OS compositor on top of ALL
 * renderer DOM — no overlay, menu, or dialog can draw above it, whatever
 * its z-index says. The only way anything appears "over" a guest is for
 * the guest's native view to hide and its screenshot placeholder (plain
 * DOM, obeying z-order) to stand in. This module is where overlays say
 * so.
 *
 * An explicit registry rather than a DOM scan (VS Code walks the document
 * for dialog class names) because we own every overlay in this app: each
 * one registers itself while open, and the guest pool freezes the guests
 * its scope covers. The failure mode this discipline guards against is
 * ugly — an unregistered overlay doesn't paint *behind* a guest (the old
 * worst case), it paints *under an opaque native view*, i.e. invisibly —
 * so when adding an overlay above the canvas, wire {@link useGuestOccluder}
 * into it or leave a comment explaining why it can't cover a guest.
 *
 * Scopes:
 *  - `'all'`           — every guest freezes (full-canvas overlays, drags).
 *  - `{ cellIds }`     — only guests of those cells (an in-cell overlay).
 *  - `{ allExcept }`   — every guest but those cells' (a fullscreen
 *                        surface that hosts a guest of its own).
 */

import { useEffect } from 'react';

export type GuestOccluderScope =
    | 'all'
    | { cellIds: readonly string[] }
    | { allExcept: readonly string[] };

const occluders = new Map<number, GuestOccluderScope>();
const subscribers = new Set<() => void>();
let nextId = 1;

function notify(): void {
    for (const subscriber of Array.from(subscribers)) {
        try {
            subscriber();
        } catch (err) {
            console.error('guest occlusion subscriber error:', err);
        }
    }
}

/**
 * Declare "something is painting above guests right now". Returns the
 * deregistration; call it when the overlay closes. Imperative form for
 * non-React callers — components use {@link useGuestOccluder}.
 */
export function registerGuestOccluder(scope: GuestOccluderScope): () => void {
    const id = nextId++;
    occluders.set(id, scope);
    notify();
    return () => {
        if (occluders.delete(id)) notify();
    };
}

/** Whether a guest hosted by `cellId` (null = no cell) is occluded now. */
export function isCellOccluded(cellId: string | null): boolean {
    for (const scope of occluders.values()) {
        if (scope === 'all') return true;
        if ('cellIds' in scope) {
            if (cellId !== null && scope.cellIds.includes(cellId)) return true;
        } else if (cellId === null || !scope.allExcept.includes(cellId)) {
            return true;
        }
    }
    return false;
}

/** Notified on every register/deregister. Returns an unsubscribe. */
export function subscribeGuestOcclusion(listener: () => void): () => void {
    subscribers.add(listener);
    return () => {
        subscribers.delete(listener);
    };
}

/** Test/HMR hygiene: forget everything. */
export function resetGuestOcclusion(): void {
    occluders.clear();
    subscribers.clear();
}

/**
 * Register this component as a guest occluder while `active`.
 *
 * Call unconditionally (hooks can't live behind an early return) and let
 * `active` carry the open/closed state: `useGuestOccluder('all', isOpen)`.
 * The scope object may be rebuilt every render — it is keyed by value.
 */
export function useGuestOccluder(scope: GuestOccluderScope, active: boolean = true): void {
    const key = scopeKey(scope);
    useEffect(() => {
        if (!active) return undefined;
        return registerGuestOccluder(parseScopeKey(key));
        // The serialized key stands in for the (rebuilt-per-render) scope.
    }, [active, key]);
}

function scopeKey(scope: GuestOccluderScope): string {
    if (scope === 'all') return 'all';
    if ('cellIds' in scope) return `cells:${[...scope.cellIds].sort().join(',')}`;
    return `except:${[...scope.allExcept].sort().join(',')}`;
}

function parseScopeKey(key: string): GuestOccluderScope {
    if (key === 'all') return 'all';
    const [kind, list] = key.split(/:(.*)/s) as [string, string];
    const ids = list === '' ? [] : list.split(',');
    return kind === 'cells' ? { cellIds: ids } : { allExcept: ids };
}
