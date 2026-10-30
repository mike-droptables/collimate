# @collimate/renderer-hooks

React hooks for Collimate renderers — the theme contract, document snapshots, and remote module / script loading with dedupe.

## Status

The SDK owns these hooks and renderer primitives. Its pinned build generates the module the app serves at `/api/sdk/react.js`. Renderers running inside the app sandbox import them from there and do **not** need this package.

This npm artifact exists so:

- The hook surface has a single source-of-truth spec, independently versioned.
- Out-of-sandbox tooling (tests, Storybook, standalone prototypes) can consume the hooks without reaching the virtual SDK URL.
- Third-party tooling can typecheck against the stable API.

If you are writing a renderer that ships in a Collimate cell, keep importing from `/api/sdk/react.js`.

## Install

```bash
npm install @collimate/renderer-hooks
```

React19 and ReactDOM19 are peer dependencies. The served module and shipped renderers share the exact19.3.1 CDN identity.

## Hooks

### `useTheme(api)`

```tsx
const { themeId, scheme, known, vars, fonts, onChange } = useTheme(api);
// themeId: 'night' | 'night-cool' | 'paper' | 'user.<name>'
// scheme:  'dark' | 'light'
// known:   false only for the placeholder before the host's first theme
// vars:    every skin token by CSS name, e.g. vars['--sheet-2']
// fonts:   { ui, mono, prose } font-family stacks
// onChange(cb): cb(theme) on subscribe and on every change; returns unsubscribe
```

The state updates on every skin change. The callback fires with the current record on subscribe, so library theme state initialises in a single `useEffect`.

### `tokens`

```tsx
<div style={{ background: tokens.color.sheet, color: tokens.color.ink,
              border: `1px solid ${tokens.color.line}`, borderRadius: tokens.radius,
              fontFamily: tokens.font.ui }} />
```

The skin tokens as frozen `var(--…)` strings: `tokens.color.{canvas, sheet, sheet2, sheet3, line, line2, ink, ink2, ink3, brass, brassSoft, teal, tealSoft, tealSheet, red, redSoft, green, codeBg, proseInk}`, `tokens.font.{ui, mono, prose}`, `tokens.radius`, `tokens.shadow`, `tokens.lift`. The host writes every token on the renderer's `:root`, and custom properties inherit into a shadow root, so these work in inline styles in both embed modes. Brass marks the selection only; teal is the live/on-state; red is destructive; green is pass.

### `themeCss(theme, { fontsUrl? })`

```ts
frame.srcdoc = `<style>${themeCss(api.getTheme(), {
    fontsUrl: new URL('/fonts/fonts.css', location.href).href,
})}</style>…`;
```

