// SPDX-License-Identifier: MIT
/**
 * StyleControl: the shared style icon and its popover, so every renderer
 * that offers a style choice offers it the same way.
 *
 * A 1.5rem sliders icon sits top right of the renderer and shows only while
 * the pointer is near it (within `STYLE_CONTROL_REACH` of the icon's box),
 * while it has keyboard focus or while its popover is open: never merely
 * because the pointer is somewhere over the renderer. The control judges
 * the reach itself (see `watch` and `frame`). At rest the icon is
 * transparent and takes no pointer, but stays in the tab order, so Tab
 * reaches it and shows it. The popover has up to three style rows, each
 * shown only when the renderer asks for it: Theme (the renderer's `themes`,
 * the first being its default: the skin), Font (the host's catalogue for
 * one role, plus "Skin default") and Text size (A− / percent / A+, and
 * Reset to the renderer's default). Every choice goes through
 * `api.setStyle`, which the host persists per channel under the renderer's
 * id; the control reads the record back through `useStyle`, so it shows
 * what the host holds. Choosing the default stores null, the renderer's
 * default. Under them the renderer may add its own view controls (`extra`,
 * laid out with `StyleControlRow`): the PDF viewer's page, zoom and pointer
 * rows, so it needs no bar of its own. The control shows whenever it has
 * a row of either kind.
 *
 * Inline styles only, every colour a skin token, so the one component works
 * in a native renderer's shadow root and in an iframe renderer's document.
 * Esc, or a press anywhere outside it (on its root node, the shadow root or
 * the document, and on the page that holds a shadow root), closes the
 * popover. So does the window losing focus: a press inside a child frame
 * (the document view's page) or, for an iframe renderer, anywhere in the
 * app outside its frame reaches none of those listeners, but it blurs the
 * window.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { POINTER_MESSAGE } from './document-view';
import { useStyle, useTheme } from './index';
import type { StyleApi, ThemeApi } from './index';
import type { FontRole, RendererFontFamily, RendererStyle } from './types';

/** One theme a renderer offers: its id (stored in `RendererStyle.theme`)
 *  and the name the select shows. */
export interface StyleControlTheme {
    id: string;
    label: string;
}

/** The text-size range a renderer offers, as factors (1 = 100%). */
export interface StyleControlSize {
    min: number;
    max: number;
    step: number;
    /** The renderer's own size, what null means. */
    default: number;
}

export interface StyleControlProps {
    api: (StyleApi & ThemeApi) | null | undefined;
    /** The themes on offer, the renderer's default (the skin) first; that
     *  one is stored as null. Fewer than two shows no Theme row. */
    themes: ReadonlyArray<StyleControlTheme>;
    /** The role whose families the Font row offers, or false for none. */
    font: FontRole | false;
    /** The Text size row's range, or false for none. */
    size: StyleControlSize | false;
    /** The element whose pointer moves are measured against the icon;
     *  leaving it hides the icon. By default the control's parent element
     *  at mount. Pass the renderer's root when the control sits in a
     *  smaller box (a toolbar row, a header). */
    watch?: React.RefObject<HTMLElement | null>;
    /** A borderless frame the control floats over whose page reports the
     *  pointer (`useDocumentView`'s `reportPointer`: pass the view's
     *  `frameRef`). The frame swallows every move over it, so the control
     *  measures the page's reports instead, mapped onto the frame's box.
     *  Entering any other frame hides the icon. */
    frame?: React.RefObject<HTMLIFrameElement | null>;
    /** Merged over the root's style, to anchor it elsewhere than top right. */
    style?: React.CSSProperties;
    /** The renderer's own view controls, placed in the popover under the
     *  style rows (a rule between them when both show): a node, or a
     *  function of `{ close }` that renders it while the popover is open.
     *  Lay each row out with `StyleControlRow`. Never a style: what it sets
     *  is the renderer's own state, not `api.setStyle`. */
    extra?: React.ReactNode | ((ctx: StyleControlExtraContext) => React.ReactNode);
}

