# How to Write a Collimate Renderer

This is a comprehensive reference for building renderers in Collimate. It contains every detail needed to write a renderer from scratch, including the full SDK API, manifest format, styling constraints, and real-world patterns.

## What Is a Renderer?

A renderer is an **ES module** (TypeScript/React or vanilla JS) that displays and optionally edits a file in the Collimate workspace. Renderers:

- Are matched to files by extension (e.g., `.md` files open in the markdown renderer)
- Can be read-only viewers or full editors with two-way content sync
- Can optionally render diff views (before/after comparison)
- Can load external libraries from CDNs
- Run in one of two hosting modes: **native** (Shadow DOM) or **iframe** (sandboxed)

A renderer package runs after the user adds it to a cell of the Capabilities stack.

## CRITICAL: Styling Constraints

**Native renderers run inside Shadow DOM.** This means:

- **className and Tailwind classes DO NOT WORK** - the host page's stylesheets do not cascade into the shadow root
- **You MUST use inline `style={}` attributes** for all styling
- Colour, font and radius come from the skin tokens: `color: 'var(--ink)'`, or the same reference from the SDK `tokens` table (`tokens.color.ink`). The host writes every token on `:root` and custom properties inherit into the shadow root; host Tailwind utilities do not cross the shadow boundary. Write no colour literal (hex, `rgb()`, `hsl()`) and no `var(--token, #hex)` fallback: the host supplies every token (an iframe renderer receives them right after it signals ready). See *Theming your renderer*.
- Size app-like controls and their typography and spacing in `rem` when they should follow the interface's root type size. A native renderer's `rem` resolves against the app document; the sandbox iframe starts at the same root type size with a low-specificity rule that your renderer may override. Keep hairline borders in device-independent `px`, and keep authored media dimensions and measurements used for pointer or viewport geometry in their original units. A renderer's Text size choice is a separate factor for its content; it does not change the host's root type size.
- If you need CSS classes, inject them via `api.injectCss()` or `api.loadCss()`
- **There is NO CSS reset in the shadow root — box model defaults to `content-box`.** This is a real footgun: an element with `width: 100%` *and* padding/border renders **wider than its parent** and overflows (the classic symptom is an editor or input spilling past the right edge on a narrow cell). The host cannot inject a global reset for you — that would silently reflow every already-shipped renderer (see the frozen-renderer rule below). So inject a `box-sizing` reset once, from a `useEffect` at the **top level of your component** — get `api` from `useCollimateFile()` at the top, and NEVER call a hook (`useCollimateFile`, `useEffect`) inside another hook's callback (a Rules-of-Hooks violation that crashes at mount — and the offline compile check will NOT catch it, see `testing-generators.md`):

  ```jsx
  function MyRenderer() {
    const { content, api } = useCollimateFile();     // hooks at the top, always
    React.useEffect(() => {
      api.injectCss(`*, *::before, *::after { box-sizing: border-box; }`);
    }, [api]);
    // …render with inline styles; width: 100% now behaves as expected.
  }
  ```

  Do this in any renderer with padded, full-width elements (editors, inputs, cards).

**Iframe renderers** have full CSS isolation and can use any styling approach (classes, Tailwind, external sheets). However, they have higher overhead due to postMessage communication.

**Rule of thumb**: Use `embed: "native"` with inline styles for simple/lightweight renderers. Use `embed: "iframe"` when you need external CSS frameworks or full DOM isolation.

## Renderer and generator contracts

Renderer source and generator source are seeded into channel cells. A breaking
contract change updates both sources, their docs and tests together; recreate
the channel with the documented reset flow before checking the new contract.
Do not retain compatibility branches or deprecated message shapes.

## File Structure

Renderers are organised into **groups**. Every first-party renderer lives in its own directory under `content/renderers/<group>/` of the Collimate repository:

```
renderers/
  documents/                   # group folder
    cell.yaml                  # OPTIONAL: the seeded cell's name, icon, order
    monaco/
      monaco.renderer         # REQUIRED: metadata, extensions, embed mode
      main.tsx                 # REQUIRED: the entrypoint module
    milkdown/
      ...
  visual/
    mermaid/
      ...
```

A group is just a top-level subdirectory. Folder location is the only thing that determines group membership — an optional `cell.yaml` in the group folder sets the seeded cell's display name, icon and order. A new channel created from a suggestion (`content/suggested/<id>.json`) seeds each group the suggestion lists as one cell of the channel's Capabilities stack, so a channel from the Standard suggestion comes up with one cell per group (e.g. "Documents"), each containing the source for its members.

To add a new group, create a new top-level subdirectory under `content/renderers/` and list it in `content/suggested/standard.json` as a `[name, folder]` pair whose name is the group's display name (the SDK tests refuse a group the Standard suggestion leaves out). To add a renderer to an existing group, drop a new subdirectory under that group's folder. A package holds exactly one definition file across all four kinds (`.generator`, `.renderer`, `.runner`, `.handler`), which all establish discovery boundaries. A renderer package holds a `.renderer` definition; its filename and folder need not match the declared `id`. Stack discovery takes the shallowest definitions, skips top-level `_runs/`, and refuses definitions at the cell root. Everything below a package folder is package content: a nested definition is not discovered as a second package. An ambiguous folder establishes the same boundary and is refused. Entrypoints resolve relative to their package folder.

### Built-in cells

The packages the app's own code names live in a second tree,
`builtins/` at the repository root, laid out the same way: `definitions/` holds the
definition, options and Git checkout handlers and the sealed preview renderer
(`renderer_example`), `live/` the run, assistant, terminal, website and webview
surfaces, and `text/` the plain text renderer, which opens every text file no
other renderer claims. The app serves it for every channel: its folders are the Definitions, Live and Text
cards at the head of the Capabilities stack, after its Manage view. They are never seeded, suggested, exported or copied, and they
cannot be removed, moved, duplicated or edited. A built-in renderer is always
available; renderer preferences still apply to it.

A channel's renderer cannot use a built-in renderer's id or claim a surface
extension (`.run`, `.chat`, `.terminal`, `.webview`, `.website`,
`.renderer_example`); Add to Capabilities and import hold such a package and report it, and one that reaches Capabilities another way (a moved cell, a direct edit) is left out of the renderer catalog and listed under Manage's problems. On
any other extension a channel's renderer wins over a built-in one, so `text` is
the floor.

## Step 1: Write the my-renderer.renderer

### Full Definition Schema

```yaml example=renderer-full-schema
# REQUIRED FIELDS
id: "my-renderer"              # Unique identifier (lowercase, hyphens ok)
name: "My Renderer"            # Display name in the UI
summary: "Short description of what this renderer does."   # 1–2 sentences
entrypoint: "main.tsx"         # Entry file (.tsx, .ts, or .js)

# OPTIONAL FIELDS
version: "1.0.0"               # Display version string
embed: "native"                # "native" = Shadow DOM (inline styles only!)
                               # "iframe" = sandboxed iframe (any CSS ok)

# FILE MATCHING — every extension must start with a dot.
extensions:                    # File extensions this renderer handles
  - ".myext"
  - ".other"

# DEFAULTS — the files this renderer is a default for: each entry one of
# its `extensions`, or "*".
default_for:                   # ".myext": it opens .myext files ahead of any
  - ".myext"                   # other renderer claiming the extension (the
                               # channel's preferences still win); an image
                               # or a PDF opens only in its default, else in
                               # the binary view. "*": "Open with" offers it
                               # for every text file, claimed or not (the
                               # code editor and plain text declare it).

# DIFF SUPPORT
supports_diff: true            # true if you provide a diff view component

# LIVE REFRESH
live_refresh: true             # declare ONLY if the renderer refetches its
                               # own file (and any sidecars) on the host's
                               # FILES_CHANGED broadcast and swaps content in
                               # place. Hosts then never force-remount it to
                               # show an external write (a generator
                               # streaming into the cell mid-run), so UI
                               # state — sidebars, compose text, scroll —
                               # survives every write. Without it, hosts fall
                               # back to a remount with fresh content.
```

### Real Examples

**Simple native text editor:**
```yaml example=renderer-manifest-text
id: "text"
name: "Text Editor"
version: "1.1.0"
summary: "Plain-text viewer and editor powered by the React SDK."
entrypoint: "main.tsx"
embed: "native"
extensions:
  - ".txt"
  - ".log"
  - ".env"
  - ".gitignore"
  - ".cfg"
  - ".ini"
  - ".csv"
  - ".sh"
supports_diff: true
```

**Iframe renderer with external library:**
```yaml example=renderer-manifest-html
id: "html"
name: "HTML Renderer"
version: "2.0.0"
summary: "Renders HTML full-page with in-cell sibling assets resolved (revision-pinned, binary-safe)."
entrypoint: "main.tsx"
embed: "iframe"
extensions:
  - ".html"
  - ".htm"
supports_diff: true
```

**Native renderer with no editing (viewer only):**
```yaml example=renderer-manifest-website
id: "website"
name: "Website Viewer"
version: "1.1.0"
summary: "Renders a URL in a sandboxed iframe."
entrypoint: "main.tsx"
embed: "native"
extensions:
  - ".website"
supports_diff: false
```

**Fallback renderer (no extensions = only used when explicitly selected):**
```yaml example=renderer-manifest-markdown
id: "markdown"
name: "Markdown Renderer"
version: "1.1.0"
summary: "Fallback markdown renderer."
entrypoint: "main.tsx"
embed: "native"
extensions: []
supports_diff: true
```

## Step 2: Write the Renderer Module

### The SDK

Renderers use the React SDK — one supported surface, served by the host at `/api/sdk/react.js`.

The implementation lives in the SDK's `packages/renderer-hooks/src/`. The pinned
`scripts/build_renderer_runtime.mjs` generates the self-contained served modules
under `shared/renderer-runtime/`. The host serves these generated assets from
the installed SDK rather than maintaining a separate runtime implementation; an
installed SDK carries them prebuilt, so serving them needs no Node or runtime
compiler.

```typescript
import React from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile } from '/api/sdk/react.js';
```

(An older `vanilla.js` SDK existed for imperative DOM mounting. It has been removed — wrap imperative renderers in a thin React component using a ref + `useEffect` instead.)

### Imports

Renderers use ES module imports. You write no build step: the host serves the bundle for the current package hash when cached. After an edit, the parent loads the exact package bytes and compiles them in a worker using the pinned local esbuild-WASM and canonical SDK compiler policy, then publishes the bundle for that hash. Compilation failures remain visible and never run stale source.

```typescript
// React from CDN
import React, { useState, useEffect, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';

// Collimate React SDK (provides hooks and defineRenderer)
import { defineRenderer, useCollimateFile, useSiblingFile } from '/api/sdk/react.js';

// External libraries from CDN
import SomeLib from 'https://esm.sh/some-library@1.0.0?target=es2022';
```

**Important**: Always use `https://esm.sh/` for React and other npm packages. Always use `/api/sdk/react.js` for the Collimate SDK (root-relative path).

Write upstream URLs as they are. `https://esm.sh/` and `https://cdn.jsdelivr.net/` are the two upstreams the app mirrors: the host document's import map sends every ES import of either (static, `import()`, and every import inside the graph) to the app's module mirror on its own origin, `<api>/mirror/<host>/…`, so a bundle never names the mirror. The mirror serves the shipped renderers' whole graph from the SDK's frozen snapshot, offline. Anything else it fetches from upstream once and keeps: an exact-version URL for good, a range or moving tag as first fetched. So pin an exact version on every upstream URL; the shipped renderers are held to it (`tests/test_renderer_cdn_pins.py`). A load that is not an ES import goes through `mirrorUrl` (*Loading External Libraries*).

**Important**: Always pin `?target=es2022` on every esm.sh import (`&target=es2022` if the URL already has a query string). Without it, esm.sh derives the JS build target from the browser's User-Agent, so the same package specifier can resolve to a different build — and therefore a different module instance — depending on who is asking. That silently breaks anything that depends on two imports sharing one instance: React shared with `/api/sdk/react.js`, ProseMirror `Plugin`/`Decoration` classes shared with an editor, any library with module-level state. Pin every esm.sh import in a file, or none — a half-pinned file is the case that actually splits.

**Important**: Pin a library's family with `deps=`. esm.sh builds a package's own dependencies from their declared ranges, to whatever is newest when it builds, so an exact version on the URL does not pin what that package imports. A library that imports React or shares a package with another import names those versions in one `deps=` list: `https://esm.sh/some-react-lib@1.0.0?deps=react@19.3.1,react-dom@19.3.1&target=es2022`. esm.sh resolves each URL's imports against the list *that URL* declares, so every import that touches a shared package (React, shared with `/api/sdk/react.js`; ProseMirror; a plugin family's core) carries the file's one list, byte for byte; a lower layer may keep a leading run of it. A stale version, another order or a package dropped from the middle is a second module instance that mounts fine and throws on first use. A URL with no `deps=` is deliberate: it lands on the plain build a bundled dependency already imports, so say why in a comment. The mirror keeps what a list resolved: for the shipped renderers the SDK's frozen snapshot, for anything else its first fetch.

## React SDK API

### defineRenderer()

Wraps your React component(s) and exports the mount/unmount lifecycle:

```typescript
export default defineRenderer({
    component: MyEditorComponent,           // REQUIRED: main view
    diffComponent: MyDiffComponent,         // OPTIONAL: diff view
    deferReady: true,                       // OPTIONAL: keep the renderer hidden until you
                                            // call api.ready() (avoid flashing a half-painted editor)
});
```

### useCollimateFile() Hook

The primary hook. Returns everything you need to read/edit the file:

```typescript
const {
    initialContent,  // string: file content when renderer first opened (never changes)
    content,         // string: current synced content (updates on external changes)
    saveContent,     // (newContent: string) => void: push edits back to the workspace
    isReadOnly,      // boolean: true if file should not be editable
    fileName,        // string: name of the file being rendered (e.g., "output.md")
    api,             // RendererAPI: full API object (see below)
    diffContent,     // string | null: previous version content (only in diff mode)
    diffLabels,      // { base, current } | null: host names for the two diff sides (diff mode)
} = useCollimateFile();
```

### MediaViewer component

`MediaViewer({ kind: 'audio' | 'video' })` is the shared React component used by the shipped audio and video renderers. Import it from `/api/sdk/react.js`, render `<MediaViewer kind="audio"/>` or `<MediaViewer kind="video"/>` inside your `defineRenderer` component, and let `useCollimateFile` supply the current file and its capability URL. It uses inline styles for native shadow DOM and paints only from the skin tokens (a `--sheet` ground, a `--sheet-2` footer on a `--line` rule, the ink tiers), with no colour literal.

Both kinds retain native playback controls, the filename/extension footer, an error message and Retry. Retry clears the error and replaces the media element; a changed file URL clears an old error. Audio keeps its filename heading and centered padded column; video keeps its contained player with `playsInline`. The component does not add autoplay or grant writes.

### The document view: an authored HTML page in your own frame

`useDocumentView({ html, api, theme, dark, prepare, variables })` is how the shipped `html` renderer shows an HTML document someone else authored, so a file is shown, darkened and recoloured the same way in any renderer that uses it. Use it for any preview of an authored page. It owns everything between your markup and your frame: it waits for the host's skin record before it builds the `srcDoc` (the page is never loaded twice), darkens the page with DarkReader (served by the app from its own origin, pinned, so it works offline; onto the skin's `--sheet` and `--ink`; re-coloured by message on a skin change), keeps your page's variables block (below) and replaces it in the loaded page by the same message, so a change never reloads the page, gives the backing behind the page (`frameBacking`), and, when asked, has the page report the pointer (`reportPointer`) and run the element picker (`pickElements`, below). Whether it darkens is `dark`: `true` darkens the page, `false` shows it as authored, `null` follows the skin (darkened in a dark skin). Nothing is stored by the view: you take `dark` from your own style (*Renderer styles and the style control*), as the shipped views do, from a Theme row of `auto` (null) / `authored` / `darkened`, so the user's choice is kept per channel under your renderer's id. A change of `dark` rebuilds the `srcDoc`, which reloads the page. You keep four things:

