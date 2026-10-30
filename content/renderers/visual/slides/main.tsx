import React, { useState, useEffect, useMemo, useCallback, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import {
    defineRenderer,
    useCollimateFile,
    useHostFocus,
    tokens,
} from '/api/sdk/react.js';

const { color, font } = tokens;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// Inline style strings for the slide markup. The markup is an HTML string
// (dangerouslySetInnerHTML), so each rule is a CSS declaration list on the
// skin tokens: prose in --font-prose on --prose-ink, code on --code-bg.
const SLIDE_CSS = {
  code: `background:${color.codeBg};color:${color.ink};border:1px solid ${color.line};padding:1px 6px;border-radius:4px;font-size:0.8em;font-family:${font.mono};`,
  h1: `font-size:2.2em;font-weight:600;color:${color.proseInk};margin:0 0 0.4em 0;line-height:1.15;letter-spacing:-0.01em;`,
  h2: `font-size:1.55em;font-weight:600;color:${color.proseInk};margin:0 0 0.5em 0;line-height:1.2;`,
  h3: `font-size:1.2em;font-weight:600;color:${color.ink2};margin:0 0 0.5em 0;line-height:1.3;`,
  li: 'margin:0.35em 0;font-size:1.1em;line-height:1.45;',
  ul: `padding-left:1.4em;margin:0.4em 0;color:${color.proseInk};`,
  p: `margin:0.5em 0;font-size:1.1em;line-height:1.55;color:${color.proseInk};`,
};

/** A slide's markdown as HTML whose every colour and font is a skin token. */
export function renderSlideMarkdown(md) {
  let html = escapeHtml(md);
  html = html.replace(/`([^`\n]+)`/g, `<code style="${SLIDE_CSS.code}">$1</code>`);
  html = html.replace(/^# (.+)$/gm, `<h1 style="${SLIDE_CSS.h1}">$1</h1>`);
  html = html.replace(/^## (.+)$/gm, `<h2 style="${SLIDE_CSS.h2}">$1</h2>`);
  html = html.replace(/^### (.+)$/gm, `<h3 style="${SLIDE_CSS.h3}">$1</h3>`);
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/^[*\-] (.+)$/gm, `<li style="${SLIDE_CSS.li}">$1</li>`);
  // A run of items becomes one list; the newline after the last item stays
  // outside it, so a paragraph after the list is still its own block.
  html = html.replace(/(?:<li[^>]*>[\s\S]*?<\/li>(?:\n(?=<li))?)+/g, (m) => `<ul style="${SLIDE_CSS.ul}">${m}</ul>`);
  html = html.split(/\n{2,}/).map((block) => {
    const t = block.trim();
    if (!t) return '';
    if (/^<[hupobldi]/.test(t)) return t;
    return `<p style="${SLIDE_CSS.p}">${t}</p>`;
  }).join('\n');
  return html;
}

/** The slide card: a sheet lifted off the canvas, set in the prose face. */
export const SLIDE_CARD = Object.freeze({
  width: '100%', maxWidth: 880, minHeight: 360,
  background: color.sheet, border: `1px solid ${color.line}`, borderRadius: tokens.radius,
  padding: '48px 56px', boxShadow: tokens.shadow, boxSizing: 'border-box',
  fontFamily: font.prose, color: color.proseInk, fontSize: 15.5,
});

/** The renderer's box: it fills the renderer's box, the toolbar at the top
 *  and the stage (or the source) under it. */
export const SLIDES_ROOT = Object.freeze({
  width: '100%', height: '100%', color: color.ink,
  fontFamily: font.ui, display: 'flex', flexDirection: 'column',
});

/** The stage the card is centred on: the canvas, to the box's bottom. */
export const SLIDES_STAGE = Object.freeze({
  flex: 1, minHeight: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
  padding: 32, boxSizing: 'border-box',
  overflow: 'auto', background: color.canvas,
});

// Caret at the end of the source: the area sizes to its text
// (`fieldSizing: 'content'`, SOURCE_AREA) inside a scroller (SOURCE_SCROLL),
// so a press below the text lands on the scroller, which focuses the area
// with the caret after its last character. Without field-sizing (the
// injected rule) the area fills the scroller instead.
const SOURCE_AREA_FALLBACK_CSS = '@supports not (field-sizing: content) { textarea[data-source-area] { min-height: 100%; } }';

/** The scroller the source sits in. */
export const SOURCE_SCROLL = Object.freeze({
  flex: 1, minHeight: 0, overflowY: 'auto', cursor: 'text',
});

/** A press on the scroller itself (below the source's text) focuses the
 *  area with the caret at its end; a press on the area is the browser's. */
export function sourceCaretToEnd(e, area) {
  if (!area || area.disabled || e.target !== e.currentTarget || (e.button !== undefined && e.button !== 0)) return false;
  e.preventDefault();
  area.focus();
  const end = area.value.length;
  area.setSelectionRange(end, end);
  return true;
}

export function splitSlides(md) {
  const raw = (md || '').split(/^\s*---\s*$/m);
  return raw.map((s) => s.trim()).filter((s) => s.length > 0);
}

function SlidesView() {
  const { content, saveContent, isReadOnly, api } = useCollimateFile();
  const { focused } = useHostFocus(api);
  const save = saveContent;
  const [local, setLocal] = useState(content);
  const [editing, setEditing] = useState(false);
  const [idx, setIdx] = useState(0);
  const areaRef = useRef(null);

  useEffect(() => { setLocal(content); }, [content]);

  useEffect(() => {
    if (api && api.injectCss) api.injectCss(SOURCE_AREA_FALLBACK_CSS);
  }, [api]);

  const slides = useMemo(() => splitSlides(local), [local]);
  const safeIdx = Math.max(0, Math.min(idx, slides.length - 1));

  const next = useCallback(() => setIdx((i) => Math.max(0, Math.min(i + 1, slides.length - 1))), [slides.length]);
  const prev = useCallback(() => setIdx((i) => Math.max(i - 1, 0)), []);

  useEffect(() => {
    // Window-level listener: a native renderer shares the main document, so
    // without the host-focus gate this heard (and preventDefault'ed) every
    // Space/arrow app-wide. Only ever act while our cell holds the focus.
    if (!focused) return undefined;
    function onKey(e) {
      if (editing) return;
      if (e.key === 'ArrowRight' || e.key === ' ') { next(); e.preventDefault(); }
      else if (e.key === 'ArrowLeft') { prev(); e.preventDefault(); }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [next, prev, editing, focused]);

  const wrap = { ...SLIDES_ROOT, background: editing ? color.sheet : color.canvas };
  const toolbar = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    height: 36, padding: '0 10px', boxSizing: 'border-box', flexShrink: 0,
    borderBottom: `1px solid ${color.line}`, background: color.sheet2, fontSize: 12.5, color: color.ink2,
  };
  const btn = (disabled = false) => ({
    background: 'none', color: disabled ? color.ink3 : color.ink2, border: `1px solid ${color.line2}`,
    borderRadius: `calc(${tokens.radius} - 3px)`, height: 24, padding: '0 9px',
    cursor: disabled ? 'default' : 'pointer', fontSize: 12.5, fontFamily: font.ui,
  });

  if (editing) {
    return (
      <div style={wrap}>
        <div style={toolbar}>
          <span>editing source ({slides.length} slide{slides.length === 1 ? '' : 's'})</span>
          <button style={btn()} onClick={() => setEditing(false)}>done</button>
        </div>
        <div style={SOURCE_SCROLL} onMouseDown={(e) => sourceCaretToEnd(e, areaRef.current)}>
          <textarea
            ref={areaRef}
            data-source-area=""
            value={local}
            onChange={(e) => { setLocal(e.target.value); save(e.target.value); }}
            spellCheck={false}
            style={{
              display: 'block', width: '100%', minHeight: '3em', fieldSizing: 'content', overflow: 'hidden',
              background: 'transparent', color: color.ink,
              fontFamily: font.mono,
              fontSize: 13, lineHeight: 1.55, padding: 16,
              boxSizing: 'border-box', border: 'none', outline: 'none', resize: 'none',
            }}
          />
        </div>
      </div>
    );
  }

  const current = slides[safeIdx] || '';

  return (
    <div style={wrap}>
      <div style={toolbar}>
        <div style={{ display: 'flex', gap: 6 }}>
          <button style={btn(safeIdx === 0)} onClick={prev} disabled={safeIdx === 0}>‹ prev</button>
          <button style={btn(safeIdx >= slides.length - 1)} onClick={next} disabled={safeIdx >= slides.length - 1}>next ›</button>
        </div>
        <div style={{ fontFamily: font.mono, fontSize: 11.5, color: color.ink3 }}>
          {slides.length === 0 ? 'no slides' : `${safeIdx + 1} / ${slides.length}`}
        </div>
        {!isReadOnly && (
          <button style={btn()} onClick={() => setEditing(true)}>edit source</button>
        )}
      </div>
      <div style={SLIDES_STAGE}>
        <div
          style={SLIDE_CARD}
          dangerouslySetInnerHTML={{ __html: renderSlideMarkdown(current) }}
        />
      </div>
    </div>
  );
}

export default defineRenderer({ component: SlidesView });