/** What an `extra` render function receives. */
export interface StyleControlExtraContext {
    /** Closes the popover (and returns focus to the icon). */
    close: () => void;
}

export interface StyleControlRowProps {
    /** The row's name, in the popover's label column. */
    label: React.ReactNode;
    /** The row's controls, laid out in a row in the control column. */
    children?: React.ReactNode;
}

const round = (n: number): number => Math.round(n * 100) / 100;
const rem = (px: number): string => `${px / 16}rem`;

/** How near, in CSS px, the pointer must come to the icon's box (on any
 *  side) for the icon to show. 40, the low end of a comfortable reach: with
 *  the icon on its 8px inset it is a 72px square at the renderer's corner,
 *  found by heading for the corner and never shown while the pointer is
 *  over the content being read (a wider reach starts showing the icon over
 *  the first line's end and an editor's own top-right toolbar). */
export const STYLE_CONTROL_REACH = 40;

/** The icon's size follows the host document's root type size. */
const ICON = rem(24);

/** The root's default inset from the top right corner. */
const INSET = rem(8);

/** Whether the point (x, y) lies within `reach` px of the box, on any side
 *  (the box grown by the reach). */
export function withinReach(
    box: { left: number; top: number; right: number; bottom: number },
    x: number, y: number, reach: number = STYLE_CONTROL_REACH,
): boolean {
    return x >= box.left - reach && x <= box.right + reach && y >= box.top - reach && y <= box.bottom + reach;
}

/** Where a page's pointer report (`POINTER_MESSAGE`) puts the pointer in
 *  the frame's own document: the page's viewport position scaled onto the
 *  frame's box (the frame may be drawn scaled), or null for a report that
 *  is not one. */
export function framePoint(
    box: { left: number; top: number; width: number; height: number }, data: unknown,
): { x: number; y: number } | null {
    const d = data as { type?: unknown; x?: unknown; y?: unknown; w?: unknown; h?: unknown } | null;
    if (!d || typeof d !== 'object' || d.type !== POINTER_MESSAGE) return null;
    const { x, y, w, h } = d;
    if (![x, y, w, h].every((n) => typeof n === 'number' && Number.isFinite(n))) return null;
    if ((w as number) <= 0 || (h as number) <= 0) return null;
    return {
        x: box.left + (x as number) * (box.width / (w as number)),
        y: box.top + (y as number) * (box.height / (h as number)),
    };
}

/** Whether an element shows keyboard focus (`:focus-visible`). */
function focusVisible(el: Element): boolean {
    try {
        return el.matches(':focus-visible');
    } catch {
        return false;
    }
}

/** The element a pointer event is really over, through a shadow root. */
function hitOf(e: Event): EventTarget | null {
    const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
    return path.length ? path[0] : e.target;
}

const isFrame = (t: EventTarget | null): boolean =>
    !!t && typeof (t as Element).tagName === 'string' && (t as Element).tagName === 'IFRAME';