- **your prepare step**, `prepare(html) => { html, nonce? }`, stable (`useCallback`) and taking nothing from the skin (a new prepare rebuilds the `srcDoc`, which reloads the page): what you do to the markup before it is shown. The html renderer rewrites sibling references and adds a `<base>` and its link bridge. A prepare step that adds a Content-Security-Policy meta of its own returns the policy's script nonce: the view's tags then carry it, right after the leading CSP meta, so the policy governs them.
- **your variables block**, `variables` (optional): CSS text, a `:root` rule, that the page takes from your view (the html renderer's is its text size, a `zoom` rule at zero specificity). The view puts it in a `<style>` of its own at the front of the page's head, so the first paint has it, and on a change posts the new text into the loaded page, where the view's bootstrap (it carries the nonce) writes it in place. With dark mode off in a dark skin the page sits on the browser's light `Canvas`, its own default ground: the backing is `Canvas` under a light scheme, because when a frame's colour scheme differs from the backing's, the browser paints an opaque `Canvas` of the frame's own scheme wherever the page paints no ground, a white ring around a light page on a dark backing.
- **your `<iframe>` and its `sandbox` literal** (never `allow-same-origin` on authored content), mounted only once `view.srcDoc` is not null: an empty frame is a light page, which the browser paints white over a dark scheme.
- **your dark choice**, `dark`, from your style's theme, offered in your `StyleControl`.

```tsx
import React, { useCallback } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile, useTheme, useStyle, useDocumentView,
    StyleControl, DOCUMENT_VIEW_STYLE } from '/api/sdk/react.js';

const THEMES = [
    { id: 'auto', label: 'Follow the skin' },
    { id: 'authored', label: 'As authored' },
    { id: 'darkened', label: 'Darkened' },
];

function Page() {
    const { content, api } = useCollimateFile();
    const theme = useTheme(api);
    const style = useStyle(api);
    const dark = style.theme === 'darkened' ? true : style.theme === 'authored' ? false : null;
    const prepare = useCallback((html: string) => ({ html: myPrepare(html, api) }), [api]);
    const view = useDocumentView({ html: content, api, theme, dark, prepare, reportPointer: true });
    return (
        <div style={view.pageStyle}>
            {view.srcDoc !== null && (
                <iframe ref={view.frameRef} srcDoc={view.srcDoc} onLoad={view.onLoad}
                    style={DOCUMENT_VIEW_STYLE.iframe} sandbox="allow-scripts allow-forms" />
            )}
            {/* top right, only while the pointer is near that corner or the popover
                is open; the frame swallows the pointer, so the page reports it */}
            <StyleControl api={api} frame={view.frameRef} themes={THEMES} font={false} size={false} />
        </div>
    );
}
```

**No frame.** The page fills your box: no margin, no hairline, no radius around it. `view.pageStyle` is your root element's style: it fills the layout root (`DOCUMENT_VIEW_STYLE.page`), on the page's backing. If your document lies in a box you lay out yourself, use `{ ...DOCUMENT_VIEW_STYLE.page, ...view.backing }` there instead.

The style control covers nothing of the page while the pointer is elsewhere: the html renderer, which has no chrome, shows it top right only while the pointer is near that corner. Pass `reportPointer: true` when your `StyleControl` floats over the page, and give the control the view's `frameRef` as `frame`, as the example does. The frame takes every pointer move over the page, so your document never sees one there; with `reportPointer` the view's bootstrap in the page posts the pointer's position to your window (passive, at most once an animation frame), and the control measures those reports against its icon, so it shows near the corner with nothing laid over the page.

**Picking an element.** `pickElements: true` adds the element picker to the page's bootstrap, and the view's `pick(signal, hint?)` lets the user click an element of the page. While a pick is armed the page outlines the element under the pointer in the skin's `--brass`, inside a closed shadow root of its own (the page's styles and DarkReader never reach it), shows `hint` (at most 200 characters) at the top, and stops every press, click and key in the capture phase, so the page's own handlers never see them. A primary click resolves `{ tag, css, id, snippet, html }`: the element's tag, its CSS path from the nearest id (or `body`), that id, its text (at most 120 characters) and its markup (at most 600), whitespace collapsed and clipped with '…'; the element flashes once to confirm. Esc in the page, an abort of `signal` or a newer `pick` resolves `null`. A reload (a `live_refresh` swap) re-arms a pending pick in the new page: the view probes the page on every load. The pick rejects when the view was built without `pickElements`, and when no picker answers within 3 s of a load (the page navigated away from the file, or a policy delivered with it refused the script). `view.pickAvailable` says whether the loaded page's picker has answered. One pick runs at a time. The html renderer answers annotation requests with it (*Answering annotation requests*).

The pure pieces are exported for tests and for renderers that need them: `injectDarkReader`, `documentSrcDoc`, `darkReaderOptions`, `darkReaderPoles`, `frameBacking`, `hostThemeKnown`, `themeSettled`, `documentSkinKey`, `documentSkinPost`, `deliveredAfterLoad`, `darkReaderScriptUrl`, `DARKREADER_VERSION`, `DARKREADER_PATH`.

### useSiblingFile() Hook

Fetches a related file from the same cell (e.g., an HTML file loading its CSS):

```typescript
const { url, loading } = useSiblingFile('styles.css');
// url: the direct cell-file URL from api.getFileUrl(path) — binary-safe,
//      revision-pinned; pass to <link href>, <script src>, <img src>. Path is
//      the full cell-relative path.
// loading: boolean — false at once on current hosts (the URL is synchronous)
```

### Publish each document edit immediately

Call `saveContent(serializedDocument)` on every document change. The host owns
one document buffer, journals it in IndexedDB, and coalesces server requests.
A renderer must not delay delivery of its latest snapshot. The host reports
local durability separately from server acknowledgement; a failed local write
must remain visible as unsaved. If serialization is expensive, use the host's
snapshot codec contract: register a trusted SDK codec with
`registerSnapshotCodec`, then publish an exact immutable payload through
`saveSnapshot({codecHash, snapshotId, payload})` on every edit. The host journals
that payload and derives text in a worker; a new snapshot ID denotes a new edit.
Only SDK-approved codec hashes may execute. A renderer using
`defineRenderer({component, recoverySnapshots: true})` must read
`getDocumentSnapshot()` and restore any recovery payload before enabling edits.
Follow `onDocumentSnapshot`, ignore echoes of your own snapshot ID, and apply
explicit replacements in place. `getContent()` waits for the latest derived
text; a snapshot's `content` is current only when `materializedSeq === editSeq`.
The shipped Milkdown renderer is the reference implementation. Its codec uses
a JSON string as its payload, avoiding repeated copies of a large nested tree
across iframe, worker, and journal boundaries; it decodes only when restoring
or materializing that snapshot.

Imperative editors may register `api.onFlush` as a serialization barrier before
Save, Run or close. It supplements immediate snapshots. The host awaits its
promise; a failure keeps the operation visibly incomplete.

### Following host content updates (onSetContent)

Every host delivers primary-file replacements through `onSetContent`, including
native and iframe hosts. `defineRenderer` subscribes and updates
`useCollimateFile().content` automatically. A private editor model should apply
the replacement in place, preserve selection and scroll, and suppress its own
change callback while doing so. Do not fetch the primary body in response to
`onFilesChange`; the host document owner alone chooses its accepted revision.
That notification remains useful for sibling resources.

A competing run or another window cannot replace a dirty buffer. The host
preserves the competing revision and exposes explicit conflict resolution.
Server acknowledgements carry writer sequence and file revision; wall clocks
and elapsed grace periods do not choose whose content wins.

### Full RendererAPI Object

Available via `const { api } = useCollimateFile()`. `RendererTheme`,
`RendererEditing`, `RendererStyle`, `RendererFontFamily` and `DesktopSurface`
are the SDK types exported by `@collimate/renderer-hooks`:

```typescript example=renderer-api-interface
interface RendererAPI {
    // Content access
    getContent(): Promise<string>;                    // Wait for current materialized text
    getDocumentSnapshot(): Promise<RendererDocumentSnapshot>;
    registerSnapshotCodec(codec: RendererSnapshotCodec): Promise<void>;
    saveSnapshot(snapshot: RendererRecoverySnapshot): void;
    onDocumentSnapshot(cb: (snapshot: RendererDocumentSnapshot) => void): () => void;
    getDiffContent(): Promise<string | null>;         // Previous version (diff mode only)
    getDiffLabels?(): { base: string; current: string } | null; // Diff side names, or null
    getFilename(): string;                            // File name
    getCellId(): string;                              // Cell UUID
    // Live registration for this mounted file (`state` "starting" while the
    // host holds it for a run that has not registered yet, then "live");
    // null when unserved or in History.
    getSurface(): Promise<SurfaceSession | null>;
    // Claim/registration/release notification scoped to this physical file.
    onSurfaceChange(callback: () => void): () => void;

    // Sibling files (other files in the same cell). `path` is always the
    // file's FULL cell-relative path — the same addressing for reads and
    // writes, so `docs/notes.md` is not reachable as `notes.md`.
    // Rejections for HTTP failures carry numeric err.status (404 = absent).
    getSiblingFile(path: string): Promise<string>;    // Fetch sibling file content (text)
    // Body-free accepted own-cell membership. History uses
    // the selected immutable version. This does not fetch file bodies. The
    // milkdown renderer's "Insert file…" slash item lists it to put a file of
    // the cell into the document as a preview, `![name](path)`, or a link,
    // `[name](path)`.
    getFilePaths(): Promise<string[]>;
    // The preview primitives: compile exact virtual bytes with the pinned
    // local compiler. They do not execute, approve, or upload the result.
    // Failures reject. See "Sealed previews of an unapproved renderer" below.
    preview: {
        prepareDependencies(files: Record<string, Uint8Array>, entrypoint: string, signal?: AbortSignal): Promise<void>;
        compile(files: Record<string, Uint8Array>, entrypoint: string): Promise<{
            source_hash: string; code: string; bundle_hash: string;
            runtime: {react: string; session: string}; apiOrigin: string;
            imports: Record<string, string>; artifact_hash: string;
        }>;
    };
    // Direct URL for a sibling — <img src>, <video>, fetch. Binary-safe
    // (getSiblingFile decodes as text) and pinned to accepted server bytes,
    // independently of local edits. In History, both reads use the exact selected
    // snapshot, including the oldest version. The host encodes each path
    // segment; pass the original path, without encoding it yourself.
    // Synchronous in every host. This is how the
    // milkdown renderer previews `![plot](charts/plot.png)` (and plays a
    // video or an audio file): the markdown keeps the clean relative path,
    // the <img> loads through getFileUrl at render time, and a path the
    // host refuses (getFileUrl throws) shows a missing-file card instead.
    getFileUrl(path: string): string;

    // Journal and conditionally save text in a sibling of your OWN cell.
    // getSiblingFile exposes the same document owner's local recovery text;
    // true means the server accepted the exact write. Checkpoints group
    // accepted revisions into history separately. Creates
    // the file when absent, replaces it in place when present. This is how
    // a renderer persists its own state file beside the document it edits.
    // A path in a run's folder (`_runs/…`) is run-owned and resolves false.
    // NEVER rejects: a missing cell, a host that can't write, a mid-run
    // refusal and a transport timeout all resolve `false` — branch on the
    // boolean, no catch needed. (getSiblingFile, by contrast, rejects on a
    // host error or a transport timeout — 10 s in the iframe host.)
    saveSiblingFile(path: string, content: string): Promise<boolean>;

    // Editing
    reportChange(newContent: string): void;            // Report each content change to the host
    // Primary-file editing is disabled for run-owned UI, history and review.
    // Never offer an edit override while this is true.
    isReadOnly(): boolean;
    // Run actions and annotation requests are available. History, review,
    // diff and structural barriers return false, and the host sends no
    // annotation request then. This never permits replacing run-owned
    // primary file bytes.
    canInteract(): boolean;
    // Answer the host's annotation requests for this view: resolve the
    // context, null when the user abandons a pick (Esc), or reject;
    // reject with annotationUnavailable(message) when this view cannot
    // answer. Stop when `signal` aborts. One handler per view (a second
    // replaces the first); returns unsubscribe. Absent in a sealed preview.
    // Prefer useAnnotationRequest(api, handler).
    onAnnotationRequest?(handler: (request: AnnotationRequest, signal: AbortSignal) => Promise<AnnotationContext | null>): () => void;

    // File change notifications
    onFilesChange(cb: (detail: { cellId: string; changedFiles: string[] }) => void): () => void;  // Subscribe; returns unsubscribe fn

    // Host-selected primary content replacement, available in every host.
    // Apply in place without emitting reportChange. Returns unsubscribe.
    onSetContent(cb: (content: string) => void): () => void;

    // Optional serialization barrier before Save, Run or close. Every edit
    // already emits reportChange immediately; this may finish async work.
    onFlush?(cb: () => void | Promise<void>): () => void;

    // Theme signal — the active skin: themeId, scheme, known, every token
    // keyed by CSS name (vars['--sheet-2']) and the three font stacks.
    // Fires on subscribe and on every change. See "Theming your renderer".
    getTheme(): RendererTheme;
    onThemeChange(cb: (t: RendererTheme) => void): () => void;

    // Editing preferences — { spellcheck }, the user's choice in Settings →
    // Editing. The host sets it on your document's <html>, so an editable
    // that leaves `spellcheck` unset follows it with no code; read it for an
    // editor that sets its own or runs in a frame of its own. Fires on
    // subscribe and on every change. See "Editing preferences".
    getEditing(): RendererEditing;
    onEditingChange(cb: (e: RendererEditing) => void): () => void;

    // This renderer's style — { theme, font, textSize }, the user's choices
    // in its style control. The host persists them per channel under this
    // renderer's id; a null field is your default (the skin, the skin's font
    // for the role, your own size). onStyleChange fires on subscribe and on
    // every change for this renderer's id. setStyle merges a patch (null
    // puts a field back to the default) and is allowed in a read-only view:
    // it styles the view, not the file; a sealed preview keeps it locally.
    // getFontFamilies lists the host's font catalogue ({ family, label,
    // role, stack }); `font` stores a family name from it. See "Renderer
    // styles and the style control".
    getStyle(): RendererStyle;
    onStyleChange(cb: (s: RendererStyle) => void): () => void;
    setStyle(patch: Partial<RendererStyle>): void;
    getFontFamilies(): ReadonlyArray<RendererFontFamily>;

    // Visibility signal — false while the cell is scrolled offscreen and the
    // host has display:none'd its subtree (which clears GPU/canvas drawing
    // buffers), true when on screen. Like onThemeChange, the callback fires
    // immediately on subscribe and on every flip thereafter. GPU/canvas
    // renderers repaint their buffer on visible=true. Native renderers only —
    // iframe-embedded renderers always report true. See "Reacting to
    // visibility" below. Prefer the `useVisibility(api)` hook.
    getVisible?(): boolean;
    onVisibleChange?(cb: (visible: boolean) => void): () => void;

    // Cell-focus signal — true only while this cell holds the canvas focus.
    // THE INPUT CONTRACT: a native renderer shares the main document, so a
    // window-level input listener (keydown, wheel, ...) hears every event
    // app-wide. Never install one without gating on this signal — an
    // unfocused cell must neither react to nor preventDefault input that
    // belongs to the rest of the app. Prefer listeners on your own root
    // element; when a window listener is unavoidable, use the
    // `useHostFocus(api)` hook. Fires immediately on subscribe. Native
    // renderers only — iframe-embedded renderers are input-scoped by the
    // browser already and report true.
    //
    // ENFORCED BY THE HOST: input-event listeners registered from a native
    // renderer module (addEventListener or on* assignment, any target) are
    // automatically force-gated on this signal — while no instance of the
    // renderer holds focus they simply never fire. Gate your own listeners
    // anyway (it skips your work, not just the delivery); code that must be
    // treated as untrusted belongs in the iframe tier (`embed: "iframe"`),
    // the real security boundary.
    getFocused?(): boolean;
    onFocusChange?(cb: (focused: boolean) => void): () => void;

    // Styling (for native/shadow DOM renderers)
    injectCss(css: string): void;    // Inject a <style> tag into the shadow root
    loadCss(href: string): void | Promise<void>;       // Inject a <link rel="stylesheet"> into the shadow root

    // Reveal the renderer — only with defineRenderer({ deferReady: true });
    // everything else is revealed automatically after the first commit.
    ready?(): void;

    // Renderer action button → generator. Returns true if delivered. The
    // generator receives the id via `gen.wait_for_action()`, with your
    // file's path as the action's source. A renderer never names a run: the
    // host routes the click to the live run serving the file or holding
    // its path lock, including a cell-scoped surface. The `.terminal`,
    // `.website` and `.webview` pointer renderers use this for their
    // buttons. False when no live run owns the file (e.g. the run
    // has ended) — say so rather than wait. `payload` optionally carries a
    // structured answer value. Native-embed renderers only. Optional —
    // feature-detect (`if (api.emitAction)`).
    emitAction?(id: string, payload?: Record<string, unknown>): Promise<boolean>;

    // Conditionally save binary bytes in your OWN cell (native only).
    // A true result means an exact server receipt; getFileUrl then reads the
    // accepted revision. Checkpoints group accepted revisions into history.
    // Use this for
    // uploads the generator will pick up. Never throws; false on failure.
    stageDraftFile?(path: string, bytes: ArrayBuffer | Uint8Array | Blob | string): Promise<boolean>;

    // Reveal a file in your OWN cell: centre the cell and open that file in
    // its own tab under its default renderer, or focus the tab already
    // showing it. The file takes over a non-pinned active tab in place;
    // `newTab: true` opens a fresh tab instead, so your own tab stays. Same
    // cell-relative addressing as getSiblingFile; strictly same-cell.
    // Fire-and-forget — an unresolvable path is a silent no-op, so it wires
    // straight to user clicks. This is how the milkdown renderer follows
    // `[data](results.csv)` links to neighboring files, each in a new tab.
    // Available in every host.
    revealFile(path: string, options?: { newTab?: boolean }): void;

    // The Electron desktop shell, or absent. Present only when the app runs
    // inside the shell AND this renderer is `embed: "native"` — a sandboxed
    // iframe renderer never gets it, because handing untrusted iframe source
    // a shell handle would cross the boundary the sandbox exists to draw.
    // So `undefined` covers browser and iframe alike, and one fallback path
    // handles both. Read it through `useDesktop(api)`; see "The desktop
    // shell" below.
    desktop?: DesktopSurface;
}
```

### Cheat sheet — hooks + `api.*`

The full surface available to a renderer (from `/api/sdk/react.js`). Most
renderers need only `useCollimateFile` and immediate `saveContent` calls.

