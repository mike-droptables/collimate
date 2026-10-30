import React, { useEffect, useLayoutEffect, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import {
    defineRenderer,
    StyleControl,
    tokens,
    useCollimateFile,
    useStyle,
} from '/api/sdk/react.js';

// Native renderers run in a shadow root with NO CSS reset (box model is the
// browser default, content-box) — style everything inline, and inject a
// box-sizing reset via api.injectCss if you use padded full-width elements
// (see how-to-write-a-renderer.md). The skin tokens (tokens.color.*,
// tokens.font.*) are var(--…) references the shadow root inherits.

// A selection is the one thing brass marks; ::selection needs a stylesheet.
// The area sizes to its text (field-sizing, TEXT_AREA), so the scroller
// below it is the ground a press below the text lands on (caretToEnd);
// without field-sizing the area fills the scroller instead.
const SELECTION_CSS = 'textarea::selection { background: var(--brass-soft); }\n'
    + '@supports not (field-sizing: content) { textarea[data-text-area] { min-height: 100%; } }';

/** The style control's rows: no theme of its own (the skin), a mono family
 *  and the text size, a factor of the 13px design size in root-relative units. */
export const TEXT_THEMES = Object.freeze([Object.freeze({ id: 'skin', label: 'Collimate' })]);
export const TEXT_SIZE = Object.freeze({ min: 0.7, max: 2, step: 0.1, default: 1 });
export const TEXT_FONT_SIZE = 13;

/** The area's root-relative font size for a stored text-size factor. */
export function textFontSize(textSize: unknown): string {
    const factor = typeof textSize === 'number' && Number.isFinite(textSize)
        ? Math.min(TEXT_SIZE.max, Math.max(TEXT_SIZE.min, textSize)) : TEXT_SIZE.default;
    return `${Math.round(TEXT_FONT_SIZE * factor * 10) / 160}rem`;
}

/** The CSS stack of a stored family, from the host's catalogue; null for
 *  none or a family it no longer has (the skin's mono then). */
export function familyStack(family: unknown, families: ReadonlyArray<{ family: string; stack: string }>): string | null {
    if (typeof family !== 'string' || !family) return null;
    const hit = (families || []).find((f) => f && f.family === family);
    return hit && hit.stack ? hit.stack : null;
}

/** The scroller the area sits in: it takes the rest of the column and
 *  scrolls the area, which is as tall as its text. */
export const TEXT_SCROLL = Object.freeze({
    flex: 1, minHeight: 0, overflowY: 'auto', cursor: 'text',
});

/** The area: as tall as its text (`fieldSizing: 'content'`), never
 *  scrolling itself. */
export const TEXT_AREA = Object.freeze({
    display: 'block', width: '100%', minHeight: '3.2em', fieldSizing: 'content', overflow: 'hidden',
    background: 'transparent', color: tokens.color.ink, caretColor: tokens.color.ink,
    lineHeight: 1.6, padding: '1rem', boxSizing: 'border-box', border: 'none', outline: 'none', resize: 'none',
});

/** Caret at the end: a press on the scroller itself (below the area's
 *  text) focuses the area with the caret after its last character. A press
 *  on the area is the browser's own. */
export function caretToEnd(e: { target: unknown; currentTarget: unknown; button?: number; preventDefault(): void },
                           area: HTMLTextAreaElement | null): boolean {
    if (!area || area.disabled || e.target !== e.currentTarget || (e.button !== undefined && e.button !== 0)) return false;
    e.preventDefault();
    area.focus();
    const end = area.value.length;
    area.setSelectionRange(end, end);
    return true;
}

/** The editor's box: it fills the renderer's box. */
export const TEXT_ROOT = Object.freeze({
    width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
    position: 'relative', background: tokens.color.sheet, color: tokens.color.ink,
});

/** The status line, along the editor's bottom. */
export const TEXT_STATUS = Object.freeze({
    height: '1.375rem', flex: 'none', background: tokens.color.sheet, borderTop: `1px solid ${tokens.color.line}`,
    display: 'flex', alignItems: 'center', paddingLeft: '0.75rem', paddingRight: '0.75rem', gap: '0.75rem',
    fontFamily: tokens.font.mono, fontSize: '0.6875rem', color: tokens.color.ink3, whiteSpace: 'nowrap', overflow: 'hidden',
});

function TextEditor() {
    const { content, saveContent, isReadOnly, fileName, api } = useCollimateFile();
    const areaRef = useRef<HTMLTextAreaElement | null>(null);
    const style = useStyle(api);
    const fontFamily = familyStack(style.font, api && api.getFontFamilies ? api.getFontFamilies() : [])
        || tokens.font.mono;

    useEffect(() => {
        if (api && api.injectCss) api.injectCss(SELECTION_CSS);
    }, [api]);

    // The textarea owns its value (React sets neither `value` nor
    // `defaultValue`): typing never round-trips through React, and the
    // file's body (at mount, and another writer's later) is written in here
    // with the caret, selection and scroll kept — a controlled value would
    // move the caret to the end on every push. The scroller keeps its
    // offset (the area itself never scrolls).
    useLayoutEffect(() => {
        const area = areaRef.current;
        if (!area || area.value === content) return;
        const { selectionStart, selectionEnd } = area;
        area.value = content;
        area.setSelectionRange(Math.min(selectionStart, content.length), Math.min(selectionEnd, content.length));
    }, [content]);

    return (
        <div style={TEXT_ROOT}>
            <div style={TEXT_SCROLL} onMouseDown={(e) => caretToEnd(e, areaRef.current)}>
                <textarea
                    ref={areaRef}
                    data-text-area=""
                    disabled={isReadOnly}
                    onChange={e => saveContent(e.target.value)}
                    spellCheck={false}
                    style={{ ...TEXT_AREA, fontFamily, fontSize: textFontSize(style.textSize) }}
                />
            </div>
            {/* Top right, shown while the pointer is near that corner (it
                watches this root, its parent). */}
            <StyleControl api={api} themes={TEXT_THEMES} font="mono" size={TEXT_SIZE}
                style={{ right: '1.25rem' }} />
            <div style={TEXT_STATUS}>
                <span>{content.split('\n').length} lines</span>
                <span>{content.length} chars</span>
                <span style={{ marginLeft: 'auto' }}>{fileName}</span>
            </div>
        </div>
    );
}

export default defineRenderer({ component: TextEditor });