CSS text for a frame the renderer owns (a `srcdoc` or sandboxed child document, which the host's variables do not reach): an `@import` of `fontsUrl` when given, then `:root{…}` with every token and `color-scheme`. Rebuild it from `onChange` to follow skin changes.

### `useEditing(api)`

```tsx
const { spellcheck, onChange } = useEditing(api);
// spellcheck: underline misspelled words (Settings → Editing)
// onChange(cb): cb(editing) on subscribe and on every change; returns unsubscribe
```

The user's editing preferences, mirroring `useTheme`. The host already sets `spellcheck` on the renderer's document, so a plain `<textarea>` or contenteditable follows it with no code; read this for an editor that sets its own attribute or runs in a frame of its own, and pass the flag through. An api without the editing members reads as spellcheck on.

### `useStyle(api)`

```tsx
const { theme, font, textSize, set, onChange } = useStyle(api);
// theme:    one of the theme ids the renderer offers, or null (the skin)
// font:     a family name from api.getFontFamilies(), or null (the skin's font for the role)
// textSize: a factor (1 = 100%), or null (the renderer's own size)
// set(patch): change some fields; null puts one back to the default
// onChange(cb): cb(style) on subscribe and on every change; returns unsubscribe
```

This renderer's style, mirroring `useEditing`. The host persists the record per channel under the renderer's id, so every file the renderer shows in the channel shares it, and pushes it back on every change. A null field is the renderer's default, which the renderer resolves itself (`textSize ?? 1.1`; the stack of the family named by `font`, else `var(--font-prose)`). An api without the style members reads all-null and `set` does nothing.

### `StyleControl`

```tsx
<div style={{ position: 'relative' }}>
    <StyleControl api={api}
        themes={[{ id: 'skin', label: 'Collimate' }, { id: 'nord', label: 'Nord' }]}
        font="prose" size={{ min: 0.7, max: 2.2, step: 0.1, default: 1.1 }} />
    …
</div>
```

The shared style icon: 24px, top right, shown only while the pointer is near that corner (within 40px of the icon), while it has keyboard focus (at rest it is transparent but stays in the tab order) or while its popover is open; never merely because the pointer is over the renderer. The control judges the reach itself: it measures pointer moves over `watch` (a ref; by default its parent element) against the icon and hides when the pointer leaves that element or enters a child frame (other than its `frame`). Pass `watch` your root's ref when the control sits in a smaller box (a toolbar row, a header). Over a frame filling the box (an authored page swallows every move) pass the frame's ref as `frame` and build the page with `useDocumentView`'s `reportPointer`: the page posts its pointer position and the control measures those reports, with nothing laid over the page. The popover offers Theme (the first entry is the default, stored as null), Font (the host's families for the role, plus "Skin default") and Text size (A− / percent / A+, Reset), each row only when asked for (`themes` with two or more entries, `font` a role, `size` a range). It writes through `api.setStyle`. Under those rows `extra` adds the renderer's own view controls (a node, or a function of `{ close }`), each laid out with `StyleControlRow({ label, children })` so it reads like the style rows; a renderer that offers no style passes `themes={[]} font={false} size={false}` and the popover holds only its rows (the PDF viewer's page, zoom and pointer rows, instead of a bar of its own). Inline styles on skin tokens, so it works in a shadow root and in an iframe; Esc or a press outside closes it. `style` overrides the anchor.

```tsx
<StyleControl api={api} themes={[]} font={false} size={false} extra={
    <StyleControlRow label="Page">
        <button onClick={prev}>‹</button> {page} / {total} <button onClick={next}>›</button>
    </StyleControlRow>
} />
```

### `CELL_FILES_MIME`, `isCellFileDrag(dt)`, `readCellFileDrop(dt, api)`

```tsx
el.addEventListener('dragover', (e) => {
    if (isCellFileDrag(e.dataTransfer)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
});
el.addEventListener('drop', (e) => {
    const paths = readCellFileDrop(e.dataTransfer, api);   // null: not this cell's files
    if (paths) { e.preventDefault(); insert(paths); }
});
```

A drag from the files pane carries `{cellId, paths}` as JSON under `CELL_FILES_MIME` (`'application/x-collimate-files'`). `isCellFileDrag` reads only the drag's types, so it answers in `dragover`; `readCellFileDrop` parses the payload at the drop and returns the cell-relative paths only when they are this renderer's cell's (`api.getCellId()`).

### `resolveVar(theme, name)` and `toHex8(color)`

For libraries that need concrete colours rather than `var()` (Monaco's `defineTheme`, Mermaid's `themeVariables`): `resolveVar(theme, '--sheet')` is the token's value (`''` when absent), and `toHex8('rgba(198, 157, 100, 0.16)')` is `'#c69d6429'` (`null` for anything but hex and `rgb()`/`rgba()`).

### `syntaxPalette(theme)` and `SYNTAX_ROLES`

```ts
const palette = syntaxPalette(api.getTheme());   // null until the first skin arrives
monaco.editor.defineTheme('collimate', { base, inherit: palette === null,
    rules: palette ? [{ token: 'keyword', foreground: palette.keyword }, …] : [], colors });
```

