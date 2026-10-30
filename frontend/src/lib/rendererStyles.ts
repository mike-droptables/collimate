// What a renderer sees when it asks for its style choices: the theme, the
// font family and the text size its style control set, per channel, under
// its renderer id (the settings doc's `renderer_styles` section). The
// store (stores/useRendererStylesStore) applies the section here; every
// host reads it back: NativeRenderer's api.getStyle() and onStyleChange(),
// and the SET_STYLE message an iframe renderer's document receives
// (lib/sandboxBridge). Same record both ways, the SDK's RendererStyle; null
// means the renderer's default (the skin, the role's skin font, its own
// size).

import type { ChannelSettingsRendererStyles, RendererStyleRecord } from '@/lib/api';
import { FONT_CATALOGUE, type FontRole } from '@/lib/fonts';

/** Fired on window for every renderer id whose applied style changed; the
 *  detail is `{ rendererId }`. */
export const RENDERER_STYLES_CHANGED_EVENT = 'collimate:renderer-styles-changed';

export interface RendererStylesChangedDetail {
    rendererId: string;
}

/** A catalogue family as a renderer's font select offers it (the SDK's
 *  RendererFontFamily). The style record stores `family`; the renderer
 *  applies `stack`. */
export interface RendererFontFamily {
    family: string;
    label: string;
    role: FontRole;
    stack: string;
}

/** The record of a renderer that set nothing: its defaults throughout. */
export const DEFAULT_RENDERER_STYLE: Readonly<RendererStyleRecord> = Object.freeze({
    theme: null, font: null, textSize: null,
});

// The server's limits (api/channel_settings.py _validate_renderer_styles).
export const RENDERER_STYLES_MAX = 256;
const THEME_MAX = 64;
const FONT_MAX = 120;
export const TEXT_SIZE_MIN = 0.5;
export const TEXT_SIZE_MAX = 3;
const RENDERER_ID = /^[A-Za-z0-9._-]{1,128}$/;

/** Whether `id` is a renderer id the section can key a record by. */
export function isRendererStyleId(id: unknown): id is string {
    return typeof id === 'string' && RENDERER_ID.test(id);
}

function styleString(value: unknown, limit: number): string | null {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    return trimmed && trimmed.length <= limit ? trimmed : null;
}

function styleTextSize(value: unknown): number | null {
    if (typeof value !== 'number' || !Number.isFinite(value)) return null;
    const clamped = Math.min(Math.max(value, TEXT_SIZE_MIN), TEXT_SIZE_MAX);
    return Math.round(clamped * 100) / 100;
}

/** One record as the server keeps it: strings trimmed and capped (theme 64,
 *  font 120 characters), the text size clamped into [0.5, 3] and rounded to
 *  0.01, anything else null. */
export function sanitizeRendererStyle(raw: unknown): RendererStyleRecord {
    const value = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
    return {
        theme: styleString(value.theme, THEME_MAX),
        font: styleString(value.font, FONT_MAX),
        textSize: styleTextSize(value.textSize),
    };
}

/** Whether a record holds no choice at all (the server drops it). */
export function isDefaultRendererStyle(style: RendererStyleRecord): boolean {
    return style.theme === null && style.font === null && style.textSize === null;
}

/** The section as the server keeps it: valid ids only (the first 256),
 *  each record sanitized, all-null records dropped. */
export function sanitizeRendererStyles(raw: unknown): ChannelSettingsRendererStyles {
    const out: ChannelSettingsRendererStyles = {};
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
    let kept = 0;
    for (const [id, entry] of Object.entries(raw as Record<string, unknown>)) {
        if (kept >= RENDERER_STYLES_MAX) break;
        if (!isRendererStyleId(id) || !entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
        const style = sanitizeRendererStyle(entry);
        if (isDefaultRendererStyle(style)) continue;
        out[id] = style;
        kept += 1;
    }
    return out;
}

let applied: ChannelSettingsRendererStyles = {};

/** The applied record for `rendererId`, or all null when it set nothing. */
export function readRendererStyle(rendererId: string): RendererStyleRecord {
    const style = Object.prototype.hasOwnProperty.call(applied, rendererId) ? applied[rendererId] : undefined;
    return style ? { ...style } : { ...DEFAULT_RENDERER_STYLE };
}

function sameStyle(a: RendererStyleRecord, b: RendererStyleRecord): boolean {
    return a.theme === b.theme && a.font === b.font && a.textSize === b.textSize;
}

/** Apply the section and tell the renderer hosts, one event per renderer id
 *  whose record changed (a channel switch re-styles every open renderer;
 *  a re-adopted identical section sends nothing). */
export function applyRendererStyles(styles: ChannelSettingsRendererStyles): void {
    const previous = applied;
    applied = styles;
    if (typeof window === 'undefined') return;
    for (const rendererId of new Set([...Object.keys(previous), ...Object.keys(styles)])) {
        if (sameStyle(readStyleOf(previous, rendererId), readStyleOf(styles, rendererId))) continue;
        window.dispatchEvent(new CustomEvent<RendererStylesChangedDetail>(
            RENDERER_STYLES_CHANGED_EVENT, { detail: { rendererId } },
        ));
    }
}

function readStyleOf(styles: ChannelSettingsRendererStyles, rendererId: string): RendererStyleRecord {
    return Object.prototype.hasOwnProperty.call(styles, rendererId) ? styles[rendererId] : DEFAULT_RENDERER_STYLE;
}

/** The font catalogue as renderers see it, every role in catalogue order:
 *  what api.getFontFamilies() returns and SET_STYLE carries as `fonts`. */
export function rendererFontFamilies(): RendererFontFamily[] {
    return (Object.keys(FONT_CATALOGUE) as FontRole[]).flatMap((role) =>
        FONT_CATALOGUE[role].map(({ family, label, stack }) => ({ family, label, role, stack })));
}