| Hook | Returns / shape |
|---|---|
| `defineRenderer({ component, diffComponent?, deferReady?, recoverySnapshots? })` | default export; the mount/unmount lifecycle (`deferReady`: stay hidden until `api.ready()`; `recoverySnapshots`: restore the exact owner snapshot before editing) |
| `useCollimateFile()` | `{ initialContent, content, saveContent, isReadOnly, fileName, api, diffContent, diffLabels }` |
| `useSiblingFile(path)` | `{ url, loading }` — direct cell-file URL (api.getFileUrl: binary-safe, revision-pinned) |
| `useVisibility(api)` | `{ visible, onChange }` — is the cell on screen; repaint GPU/canvas buffers on re-show |
| `useHostFocus(api)` | `{ focused, onChange }` — does the cell hold canvas focus; REQUIRED gate for any window-level input listener in a native renderer |
| `useAnnotationRequest(api, handler)` · `annotationUnavailable(message)` | subscribe this view to the host's annotation requests (`api.onAnnotationRequest`), once per `api`, always calling the latest `handler`; `null` withdraws it · the rejection that says this view cannot answer (the host shows `message`). See *Answering annotation requests* |
| `useTheme(api)` | `{ themeId, scheme, known, vars, fonts, onChange }` — the host's skin as React state; `onChange(cb)` gets the whole `RendererTheme` on subscribe and on every change |
| `useEditing(api)` | `{ spellcheck, onChange }` — the user's editing preferences as React state, for an editor the document's `<html spellcheck>` does not reach (its own attribute, its own frame); `onChange(cb)` gets the whole `RendererEditing` on subscribe and on every change |
| `useStyle(api)` | `{ theme, font, textSize, set, onChange }` — this renderer's style as React state, persisted by the host per channel under the renderer's id; a null field is your default; `set(patch)` writes through `api.setStyle`; `onChange(cb)` gets the whole `RendererStyle` on subscribe and on every change |
| `StyleControl({ api, themes, font, size, watch?, frame?, style?, extra? })` · `StyleControlRow({ label, children })` | the shared style icon (24px, top right, shown only while the pointer is within 40px of it, while it has keyboard focus or while open; `watch` is the element whose pointer moves it measures, by default its parent; `frame` a frame it floats over whose page reports the pointer, `useDocumentView`'s `reportPointer`) and its popover: Theme over `themes` (the first is your default, stored as null), Font over the host's families for one role plus "Skin default", Text size A− / % / A+ with Reset, then `extra`, your own view controls (a node, or a function of `{ close }`), under a rule; the control shows when it has any row, so `themes={[]} font={false} size={false} extra={…}` is a corner menu of view controls alone · one `extra` row, a label and the controls beside it, styled like the style rows; inline styles on skin tokens. See *Renderer styles and the style control* |
| `CELL_FILES_MIME` · `isCellFileDrag(dt)` · `readCellFileDrop(dt, api)` | the files pane's drag type (`'application/x-collimate-files'`) · whether a drag carries files from the files pane (types only, so it answers in `dragover`) · the dropped cell-relative paths when they are THIS cell's, else `null`. See *Dragging a file from the files pane* |
| `useRemoteScript(url)` / `useRemoteModule(url)` | `{ loading, error }` / `{ module, loading, error }` — load a CDN `<script>` (UMD/AMD global) or ESM module once, deduped by URL |
| `mirrorUrl(url)` | the app's same-origin module-mirror copy of an `https://esm.sh/` or `https://cdn.jsdelivr.net/` URL, for a load no import map routes (a `<script>`, a fetched stylesheet, a font, wasm, a worker); any other URL, and every URL in a sealed preview, unchanged. See *Loading External Libraries* |
| `MediaViewer({ kind })` | Shared native audio/video component inside `defineRenderer`; `kind` is `'audio'` or `'video'`. Current file capability URL, controls, error/Retry and filename/extension footer. |
| `useDocumentView({ html, api, theme, dark, prepare, variables?, reportPointer?, pickElements? })` · `DOCUMENT_VIEW_STYLE` | An authored HTML document in your own opaque frame: `{ srcDoc, frameRef, onLoad, pageStyle, backing, dark, pick, pickAvailable }` (the host-theme gate, DarkReader on the skin from the app's origin when `dark` is true, or null in a dark skin, the variables block rewritten in place on a change, the backing behind the page, the page reporting the pointer for a `StyleControl` over it when `reportPointer`; with `pickElements`, `pick(signal, hint?)` lets the user click an element of the page and resolves `{ tag, css, id, snippet, html }`, or `null` on Esc or abort, and `pickAvailable` says the page's picker answered) · the view's styles (`page`, `iframe`). See *The document view* |
| `injectDarkReader(html, on, options, { src, nonce? })` · `documentSrcDoc(prepared, dark, options, src, variables?, pointer?, pick?)` · `darkReaderScriptUrl()` · `DARKREADER_VERSION` · `DARKREADER_PATH` | the view's pure steps: DarkReader at the front of `<head>` (behind a leading CSP meta when a nonce is given, every tag carrying it) · a prepared document darkened or not, with the variables block and its listener, the pointer report and the element picker · the pinned build's absolute URL on the app's origin · its version and path |
| `darkReaderOptions(theme)` · `darkReaderPoles(scheme)` · `frameBacking(settled, dark, scheme)` · `documentSkinKey(dark, options, variables)` · `documentSkinPost(listening, delivered, key)` · `deliveredAfterLoad(key)` · `DARKREADER_MESSAGE` | DarkReader's colours from the skin · which tokens are ground and text · the backing behind the page · what a page takes from the skin (a darkened page's colours, the variables block) · the message that carries a new skin into the loaded page, or `null` · what a loaded page has · that message's type |
| `hostThemeKnown(theme)` · `themeSettled(theme, api)` · `HOST_THEME_WAIT_MS` | the host-theme gate (every `srcDoc` waits for it: the skin's colours arrive with the theme): a pushed snapshot, not the placeholder · nothing left to wait for · the bounded wait |
| `useDesktop(api)` | `DesktopSurface \| undefined` — the Electron shell slice; `undefined` in a browser AND in an iframe renderer, which is the feature test |
| `tokens` | the skin tokens as `var(--…)` strings (`tokens.color.sheet2` is `'var(--sheet-2)'`); see *Styling Patterns* |
| `themeCss(theme, { fontsUrl? })` | CSS text (`@import` of the fonts, then `:root{…}` with every token and `color-scheme`) for a frame the renderer owns |
| `resolveVar(theme, name)` · `toHex8(color)` | a token's concrete value · a hex or `rgb()`/`rgba()` colour as `#rrggbbaa` (or `null`), for libraries that need real colours |
| `syntaxPalette(theme)` · `SYNTAX_ROLES` | code-highlighting colours derived from the skin, each role (`comment`, `keyword`, `string`, `number`, `type`, `function`, `variable`, `operator`, `punctuation`, `tag`, `attribute`, `regexp`, `invalid`, `added`, `removed`, `modified`) a `#rrggbb` with at least 3:1 contrast on `--sheet` and `--code-bg`; `null` before the first skin arrives, or when `--sheet` or `--ink` cannot be read · the role names |
| `THEME_VAR_NAMES` | every token's CSS name (`'--canvas'` … `'--radius'`), the keys of `RendererTheme.vars` |

`onFilesChange` carries `{ cellId, changedFiles }`: the full cell-relative
paths actually changed by a commit, including deletions. An empty list means
no file changed. Before reading a file, check whether `changedFiles` includes
that sibling path; a commit to an unrelated file needs no read. The primary
file receives only owner-selected content or snapshot pushes; it never
refetches itself in response to a sibling notification.

`api` (the `RendererAPI` object):

| Group | Methods |
|---|---|
| Content | `getContent()` · `getDiffContent()` · `getDiffLabels?()` · `getFilename()` · `getCellId()` |
| Sibling files | `getFilePaths()` → `Promise<string[]>` (the cell's accepted paths, no bodies) · `getSiblingFile(path)` → `Promise<string>` (text) · `getFileUrl(path)` → URL (binary-safe, revision-pinned — `<img src>`, fetch) · `saveSiblingFile(path, text)` → `Promise<boolean>` (conditional save with an exact receipt; accepted bytes are immediately readable; never rejects) · `stageDraftFile?(path, bytes)` → `Promise<boolean>` (conditional binary save, accepted bytes readable through revision-pinned reads; native only, so an iframe renderer stores no uploaded bytes: the milkdown renderer takes no uploads and inserts a file already in the cell instead) · `revealFile(path, { newTab? })` (opens the file in its own tab under its default renderer; `newTab` opens a fresh tab instead of taking over the active one; fire-and-forget no-op when unresolvable). All paths are the file's full cell-relative path, always the renderer's OWN cell |
| Editing | `reportChange(next)` · `isReadOnly()` — primary document edits are disabled for run-owned UI, history, diff and review. `canInteract()` separately permits run actions and annotation requests (the host sends none while it is false); it is false in history, diff, review and structural barriers. Neither permission has a user override. |
| Host sync | `onSetContent(cb)` (apply pushed content, don't re-`reportChange`) · `onFlush?(cb)` (finish pending serialization) · `onFilesChange(cb)` (sibling notifications) · `getDocumentSnapshot()` / `onDocumentSnapshot(cb)` (exact recovery state) · `registerSnapshotCodec(codec)` / `saveSnapshot(snapshot)` (approved background serialization) |
| Annotations | `onAnnotationRequest?(handler)` → unsubscribe — answer the host's request (`{ id, mode: 'pick' \| 'selection', hint? }`) with `{ label, text, anchor? }`, `null` (abandoned) or a rejection (`annotationUnavailable(message)`: cannot answer); prefer `useAnnotationRequest(api, handler)`. See *Answering annotation requests* |
| Theme | `getTheme()` → `{ themeId, scheme, known, vars, fonts }` · `onThemeChange(cb)` (fires on subscribe and on every change) |
| Editing preferences | `getEditing()` → `{ spellcheck }` · `onEditingChange(cb)` (fires on subscribe and on every change); the host already sets `spellcheck` on your document's `<html>` |
| Style | `getStyle()` → `{ theme, font, textSize }` · `onStyleChange(cb)` (fires on subscribe and on every change for this renderer's id) · `setStyle(patch)` (the host persists it per channel under this renderer's id; allowed read-only) · `getFontFamilies()` → `[{ family, label, role, stack }]`; use `useStyle(api)` and `StyleControl` |
| Visibility | `getVisible?()` → `boolean` · `onVisibleChange?(cb)` — fires when the cell scrolls on/off screen; repaint GPU/canvas buffers on `true` (native only) |
| Focus | `getFocused?()` → `boolean` · `onFocusChange?(cb)` — does the cell hold canvas focus (native only); use `useHostFocus(api)` |
| Styling (native only) | `injectCss(css)` · `loadCss(href)` |
| Lifecycle | `ready?()` — reveal the renderer; only with `defineRenderer({ deferReady: true })` |
| Served file | `getSurface()` → `Promise<SurfaceSession \| null>` (`{ state, chat_id, ws_path, token, label, run_id }`; `state` is `"starting"` with empty coordinates until the run registers the file, then `"live"`) and `onSurfaceChange(callback)` → unsubscribe; History and sealed unapproved previews have no live session |
| Actions | `emitAction?(id, payload?)` → `Promise<boolean>` — fires a renderer button; the generator receives it via `gen.wait_for_action()` (native only) |
| Preview | `preview` → `{ prepareDependencies(files, entrypoint, signal?), compile(files, entrypoint) }` — compile a renderer package's files as the host does, without executing them; see *Sealed previews of an unapproved renderer* |
| Desktop (native only) | `desktop?` → `{ capabilities, openExternal(url), mountGuest?(hostEl, opts) }` — the Electron shell slice; read it via `useDesktop(api)`, test capability **truthiness**, never presence. See *The desktop shell* |

Subscriptions (`onSetContent`/`onFlush`/`onFilesChange`/`onThemeChange`/`onEditingChange`/`onStyleChange`/`onVisibleChange`/`onFocusChange`)
return an **unsubscribe** fn — call it on unmount (`useEffect(() => api.onThemeChange(cb), [api])`).
The `?`-marked methods are optional — feature-detect (`if (api.emitAction)`).

### Accepted upload readback

For a successful native `stageDraftFile`, this text payload is immediately
visible through `getSiblingFile` and `getFileUrl`, before a checkpoint:

```json example=renderer-upload-readback
{"path":"attachments/example.txt","text":"Accepted upload — readable now","visibleBeforeCheckpoint":true,"acceptedBeforeCheckpoint":true,"historyBeforeCheckpoint":false}
```

The accepted file has its own revision immediately; a checkpoint records it
in version history. Binary uploads use the same path and readback rule,
with `getFileUrl` preserving their bytes.

## Theming your renderer

The user picks a skin (`night`, `night-cool`, `paper`, or one of their own). A renderer follows it through one vocabulary of tokens, delivered two ways.

**1. CSS variables on `:root`.** The host writes every token on the renderer's `:root`: a native renderer's shadow root inherits the page's variables, and the iframe sandbox mirrors them onto its own document, verbatim, on every change. Inline styles read them directly and repaint with the skin, no subscription needed:

```tsx
<div style={{
    background: 'var(--sheet)',              // the renderer's own ground
    color: 'var(--ink)',                     // primary text
    border: '1px solid var(--line)',         // hairline
    fontFamily: 'var(--font-ui)',
    borderRadius: 'var(--radius)',
}}>
```

The tokens:

| CSS name | `tokens.` | Meaning |
|---|---|---|
| `--canvas` | `color.canvas` | the ground behind stacks and bars |
| `--sheet` | `color.sheet` | a renderer's own ground; panels, cards, menus |
| `--sheet-2` | `color.sheet2` | raised rows, toolbars, hover fills |
| `--sheet-3` | `color.sheet3` | pressed fills, a primary button's hover |
| `--line` | `color.line` | hairlines between regions |
| `--line-2` | `color.line2` | control borders, a menu's edge |
| `--ink` | `color.ink` | primary text |
| `--ink-2` | `color.ink2` | secondary text |
| `--ink-3` | `color.ink3` | hints, placeholders, disabled |
| `--brass` | `color.brass` | the selection accent, and only that |
| `--brass-soft` | `color.brassSoft` | a selected fill |
| `--teal` | `color.teal` | live / on-state: running, the open tab, links, focus |
| `--teal-soft` | `color.tealSoft` | a live fill |
| `--teal-sheet` | `color.tealSheet` | the head of the Capabilities stack and its cells |
| `--red` | `color.red` | destructive, failed, removed |
| `--red-soft` | `color.redSoft` | destructive hover, a confirm strip, a removed line's fill |
| `--green` | `color.green` | pass, added |
| `--code-bg` | `color.codeBg` | code blocks and terminals |
| `--prose-ink` | `color.proseInk` | body text of prose |
| `--font-ui` | `font.ui` | interface text |
| `--font-mono` | `font.mono` | code, paths, keys |
| `--font-prose` | `font.prose` | documents |
| `--radius` | `radius` | 8px in the built-in skins; step down from it (`calc(var(--radius) - 2px)`) for rows and buttons |
| `--shadow` | `shadow` | menus and lifted surfaces (a `box-shadow` value) |
| `--lift` | `lift` | scrims and backdrops |

Pick the token for what a colour MEANS: brass marks the selection and nothing else, teal is live or on, red is destructive, green is pass. Where no soft token fits, mix one: `color-mix(in srgb, var(--teal) 20%, transparent)`.

**2. `api.getTheme()` and `api.onThemeChange(cb)`** (or `useTheme(api)`) give the same skin as a record, for libraries that need concrete values or a dark/light switch:

```ts example=renderer-theme-interface
interface RendererTheme {
    themeId: string;     // 'night' | 'night-cool' | 'paper' | 'user.<name>'
    scheme: 'dark' | 'light';
    known: boolean;      // false only for the placeholder before the first theme
    vars: Record<ThemeVarName, string>;  // every token above, keyed by CSS name
    fonts: { ui: string; mono: string; prose: string };
}
```

`vars` is keyed by CSS name: `theme.vars['--sheet-2']`. `onThemeChange` fires immediately on subscribe with the current record and again on every change, so one subscription both initialises and follows. `resolveVar(theme, '--sheet')` reads one value, and `toHex8(value)` turns a hex or `rgba()` token into `#rrggbbaa` for an API that takes only hex (Monaco's `defineTheme`, Mermaid's `themeVariables`). Rebuild the library theme from `onChange`. By default a syntax highlighter takes its colours from `syntaxPalette(theme)`, not from its library's own theme: the skin names four hues, and the palette derives every role from them and the inks (type, function and regexp are teal, brass and green one OKLCH lightness step away from the ground), nudged until each reads at 3:1 on the code grounds, so every editor in the workbench draws code alike. A user skin may write a token the palette cannot read (`hsl()`, `oklch()`, a named colour); that role falls back to an ink, and the palette is `null` only when `--sheet` or `--ink` cannot be read, in which case the library keeps its own colours. Excalidraw, Mermaid and Monaco also keep a `scheme` switch where the library itself only knows dark and light: Excalidraw's `theme` prop, Mermaid's `darkMode`, and the `vs` or `vs-dark` base that Monaco's `defineTheme` requires. With a palette, `inherit: false` drops that base's own colour table and token rules; without one, `inherit: true` lets the base's highlighting stand in. Either way Monaco's colour registry still supplies a default for every colour key the theme leaves unnamed, so the theme must name every key a visible surface uses (the shipped Monaco renderer names the overview ruler, peek view, light bulb, links, keybinding labels and find toggles along with the editor, widgets and lists). CodeMirror (the milkdown renderer's fenced code) takes the palette as a `HighlightStyle` whose colours are `var(--colmd-syntax-<role>)` properties, written from `syntaxPalette` on every record and removed when it is `null`, so a skin change recolours every block with no rebuild; drop the library's default theme (Crepe's One Dark) rather than overriding its spans.

A library that declares its palette as CSS variables (Excalidraw's `--island-bg-color`, `--color-primary`, `--color-surface-*` and the rest) takes the skin through an injected sheet that sets each one to a `var(--token)`, at a specificity above the library's own rules, and restates the few rules where the library wrote a literal colour instead of a variable (its tooltip, scrim, context menu, hover fills); the sheet then follows a skin change with no rebuild. Where a library's dark mode is a filter over its drawing (Excalidraw inverts its canvas), do not hand it the skin's ground, which the filter would invert: give it a transparent ground and let the frame's `--canvas` show through, keep any ground the user chose for the drawing, and never write the skin's colour into the file.

**3. Frames the renderer owns.** A `srcdoc` or sandboxed child frame (a preview, an embedded editor) is a separate document, so the host's variables do not reach it. Inject `themeCss` into its `<head>` and rebuild it from `onChange`:

```ts
const css = themeCss(api.getTheme(), {
    fontsUrl: new URL('/fonts/fonts.css', location.href).href,
});
frame.srcdoc = `<!doctype html><html><head><style>${css}</style>…`;
```

`themeCss(theme, { fontsUrl? })` returns an `@import` of `fontsUrl` (when given), then `:root{…}` with every token and `color-scheme`. The URL must be absolute: an opaque frame has no base to resolve a relative one against. The host serves its fonts to opaque frames.

**Previews of authored content keep their own styles.** An HTML preview, a website or a webview shows a page someone else styled; forcing the skin onto it would misrepresent the page. The renderer's CHROME (toolbars, pills, address bars) follows the skin; the page does not, and an authored HTML page gets no frame around it at all (*The document view*). A renderer may darken an authored page (see DarkReader in Example 3), and it darkens it onto the skin: the document view passes the skin's `--sheet` and `--ink` (read with `resolveVar` and `toHex8`; swapped in a light skin, so the page still comes out dark) as DarkReader's `darkSchemeBackgroundColor` and `darkSchemeTextColor`, and re-enables it with the new colours on a skin change. Nothing else of the skin enters the page: no ground, ink, font, link rule or variable; the picker's highlight is drawn apart from the page.

The documented exceptions to "no colour literal": content someone authored keeps its own colours (a guest or authored page that is not darkened, with the browser's light `Canvas` behind it in a dark skin; the colours a user gave a drawing's elements, which Excalidraw draws on its canvas, with its grid, beyond the reach of CSS), and a terminal's ANSI palette, which is a palette (sixteen named colours, declared once per scheme) rather than chrome. The terminal's ground is `--code-bg`.

**The skin is always the default; a library's own theme is a user choice.** Out of the box a renderer's chrome, ground and syntax colours come from the skin, whatever library draws them, so a new skin reaches every renderer at once. A renderer may OFFER the library's own themes as further entries of its style control's Theme row (*Renderer styles and the style control*): Crepe's Classic, Frame and Nord for the markdown editor, Excalidraw's own look, Monaco's `vs`, `vs-dark` and `hc-black`. The skin's entry comes first and is stored as null, so a user who never opens the control sees the skin; a library theme that has a dark and a light variant follows the skin's `scheme` (the dark one in a dark skin), and it is loaded from the same pinned source as the rest of the library, in one replaceable sheet, so choosing the skin again removes it whole. Picking one is the user's choice for that renderer in that channel, never a default you ship.

## Editing preferences

The user chooses whether misspelled words are underlined (Settings → Editing → Spellcheck, per channel). The host writes that choice as the HTML `spellcheck` attribute on your document's `<html>`: the page a native renderer's shadow root lives in, or the iframe sandbox's own document. The attribute inherits, so a `<textarea>`, an `<input>` or a contenteditable editor (Crepe and ProseMirror included) that leaves `spellcheck` unset follows the choice, and every change to it, with no code. Keep `spellCheck={false}` on fields where spelling means nothing (a URL, a path, a search box); the user's choice never turns those back on.

Read the choice yourself only where the attribute cannot reach: an editor that sets its own `spellcheck`, or one in a frame you own (a `srcdoc` editor is a separate document). `api.getEditing()` returns `{ spellcheck }`, `api.onEditingChange(cb)` fires on subscribe and on every change, and `useEditing(api)` gives `{ spellcheck, onChange }` as React state. Send the flag into the frame with its other settings and set it on that document's `<html>`, as a renderer that hosts its own editor frame must.

## Renderer styles and the style control

A renderer that has a look worth choosing (a theme, a font, a text size) offers it through one shared control, so every renderer offers it the same way and the user's choice outlives a reload. The skin is always the default; the control only offers alternatives.

**The record.** `api.getStyle()` returns `{ theme, font, textSize }`, this renderer's style; `api.onStyleChange(cb)` fires on subscribe and on every change for this renderer's id; `api.setStyle(patch)` merges a patch. The host persists the record **per channel under your renderer's id** (the `id` of your `.renderer`), so every file your renderer shows in the channel shares it, another renderer never sees it, and it reaches the renderer again on the next load in every host (native, iframe; a sealed preview keeps it for the session only). A **null field is your default**, which you resolve yourself:

- `theme`: one of the ids your control offers; null is your first entry, the skin's look.
- `font`: a family name from `api.getFontFamilies()` (`{ family, label, role, stack }`, the host's catalogue; apply `stack`); null is the skin's stack for the role (`var(--font-prose)`, `var(--font-mono)`, `var(--font-ui)`).
- `textSize`: a factor, 1 = 100%; null is your own size (the markdown editor's is 1).

`useStyle(api)` gives the record as React state, `{ theme, font, textSize, set, onChange }`; `set(patch)` is `api.setStyle`. Setting a style is allowed in a read-only view (history, a run-owned file): it styles the view, not the file. Never write a style into the document or into `localStorage`.

**The control.** `StyleControl` is the one UI for it: a 24px sliders icon top right of your renderer, shown **only while the pointer is near that corner**, within 40px of the icon, never merely because the pointer is somewhere over your renderer; also while the icon has keyboard focus (at rest it is transparent but stays in the tab order, so Tab reaches it) and while its popover is open. The control judges the reach itself, you pass no hover state: it measures the pointer moves over the element it watches (`watch`, a ref; by default its parent element) against the icon's box and hides when the pointer leaves that element or enters a child frame (other than its `frame`, below). Mount it inside your root, or pass `watch={rootRef}` when it sits in a smaller box (a toolbar row, a header). Over a frame that fills your box (an authored page) the frame swallows every move, so the page reports them: build the page with the document view's `reportPointer` and pass the view's `frameRef` as `frame`; the control measures the page's reports the same way, and nothing is laid over the page (*The document view*). The popover has up to three style rows, each only when you ask for it, and your own rows under them (`extra`, below):

- **Theme**, a select over `themes` (`{ id, label }[]`, at least two). The first entry is your default (the skin, id `'skin'` by convention, or `'auto'` for a view whose default follows the skin's scheme) and is stored as null.
- **Font**, a select over the catalogue's families for one role (`font: 'prose' | 'mono' | 'ui'`), plus "Skin default"; `false` for none.
- **Text size**, A− / the percentage / A+ in `size.step` steps between `size.min` and `size.max`, and Reset; `size: { min, max, step, default }` (`default` is what null means; reaching it stores null), or `false` for none.

```tsx example=renderer-style-control
import React from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile, useStyle, StyleControl } from '/api/sdk/react.js';

const SIZE = { min: 0.7, max: 2.2, step: 0.1, default: 1 };

function Notes() {
    const { content, api } = useCollimateFile();
    const style = useStyle(api);
    const family = api.getFontFamilies().find((f) => f.family === style.font);
    // The control watches this root, its parent: it shows near its corner.
    return (
        <div style={{ position: 'relative', height: '100%' }}>
            <pre style={{ margin: 0, padding: 16, color: 'var(--ink)',
                fontFamily: family ? family.stack : 'var(--font-mono)',
                fontSize: `${13 * (style.textSize ?? SIZE.default)}px` }}>{content}</pre>
            <StyleControl api={api} themes={[]} font="mono" size={SIZE} />
        </div>
    );
}

export default defineRenderer({ component: Notes });
```

**Your own view controls: `extra`.** A renderer whose view has a few controls of its own (a page, a zoom, a pointer mode) puts them in the same corner menu rather than in a bar, so the content keeps the whole box and every renderer's menu sits in the same place. Pass them as `extra`, a node or a function of `{ close }` (call `close()` after a choice that should dismiss the menu), and lay each row out with `StyleControlRow({ label, children })`: the label in the popover's label column, your controls in a row beside it, so they read like the style rows above them (a rule separates the two when both show). `extra` is not a style: it never goes through `api.setStyle`, and what it sets is your renderer's own state, which the rows read back while the popover is open (they render inside your tree, so your hooks and providers reach them). With no style to offer, pass `themes={[]} font={false} size={false}`: the popover holds only your rows, and the icon is the same 24px corner control with the same reach, keyboard focus and open-state rules. Use `extra` for a handful of view controls you would otherwise draw as a toolbar or footer; keep an editor's editing toolbar (bold, insert, undo), which the user reaches for constantly, where it is. The shipped PDF viewer has no bar at all: its control holds Page (‹ n / N ›), Zoom (− % +), Fit (Width, Page), Pointer (Pan | Select; Pan, a press and drag that scrolls the document, is the default) and the file in a new tab.

```tsx
<StyleControl api={api} themes={[]} font={false} size={false} extra={
    <StyleControlRow label="Page">
        <button type="button" title="Previous page" onClick={previous}>‹</button>
        <span>{page} / {pages}</span>
        <button type="button" title="Next page" onClick={next}>›</button>
    </StyleControlRow>
} />
```

The control is inline styles on the skin tokens, so it works in a native renderer's shadow root and in an iframe renderer's document; it closes on Esc or a press anywhere outside it. Its root is `position: absolute` at `top: 8, right: 8` of the nearest positioned ancestor; pass `style` to anchor it elsewhere (inside a library's own toolbar slot, for example, as the Excalidraw renderer does through `renderTopRightUI`, passing `watch` its wrapper's ref so the reach is measured over the whole drawing, not that small row). A library theme you offer follows the rule in *Theming your renderer*: one entry per family, following the skin's scheme, from the library's pinned source, in one sheet you replace or remove.

## Dragging a file from the files pane

A file dragged from the files pane carries `{ cellId, paths }` (cell-relative paths) as JSON under `CELL_FILES_MIME`, `'application/x-collimate-files'`. A renderer that can take a file of its cell (a link or an image in a document) accepts the drag in `dragover` with `isCellFileDrag(e.dataTransfer)`, which reads only the drag's types (a page cannot read drag data before the drop), and reads it in `drop` with `readCellFileDrop(e.dataTransfer, api)`, which returns the paths only when they are files of **this renderer's cell** (`api.getCellId()`), and `null` for another cell's files, a malformed payload or any other drag. Refuse what it refuses: a renderer only ever reaches its own cell. The markdown editor inserts each dropped path at the drop point (ProseMirror's `posAtCoords`), as its slash menu's "Insert file…" does: `![name](path)` (a preview) for an image, `[name](path)` (a link) otherwise.

```ts
el.addEventListener('dragover', (e) => {
    if (!isCellFileDrag(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
});
el.addEventListener('drop', (e) => {
    const paths = readCellFileDrop(e.dataTransfer, api);
    if (!paths) return;
    e.preventDefault();
    for (const path of paths) insertAt(e.clientX, e.clientY, path);
});
```

## Answering annotation requests

An annotation is a message a user writes to a run in the cell's status pane. Its composer's **Add annotation** asks the file the user last viewed for context: part of the file, chosen in your view, that rides with the message as a chip the user sees and markdown the run reads. The host sends the request to that file's view: the one holding the focus, else the visible one, else the one mounted last. When the file is not open the host opens it in a new tab first, and for a pick it focuses the cell, so the user's first click lands in your view. One request per cell is in flight at a time. A request is a `pick` (the user chooses something in your view now, at their own pace: arm a pick mode and wait for the click) or a `selection` (answer at once from what is selected).

```tsx
import { useCallback } from 'https://esm.sh/react@19.3.1?target=es2022';
import { useCollimateFile, useAnnotationRequest, annotationUnavailable } from '/api/sdk/react.js';

// In a textarea editor's component: the selected text, with where it is.
function useSelectionAnswer(textarea: { current: HTMLTextAreaElement | null }) {
    const { api, fileName } = useCollimateFile();
    const answer = useCallback(async (request: { mode: 'pick' | 'selection' }) => {
        if (request.mode !== 'selection') throw annotationUnavailable('Select some text, then attach it.');
        const el = textarea.current;
        if (!el || el.selectionStart === el.selectionEnd) throw annotationUnavailable('Nothing is selected.');
        const line = el.value.slice(0, el.selectionStart).split('\n').length;
        const quoted = el.value.slice(el.selectionStart, el.selectionEnd).split('\n').map((l) => '> ' + l).join('\n');
        return {
            label: `${fileName} › line ${line}`,
            text: `Selected in \`${fileName}\`, from line ${line}:\n\n${quoted}`,
            anchor: { start: el.selectionStart, end: el.selectionEnd },
        };
    }, [fileName, textarea]);
    useAnnotationRequest(api, answer);
}
```

- **The request** is `{ id, mode, hint? }`: `mode` is `'pick'` or `'selection'`, and `hint` is short host text a pick prompt may show.
- **The answer** is `{ label, text, anchor? }`. `label` is the chip, under 80 characters: the file's name and where in it. `text` is the markdown the run reads (the host keeps at most 16 KiB). `anchor` is a JSON object locating the answer in your view's own terms (at most 4 KiB, dropped when larger). The host stamps the file's path and your renderer's id itself.
- **`null`** means the user abandoned the pick in your view (Esc there). **`annotationUnavailable(message)`** means this view cannot answer (no selection, a mode you do not take, a page that cannot be picked); the host shows `message`. Any other rejection is an error the host reports.
- **`signal`** aborts when the user presses Esc in the app or cancels, sends the message, leaves the file's tab, or after 5 minutes: undo your highlights and listeners then. The host ignores a late answer.
- **One handler per view.** A second subscription replaces the first; `null` (to the hook) or the unsubscribe withdraws it. The hook subscribes once per `api` and always calls the latest callback, so a handler built in render never resubscribes.
- **No requests while `canInteract()` is false** (history, review, diff, a pending structural operation). A run's UI file is read-only but takes them.
- **Iframe renderers** need nothing more: the SDK host inside the frame acknowledges each request (the host waits 2 s for that, then treats the view as not answering), and a frame with no handler answers at once that it takes none.
- **Absent in a sealed preview**: `api.onAnnotationRequest` is optional, and the hook is inert without it.

The html renderer answers `pick` with the document view's element picker (*Picking an element* in *The document view*): the label is `<file> › <CSS path>`, the text names the element's tag, CSS path and id, quotes its text and fences its markup, and the anchor is `{ css, id, snippet }`. The page's own handlers never see the click that picks. It refuses `selection`, and a pick its page's picker does not answer, with `annotationUnavailable`.

## Embeds in the markdown editor

In the markdown editor (the milkdown renderer) `![alt](path)` is a preview and `[text](path)` is a link. A preview's size and alignment live in a pandoc-style attribute block directly after it, `![plot](charts/plot.png){width=60% align=center}`: `width` is a whole percent (1 to 100) of the text column, `align` is `left`, `center` or `right`, either may be left out, and the block is written only when one is set. The alt stays the alt (Insert writes the file's name), and the title is the caption. An image (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.avif`, `.bmp`, `.ico`) or a web URL previews as an image; a video (`.mp4`, `.webm`, `.ogv`, `.mov`, `.m4v`) or an audio file (`.mp3`, `.wav`, `.ogg`, `.oga`, `.m4a`, `.flac`, `.aac`, `.opus`) plays with its controls; any other file of the cell is a card whose Open shows it in a new tab (`api.revealFile(path, { newTab: true })`); and a path the host does not serve is a missing-file card. A link to a file of the cell opens that file in a new tab the same way, from the address in the link's tooltip, a Cmd/Ctrl-click on the link, or any click while the document is read-only; a web URL is not opened. A selected preview has a toolbar (width presets and a size handle, Left, Center and Right, and Link), a link to an image, a video or an audio file of the cell offers Preview, and neither shows while the document is read-only. A generator that writes markdown can size and place its images the same way.