Code-highlighting colours derived from the skin, so every highlighter draws code alike. Each role in `SYNTAX_ROLES` (`comment, keyword, string, number, type, function, variable, operator, punctuation, tag, attribute, regexp, invalid, added, removed, modified`) is a lowercase `#rrggbb`: comment is `--ink-3`; punctuation and operator `--ink-2`; variable `--ink`; keyword and tag `--teal`; string and added `--green`; number, attribute and modified `--brass`; invalid and removed `--red`; type, function and regexp are teal, brass and green one OKLCH lightness step further from the ground. Every role is nudged away from the ground (lighter on a dark `--sheet`, darker on a light one, read from the sheet's luminance rather than the record's `scheme`) until its WCAG contrast is at least 3:1 on both `--sheet` and `--code-bg`; a role that reaches black or white short of the floor is nudged the other way, and failing both takes `--ink`. A translucent token is composited over `--sheet`. A token the helper cannot read (`hsl()`, `oklch()`, a named colour, as a user skin may write) gives way to the next in the role's chain: `--ink-2` for comment, `--ink` for every other role. The record is frozen; it is `null` only when `--sheet` or `--ink` cannot be read (the placeholder record names no tokens), and a highlighter then keeps its library's own colours.

### `useDesktop(api)`

```tsx
const desktop = useDesktop(api);   // DesktopSurface | undefined

desktop ? desktop.openExternal(href) : window.open(href, '_blank');
```

Returns `api.desktop` — the slice of the Electron desktop shell the host
attaches, or `undefined`. It is `undefined` in a browser tab **and** in a
sandboxed `embed: "iframe"` renderer (handing untrusted iframe source a shell
handle would cross the sandbox boundary), so one fallback path covers both;
that is why this is not a boolean `isDesktop()`.

Individual capabilities live on `desktop.capabilities` — test **truthiness**,
never presence: a browser tab has no capability map at all. Where
`capabilities.nativeGuests` is set the host also attaches
`desktop.mountGuest(hostEl, opts)`, which hosts a real Chromium view in the
cell; see the renderer how-to (`shared/docs/how-to-write-a-renderer.md`) for
that surface.

### `useVisibility(api)`

```tsx
const { visible } = useVisibility(api);
useEffect(() => { if (visible) repaintMyCanvas(); }, [visible]);
```

Tracks whether the cell is on screen, so a canvas renderer can repaint a buffer the browser dropped while it was hidden. An api without the visibility methods (iframe embed) reads as always visible. See "Reacting to visibility" in `shared/docs/how-to-write-a-renderer.md`.

### `useHostFocus(api)`

```tsx
const { focused } = useHostFocus(api);
// gate any window-level keydown/wheel listener on `focused`
```

Tracks whether the cell holds the canvas focus. Native renderers share the main document, so a window-level input listener must gate on it. An api without the focus methods (iframe embed) reads as always focused. See the `RendererAPI` focus members in `shared/docs/how-to-write-a-renderer.md`.

### `useAnnotationRequest(api, handler)` and `annotationUnavailable(message)`

```tsx
const { api, fileName } = useCollimateFile();
useAnnotationRequest(api, useCallback(async (request, signal) => {
    if (request.mode !== 'selection') throw annotationUnavailable('Select some text first.');
    const text = window.getSelection()?.toString() || '';
    return text ? { label: `${fileName} › selection`, text } : null;
}, [fileName]));
```

Answers the host's annotation requests (`api.onAnnotationRequest`): a user attaching part of a file to a message for a run. Resolve `{ label, text, anchor? }`, `null` when the user abandons a pick, or reject with `annotationUnavailable(message)` when this view cannot answer (the host shows `message`); stop when `signal` aborts. One handler per view: the hook subscribes once per `api` and always calls the latest `handler`, and `null` withdraws it. Inert where the host has no `onAnnotationRequest` (a sealed preview). See *Answering annotation requests* in `shared/docs/how-to-write-a-renderer.md`.

### Document snapshots

Call `useCollimateFile().saveContent(text)` on every document change. The host
journals the snapshot locally and coalesces remote writes. All hosts deliver
primary-file replacements through `api.onSetContent`; keep the editor mounted
and apply those values in place. See the canonical renderer guide for the
conflict and serialization-barrier contract.

### `useRemoteModule(url)`

```tsx
const { module, loading, error } = useRemoteModule('https://esm.sh/mermaid@12.2.0?target=es2022');
```

Dynamic ESM import with a module-level cache (multiple renderers loading the same URL share one promise). The cache keys on the URL, so pin `?target=es2022` on esm.sh URLs — unpinned, esm.sh varies the build by User-Agent and callers can end up on different module instances. The host's import map routes the import (and the module's whole graph) through the app's module mirror; pass the upstream URL.

### `useRemoteScript(url)`