const S = {
    root: {
        position: 'absolute', top: INSET, right: INSET, zIndex: 3,
    } as React.CSSProperties,
    icon: {
        width: ICON, height: ICON, padding: 0, margin: 0, boxSizing: 'border-box',
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        border: '1px solid var(--line-2)', borderRadius: 'calc(var(--radius) - 0.125rem)',
        background: 'var(--sheet-2)', color: 'var(--ink-2)',
        cursor: 'pointer', transition: 'opacity 120ms ease',
    } as React.CSSProperties,
    iconOpen: {
        background: 'var(--sheet-3)', color: 'var(--ink)',
    } as React.CSSProperties,
    popover: {
        position: 'absolute', top: rem(30), right: 0, width: rem(248), boxSizing: 'border-box',
        display: 'grid', gridTemplateColumns: '4.25rem 1fr', alignItems: 'center', gap: '0.5rem 0.625rem',
        padding: rem(12),
        background: 'var(--sheet)', color: 'var(--ink)',
        border: '1px solid var(--line-2)', borderRadius: 'var(--radius)', boxShadow: 'var(--shadow)',
        font: '0.75rem/1.3 var(--font-ui)', textAlign: 'left',
    } as React.CSSProperties,
    label: {
        color: 'var(--ink-2)',
    } as React.CSSProperties,
    select: {
        width: '100%', height: rem(26), boxSizing: 'border-box', padding: '0 0.375rem', margin: 0,
        border: '1px solid var(--line-2)', borderRadius: 'calc(var(--radius) - 0.125rem)',
        background: 'var(--sheet-2)', color: 'var(--ink)', font: 'inherit',
    } as React.CSSProperties,
    sizeRow: {
        display: 'flex', alignItems: 'center', gap: rem(6),
    } as React.CSSProperties,
    step: {
        height: rem(26), minWidth: rem(30), padding: '0 0.375rem', margin: 0, boxSizing: 'border-box',
        border: '1px solid var(--line-2)', borderRadius: 'calc(var(--radius) - 0.125rem)',
        background: 'var(--sheet-2)', color: 'var(--ink)', font: 'inherit', cursor: 'pointer',
    } as React.CSSProperties,
    percent: {
        minWidth: rem(40), textAlign: 'center', fontVariantNumeric: 'tabular-nums',
    } as React.CSSProperties,
    reset: {
        marginLeft: 'auto', padding: 0, border: 'none', background: 'none',
        color: 'var(--ink-2)', font: 'inherit', cursor: 'pointer', textDecoration: 'underline',
    } as React.CSSProperties,
    disabled: {
        color: 'var(--ink-3)', cursor: 'default',
    } as React.CSSProperties,
    rule: {
        gridColumn: '1 / -1', height: 0, margin: '0.125rem 0', borderTop: '1px solid var(--line)',
    } as React.CSSProperties,
};

/** One row of a `StyleControl`'s `extra`: the label in the popover's label
 *  column, the controls in a row beside it, styled like the style rows. */
export function StyleControlRow({ label, children }: StyleControlRowProps) {
    return (
        <>
            <span style={S.label}>{label}</span>
            <div style={S.sizeRow}>{children}</div>
        </>
    );
}

/** Lucide's sliders-horizontal, drawn inline (no icon font reaches a shadow
 *  root). */
function SlidersIcon() {
    return (
        <svg style={{ width: rem(14), height: rem(14) }} viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <line x1="21" x2="14" y1="4" y2="4" />
            <line x1="10" x2="3" y1="4" y2="4" />
            <line x1="21" x2="12" y1="12" y2="12" />
            <line x1="8" x2="3" y1="12" y2="12" />
            <line x1="21" x2="16" y1="20" y2="20" />
            <line x1="12" x2="3" y1="20" y2="20" />
            <line x1="14" x2="14" y1="2" y2="6" />
            <line x1="8" x2="8" y1="10" y2="14" />
            <line x1="16" x2="16" y1="18" y2="22" />
        </svg>
    );
}