## Reacting to visibility (surviving offscreen)

**Most renderers can skip this section.** A normal React/DOM renderer (text, table, markdown, a form) needs nothing here — the browser preserves DOM, and React state drives the next paint. This matters only for **native** (`embed: "native"`) renderers that paint into a **GPU or `<canvas>` drawing buffer** (xterm.js; a hypothetical WebGL/canvas chart) or hold **imperative visual state** they repaint by hand. Iframe renderers are handled differently — see the note at the end.

**The problem.** To keep many cells cheap, the host scrolls offscreen native cells out by flipping their subtree to `display:none` — and it keeps the renderer **mounted** (so scrollback, editor state, and the WebSocket survive). But `display:none` halts painting and lets the browser **discard a GPU/canvas drawing buffer**. There is no `webglcontextlost` event for this case. When the cell scrolls back, the buffer can be blank and a full-screen TUI or imperative canvas won't repaint on its own — so the surface shows **black/stale** until the next keystroke. Detaching/re-attaching the element on a remount has the same effect.

**The fix — repaint when you become visible:**

```tsx
import { useCollimateFile, useVisibility } from '/api/sdk/react.js';

function MyCanvasRenderer() {
    const { api } = useCollimateFile();
    const { visible } = useVisibility(api);

    useEffect(() => {
        if (visible) repaintMyBuffer();   // e.g. term.refresh(0, rows-1)
    }, [visible]);
    // …
}
```

