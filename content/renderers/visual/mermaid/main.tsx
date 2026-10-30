import React, { useState, useEffect, useMemo, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import mermaid from 'https://esm.sh/mermaid@12.2.0?target=es2022';
import {
    defineRenderer,
    resolveVar,
    toHex8,
    tokens,
    useCollimateFile,
    useTheme,
} from '/api/sdk/react.js';

// Mermaid cannot read CSS variables: its `base` theme takes concrete
// colours and derives the rest from them. The variables come from the skin
// record's resolved values; `darkMode` is the scheme switch Mermaid needs to
// derive its own shades the right way round. Mermaid's theme is global (set
// by mermaid.initialize), so the view re-initialises and re-renders whenever
// the record changes. A token the record lacks is left to Mermaid.
type ThemeRecord = {
  scheme: 'dark' | 'light';
  vars: Record<string, string>;
  fonts: { ui: string; mono: string; prose: string };
};

/** A resolved colour as #rrggbb (#rrggbbaa when not opaque), or undefined. */
function colour(theme: ThemeRecord, name: string): string | undefined {
  const hex = toHex8(resolveVar(theme as any, name as any));
  if (!hex) return undefined;
  return hex.endsWith('ff') ? hex.slice(0, 7) : hex;
}

export function mermaidThemeVariables(theme: ThemeRecord): Record<string, string | boolean> {
  const vars: Record<string, string | boolean | undefined> = {
    darkMode: theme.scheme !== 'light',
    background: colour(theme, '--sheet'),
    primaryColor: colour(theme, '--sheet-2'),
    primaryTextColor: colour(theme, '--ink'),
    primaryBorderColor: colour(theme, '--line-2'),
    lineColor: colour(theme, '--ink-3'),
    secondaryColor: colour(theme, '--sheet-3'),
    tertiaryColor: colour(theme, '--sheet'),
    // The base theme hard-sets or derives these outside the skin (yellow
    // notes, grey lifelines, white gantt bands, dark-mode edge-label boxes
    // darkened from secondaryColor), so each is named from the record.
    textColor: colour(theme, '--ink'),
    noteBkgColor: colour(theme, '--sheet-3'),
    noteTextColor: colour(theme, '--ink'),
    noteBorderColor: colour(theme, '--line-2'),
    actorLineColor: colour(theme, '--line-2'),
    edgeLabelBackground: colour(theme, '--sheet'),
    sectionBkgColor: colour(theme, '--sheet-2'),
    altSectionBkgColor: colour(theme, '--sheet'),
    fontFamily: (theme.fonts && theme.fonts.ui) || undefined,
  };
  return Object.fromEntries(
    Object.entries(vars).filter((entry): entry is [string, string | boolean] => entry[1] !== undefined),
  );
}

export function mermaidConfig(theme: ThemeRecord) {
  const themeVariables = mermaidThemeVariables(theme);
  return {
    startOnLoad: false,
    theme: 'base',
    securityLevel: 'strict',
    // Mermaid 12 lays flowcharts out with ELK and draws its new look by
    // default; dagre and the classic look keep diagrams as they were drawn.
    layout: 'dagre',
    look: 'classic',
    ...(themeVariables.fontFamily ? { fontFamily: themeVariables.fontFamily as string } : {}),
    themeVariables,
  };
}

// The pane heads: the mock's 11px uppercase section head on a 30px strip.
const headStyle = {
  height: 30, flex: 'none', boxSizing: 'border-box' as const,
  display: 'flex', alignItems: 'center', padding: '0 12px',
  fontFamily: tokens.font.ui, fontSize: 11, fontWeight: 600,
  letterSpacing: '0.08em', textTransform: 'uppercase' as const,
  color: tokens.color.ink3, background: tokens.color.sheet,
  borderBottom: `1px solid ${tokens.color.line}`,
};

// A selection is the one thing brass marks; ::selection needs a stylesheet.
const SELECTION_CSS = 'textarea::selection { background: var(--brass-soft); }';

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

function MermaidView() {
  const { content, saveContent, isReadOnly, api } = useCollimateFile();
  const { scheme, vars, fonts } = useTheme(api);
  // One string per distinct theme: the render effect re-runs only when the
  // colours or the font actually change.
  const configJson = useMemo(
    () => JSON.stringify(mermaidConfig({ scheme, vars, fonts })),
    [scheme, vars, fonts],
  );
  const save = saveContent;
  const [local, setLocal] = useState(content);
  const [svg, setSvg] = useState('');
  const [error, setError] = useState('');
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2, 10)}`);
  const areaRef = useRef(null);
  const renderNonce = useRef(0);

  useEffect(() => { setLocal(content); }, [content]);

  useEffect(() => {
    if (api && api.injectCss) api.injectCss(`${SELECTION_CSS}\n${SOURCE_AREA_FALLBACK_CSS}`);
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    const src = (local || '').trim();
    if (!src) { setSvg(''); setError(''); return; }
    const timer = setTimeout(() => {
      mermaid.initialize(JSON.parse(configJson));
      // A fresh id for every render prevents cached diagrams/theme reuse.
      const renderId = `${idRef.current}-${++renderNonce.current}`;
      // Mermaid uses a temporary live DOM for layout, including on parse
      // failure. Own that container so its error drawing never escapes the
      // renderer's retained preview into the host document.
      const container = document.createElement('div');
      container.style.cssText = 'position:absolute;left:-100000px;top:0';
      container.setAttribute('aria-hidden', 'true');
      document.body.appendChild(container);
      mermaid
        .render(renderId, src, container)
        .then(({ svg }) => { if (!cancelled) { setSvg(svg); setError(''); } })
        .catch((e) => { if (!cancelled) setError(String(e && e.message || e)); })
        .finally(() => container.remove());
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [local, configJson]);

  return (
    <div style={{ display: 'flex', width: '100%', height: '100%', background: tokens.color.sheet, color: tokens.color.ink }}>
      {!isReadOnly && (
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', borderRight: `1px solid ${tokens.color.line}` }}>
          <div style={headStyle}>Source</div>
          <div style={SOURCE_SCROLL} onMouseDown={(e) => sourceCaretToEnd(e, areaRef.current)}>
            <textarea
              ref={areaRef}
              data-source-area=""
              value={local}
              onChange={(e) => { setLocal(e.target.value); save(e.target.value); }}
              spellCheck={false}
              style={{
                display: 'block', width: '100%', minHeight: '3em', fieldSizing: 'content', overflow: 'hidden',
                background: 'transparent', color: tokens.color.ink, caretColor: tokens.color.ink,
                fontFamily: tokens.font.mono,
                fontSize: 13, lineHeight: 1.55, padding: 14,
                boxSizing: 'border-box', border: 'none', outline: 'none', resize: 'none',
              }}
            />
          </div>
        </div>
      )}
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={headStyle}>Preview</div>
        <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 16, boxSizing: 'border-box', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
          {error && (
            <pre role="alert" style={{ color: tokens.color.red, fontFamily: tokens.font.mono, fontSize: 12, whiteSpace: 'pre-wrap', maxWidth: '100%' }}>
              {error}
            </pre>
          )}
          {svg ? (
            <div style={{ maxWidth: '100%' }} dangerouslySetInnerHTML={{ __html: svg }} />
          ) : (
            <div style={{ color: tokens.color.ink3, fontFamily: tokens.font.ui, fontSize: 12 }}>(empty)</div>
          )}
        </div>
      </div>
    </div>
  );
}

export default defineRenderer({ component: MermaidView });