export function StyleControl({ api, themes, font, size, watch, frame, style, extra }: StyleControlProps) {
    const current = useStyle(api);
    const { scheme } = useTheme(api);
    const [open, setOpen] = useState(false);
    const rootRef = useRef<HTMLDivElement | null>(null);
    const iconRef = useRef<HTMLButtonElement | null>(null);
    // The root as state too, so the reach listeners attach once it mounts
    // (the control renders nothing until it has a row to offer).
    const [rootEl, setRootEl] = useState<HTMLDivElement | null>(null);
    const attachRoot = useCallback((el: HTMLDivElement | null) => {
        rootRef.current = el;
        setRootEl(el);
    }, []);
    // Near is set only when it changes: a pointer move re-renders nothing.
    const [near, setNearState] = useState(false);
    const nearRef = useRef(false);
    const setNear = useCallback((next: boolean) => {
        if (nearRef.current === next) return;
        nearRef.current = next;
        setNearState(next);
    }, []);
    const [focused, setFocused] = useState(false);

    // Every move over the watched element is measured against the icon's
    // box (its rect holds while the icon is transparent). In the capture
    // phase, so an editor that stops a move's propagation still reports it.
    // A child frame swallows the moves over it: entering one hides the icon,
    // except `frame`, whose page reports its own moves (a message), measured
    // the same way. A report is taken only while the pointer is in that
    // frame, so one posted just before the pointer left (a report waits for
    // the page's next animation frame) never shows the icon after it.
    useEffect(() => {
        if (!rootEl) return undefined;
        const target = (watch && watch.current) || rootEl.parentElement;
        if (!target) return undefined;
        const reporting = (t: EventTarget | null): boolean => !!frame && !!t && t === frame.current;
        let inFrame = true;
        const measure = (x: number, y: number) => {
            const icon = iconRef.current;
            if (icon) setNear(withinReach(icon.getBoundingClientRect(), x, y));
        };
        const onMove = (e: Event) => {
            const p = e as PointerEvent;
            measure(p.clientX, p.clientY);
        };
        const onOver = (e: Event) => {
            const hit = hitOf(e);
            if (reporting(hit)) inFrame = true;
            else if (isFrame(hit)) setNear(false);
        };
        const onOut = (e: Event) => {
            if (reporting(hitOf(e))) inFrame = false;
            const to = (e as PointerEvent).relatedTarget;
            if (isFrame(to) && !reporting(to)) setNear(false);
        };
        const onLeave = () => {
            inFrame = false;
            setNear(false);
        };
        const view = rootEl.ownerDocument ? rootEl.ownerDocument.defaultView : null;
        const onReport = (e: Event) => {
            const f = frame ? frame.current : null;
            const m = e as MessageEvent;
            if (!f || !inFrame || m.source !== f.contentWindow) return;
            const at = framePoint(f.getBoundingClientRect(), m.data);
            if (at) measure(at.x, at.y);
        };
        const opts: AddEventListenerOptions = { capture: true, passive: true };
        target.addEventListener('pointermove', onMove, opts);
        target.addEventListener('pointerover', onOver, opts);
        target.addEventListener('pointerout', onOut, opts);
        target.addEventListener('pointerleave', onLeave);
        if (frame && view) view.addEventListener('message', onReport);
        return () => {
            target.removeEventListener('pointermove', onMove, opts);
            target.removeEventListener('pointerover', onOver, opts);
            target.removeEventListener('pointerout', onOut, opts);
            target.removeEventListener('pointerleave', onLeave);
            if (frame && view) view.removeEventListener('message', onReport);
        };
    }, [rootEl, watch, frame, setNear]);

    useEffect(() => {
        const el = rootRef.current;
        if (!open || !el) return undefined;
        // The root node is the shadow root of a native renderer or the
        // iframe renderer's document; a press on the page around a shadow
        // root reaches only that page's document, so listen there too.
        const targets: EventTarget[] = [el.getRootNode()];
        if (el.ownerDocument && !targets.includes(el.ownerDocument)) targets.push(el.ownerDocument);
        const onPress = (e: Event) => {
            if (typeof e.composedPath === 'function' && e.composedPath().includes(el)) return;
            setOpen(false);
        };
        const onKey = (e: Event) => {
            if ((e as KeyboardEvent).key !== 'Escape') return;
            e.stopPropagation();
            setOpen(false);
            if (iconRef.current) iconRef.current.focus();
        };
        // A press inside a child frame, or outside an iframe renderer's own
        // frame, is never delivered here; the window's blur is.
        const view = el.ownerDocument ? el.ownerDocument.defaultView : null;
        const onBlur = () => setOpen(false);
        for (const target of targets) {
            target.addEventListener('pointerdown', onPress, true);
            target.addEventListener('keydown', onKey, true);
        }
        if (view) view.addEventListener('blur', onBlur);
        return () => {
            for (const target of targets) {
                target.removeEventListener('pointerdown', onPress, true);
                target.removeEventListener('keydown', onKey, true);
            }
            if (view) view.removeEventListener('blur', onBlur);
        };
    }, [open]);

    const themeRow = themes.length > 1;
    const families: ReadonlyArray<RendererFontFamily> =
        font && api && api.getFontFamilies ? api.getFontFamilies().filter((f) => f.role === font) : [];
    const fontRow = font !== false && families.length > 0;
    const sizeRow = size !== false;
    const styleRows = themeRow || fontRow || sizeRow;
    const extraRows = extra !== undefined && extra !== null && extra !== false;
    if (!styleRows && !extraRows) return null;
    const close = () => {
        setOpen(false);
        if (iconRef.current) iconRef.current.focus();
    };

    const set = (patch: Partial<RendererStyle>) => current.set(patch);
    const shown = near || focused || open;

    const themeValue = current.theme !== null && themes.some((t) => t.id === current.theme)
        ? current.theme
        : (themes[0] ? themes[0].id : '');
    const fontValue = current.font || '';
    const unknownFont = fontValue !== '' && !families.some((f) => f.family === fontValue);

    let sizeControls: React.ReactNode = null;
    if (size) {
        const factor = current.textSize ?? size.default;
        const commit = (next: number) => {
            const value = round(Math.min(size.max, Math.max(size.min, next)));
            set({ textSize: Math.abs(value - size.default) < 1e-9 ? null : value });
        };
        const atMin = factor <= size.min + 1e-9;
        const atMax = factor >= size.max - 1e-9;
        sizeControls = (
            <div style={S.sizeRow}>
                <button type="button" style={{ ...S.step, ...(atMin ? S.disabled : null) }} disabled={atMin}
                    aria-label="Smaller text" onClick={() => commit(factor - size.step)}>A−</button>
                <span style={S.percent}>{Math.round(factor * 100)}%</span>
                <button type="button" style={{ ...S.step, ...(atMax ? S.disabled : null) }} disabled={atMax}
                    aria-label="Larger text" onClick={() => commit(factor + size.step)}>A+</button>
                <button type="button" style={{ ...S.reset, ...(current.textSize === null ? S.disabled : null) }}
                    disabled={current.textSize === null} onClick={() => set({ textSize: null })}>Reset</button>
            </div>
        );
    }

    return (
        <div ref={attachRoot} data-collimate-style-control=""
            style={{ ...S.root, pointerEvents: shown ? 'auto' : 'none', ...style }}>
            <button ref={iconRef} type="button" title="Style" aria-label="Style"
                aria-haspopup="dialog" aria-expanded={open}
                style={{ ...S.icon, ...(open ? S.iconOpen : null), opacity: shown ? 1 : 0 }}
                onFocus={(e) => setFocused(focusVisible(e.currentTarget))}
                onBlur={() => setFocused(false)}
                onClick={() => setOpen((o) => !o)}>
                <SlidersIcon />
            </button>
            {open && (
                <div role="dialog" aria-label="Style" style={{ ...S.popover, colorScheme: scheme }}>
                    {themeRow && (
                        <>
                            <span style={S.label}>Theme</span>
                            <select style={S.select} value={themeValue} aria-label="Theme"
                                onChange={(e) => set({ theme: e.target.value === themes[0].id ? null : e.target.value })}>
                                {themes.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                            </select>
                        </>
                    )}
                    {fontRow && (
                        <>
                            <span style={S.label}>Font</span>
                            <select style={S.select} value={fontValue} aria-label="Font"
                                onChange={(e) => set({ font: e.target.value || null })}>
                                <option value="">Skin default</option>
                                {families.map((f) => (
                                    <option key={f.family} value={f.family} style={{ fontFamily: f.stack }}>{f.label}</option>
                                ))}
                                {unknownFont && <option value={fontValue}>{fontValue}</option>}
                            </select>
                        </>
                    )}
                    {sizeRow && (
                        <>
                            <span style={S.label}>Text size</span>
                            {sizeControls}
                        </>
                    )}
                    {styleRows && extraRows && <div role="separator" style={S.rule} />}
                    {extraRows && (typeof extra === 'function' ? extra({ close }) : extra)}
                </div>
            )}
        </div>
    );
}