`useVisibility(api)` returns `{ visible, onChange }`. Like `useTheme`, it initialises to the current state and the subscription fires **immediately on subscribe**, so a renderer that first mounts while hidden still reads the right value. `onChange(cb)` is the imperative handle (returns an unsubscribe) for repainting outside React state. It degrades safely: iframe-embedded renderers always see `visible: true`.

**WebGL context loss is separate.** `display:none` clears the *buffer*; an actual lost GPU context (a reset, or the browser reclaiming contexts when many WebGL surfaces are open) fires `webglcontextlost`. Libraries that own the context usually expose a hook — handle it so you don't get stuck blank:

- **xterm.js**: `WebglAddon.onContextLoss(() => { addon.dispose(); /* reverts to DOM renderer */ refresh(); })`. The built-in `.terminal` renderer avoids the question: it draws with xterm's DOM renderer (xterm 6 ships no canvas renderer), so it holds no WebGL context to lose.
- **Other native canvas libraries**: if the library owns the GL context, wire its loss/restore hooks and repaint on `onVisibleChange(true)`.

**Reference implementation:** `builtin/live/terminal/main.tsx` wires `useVisibility` (repaint on re-show), a ResizeObserver `0→box` repaint (belt-and-suspenders for the same case), and xterm's DOM renderer described above. It is currently the only built-in renderer that needs any of this — every other native renderer is DOM/SVG/`<img>` (retained by the browser across `display:none`, so nothing goes black).

**Don't remount on visibility.** Visibility flips constantly while scrolling; treat it as a lightweight signal (repaint/pause), never as a reason to tear down and rebuild — the host deliberately keeps you mounted so your state survives.

**Iframe renderers (`embed: "iframe"`) don't need this.** They're managed by a warm iframe pool, not `display:none`: offscreen, the iframe is parked with `visibility:hidden`, which preserves its canvas buffer and everything the renderer holds in memory, unsaved edits included, and scrolling back reveals the same document. If the pool recycles a parked slot, the host boots a new frame from the text your `onChange` last reported (a full reload + remount that repaints from scratch). The native visibility signal is *not* forwarded across the iframe boundary, so `useVisibility` reports `visible: true` inside an iframe — calling it there is a harmless no-op. If a future iframe renderer genuinely needs a true on/offscreen signal, forward it over the same postMessage channel the host uses for `SET_THEME`.

## The desktop shell — `api.desktop`

Collimate also ships as an Electron desktop client. Inside that shell a
**native** renderer is handed `api.desktop`: a narrow slice of the shell, not
the shell itself — capability facts and a couple of verbs, never the shell's
own handle. It is `undefined` everywhere else — a browser tab, and any
`embed: "iframe"` renderer (handing untrusted iframe source a shell handle
would cross the boundary the sandbox exists to draw). That single `undefined`
is the whole feature test, and it covers both cases with one fallback path:

```tsx
import { useCollimateFile, useDesktop } from '/api/sdk/react.js';

const { api } = useCollimateFile();
const desktop = useDesktop(api);          // DesktopSurface | undefined

desktop ? desktop.openExternal(href) : window.open(href, '_blank');
```

`useDesktop(api)` is deliberately not a boolean `isDesktop()`: the useful
question is never "am I on the desktop" but "do I have this handle".

```typescript
interface DesktopSurface {
    // What this shell can do. Falsy means NO: a browser tab has no
    // capability map at all, and a shell may carry a key as `false` —
    // so test truthiness (a plain `if`), NEVER presence
    // (`'nativeGuests' in capabilities` is a lie).
    capabilities: Partial<{
        nativeGuests: boolean;          // host a real Chromium view (not an iframe) for a cell
        guestCapture: boolean;          // capture that view's pixels, visible or not
        persistentPartitions: boolean;  // storage jars that survive a restart
        directLoopback: boolean;        // reach 127.0.0.1 directly, unblocked by page CSP
        externalOpen: boolean;          // open a URL in the user's real browser
        customTitleBar: boolean;        // the app draws its own title bar (page chrome, not renderers)
        customWindowControls: boolean;  // …and its own minimise/maximise/close buttons
    }>;

    // Open a URL in the user's real browser, outside the app.
    openExternal(url: string): Promise<void>;

    // Mount a live web page inside `hostEl` and get a handle on it.
    // ABSENT — not a function that fails — unless `nativeGuests` is
    // true, so `api.desktop?.mountGuest` is the whole test for this path.
    mountGuest?(hostEl: HTMLElement, options: {
        src: string;            // initial URL; http(s) or about:blank — the shell refuses the rest
        partition?: string;     // bare storage-jar name (see below)
        device?: 'desktop' | 'phone' | 'tablet';  // UA + viewport the guest presents (default desktop)
        keepAlive?: boolean;    // stay loaded while parked offscreen (default false)
        interactive?: boolean;  // guest takes pointer input (default true)
        guestSessionKey?: string;    // names the session this guest belongs to (see below)
    }): GuestHandle;
}

interface GuestHandle {
    readonly guestId: Promise<string>;    // resolves once the shell has created the guest
    loadURL(url: string): Promise<void>;  // http(s) or about:blank
    goBack(): Promise<boolean>;           // false when there was nowhere to go
    goForward(): Promise<boolean>;
    reload(): Promise<void>;
    evaluate(js: string): Promise<unknown>;   // runs in the guest's main frame
    capturePng(): Promise<ArrayBuffer>;       // needs `guestCapture`
    currentUrl(): Promise<string>;
    on(kind: 'navigate' | 'title' | 'loading' | 'crashed' | 'focus' | 'ready', cb): () => void;
    setInteractive(on: boolean): void;
    setDevice(device: 'desktop' | 'phone' | 'tablet'): Promise<void>;  // switch in place, then reload
    destroy(): void;                      // idempotent
}
```

Event payloads: `navigate` → `{url, inPage}` · `title` → `{title}` ·
`loading` → `{loading}` · `crashed` → `{reason, exitCode}` · `focus` →
`{focused}` (the guest took or lost input focus — the DOM cannot see this) ·
`ready` → `{url}` (a main-frame load finished). Like every other
subscription on `api`, `on()` returns an **unsubscribe**. A crash kills the
guest process but not the mount (or its partition), so the honest response is
an in-place reload affordance, not a teardown.

**Mounting a guest.** Mount once and *navigate* it — a re-mount is a new guest
with a new id and a cold page, so an address-bar commit is `loadURL`, not a
different `src`:

```tsx
function NativePage({ url }: { url: string }) {
    const { api } = useCollimateFile();
    const desktop = useDesktop(api);
    // Callback ref, not useRef: the mount effect must run the moment the host
    // node exists, and must not re-run when the component merely re-renders.
    const [hostEl, setHostEl] = useState<HTMLDivElement | null>(null);
    const [title, setTitle] = useState('');

    useEffect(() => {
        if (!hostEl || !desktop?.mountGuest) return;
        const guest = desktop.mountGuest(hostEl, { src: url });
        const off = guest.on('title', (d) => setTitle(d.title));
        return () => { off(); guest.destroy(); };   // ALWAYS destroy on unmount
    }, [hostEl, desktop, url]);

    if (!desktop?.mountGuest) {
        return <iframe src={url} style={{ width: '100%', height: '100%', border: 'none' }} />;
    }
    return <div ref={setHostEl} style={{ width: '100%', height: '100%' }} />;
}
```

The handle is the **only** way to drive a guest: every op is authorised in the
shell's main process against this mount's own id, so a renderer can neither
see nor address anyone else's guest. `destroy()` releases the *mount*, not the
page — the guest parks warm, and a re-mount in the same spot reattaches to it
with its URL, history and logins intact (ask `currentUrl()` after mounting
rather than assuming your `src`; no navigation event replays for a page that
was already there). Parked ordinary guests are eventually reclaimed by the
host's LRU; `keepAlive` ones live until the app window closes — or until a
mount names a different `guestSessionKey`, or until the host removes the
`.webview` file that showed it.

**The guest's rectangle belongs to the guest.** The view is composited by the
OS above every piece of DOM — no z-index you write can paint over it, and the
same is true of its screenshot stand-in, which lives in the host's overlay
above your renderer's own DOM. Design your chrome *beside* the guest (the
address-bar-above pattern), never on top of it; anything inside the rectangle
must come from the page itself (`evaluate`). The one exception is a crash:
while a guest is crashed the host hides both the view and its stand-in, so an
in-place "reload" panel over the mount point is visible exactly when it is
needed — and a `reload()` that completes brings the guest back over it.

**Partitions.** A guest's cookies and storage live in a partition. Pass a
**bare name**: the host lower-cases it, sanitises it to `[a-z0-9-]`, and
prefixes `persist:collimate-` — the only prefix the shell will attach, so a
name you hand it can never widen its own reach. The default is one jar per
renderer per cell, so two generators' pages can't read each other's cookies
and the same renderer on two cells doesn't share a login the user set up on
one of them. Name a partition explicitly only to deliberately *share* state
across cells. Sanitisation is lossy (`a/b` and `a b` both become `a-b`), so
colliding names silently share a login — pick distinct ones.

**`keepAlive`.** Parked offscreen, a guest is subject to the pool's LRU.
`keepAlive: true` exempts it and keeps it loaded (and unmuted) while parked —
for a guest whose whole value is that it stays live, e.g. one being polled
with `capturePng()` on a cell nobody is looking at. It costs an OS process
until the app window closes: `destroy()` only releases the mount, and
beyond a different `guestSessionKey` or the host deleting the guest's `.webview`
file, nothing else reclaims a keepAlive guest.

**`guestSessionKey`.** Reattachment is keyed on the mount point, which cannot tell
a re-mount of one session from the start of the next: the same cell running
the same renderer a second time would be handed the first run's page, `src`
and all. Pass whatever is unique per session — a run id, a proxy base — and
the host reattaches only within it: a matching key (both absent counts)
reattaches as always, a different one **destroys** the parked guest and mounts
a fresh page on your `src`. Renderers whose guest has no session of its own
can leave it out.

**Pointer input.** The host already drops pointer events for the duration of a
canvas pan (otherwise a drag starting over the guest is swallowed by it), so
`setInteractive(false)` is only for your own modes — an overlay, a resize
handle, a preview that shouldn't be clickable. A non-interactive guest is
shown as a periodically refreshed screenshot that clicks fall straight
through — a live native view cannot pass input through itself.

**Devices.** `device` makes the guest present as a phone or a tablet: the
shell gives it that device's user agent, composed from the shell's own Chrome
version, and emulates a mobile viewport the size of your host element. Size
the host element to the device's width yourself — the guest's rectangle
follows it, so a narrower host centred on your canvas letterboxes the page.
`setDevice(device)` switches a mounted guest in place: the shell changes the
user agent and the emulation, then reloads the page, keeping the same guest,
its partition and its history (no remount, no new `guestSessionKey`). Where
the guest is an iframe fallback the call does nothing — a frame cannot change
its user agent — so letterboxing is all a browser tab gets.

**The `requires` gate.** A generator whose artifact wants shell capabilities
says so *in the artifact* rather than leaving the renderer to guess. Each
tab of a web run is its own file, `_runs/<label>/tab-<n>.webview`, holding the
artifact v4 `{"v":4,"tab","url","device","viewport":{"width"}|null,"control":{"wsBase"},"requires","native":{"partition","device"},"buttons"}`,
and it always carries `"requires": ["nativeGuests"]`. The renderer names its
tab in the control socket's first frame, `{"kind":"hello","tab","client","caps"}`,
and passes `guestSessionKey: wsBase + "#" + tab` to `mountGuest`, so every tab of
every run gets its own guest.
A renderer handed one reads that list against `capabilities` — and what the
gate decides is the **mode**, not whether anything renders at all:

```tsx
const missing = artifact.requires.filter((cap) => !desktop?.capabilities[cap]);
if (missing.length || typeof desktop?.mountGuest !== 'function') {
    return <FallbackSessionView artifact={artifact} />;   // degraded, still live
}
return <SessionView artifact={artifact} desktop={desktop} />;  // native guest
```

That is what the webview renderer does. With the capability it hosts the page
in the shell's own guest view. Without it — a browser tab, above all — it
frames the page best-effort instead: the same toolbar and working address bar,
a banner saying that some sites refuse framing and logins won't persist, and
honest `rpc_error` refusals for the capture verbs only a native view can
serve. Keep the gate as data on the artifact rather than scattered
`if (desktop)` checks: it is the same list on both sides, and the artifact
stays the single place that says what the session wants.

## Diff mode

The app mounts a renderer in diff mode in three places: the zone in the gap between two adjacent columns diffs the right column's visible cell against the left one's (each file against the same-named file in the left cell), the files pane's History diffs two versions of a cell, and a file's **Compare with…** opens a temporary tab in the cell that diffs two files. Two things make a renderer diff-capable:

1. **`supports_diff: true`** in your `my-renderer.renderer`. The app gates the UI on this — false means "my filetype can't be diffed meaningfully" and a diff in the cell shows a placeholder instead of mounting your renderer (**Compare with…** falls back to a plain side-by-side line diff).

2. **Either** an explicit `diffComponent` in `defineRenderer()`, **or** you opt into the built-in default.

### The default diff view

If you set `supports_diff: true` and DON'T provide a `diffComponent`, the SDK mounts a built-in side-by-side text diff: two scrollable columns with line-level add/remove tinting. The columns are titled with the host's names for the two sides when it provides them (the version-history slider names them "v3"/"v4"), falling back to generic "Before"/"After". This is free and works for anything text-shaped (plain text, markdown, HTML source, code).

```yaml
# my-renderer.renderer
supports_diff: true
```

```tsx
// main.tsx
export default defineRenderer({
    component: MyEditor,
    // no diffComponent — you get the default diff view
});
```

### Custom diff component

For a richer diff (side-by-side Monaco, side-by-side structured comparison, visual image diff, etc.), provide your own:

```tsx
function MyDiffView() {
    const { content, diffContent, diffLabels } = useCollimateFile();
    // content = "after" file, diffContent = "before" file
    // diffLabels = host names for the two sides ({ base, current },
    // e.g. "v3"/"v4" from the version-history slider) or null — fall
    // back to your own generic labels when null.
    return <div>…custom diff UI…</div>;
}

export default defineRenderer({
    component: MyEditor,
    diffComponent: MyDiffView,
});
```

Your `diffComponent` receives the same `useCollimateFile()` context as the regular component, with `diffContent` populated as the "before" side and `diffLabels` carrying the host's names for the two panes (null when the host has none — use generic labels then). `isReadOnly` is forced to `true` and `saveContent` is a no-op — diff mode is view-only.

`content`, the "after" side, follows its file as it does in the regular component. A diff the app keeps open against a live file (**Compare with…**, the gap between two columns) gets each later write of that file (a save from its own tab, a run, another writer) as a new `content` (an iframe host may instead remount the diff on the new body), so build the view from `content` on every render, or rebuild it in an effect keyed on it as the Monaco example below does, never once at mount. `diffContent` is fixed for the mount: the host remounts the diff for a new base. Under `deferReady` your `diffComponent` calls `api.ready()` itself, as your component does; the built-in diff view is ready at its first paint.

### When to set `supports_diff: false`

Set it false when line-by-line text diff is meaningless for your filetype — canvas / whiteboard renderers (Excalidraw), binary image viewers, terminal replays. Without a custom `diffComponent` that can render the filetype's diff semantics, the built-in view would just show a wall of serialized JSON or base64.

## Keyboard shortcuts and the iframe host

When your renderer runs inside an iframe (`embed: "iframe"`), the host can reclaim certain modifier combos before your renderer's own keydown listeners see them. The host pushes a dynamic list of combos into the iframe via a `SET_KEYBINDINGS` postMessage; any keydown matching one of those combos is `preventDefault`'d in the iframe's capture phase and forwarded back to the parent as a `FORWARD_KEYDOWN`.

**What this means for renderer authors:**

- Bare keys (`ArrowLeft`, `Enter`, letters with no modifier) are never forwarded — they reach your renderer normally.
- Modifier combos that the user can re-bind in the Commands pane (Shift+Enter on a command), and that have `allowedInRenderer: true` on the host side, are intercepted, as is any modifier combo the user bound to a command there. At time of writing that includes `Alt+<letter>` for stack actions, `Ctrl+Arrow` for stack nav, `Ctrl+Shift+Arrow` for cell move, `Mod+K` / `Mod+,` for the Commands pane / Settings, `Mod+P` for Find file, `Mod+S` for editor save and `Alt+a` for the overview. Escape is not a generic forwarded combo: the iframe gives the first press to its renderer and forwards a second press within 600 ms to the host close action.
- If your renderer needs to own one of those combos (e.g. your editor has its own Mod+S save), either pick a different binding or acknowledge that the host wins. There is currently no API for a renderer to opt out of a forwarded combo.

**What this doesn't affect:**

- `window.addEventListener('keydown', …)` still works for keys the host doesn't claim. The slides renderer's bare ArrowLeft/ArrowRight listener, for example, is untouched.
- Monaco's internal command palette (F1, Ctrl+Shift+P) is untouched — nothing in the host registry binds it.
- Text input inside contenteditable / textarea — typing letters, symbols, and IME composition — is untouched.

## Imperative DOM inside React

If you need low-level DOM control (e.g. mounting an existing non-React library like Monaco or Excalidraw), wrap it in a React component that uses `useRef` + `useEffect` for the mount/cleanup lifecycle. The old `vanilla.js` SDK is no longer available.