```tsx
const { loading, error } = useRemoteScript('https://.../loader.js');
```

Dynamic `<script>` tag loader with dedupe. For UMD-style bundles that expose themselves on `window` (e.g. the monaco loader). A `<script>` is not an ES import, so pass `mirrorUrl(url)` for an esm.sh or jsdelivr script.

### `mirrorUrl(url)`

```tsx
import { mirrorUrl } from '/api/sdk/react.js';
const wasmUrl = mirrorUrl('https://cdn.jsdelivr.net/npm/@embedpdf/pdfium@2.15.1/dist/pdfium.wasm');
```

The app mirrors `https://esm.sh/` and `https://cdn.jsdelivr.net/` on its own origin, at `<api>/mirror/<host>/…` beside the served runtime (`<api>/sdk/react.js`); document import maps route ES imports there. `mirrorUrl` maps a load no import map covers (a `<script>`, a fetched stylesheet, a font or a library's asset path, wasm, a worker URL) to that same-origin copy. Any other URL is returned unchanged, and so is every URL where the runtime is not served by the app (a sealed preview, Node tests). `api.loadCss(href)` applies the same mapping. Exported by the served runtime only (`/api/sdk/react.js`, typed in `platform.d.ts`), not by the npm package, which has no app beside it.

## Session sockets — `./session`

`openSessionSocket`, `sessionWsUrl`, `apiOrigin`, `isSessionDead`, `clearDeadSession` — the one WebSocket reconnect state machine every pointer renderer (terminal, assistant, webview) builds on. Framework-free (no React import); the platform serves it at `/api/sdk/session.js` so renderers that pin their own React version can import it without pulling a second React.

The close-code policy is centralized: 4401/4404 end the socket for good (the session id is marked dead so remounts don't redial), 4409 polls slowly to take over a single-attach session, everything else retries per `transientRetryMs` (default backoff). See `src/session.ts` for the full option set and the renderer how-to (`shared/docs/how-to-write-a-renderer.md`) for usage.

```ts
import { openSessionSocket, sessionWsUrl } from '@collimate/renderer-hooks/session';
```

## Host-mounted exports

`defineRenderer`, `useCollimateFile`, and `useSiblingFile` are implemented in `src/platform.tsx` and exported by this package. The generated `/api/sdk/react.js` module uses that same implementation; `defineRenderer` mounts the context around your component:

- `CollimateFile`, `RendererAPI`, `RendererTheme`, `RendererEditing`, `RendererStyle`, `RendererFontFamily`, `FontRole`, `ThemeVarName`, `ThemeFonts`, `Scheme`, `SiblingFileState`, `DefineRendererConfig`, `RendererDefinition`, `AnnotationRequest`, `AnnotationContext` are exported from the package root for `import type` use — as are the desktop-shell types `DesktopCapabilities`, `DesktopSurface`, `GuestOptions`, `GuestHandle`, and `GuestEventData` (what `mountGuest` takes and returns).
- `platform.d.ts` types the `/api/sdk/react.js` module so renderer `.tsx` files typecheck under plain `tsc`. TypeScript forbids ambient declarations for rooted paths, so map the specifier via `paths`:

```jsonc
// tsconfig.json
{
  "compilerOptions": {
    "paths": {
      "/api/sdk/react.js": ["./node_modules/@collimate/renderer-hooks/platform"]
    }
  }
}
```

## Build

```bash
npm run build     # tsup → dist/index.js (ESM), dist/index.cjs (CJS), dist/index.d.ts
npm run typecheck # tsc --noEmit
# From the SDK root, install both pinned build dependencies and generate the served runtime/common snapshot:
npm ci --prefix scripts
npm ci --prefix packages/renderer-hooks
node scripts/build_renderer_runtime.mjs
# Check generated bytes and source identities without changing files:
node scripts/build_renderer_runtime.mjs --check
```

## License

MIT

### `MediaViewer({ kind })`

Shared native media UI for the shipped audio and video renderers. Pass `kind: 'audio' | 'video'` inside a `defineRenderer` component; the shared viewer reads the current file capability URL, handles errors and Retry, and renders the common footer using inline styles. Audio/video retain their distinct layout and controls. See the canonical renderer guide for the complete behavior.
