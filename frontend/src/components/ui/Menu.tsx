// The one popup menu (the mock's `.menu`): the channel menu, the stack menu
// and the fan head's menu, the runs menu, the width popup. Rendered through a
// portal on document.body: an anchor inside the canvas's transformed pan/zoom
// layer (a stack head) would otherwise neither land on the viewport nor
// escape its strip's clip.
//
// role=menu: a list of menuitem / menuitemradio rows with roving focus
// (↑↓ wrap, Home/End, Enter/Space select); the first live row takes focus on
// open. role=dialog: the same surface for a body of its own (`children`, the
// runs menu's groups and footer), which handles its own keys; Tab moves
// through it and closes past either end.
//
// It closes on Esc, Tab and activation — each hands focus back to the anchor
// element (Tab then moves on from there, so focus never drops out of the
// portal onto <body>) — and on an outside pointerdown, window blur (focus
// moving into an iframe renderer), and as soon as the anchor moves on screen
// (a camera pan or zoom, a reflow, the anchor unmounting), since a menu
// floating away from what it belongs to would act on the wrong thing.
//
// Overlay plumbing (C20): native guest views paint above the DOM in Electron,
// so an open menu freezes them (useGuestOccluder); it owns the keyboard
// (KEYBOARD_OWNER_PROPS) so no scene shortcut or Esc rung fires under it.

import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { LucideIcon } from 'lucide-react';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { useShortcutDisplay } from '@/lib/keybindings/useShortcutDisplay';
import type { ShortcutId } from '@/lib/keybindings/types';
import { KEYBOARD_OWNER_PROPS } from '@/lib/keyboardOwner';
import { menuHead, menuItem, menuItemDanger, menuSep, menuSurface } from '@/components/ui/menuStyles';
import { cn } from '@/lib/utils';

/** A row that acts: a plain item, or one choice of a radio set. */
export interface MenuAction {
    kind: 'item' | 'radio';
    id: string;
    label: string;
    icon?: LucideIcon;
    /** Live shortcut hint at the row's right edge (follows the user's bindings). */
    shortcut?: ShortcutId;
    /** Radio rows: the current choice (ink text, brass icon). */
    checked?: boolean;
    /** A short tag at the right edge in brass (the channel menu's "active"). */
    tag?: string;
    /** Inert: still focusable, so its reason (the title) is reachable from
     *  the keyboard too. */
    disabled?: boolean;
    /** Why the row is inert; its title while disabled. */
    reason?: string;
    /** What the live row does; its title. */
    hint?: string;
    /** Destructive: red on hover and focus. */
    danger?: boolean;
    onSelect: () => void;
}

export type MenuEntry =
    | MenuAction
    | { kind: 'separator'; id: string }
    | { kind: 'head'; id: string; label: string };

/** Why the menu closed. Esc, Tab and select have already put focus back on
 *  an element anchor; the others leave focus where the user put it. */
export type MenuCloseReason = 'escape' | 'tab' | 'select' | 'outside' | 'blur' | 'anchor';

/** What the menu hangs from: an element (a ref to it, or the element), or a
 *  rect getter for an anchor that is not one element (null closes). */
export type MenuAnchor =
    | HTMLElement
    | React.RefObject<HTMLElement | null>
    | (() => DOMRect | null);

export interface MenuProps {
    anchor: MenuAnchor;
    open: boolean;
    onClose: (reason: MenuCloseReason) => void;
    role?: 'menu' | 'dialog';
    /** The accessible name. */
    label: string;
    id?: string;
    items?: MenuEntry[];
    /** A body of its own after the items (role=dialog surfaces). */
    children?: React.ReactNode;
    /** Which anchor edge the menu lines up with: its left (`start`) or right (`end`). */
    align?: 'start' | 'end';
    /** px; the mock's `.menu` is 248 (the stack menu 236). */
    minWidth?: number;
    maxWidth?: number;
    /** px between the anchor and the menu. */
    gap?: number;
}

const PAD = 8;
const isAction = (e: MenuEntry): e is MenuAction => e.kind === 'item' || e.kind === 'radio';

function anchorElement(anchor: MenuAnchor): HTMLElement | null {
    if (typeof anchor === 'function') return null;
    if (anchor instanceof HTMLElement) return anchor;
    return anchor.current;
}

function anchorRect(anchor: MenuAnchor): DOMRect | null {
    if (typeof anchor === 'function') return anchor();
    const el = anchorElement(anchor);
    return el && el.isConnected ? el.getBoundingClientRect() : null;
}

function ShortcutHint({ id }: { id: ShortcutId }) {
    const display = useShortcutDisplay(id);
    if (!display) return null;
    return <span className="ml-auto pl-[1.125rem] font-mono text-[0.6875rem] text-ink-3">{display}</span>;
}

const focusables = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
));

export function Menu(props: MenuProps) {
    return props.open ? <MenuSurface {...props} /> : null;
}