```tsx example=renderer-imperative-dom
import React, { useEffect, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile } from '/api/sdk/react.js';

function ImperativeView() {
    const { content } = useCollimateFile();
    const ref = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
        if (!ref.current) return;
        ref.current.innerHTML = `<pre>${content}</pre>`;
        return () => { if (ref.current) ref.current.innerHTML = ''; };
    }, [content]);
    return <div ref={ref} style={{ width: '100%', height: '100%' }} />;
}

export default defineRenderer({ component: ImperativeView });
```

See the `monaco` renderer for a production example.

## Complete Working Examples

### Example 1: Simple Text Editor (Native, React)

The simplest possible renderer. A textarea with a status bar.

**my-renderer.renderer:**
```yaml example=renderer-example-1-manifest
id: "text"
name: "Text Editor"
version: "1.1.0"
summary: "Plain-text viewer and editor."
entrypoint: "main.tsx"
embed: "native"
extensions:
  - ".txt"
  - ".log"
supports_diff: true
```

**main.tsx:**
```tsx example=renderer-example-1-module
import React, { useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';

function TextEditor() {
    const { content, saveContent, isReadOnly, fileName } = useCollimateFile();

    return (
        <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', background: tokens.color.sheet }}>
            <textarea
                value={content}
                disabled={isReadOnly}
                onChange={e => saveContent(e.target.value)}
                spellCheck={false}
                style={{
                    flex: 1,
                    width: '100%',
                    background: 'transparent',
                    color: tokens.color.ink,
                    fontFamily: tokens.font.mono,
                    fontSize: 13,
                    lineHeight: 1.6,
                    padding: 16,
                    boxSizing: 'border-box',
                    border: 'none',
                    outline: 'none',
                    resize: 'none'
                }}
            />
            <div style={{
                height: 22,
                background: tokens.color.sheet2,
                borderTop: `1px solid ${tokens.color.line}`,
                display: 'flex',
                alignItems: 'center',
                padding: '0 12px',
                gap: 12,
                fontFamily: tokens.font.mono,
                fontSize: 10,
                color: tokens.color.ink3
            }}>
                <span>{content.split('\n').length} lines</span>
                <span>{content.length} chars</span>
                <span style={{ marginLeft: 'auto' }}>{fileName}</span>
            </div>
        </div>
    );
}

export default defineRenderer({ component: TextEditor });
```

**Key takeaways:**
- Every style is inline (Shadow DOM requirement for `embed: "native"`), on the skin tokens
- `saveContent()` is called on every keystroke for real-time sync
- `isReadOnly` disables the textarea when the file is read-only
- A controlled `value` moves the caret to the end when another writer's body arrives; the shipped text renderer keeps its textarea uncontrolled and restores the selection (see *The user's unsaved text always wins*)
- Root div fills 100% width and height

### Example 2: Monaco Code Editor (Iframe, React, with Diff Support)

A full code editor using the Monaco library from CDN, with language detection and diff view.

**my-renderer.renderer:**
```yaml example=renderer-example-2-manifest
id: "monaco"
name: "Code Editor"
version: "1.1.0"
summary: "VS Code-powered code editor with language detection."
entrypoint: "main.tsx"
embed: "iframe"
extensions:
  - ".js"
  - ".ts"
  - ".py"
  - ".go"
  - ".json"
  - ".yaml"
supports_diff: true
```

**main.tsx:**
```tsx example=renderer-example-2-module
import React, { useEffect, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, mirrorUrl, resolveVar, syntaxPalette, toHex8, useCollimateFile, useRemoteScript, useTheme } from '/api/sdk/react.js';

// The AMD tree (loader, editor, workers) is classic scripts no import map
// routes, so it loads from the app's module mirror.
const MONACO_BASE = mirrorUrl('https://cdn.jsdelivr.net/npm/monaco-editor@0.57.0/min/vs');

// Monaco's loader.js is AMD: useRemoteScript loads it once (deduped by URL),
// then this promise blocks on the AMD require for the editor itself.
let _monacoPromise = null;
function ensureMonaco() {
    if (_monacoPromise) return _monacoPromise;
    _monacoPromise = new Promise((resolve) => {
        window.require.config({ paths: { vs: MONACO_BASE } });
        window.require(['vs/editor/editor.main'], () => resolve(window.monaco));
    });
    return _monacoPromise;
}

// Monaco cannot read CSS variables: build its theme from the skin's values
// (hex only, so rgba() tokens become #rrggbbaa) and re-apply it on change.
// Token colours come from the skin's syntax palette; inherit: false drops the
// base's own colour table and token rules, inherit: true (no palette yet)
// keeps them. Monaco's colour registry still supplies a default for every
// key left unnamed, so a full theme names every key a visible surface uses;
// this sample names a few, the shipped renderer names them all.
const TOKEN_ROLES = {
    comment: 'comment', keyword: 'keyword', string: 'string', number: 'number',
    type: 'type', identifier: 'variable', delimiter: 'punctuation', operator: 'operator',
    tag: 'tag', 'attribute.name': 'attribute', 'attribute.value': 'string',
    regexp: 'regexp', invalid: 'invalid',
};
function applyMonacoTheme(monaco, theme) {
    const hex = (name) => toHex8(resolveVar(theme, name)) || undefined;
    const palette = syntaxPalette(theme);   // null until the first skin arrives
    monaco.editor.defineTheme('collimate', {
        base: theme.scheme === 'light' ? 'vs' : 'vs-dark',
        inherit: palette === null,
        rules: palette
            ? Object.entries(TOKEN_ROLES).map(([token, role]) => ({ token, foreground: palette[role] }))
            : [],
        colors: Object.fromEntries(Object.entries({
            'editor.background': hex('--sheet'),
            'editor.foreground': hex('--ink'),
            'editorLineNumber.foreground': hex('--ink-3'),
            'editor.selectionBackground': hex('--brass-soft'),
            'editorCursor.foreground': hex('--teal'),
        }).filter(([, value]) => value)),
    });
    monaco.editor.setTheme('collimate');
}

// Language detection from file extension or content
function detectLanguage(content = '', filename = '') {
    const ext = filename.split('.').pop().toLowerCase();
    const extMap = {
        'js': 'javascript', 'jsx': 'javascript', 'mjs': 'javascript',
        'ts': 'typescript', 'tsx': 'typescript',
        'py': 'python', 'go': 'go', 'rs': 'rust', 'java': 'java',
        'c': 'c', 'cpp': 'cpp', 'css': 'css', 'scss': 'scss',
        'html': 'html', 'xml': 'xml', 'svg': 'xml',
        'json': 'json', 'yaml': 'yaml', 'yml': 'yaml',
        'sql': 'sql', 'sh': 'shell', 'bash': 'shell',
    };
    if (extMap[ext]) return extMap[ext];

    // Content-based fallback
    const t = content.trimStart();
    if (/^(import|export|const|let|var|function|class)\b/.test(t)) return 'typescript';
    if (/^(import |from |def |class )/.test(t)) return 'python';
    if (t.startsWith('{') || t.startsWith('[')) return 'json';
    return 'plaintext';
}

// Main editor component
function MonacoEditor() {
    const { content, saveContent, isReadOnly, fileName, api } = useCollimateFile();
    const { onChange: onThemeChange } = useTheme(api);
    const { loading, error } = useRemoteScript(`${MONACO_BASE}/loader.js`);
    const containerRef = useRef(null);
    const editorRef = useRef(null);
    // Set while the effect below applies another writer's body, so that
    // change is not saved back as an edit.
    const applyingRef = useRef(false);

    useEffect(() => {
        if (loading || error || !containerRef.current) return;
        let editor;
        let unsubTheme;
        ensureMonaco().then(monaco => {
            applyMonacoTheme(monaco, api.getTheme());
            editor = monaco.editor.create(containerRef.current, {
                value: content || '',
                language: detectLanguage(content, fileName),
                theme: 'collimate',
                readOnly: isReadOnly,
                automaticLayout: true,
                minimap: { enabled: false }
            });
            if (!isReadOnly) {
                editor.onDidChangeModelContent(() => {
                    if (!applyingRef.current) saveContent(editor.getValue());
                });
            }
            editorRef.current = editor;
            unsubTheme = onThemeChange((theme) => applyMonacoTheme(monaco, theme));
        });
        return () => { if (unsubTheme) unsubTheme(); if (editor) editor.dispose(); };
    }, [loading, error]);

    // Another writer's body: one edit over the whole model, the view state
    // kept (setValue would put the cursor back at the top)
    useEffect(() => {
        const editor = editorRef.current;
        const model = editor?.getModel();
        if (!model || model.getValue() === content) return;
        const view = editor.saveViewState();
        applyingRef.current = true;
        model.pushEditOperations([], [{ range: model.getFullModelRange(), text: content || '' }], () => null);
        applyingRef.current = false;
        editor.restoreViewState(view);
    }, [content]);

    return <div ref={containerRef} style={{ width: '100%', height: '100%', backgroundColor: 'var(--sheet)' }} />;
}

// Diff editor component
function MonacoDiffEditor() {
    const { content, diffContent, fileName, api } = useCollimateFile();
    const { onChange: onThemeChange } = useTheme(api);
    const { loading, error } = useRemoteScript(`${MONACO_BASE}/loader.js`);
    const containerRef = useRef(null);

    useEffect(() => {
        if (loading || error || !containerRef.current) return;
        let diffEditor;
        let unsubTheme;
        ensureMonaco().then(monaco => {
            applyMonacoTheme(monaco, api.getTheme());
            diffEditor = monaco.editor.createDiffEditor(containerRef.current, {
                theme: 'collimate',
                automaticLayout: true,
                renderSideBySide: true,
                readOnly: true
            });
            const lang = detectLanguage(content, fileName);
            diffEditor.setModel({
                original: monaco.editor.createModel(diffContent || '', lang),
                modified: monaco.editor.createModel(content || '', lang)
            });
            unsubTheme = onThemeChange((theme) => applyMonacoTheme(monaco, theme));
        });
        return () => { if (unsubTheme) unsubTheme(); if (diffEditor) diffEditor.dispose(); };
    }, [content, diffContent, fileName, loading, error]);

    return <div ref={containerRef} style={{ width: '100%', height: '100%', backgroundColor: 'var(--sheet)' }} />;
}

export default defineRenderer({
    component: MonacoEditor,
    diffComponent: MonacoDiffEditor
});
```

**Key takeaways:**
- Uses `embed: "iframe"` because Monaco manages its own DOM (needs full document context)
- `useRemoteScript` loads `loader.js` once (deduped by URL); the `_monacoPromise` singleton only wraps the AMD `require` for the editor
- `mirrorUrl` puts the whole AMD tree on the app's module mirror. Monaco starts its workers itself, from `require.toUrl` inside a blob bootstrap, so they load from the mirror too and no `MonacoEnvironment` is needed; the shipped renderer's `mirror_assets.json` names every file that tree loads (its hashed chunks and workers) for the frozen snapshot
- `applyMonacoTheme` defines a `collimate` theme from the skin's values (`resolveVar` + `toHex8`, since Monaco takes only hex) and `useTheme(api).onChange` re-applies it on every skin change
- Cleanup function `editor.dispose()` prevents memory leaks
- Separate `diffComponent` provides side-by-side diff view
- Another writer's body arrives through `content` and lands in place (`pushEditOperations` + the saved view state), never saved back as an edit

### Example 3: HTML Preview (Iframe, React, Sibling File Loading)

A full-page HTML preview that resolves in-cell asset references. The shipped
`html` renderer is exactly this shape.

**main.tsx:**
```tsx example=renderer-example-3-module
import React, { useCallback } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile, useTheme, useStyle, useDocumentView, StyleControl,
    DOCUMENT_VIEW_STYLE } from '/api/sdk/react.js';

// The dark choice: follow the skin (stored as null), as authored, darkened.
const THEMES = [
    { id: 'auto', label: 'Follow the skin' },
    { id: 'authored', label: 'As authored' },
    { id: 'darkened', label: 'Darkened' },
];

// Leave absolute (own-scheme or protocol-relative), self-contained (data:),
// and same-document (#) references alone; everything else is in-cell.
function isExternalRef(url) {
    const u = (url || '').trim();
    return !u || u.startsWith('#') || u.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(u);
}

function rewriteSiblingRefs(html, api) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    for (const el of [...doc.querySelectorAll('[src], [href]')]) {
        for (const name of ['src', 'href']) {
            const url = el.getAttribute(name);
            if (url == null || isExternalRef(url)) continue;
            let path = url.split(/[?#]/)[0];
            while (path.startsWith('./')) path = path.slice(2);
            while (path.startsWith('/')) path = path.slice(1);       // cell root, not app root
            if (!path || path.split('/').some((s) => s === '..')) continue;   // no climbing out
            try {
                // The host encodes path segments and preserves slashes.
                // Resolve relative URLs against the app before entering srcdoc.
                el.setAttribute(name, new URL(api.getFileUrl(path), window.location.href).href);
            } catch (e) { /* unresolvable → leave relative */ }
        }
    }
    const base = doc.createElement('base');   // CSS url(), srcset: the cell's folder
    base.href = new URL(api.getFileUrl(''), window.location.href).href;
    doc.head.prepend(base);
    return '<!doctype html>\n' + doc.documentElement.outerHTML;   // doctype → standards mode
}

function HtmlPage() {
    const { content, api } = useCollimateFile();
    const theme = useTheme(api);
    const style = useStyle(api);
    const dark = style.theme === 'darkened' ? true : style.theme === 'authored' ? false : null;
    // The prepare step: everything else (dark mode, the backing, the wait
    // for the host's skin) is the SDK's document view.
    const prepare = useCallback((html) => ({ html: rewriteSiblingRefs(html, api) }), [api]);
    // The page reports the pointer, so the style control shows near its corner.
    const view = useDocumentView({ html: content, api, theme, dark, prepare, reportPointer: true });
    // No frame: the page fills the cell, on the page's own backing.
    return (
        <div style={view.pageStyle}>
            {view.srcDoc !== null && (
                <>
                    <iframe
                        ref={view.frameRef}
                        title="page"
                        srcDoc={view.srcDoc}
                        onLoad={view.onLoad}
                        style={DOCUMENT_VIEW_STYLE.iframe}
                        sandbox="allow-scripts allow-forms allow-popups"
                    />
                    {/* Top right, only while the pointer is near that corner: the
                        frame swallows the pointer, so the page reports it. */}
                    <StyleControl api={api} frame={view.frameRef} themes={THEMES} font={false} size={false} />
                </>
            )}
        </div>
    );
}

export default defineRenderer({ component: HtmlPage });
```

**Key takeaways:**
- Resolve in-cell refs with **`api.getFileUrl(path)`**, not the old
  `getSiblingFile` → `Blob` → `createObjectURL` loop: `getSiblingFile` decodes
  bytes as text (corrupting images) and a `blob:` URL is keyed to the minting
  realm, so it renders broken inside a sandboxed (opaque-origin) preview.
- **No `allow-same-origin`** on a preview of file content you did not author.
  With it, a `<script>` in the previewed HTML runs *at the app origin* and can
  call `/api/*` as the app itself. The opaque origin an
  `allow-scripts`-only sandbox gives is the boundary; for a page rendered under
  a strict contract (sanitized markup that must never run script) also inject a
  `Content-Security-Policy` meta so a sanitizer miss still cannot run script or
  reach the network.
- The shipped html renderer goes further than this example: it reads each
  reference beside the page (not from the cell root) and follows a click on
  a link to a file of the cell into a new tab (*HTML document resource bases*).
- Editing is a separate concern — offer "Open with → code editor" rather than a
  split-pane Monaco; page mode keeps the preview full-width. For that menu to
  be the *only* way in, exactly one renderer may claim `.html`/`.htm`: the
  frontend breaks a same-length extension tie by `default_for`, then by
  registry order, so a second claimant without one makes the default an
  accident. Every renderer whose `default_for` holds `"*"` (the code editor,
  plain text) is on every text file's "Open with" menu regardless of what the
  manifests claim, so dropping an extension never strands a file.
- `srcDoc` (not `src`) so the string renders directly; emit a `<!doctype>` so
  the preview is in standards mode, not quirks.
- To **darken a page you did not author**, the document view injects
  [DarkReader](https://github.com/darkreader/darkreader) into the `srcDoc`, a
  pinned build the app serves from its own origin (`DARKREADER_PATH`), spliced
  in at the *front* of `<head>` (anything later and the page paints light,
  then flips), rather than putting `filter: invert(1) hue-rotate(180deg)` on
  the iframe. The filter is cheaper and wrong: it inverts photos, video and
  canvas along with the prose, and it cannot tell an already-dark element from
  a light one. "Is it dark" is **the renderer's style wins, else host
  scheme**: the style control's Theme (`darkened`, `authored`, or `auto`,
  stored as null), which the host keeps per channel under the renderer's id.
  It is a *viewing* choice, so it lives in the style, never in the cell:
  writing it into the file would edit the user's page to record how it is
  being looked at. A change rebuilds the `srcDoc` (the page reloads); a skin
  change only posts the new colours and variables block into the page.
- The view does not build the `srcDoc` on the first render of an iframe
  renderer: `api.getTheme()` (and `useTheme`, which seeds from it) returns a
  placeholder with `known: false` until the host posts its theme, and a
  `srcDoc` built on that guess would load the previewed page twice (its
  scripts and network calls run twice). It waits for `known`, bounded by a
  timeout so an embedder that never pushes a theme still shows the page.

### Example 4: Website Viewer (Native, React, No Edit)

A URL viewer with address bar, stripped to its essentials — it treats the file
as a bare URL. Real `.website` pointers are JSON (see *Pointer-File Renderers*
below); the built-in `website` renderer parses that shape.

**main.tsx:**
```tsx example=renderer-example-4-module
import React, { useState } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, tokens, useCollimateFile } from '/api/sdk/react.js';

function WebsiteViewer() {
    const { content, saveContent, isReadOnly } = useCollimateFile();
    const [localUrl, setLocalUrl] = useState(content.trim());
    const [activeUrl, setActiveUrl] = useState(content.trim());

    const commit = () => {
        const u = localUrl.trim();
        saveContent(u);
        setActiveUrl(u);
    };

    return (
        <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', background: tokens.color.sheet }}>
            <div style={{
                height: 32,
                background: tokens.color.sheet2,
                borderBottom: `1px solid ${tokens.color.line}`,
                display: 'flex',
                alignItems: 'center',
                padding: '0 8px',
                gap: 8,
                flexShrink: 0
            }}>
                <input
                    type="text"
                    value={localUrl}
                    onChange={e => setLocalUrl(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && commit()}
                    readOnly={isReadOnly}
                    placeholder="https://..."
                    style={{
                        flex: 1, height: 22,
                        background: tokens.color.sheet,
                        border: `1px solid ${tokens.color.line2}`,
                        borderRadius: 4,
                        padding: '0 8px',
                        color: tokens.color.ink,
                        fontFamily: tokens.font.mono,
                        fontSize: 11,
                        outline: 'none'
                    }}
                />
                {!isReadOnly && (
                    <button
                        onClick={commit}
                        style={{
                            height: 22, padding: '0 12px',
                            background: tokens.color.sheet3,
                            border: `1px solid ${tokens.color.line2}`,
                            borderRadius: 4,
                            color: tokens.color.ink,
                            fontFamily: tokens.font.ui,
                            fontSize: 11,
                            cursor: 'pointer'
                        }}
                    >Go</button>
                )}
            </div>
            {activeUrl ? (
                // A guest page keeps its own colours, white default included.
                <iframe
                    src={activeUrl}
                    style={{ flex: 1, width: '100%', border: 'none', background: '#fff' }}
                    sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                />
            ) : (
                <div style={{
                    flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: tokens.color.ink3, fontFamily: tokens.font.ui, fontSize: 13
                }}>
                    Enter a URL above and press Enter
                </div>
            )}
        </div>
    );
}

export default defineRenderer({ component: WebsiteViewer });
```

**Key takeaways:**
- File content is just a URL string
- `saveContent()` only called on explicit "Go" action (not every keystroke)
- Empty state shows a placeholder message
- Native embed with all inline styles; the chrome is on the skin tokens, the guest page is not

### Example 5: Imperative editor (onSetContent + onFlush)

A renderer whose editor owns its own document model — here a tiny stand-in
for CodeMirror/Monaco/a rich-text engine that you mutate imperatively
rather than re-rendering from `content`. Because the editor isn't driven
by the `content` field, the automatic host-push behaviour doesn't reach
it, so this renderer wires `onSetContent` (follow a host content push in
place) and `onFlush` (finish serialization on demand) itself. A
plain `content`-driven renderer (Examples 1–2) needs **none** of this.

**main.tsx:**
```tsx example=renderer-example-5-module
import React, { useEffect, useRef } from 'https://esm.sh/react@19.3.1?target=es2022';
import { defineRenderer, useCollimateFile } from '/api/sdk/react.js';

function ImperativeEditor() {
    const { initialContent, saveContent, isReadOnly, api } = useCollimateFile();
    const editorRef = useRef(null);   // your real editor instance
    const hostRef = useRef(null);     // DOM node the editor mounts into
    const save = saveContent;

    // Mount the editor ONCE from initialContent. It owns its document
    // model from here — we never re-render it from `content`.
    useEffect(() => {
        const ed = createMyEditor(hostRef.current, initialContent, isReadOnly);
        editorRef.current = ed;
        // Every user edit → serialized snapshot to the host document owner.
        const off = ed.onChange((text) => save(text));
        return () => { off(); ed.destroy(); };
    }, []);

    // Follow host content pushes (an external write) in place — apply the
    // value WITHOUT calling saveContent/reportChange: the host already owns
    // the value it handed us. The equality guard skips a push that matches
    // what the editor already shows. Replace the text as one edit and put
    // the selection and scroll back: a plain setValue would move the caret
    // to the start or the end on every external write.
    useEffect(() => api.onSetContent((next) => {
        const ed = editorRef.current;
        if (!ed || ed.getValue() === next) return;
        const view = ed.saveViewState();
        ed.replaceText(next);
        ed.restoreViewState(view);
    }), [api]);

    // Finish serialization when the host requests a barrier.
    useEffect(() => api.onFlush?.(() => save(editorRef.current.getValue())), [api, save]);

    return <div ref={hostRef} style={{ width: '100%', height: '100%' }} />;
}

export default defineRenderer({ component: ImperativeEditor });
```

**Key takeaways:**
- The editor is mounted once from `initialContent` and owns its document — `content` is intentionally not consumed.
- `onSetContent` applies a host push directly to the editor, keeping the selection and scroll; never re-fire `reportChange` from it.
- `onFlush` serializes the current model as an additional barrier.
- `onSetContent` is available in every host; sibling notifications never choose the primary document.

## Styling Patterns

### Design tokens — `tokens` from `/api/sdk/react.js`

Prefer the `tokens` table to typing `var(--…)` strings by hand. Every value is a reference to a skin token the host writes on `:root`, so the renderer repaints with every skin change, in both embed modes, with no wiring (custom properties inherit into a shadow root).

```tsx example=renderer-tokens-usage
import {
    defineRenderer,
    tokens,
    useCollimateFile,
} from '/api/sdk/react.js';

function MyRenderer() {
    const { content, saveContent, isReadOnly } = useCollimateFile();
    return (
        <div style={{
            background: tokens.color.sheet,
            color:      tokens.color.ink,
            border:     `1px solid ${tokens.color.line}`,
            fontFamily: tokens.font.ui,
            borderRadius: tokens.radius,
            padding: 16,
        }}>
            …
        </div>
    );
}

export default defineRenderer({ component: MyRenderer });
```

What's in the table (meanings in *Theming your renderer*):

```ts example=renderer-tokens-table
tokens.color = {
    canvas, sheet, sheet2, sheet3,       // grounds, lowest to highest
    line, line2,                         // hairlines, control borders
    ink, ink2, ink3,                     // text: primary, secondary, hints
    brass, brassSoft,                    // the selection, and only that
    teal, tealSoft, tealSheet,           // live / on-state
    red, redSoft, green,                 // destructive, pass
    codeBg, proseInk,                    // code and terminals, prose text
};
tokens.font = { ui, mono, prose };
tokens.radius = 'var(--radius)';
tokens.shadow = 'var(--shadow)';
tokens.lift = 'var(--lift)';
```

The values are `var(--…)` references, not colours: do not copy them into snapshots or pass them to a library that needs a concrete colour. For that, read the record (`useTheme(api).vars`, `resolveVar`, `toHex8`) and rebuild on change.

`tokens` is `Object.freeze`'d so a buggy renderer can't mutate the table for every other renderer in the same shadow root.

### Inline Style Object Patterns

**Full-bleed container:**
```typescript
style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column', background: tokens.color.sheet }}
```

**Split pane (horizontal):**
```typescript
<div style={{ display: 'flex', width: '100%', height: '100%' }}>
    <div style={{ flex: 1, borderRight: `1px solid ${tokens.color.line}` }}>Left</div>
    <div style={{ flex: 1 }}>Right</div>
</div>
```

**Status bar:**
```typescript
<div style={{
    height: 22,
    background: tokens.color.sheet2,
    borderTop: `1px solid ${tokens.color.line}`,
    display: 'flex',
    alignItems: 'center',
    padding: '0 12px',
    gap: 12,
    fontFamily: tokens.font.mono,
    fontSize: 10,
    color: tokens.color.ink3
}}>
```

**Monospace textarea:**
```typescript
<textarea style={{
    flex: 1,
    width: '100%',
    background: 'transparent',
    color: tokens.color.ink,
    fontFamily: tokens.font.mono,
    fontSize: 13,
    lineHeight: 1.6,
    padding: 16,
    border: 'none',
    outline: 'none',
    resize: 'none'
}} />
```

### Injecting CSS (for Native Renderers That Need Classes)

If you must use CSS classes in a native renderer, inject them into the shadow root:

```typescript
function MyRenderer() {
    const { api } = useCollimateFile();

    useEffect(() => {
        api.injectCss(`
            .my-container { display: flex; gap: 8px; }
            .my-button { background: var(--sheet-2); color: var(--ink); border: 1px solid var(--line-2); padding: 4px 12px; }
            .my-button:hover { background: var(--sheet-3); }
        `);
    }, []);

    return <div className="my-container">...</div>;
}
```

For external CSS (e.g., from a CDN; the host loads an `https://esm.sh/` or `https://cdn.jsdelivr.net/` stylesheet from the app's module mirror):
```typescript
api.loadCss('https://cdn.jsdelivr.net/npm/some-library@1.0.0/dist/styles.css');
```

## Loading External Libraries

### Pattern: `useRemoteScript` / `useRemoteModule`

Don't hand-roll a `<script>` loader or a module-level promise: the SDK ships both hooks, and each dedupes by URL across renderers (a failed load is evicted so a later mount retries).

```typescript
import { mirrorUrl, useRemoteScript, useRemoteModule } from '/api/sdk/react.js';

const LIB_VERSION = '1.0.0';
const LIB_CDN = `https://cdn.jsdelivr.net/npm/my-lib@${LIB_VERSION}/dist/index.js`;

// UMD/AMD bundle that exposes a window global (a <script>, so mirrorUrl):
const { loading, error } = useRemoteScript(mirrorUrl(LIB_CDN));
useEffect(() => {
    if (loading || error) return;
    const lib = window.MyLib;
    // Use lib here
}, [loading, error]);

// ESM module:
const { module, loading: modLoading, error: modError } = useRemoteModule('https://esm.sh/some-library@1.0.0?target=es2022');
```

### Pattern: ESM Import from CDN

```typescript
import SomeLib from 'https://esm.sh/some-library@1.0.0?target=es2022';
```

Pin the version and `?target=es2022` (use `&target=es2022` when the URL already carries a query string) — unpinned, esm.sh picks the build target from the User-Agent and two imports can land on two different module instances.

### Pattern: assets outside the module graph — `mirrorUrl`

An import map routes ES module imports only. Everything else a library loads from `https://esm.sh/` or `https://cdn.jsdelivr.net/` reaches the app's module mirror only through `mirrorUrl(url)` from `/api/sdk/react.js`: a stylesheet you `fetch`, a font (`FontFace`, or a library's asset path), wasm, a `<script>` (an AMD or UMD loader), a worker's script URL. It returns the app's same-origin copy of that URL, and any other URL unchanged; in a sealed preview (no app beside the runtime) it returns every URL unchanged. `api.loadCss(href)` maps the same way, so pass it the upstream URL. A stylesheet's relative `url()`s resolve against where it was loaded, so mirrored CSS finds its fonts in the mirror too.

```typescript
import { mirrorUrl } from '/api/sdk/react.js';

const WASM = 'https://cdn.jsdelivr.net/npm/some-engine@2.1.0/dist/engine.wasm';
const engine = await createEngine({ wasmUrl: mirrorUrl(WASM) });
api.loadCss('https://cdn.jsdelivr.net/npm/some-library@1.0.0/dist/styles.css');
```

The shipped renderers' graph is frozen in the repository (`module-snapshot/`), whose freeze finds its roots as the quoted upstream URLs in their sources. A loader that fetches files by a computed name (Monaco's AMD tree, Excalidraw's and KaTeX's fonts) names them in a `mirror_assets.json` beside the renderer's definition: `{"assets": [{"base": "https://cdn.jsdelivr.net/npm/<pkg>@<version>/<dir>/", "files": ["a.js", "sub/b.woff2"]}]}`. An authored renderer needs none of that: its first online use fills the mirror.

## Hosting Modes: Native vs Iframe

### Native (`embed: "native"`)

- Renderer is mounted inside a Shadow DOM container in the main page
- **Pros**: Better performance, shared memory, no postMessage overhead
- **Cons**: CSS isolation via shadow root means only inline styles or injected CSS
- **Use for**: Simple renderers, text editors, status displays, URL viewers
- **Most built-in renderers are native** (text, markdown, terminal, table, image, pdf, …)

### Iframe (`embed: "iframe"`)

- Renderer is mounted inside a sandboxed iframe
- **Pros**: Full CSS support (classes, external sheets), complete DOM isolation
- **Cons**: Higher memory usage, postMessage-based communication
- **Use for**: Complex renderers needing external CSS libraries, security-sensitive content
- **Built-in iframe renderers**: monaco, milkdown, html, excalidraw

### How the Runtime Loads Your Renderer

1. The parent fetches `/api/renderers/{id}/source?v={source_hash}` for the selected package hash.
2. A cache miss loads `/api/renderers/{id}/package?v={source_hash}` and compiles the exact bytes in a worker using local esbuild-WASM. The parent publishes the result through `/api/renderers/{id}/bundle`; a stale identity refuses rather than loading the old bundle.
3. SDK import paths (`/api/sdk/react.js`) are rewritten to absolute URLs.
4. The compiled code is loaded as a dynamic ES module. The document's import map resolves its `https://esm.sh/` and `https://cdn.jsdelivr.net/` imports to the app's module mirror. The mirror rewrites the specifiers inside each mirrored module to relative ones (to absolute upstream URLs across the two hosts), so a graph stays inside the mirror wherever it is resolved, workers included, which have no import map.
5. `defineRenderer`'s `mount(container, api)` is called
6. On unmount it unmounts the React root, so clean up in your `useEffect` returns

## Checklist for Creating a New Renderer

1. Create directory: `content/renderers/<group>/my_renderer/` (a new top-level folder becomes its own seeded cell; list it in `content/suggested/standard.json`)
2. Write `my-renderer.renderer` with id, name, version, entrypoint, embed mode, extensions
3. Write `main.tsx` (or `.ts`/`.js`)
4. Import React from `https://esm.sh/react@19.3.1?target=es2022` and SDK from `/api/sdk/react.js`
5. Create your component using `useCollimateFile()` hook
6. Export via `defineRenderer({ component: MyComponent })`
7. If supporting diff mode, add `diffComponent` to `defineRenderer()`
8. **If embed is "native"**: use ONLY inline styles, no className/Tailwind
9. **If embed is "iframe"**: any styling approach works
10. Ensure your root element is `width: 100%` and `height: 100%`
11. Handle both `isReadOnly === true` (viewer) and `isReadOnly === false` (editor) states
12. Call `saveContent()` to push edits back (not needed for read-only renderers)
13. Release resources (editors, listeners, observers) in `useEffect` cleanup returns — `defineRenderer` owns mount/unmount
14. Every esm.sh import carries a version and `?target=es2022` (or `&target=es2022`) — all of them, or none; a mixed file can end up with two instances of the same module
15. Validate the `.renderer` definition through its accepted handler: schema, native package compilation and static default-export checks must pass. Then mount it to exercise runtime behavior. See [Verifier authoring](how-to-write-a-verifier.md).

## Renderer Resolution Order

When a file needs to be displayed, the frontend picks a renderer using this priority:

1. **Images, PDFs and binaries** - An image extension (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.bmp`, `.ico`, `.avif`, `.tiff`, `.tif`) opens in the `image` renderer and `.pdf` in the `pdf` renderer (both read the file's bytes through `api.getFileUrl`); a binary one (`.zip`, `.tar`, `.gz`, `.mp4`, `.webm`, `.wasm`) opens in the built-in binary view. Preferences change none of them.
2. **User preferences** - The first preference whose extension ends the filename
3. **Extension match** - The renderer with the LONGEST matching entry in its `extensions` (`.slides.md` beats `.md`). Two renderers claiming the same extension tie by registry order, which is an accident of seeding order; to make a renderer the default, drop the claim from the other manifest.
4. **Wildcard preference** - A `*` preference, if the user set one
5. **Hardcoded fallbacks**:
   - `.md`, `.markdown`, `.mdx` -> milkdown (iframe)
   - `.excalidraw`, `.excalidraw.json` -> excalidraw (iframe)
   - `.txt`, `.log`, `.env`, `.gitignore`, `.sh`, `.bash` -> text (native)
   - Everything else -> monaco (iframe)

If your renderer has `extensions: []` (empty), it will only be used when explicitly selected by the user or referenced by name in the code.

### Pointer-File Renderers (`.website`, `.webview`, `.terminal`, `.chat`, `.run`)

A few built-in renderers dispatch on files whose contents are a small JSON or text *pointer* rather than the thing to display. The renderer opens an iframe or WebSocket against the pointer's target at mount time.

Chat and terminal pointers live in `_runs/<label>/` and are swept at run end.

**Starting.** A run's surfaces show the moment the host accepts the run, before its process exists. For each file the generator's manifest `opens`, the host writes a placeholder — `{"starting": true, "label": <runner name>}` in a pointer file (`.chat`, `.terminal`, `.webview`, `.website`) — and holds the path as a starting surface, so `getSurface()` answers `state: "starting"` with empty coordinates. The run replaces the placeholder with its real pointer, and `onSurfaceChange` fires. A renderer shows the starting surface and takes the user's input at once: the assistant keeps its composer open and queues messages (shown as queued) above its per-session remount, sending them in order on the real session's socket and letting them go only once the attach is confirmed (the host's first frame on that socket: the host accepts a refused attach, 4409/4404/4401, and then closes it, so an open alone proves nothing; a socket that closes first keeps them queued for the next one); the terminal buffers keystrokes (up to 64 KiB) and sends them after the first output of the first real session; the webview shows its toolbar disabled. A run that ends without taking queued input gives it back to the user, never drops it silently.

| Extension | Renderer | Pointer contents | Generator API |
|---|---|---|---|
| `.run` | `run` (native) | The run card: JSON `{"v":1,"run_id","runner":{"id","name"},"generator","label","folder","started_at"}`, written by the HOST at `_runs/<label>/<label>.run` before the run starts and removed when it ends — the run's first locked file, so a run always has a tab. The renderer shows the runner, generator, label and start time. | None — no generator writes it. |
| `.website` | `website` (native) | JSON `{"url","directUrl","loadingCover","buttons"}` — `url` is the proxy path browsers iframe, `directUrl` the loopback URL a native guest view loads. Each of `buttons` (`[{"id","label"}]`) renders in the preview's toolbar, and a click emits its `id` with `api.emitAction`, so it reaches the generator as an action whose source is the `.website` file (the toolbar shows "`<label>` sent" / "`<label>` failed"). Launch's serve mode declares Re-run, which runs its command again. | `gen.serve_port()` + `collimate.pointers.website_pointer(port, buttons=[...])` |
| `.webview` | `webview` (native) | JSON artifact v4 `{"v":4,"tab","url","device","viewport":{"width"}\|null,"control":{"wsBase"},"requires":["nativeGuests"],"native":{"partition","device"},"buttons"}`, one file per browser tab at `_runs/<label>/tab-<n>.webview` (`tab` is the file's stem, `tab-<n>`). `device` is `desktop`, `phone` or `tablet`; `viewport.width` (412 for a phone, 800 for a tablet, `null` on desktop) is the width the renderer letterboxes the guest to. The renderer opens the control socket at `<wsBase>__collimate/ws`, says `{"kind":"hello","tab","client","caps"}` first, and mounts its guest with `guestSessionKey` `wsBase#tab` and the artifact's `device`. | The web generator's daemon (`gen.update_ui_file("tab-<n>.webview", ...)`). Each tab's toolbar carries the artifact's `buttons` — Capture (when the run captures) and New tab — and a Desktop / Phone / Tablet toggle; each click comes back as an action whose source is that tab's file: Capture captures that tab's guest into the cell, New tab writes the next tab file and reveals it, and `device` (payload `{device}`) rewrites that tab's file with the new device, which the renderer applies with `setDevice`. |
| `.terminal` | `terminal` (native, xterm.js) | JSON `{"terminal_id","ws_path","token","label"}` (+ optional `"buttons"`: `[{"id","label"}]`, each with an optional `"title"` — its tooltip — and `"confirm"` — a question asked inline before the click is sent) written as a single file. Each button renders in the toolbar and a click emits its `id` with `api.emitAction` (the toolbar shows "`<label>` sent" / "`<label>` failed"); once the session has ENDED (4404/4401) the toolbar offers no button but Reconnect. The Agent's Claude Code terminal declares `{"id":"capture_chat","label":"Capture Chat"}`. | `gen.serve_terminal()` (or `gen.start_terminal()` + `collimate.pointers.terminal_pointer`) |
| `.chat` | `assistant` (native) | JSON `{"chat_id","ws_path","token","label","threads_dir","active_thread","threads"}` — the run's chat session coords. `threads` is a list of `{id,title,file}` entries; each thread's transcript is `<threads_dir>/<id>.json` (default: one `main` thread). | `gen.serve_chat(on_turn, pointer_file=..., threads_dir=...)` writes and reveals the declared pointer (the manifest's `ui_files` must declare `.chat` and `.json`), or build it with `collimate.pointers.chat_session_pointer` |

On the generator side `gen.serve_terminal(..., pointer=gen.run_path("app.terminal"))` writes and reveals the pointer and deletes it on exit for you; `gen.update_ui_file("preview.website", json.dumps(website_pointer(port)))` + `gen.commit()` + `gen.reveal_file(...)` covers the website case (the generator's manifest declares both suffixes in `ui_files`). The renderer handles the WebSocket upgrade, PTY lifecycle, and reconnects entirely on the frontend side — via the session-socket module below.

Writing a custom renderer that claims `.terminal` or `.website` is possible but rarely useful — prefer a new extension for your own pointer format.

### Session sockets — `/api/sdk/session.js`

If your renderer opens a WebSocket against a host session (a custom pointer format, a live stream from a daemon generator), **do not hand-roll the connection lifecycle**. The platform serves a framework-free module with the one canonical reconnect state machine, and every built-in live renderer (terminal, assistant, webview) is built on it:

```tsx
import { openSessionSocket, sessionWsUrl, isSessionDead, clearDeadSession }
    from '/api/sdk/session.js';

const sock = openSessionSocket({
    // Always build the URL with sessionWsUrl — it resolves against the API
    // origin (split-origin aware), not the page origin.
    url: sessionWsUrl(api, pointer.ws_path, pointer.token),
    // With connectionKey set, a session the server declared gone (close 4404/4401)
    // is remembered module-wide: remounts render "ended" instead of redialing
    // forever. Check isSessionDead(id) before building expensive per-session
    // state; call clearDeadSession(id) (or sock.redial()) on an explicit
    // user Reconnect.
    connectionKey: pointer.terminal_id,
    binaryType: 'arraybuffer',            // for binary streams (PTY bytes)
    onStatus: (st) => { /* 'connecting' | 'open' | 'closed' | 'busy' | 'ended' */ },
    onOpen:   (ws) => { /* reset per-connection state, push initial size, … */ },
    onMessage: (ev) => { /* your frame reducer — the only part you own */ },
});
sock.sendJson({ t: 'resize', cols, rows });  // OPEN-guarded; returns false when not open
sock.close();                                 // in your unmount cleanup — final, no redial
```

The close-code policy is centralized and matches the host contract: **4401/4404** (session gone — run stopped, server restarted) end the socket for good and mark the session dead; **4409** (another mounted view holds the single-attach session) polls slowly to take over when the holder unmounts; anything else retries with backoff. Tune per surface without re-implementing the machine:

| Option | Default | Use |
|---|---|---|
| `transientRetryMs` | `min(500·attempt, 5000)` backoff | `false` for stateful single-attach sessions (a PTY) where reconnects must be an explicit user action; a fixed number for simple streams. |
| `busyRetryMs` | `5000` | `false` to park on `'busy'` instead of polling for takeover. |
| `maxFailedOpens` | unlimited | Give up (`'ended'`) after N consecutive dials without a received frame, including an upgrade that opens and closes without a frame. A received frame resets the count. |

A concrete retry trace (the `open` event alone does not confirm a dial):

```json example=renderer-retry-ended
{"maxFailedOpens":2,"attempts":[["open","close:1006"],["open","close:1006"]],"status":"ended"}
```

Receiving a frame on the second attempt resets that consecutive-failure count:

```json example=renderer-retry-reset
{"maxFailedOpens":2,"attempts":[["open","close:1006"],["open","message","close:1006"]],"status":"closed"}
```

The module is deliberately **framework-free**: unlike `/api/sdk/react.js` (which pins React 19.3.1) it can be imported by a renderer that pins its own React version, or none at all. Spec + npm artifact: `@collimate/renderer-hooks/session`.


### Editing and approving definitions in the app

The app owns forms for `.renderer`, `.generator`, `.runner`, `.handler` and
`.options_stack` files. Saving a form edits that selected cell file; it does not
approve the package. The SDK schema form handles `.set_yaml` settings only.
Renderer Preview creates a `.renderer_example` beside the package, and the
mounted package hash is temporary evidence rather than approval authority.
A runner form can use the generator schema and named configs drafted in the same
cell, without first approving that generator. Its save updates the runner file
and companion `files/` subtree while preserving unrelated files.

The cell's files panel (**Find file** on the files rail, or `Mod+P`) offers
**Add to Capabilities…** for definitions in any cell that is not itself on
Capabilities. Its confirmation step shows the targets (New cell first, then
every Capabilities cell, with a cell that already holds the same package
preselected), metadata, the package's content hash and last mounted preview
hash, and its exact file paths. Replacing a package needs the explicit Replace
switch. The server refuses changed source hashes or target revisions; the user
must inspect the refreshed preview before retrying.

The Capabilities stack's Manage view lists every generator, runner, renderer
and handler in the stack. A renderer row has its availability switch and
default-for extensions. Each handler is a card with its status and actions:
Validate (each example file's verdict from the latest build's checks against
its declared expectation), Build… (opens a work stack beside Capabilities with
a copy of the handler's folder and a brief; any runner can do the work there,
and the result comes back through that stack's files panel with Add to
Capabilities and Replace), Review and accept (an inspection of the build
waiting for review, then acceptance of exactly the inspected content hash), Add
example and Edit (name, types, guidance and examples).

The built-in handlers on the Definitions card are read-only: their definition,
guidance, verifier and shipped examples are part of the app. A channel can only
add examples to one and regenerate its skills. Its card offers Examples (the
shipped examples read-only; Add example, and editing or removing an added one),
Validate, Regenerate skills… (opens a work stack whose brief rewrites only the
skills; the checks do not change, and **Use these skills** in that stack's
files panel sends them back), Review and accept, and Use the app's skills
(which drops the channel's regenerated skills). Added examples and regenerated
skills stay in the channel: exports and channel copies leave them out.

### Sealed previews of an unapproved renderer

The preview chrome states that the renderer is unapproved and can reach the
network during explicit dependency preparation. Preview untrusted code on an example file.
Frame self-navigation can send data; per-mount edit permission does not remove that outbound channel.

The shipped `.renderer_example` renderer reads a JSON pointer such as
`{"renderer":"my-renderer/main.renderer","file":"sample.txt"}`. The definition
must be inside its package folder, and the sample must exist in the same cell.
It compiles that package with the same pinned virtual-file compiler and mounts
it in a fresh opaque iframe (`sandbox="allow-scripts"`). Both native and iframe
renderers can be previewed; a native renderer still receives its own shadow root.
A successful mount reports the original package content hash as preview evidence.
The temporary compiler wrapper hash is not approval evidence, and previewing never
adds the code to the trusted renderer cache.

Preview reads address only the current cell. Writes initially refuse with
`preview is read-only`. **Allow edits for this preview** grants writes for that
mount only; replacing the preview or changing package content resets it. A hard
read-only host cannot grant edits. Even with permission, writes into the renderer
package and writes to `.renderer`, `.generator`, `.runner`, `.handler`, or
`.options_stack` files are refused, case-insensitively. Selected-file edits and sibling writes use the same guarded host
path. Desktop and other native-only features report `not available in preview`.
Pending saves participate in the ordinary bounded renderer flush barrier.

The preview CSP blocks connect requests, forms, parent DOM and browser storage.
It permits inline/data script modules from the frozen dependency graph;
images are limited to the actual API origin and data URLs; fonts to that origin,
the app origin and data URLs; styles are inline plus the app origin's font
stylesheet (`/fonts/fonts.css`, which the frame imports). The preview mirrors the
host's theme record onto the sealed frame's `:root` (every token by CSS name,
`color-scheme` and `data-theme`) on mount and on every skin change, so a previewed
renderer sees `var(--sheet-2)` and the bundled fonts as it would in the app; it
sets the user's spellcheck preference on that `<html>` the same way, and its
`api.getEditing()` / `api.onEditingChange` follow it. Browser CSP does
not prevent the frame navigating itself to another URL, which can send outbound
data. A subsequent frame load revokes the preview hash, edit grant and RPC owner,
rejects pending flushes and removes the frame; this cannot undo the navigation
request already sent. These are explicit platform limits of the selected preview
policy, not a zero-egress guarantee. Compiled code is inserted as module text, never
interpolated into the surrounding HTML. The initial bootstrap transfer checks frame source, opaque origin, current mount
and token exactly once, before importing untrusted code. All reads, writes,
replies, permission changes and flushes then use a document-owned MessagePort;
there is no WindowProxy/token RPC fallback or replacement-document reacquisition.
Cleanup closes the port. A navigated document cannot recover it from a copied
token even if it suppresses the original document's pagehide notification.

`api.preview.compile` accepts at most 5,000 files and 64 MiB of exact bytes, with at
most four simultaneous preview requests per host page. Its returned `runtime`
contains the actual served React/session SDK modules (at most 2 MiB each), and
`apiOrigin` identifies the served environment; neither grants authority. Runtime
fetch failures are retryable. The compiler has its existing 30-second deadline;
preview writes and replies remain tied to their originating mount.

### Handler rendering aliases

A handler type can set `render_as` to a different extension. The host resolves
that suffix once, using the longest case-folded handler type. For example,
`notes.llm_summary` presents as `notes.md` through `getFilename()` while the
stored file retains its original name. A channel preference for the real
extension takes precedence when choosing the renderer. Open with and the
Add-file extension list use the same rules. The renderer catalog keeps declared
`extensions` separate from handler-derived `fallback_extensions` so these rules
cannot chain.

The host maps only its own aliased path back to the real file in
`getSiblingFile`, `getFileUrl` and `saveSiblingFile`. Own-file change notifications
also use the presented name, keeping `getSiblingFile(getFilename())` live refresh
working. All other sibling paths remain real paths. A renderer that derives
another sibling path from `getFilename()` therefore derives it from the alias;
use explicit real sibling paths when that distinction matters.

### HTML document resource bases

The shipped HTML renderer reads every reference in a page as a browser reads
it beside the page: relative to the page's own folder (`../` climbs, within
the cell), with a leading `/` meaning the cell root. A scheme, a
protocol-relative `//host`, a pure `#anchor` or a path climbing out of the
cell is not a cell file and is left as written. `src` and `href` attributes are
rewritten to the files' revision-pinned URLs, and a first `<base>` points at
the page's folder under the cell's capability file directory, so CSS, `srcset`
and script-created URLs resolve beside the page instead of against the app
route. Its own frame does not add a `base-uri` CSP restriction; the separate
renderer-example preview intentionally has a different policy.

Keep path segments visible in file URLs: a stylesheet at `styles/main.css`
resolves `img/icon.svg` relative to `styles/`. Whole-path URL encoding does not
inherently produce a404; the reason for segment encoding is correct relative
resolution of sub-resources. Preview rewriting leaves stored HTML unchanged.

A click on a link to a file of the cell opens that file in a new tab of the
cell under its own renderer (`api.revealFile(path, { newTab: true })`), so the
page's tab stays. The page's frame is a separate, opaque browsing context the
renderer cannot script, so the prepare step puts a small click bridge into the
page's head beside the `<base>`: on a primary click the page did not handle
itself, on a link whose address lies under the cell's file base, it cancels
the navigation and posts the address to the renderer, which reads it back to a
cell path (the page is not trusted to name one) and reveals it. A `#anchor`
scrolls the page in place. A web URL keeps the browser's behaviour: it
navigates the frame (a site that refuses framing shows the browser's error
page), and one with `target=_blank` does nothing, because the host's sandbox
around an iframe renderer allows no popups. The bridge listens on the page's
window in the bubble phase, ahead of the page's own scripts, so a page handler
that stops the click's propagation keeps it from the bridge (the frame then
shows the file's raw URL), and a page whose own Content-Security-Policy
refuses inline script runs without it.


### Session socket refusal and retry accounting

The generated `/api/sdk/session.js` helper owns pointer-socket retry and dead-session state. A received frame confirms a dial and resets consecutive failed-open accounting; an upgrade that opens and then closes without any frame still counts toward `maxFailedOpens`. Accepted sockets expose application close codes such as 4401, 4404 and 4409. A rejection before WebSocket acceptance appears to browser code as 1006, so do not expect the rejected-upgrade application code there. The helper retains immediate open callbacks; it does not add a wire handshake or a timed acceptance grace period.

### Preparing sealed preview dependencies

`api.preview.prepareDependencies(files, entrypoint, signal?)` is an explicit online action. The sealed preview offers **Prepare dependencies** when a pinned dependency is missing, and labels it as the only preview action that accesses the network. Preparation compiles and parses literal import/export edges outside the frame, downloads inert module bytes through the app's module mirror, executes nothing, and freezes exact bodies and SHA-256 hashes locally for that source identity. An optional AbortSignal cancels preparation when its owner unmounts. `api.preview.compile` never downloads dependencies: it rejects unprepared literal dependencies. Computed imports use a confined importer: targets in the frozen graph succeed; targets outside it refuse. Add a literal import edge when an otherwise computed dependency must be prepared. Runtime and common React/ReactDOM dependencies ship prepared and share one React identity.

The compilation result carries `imports`, the frozen offline import map, and `artifact_hash`, which includes source, runtime and prepared dependency bodies/hashes. Package approval continues to use the unchanged package content identity. The sealed frame has no CDN script permission, no connection permission and no dependency proxy. Literal imports outside its frozen graph fail closed. An embedded preview cannot prepare dependencies or expand its initial graph. Read/write approval and navigation revocation are unchanged.

Pinned module roots use HTTPS with an exact semantic version in the path (for example `https://esm.sh/demo@1.2.3`, `https://cdn.jsdelivr.net/npm/demo@1.2.3/dist/main.js`, or `https://unpkg.com/demo@1.2.3/module.js`), or a complete 40/64-character hexadecimal commit/content-hash path segment. Credentials, fragments, known moving tags and version ranges refuse. There is no finite dependency origin catalog. Same-origin child URLs such as esm.sh `/node/process.mjs` need no separate version token: their exact bytes and hashes belong to the frozen graph. Cross-origin child URLs require the same explicit root pin format. Bounds are 512 modules and 64 MiB per graph, and preparation has a 30-second deadline composed with cancellation.