function MenuSurface({
    anchor, onClose, role = 'menu', label, id, items = [], children,
    align = 'end', minWidth = 248, maxWidth, gap = 5,
}: MenuProps) {
    const menuRef = useRef<HTMLDivElement>(null);
    const rowRefs = useRef<(HTMLButtonElement | null)[]>([]);
    // Mounted means open; a native guest view would paint over the menu.
    useGuestOccluder('all');

    // The latest anchor and onClose, read by the mount-only effects below:
    // an inline getter or callback must not re-arm them (and reset the
    // anchor-moved baseline) on every render.
    const anchorRef = useRef(anchor);
    const onCloseRef = useRef(onClose);
    useLayoutEffect(() => { anchorRef.current = anchor; onCloseRef.current = onClose; });
    const close = useCallback((reason: MenuCloseReason) => {
        if (reason === 'escape' || reason === 'tab' || reason === 'select') {
            anchorElement(anchorRef.current)?.focus({ preventScroll: true });
        }
        onCloseRef.current(reason);
    }, []);

    // Under the anchor on the chosen edge, flipped above when it would spill
    // past the viewport bottom and the room above is larger, clamped
    // horizontally, and capped to the room on the chosen side (a long list
    // scrolls; the arrows' row.focus() scrolls the focused row into view).
    // Parked off-screen until measured (not visibility:hidden: that element
    // could not take the initial focus). Re-placed when the body changes size
    // (the runs menu).
    const [pos, setPos] = useState<{ left: number; top: number; above: boolean; maxHeight: number } | null>(null);
    useLayoutEffect(() => {
        const el = menuRef.current;
        if (!el) return;
        const place = () => {
            const a = anchorRect(anchorRef.current);
            if (!a) return;
            // Layout size, not the rect: the entry zoom scales the menu
            // while it is measured. scrollHeight is the whole list, so the
            // cap never feeds back into the flip.
            const width = el.offsetWidth;
            const height = el.scrollHeight;
            const want = align === 'end' ? a.right - width : a.left;
            const left = Math.max(PAD, Math.min(want, window.innerWidth - width - PAD));
            const roomBelow = window.innerHeight - a.bottom - gap - PAD;
            const roomAbove = a.top - gap - PAD;
            const above = height > roomBelow && roomAbove > roomBelow;
            const maxHeight = Math.max(0, above ? roomAbove : roomBelow);
            const top = above ? Math.max(PAD, a.top - gap - Math.min(height, maxHeight)) : a.bottom + gap;
            setPos(p => (p && p.left === left && p.top === top && p.above === above && p.maxHeight === maxHeight)
                ? p : { left, top, above, maxHeight });
        };
        place();
        if (typeof ResizeObserver === 'undefined') return;
        const ro = new ResizeObserver(place);
        ro.observe(el);
        return () => ro.disconnect();
    }, [align, gap]);

    // Close once the anchor moves on screen or leaves the DOM. One rect read
    // per frame, only while the menu is open. Only the edge the menu lines up
    // with counts (its right for `end`, its left for `start`) plus the top:
    // an anchor that grows away from that edge (a right-column label whose
    // count changes width) leaves the menu where it belongs.
    useEffect(() => {
        const start = anchorRect(anchorRef.current);
        if (!start) { close('anchor'); return; }
        const edge = (r: DOMRect) => (align === 'end' ? r.right : r.left);
        let raf = 0;
        const tick = () => {
            const r = anchorRect(anchorRef.current);
            if (!r || Math.abs(edge(r) - edge(start)) > 0.5 || Math.abs(r.top - start.top) > 0.5) {
                close('anchor');
                return;
            }
            raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
        return () => cancelAnimationFrame(raf);
    }, [close, align]);

    useEffect(() => {
        const onPointerDown = (e: PointerEvent) => {
            const t = e.target;
            if (!(t instanceof Node)) return;
            // The anchor's own press toggles the menu through its click.
            if (menuRef.current?.contains(t) || anchorElement(anchorRef.current)?.contains(t)) return;
            close('outside');
        };
        // Iframe renderers swallow the document pointerdown; window blur
        // fires as focus moves into the iframe.
        const onBlur = () => close('blur');
        document.addEventListener('pointerdown', onPointerDown, true);
        window.addEventListener('blur', onBlur);
        return () => {
            document.removeEventListener('pointerdown', onPointerDown, true);
            window.removeEventListener('blur', onBlur);
        };
    }, [close]);

    // Focus on open: the first live row (the first row when all are inert),
    // else the body's first control, else the surface. Mount only: a later
    // items change must not pull focus from where the arrows moved it.
    const itemsAtOpen = useRef(items);
    useEffect(() => {
        const rows = rowRefs.current;
        const live = itemsAtOpen.current.findIndex(e => isAction(e) && !e.disabled);
        const first = live !== -1 ? rows[live] : rows.find(Boolean);
        const el = menuRef.current;
        (first ?? (el && focusables(el)[0]) ?? el)?.focus({ preventScroll: true });
    }, []);

    const activate = (item: MenuAction) => {
        if (item.disabled) return;
        close('select');
        item.onSelect();
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        if (e.defaultPrevented) return;
        const rows = items.map((it, i) => (isAction(it) ? i : -1)).filter(i => i !== -1);
        const at = rows.findIndex(i => rowRefs.current[i] === document.activeElement);
        const inBody = at === -1 && role === 'dialog';
        const focusRow = (k: number) => {
            if (rows.length === 0) return;
            rowRefs.current[rows[((k % rows.length) + rows.length) % rows.length]]?.focus();
        };
        switch (e.key) {
            case 'Escape': close('escape'); break;
            case 'Tab': {
                if (role === 'menu') { close('tab'); return; }
                // A dialog keeps Tab inside until it would leave either end.
                const list = menuRef.current ? focusables(menuRef.current) : [];
                const k = list.indexOf(document.activeElement as HTMLElement);
                const leaving = list.length === 0 || (e.shiftKey ? k <= 0 : k === list.length - 1);
                if (leaving) close('tab');
                return;
            }
            // In a dialog the arrows belong to its body unless a row has focus.
            case 'ArrowDown': if (inBody) return; focusRow(at + 1); break;
            case 'ArrowUp': if (inBody) return; focusRow(at < 0 ? rows.length - 1 : at - 1); break;
            case 'Home': if (inBody) return; focusRow(0); break;
            case 'End': if (inBody) return; focusRow(rows.length - 1); break;
            case 'Enter':
            case ' ': {
                if (at === -1) return;
                const it = items[rows[at]];
                if (isAction(it)) activate(it);
                break;
            }
            default: return;
        }
        e.preventDefault();
        e.stopPropagation();
    };

    return createPortal(
        <div
            ref={menuRef}
            id={id}
            role={role}
            aria-label={label}
            aria-orientation={role === 'menu' ? 'vertical' : undefined}
            tabIndex={-1}
            {...KEYBOARD_OWNER_PROPS}
            onKeyDown={onKeyDown}
            // A portal leaves the DOM but not the React tree: a press inside
            // the menu must not bubble to its owner's ancestors (a stack
            // card's pointerdown selects the stack).
            onPointerDown={(e) => e.stopPropagation()}
            // The entry zoom grows from the corner nearest the anchor.
            className={cn(
                'fixed z-[460] outline-none overflow-y-auto',
                pos?.above
                    ? (align === 'end' ? 'origin-bottom-right' : 'origin-bottom-left')
                    : (align === 'end' ? 'origin-top-right' : 'origin-top-left'),
                menuSurface,
            )}
            // no-drag keeps the popup interactive inside the desktop title
            // bar's drag band.
            style={{
                WebkitAppRegion: 'no-drag',
                left: pos?.left ?? -10000,
                top: pos?.top ?? -10000,
                minWidth,
                maxWidth,
                maxHeight: pos?.maxHeight,
            } as React.CSSProperties}
        >
            {items.map((entry, i) => {
                if (entry.kind === 'separator') {
                    return <div key={entry.id} role="separator" className={menuSep} />;
                }
                if (entry.kind === 'head') {
                    return <div key={entry.id} role="presentation" className={menuHead}>{entry.label}</div>;
                }
                const Icon = entry.icon;
                const inert = !!entry.disabled;
                return (
                    <button
                        key={entry.id}
                        ref={el => { rowRefs.current[i] = el; }}
                        type="button"
                        role={entry.kind === 'radio' ? 'menuitemradio' : 'menuitem'}
                        aria-checked={entry.kind === 'radio' ? !!entry.checked : undefined}
                        tabIndex={-1}
                        aria-disabled={inert || undefined}
                        title={inert ? entry.reason : entry.hint}
                        data-menu-item={entry.id}
                        onClick={(e) => { e.stopPropagation(); activate(entry); }}
                        // An inert row keeps its pointer so the hover title
                        // can say why it is inert.
                        className={cn(
                            entry.danger && !inert ? menuItemDanger : menuItem,
                            inert && 'aria-disabled:pointer-events-auto cursor-default hover:bg-transparent hover:text-ink-2',
                            entry.checked && 'text-ink [&_svg]:text-brass',
                        )}
                    >
                        {Icon && <Icon className="w-3.5 h-3.5 flex-none" strokeWidth={1.6} />}
                        <span className="truncate">{entry.label}</span>
                        {entry.tag && <span className="ml-auto pl-[1.125rem] text-[0.71875rem] text-brass">{entry.tag}</span>}
                        {entry.shortcut && <ShortcutHint id={entry.shortcut} />}
                    </button>
                );
            })}
            {children}
        </div>,
        document.body,
    );
}
