# Renderer credits

The renderers under `content/renderers/` (the `documents/`, `status/`, `visual/`
and `utility/` cells) and under `builtins/` (the app's built-in `definitions/`,
`live/` and `text/` cells) are thin Collimate wrappers around
upstream open-source libraries. Each renderer names its dependencies by
pinned CDN URLs (`https://esm.sh/…?target=es2022`, `https://cdn.jsdelivr.net/…`),
and Collimate redistributes them: `module-snapshot/` holds the shipped
renderers' whole module graph, frozen from those CDNs, with esm.sh's module
bodies rewritten (their import specifiers made relative so the graph loads
from the app's own module mirror). `module-snapshot/LICENSES.json`
names every redistributed package and version with its SPDX license and its
source, and the SDK's tests allow only an explicit list of licenses there.
Licenses and copyright remain with the upstream projects.

This file exists so downstream distributors have a single place to point
end users for attribution.

| Renderer | Upstream component | Upstream license | Upstream repo |
|---|---|---|---|
| `monaco` | Monaco Editor | MIT | https://github.com/microsoft/monaco-editor |
| `milkdown`, `todo` | Milkdown + Crepe | MIT | https://github.com/Milkdown/milkdown |
| `excalidraw` | Excalidraw | MIT | https://github.com/excalidraw/excalidraw |
| `mermaid` | Mermaid | MIT | https://github.com/mermaid-js/mermaid |
| `slides` | Collimate's own parser + React | MIT (this repo) | — |
| `table` | React + Collimate table code | MIT-compatible | — |
| `terminal` | xterm.js + addons | MIT | https://github.com/xtermjs/xterm.js |
| `schema_form` | Collimate's own schema-driven form engine | MIT (this repo) | — |
| `assistant` | assistant-ui (`@assistant-ui/react`, `@assistant-ui/react-markdown`, `@assistant-ui/react-opencode`) + lucide-react | MIT / ISC | https://github.com/assistant-ui/assistant-ui , https://github.com/lucide-icons/lucide |
| `website` | browser `<iframe>` | native | — |
| `html`, `todo` | browser `<iframe>` + Dark Reader (served by the app from its own origin, pinned 4.9.133) | MIT | https://github.com/darkreader/darkreader |
| `pdf` | EmbedPDF (`@embedpdf/*`, pinned 2.15.1) and its PDFium WebAssembly build (`@embedpdf/pdfium`, from jsdelivr) | MIT (EmbedPDF) / BSD-3-Clause (PDFium) | https://github.com/embedpdf/embed-pdf-viewer |
| `image`, `text`, `webview`, `run`, `axes`, `challenge`, `audio`, `video` | Native browser + React | — | — |

Every renderer also depends on:

- **React / ReactDOM** (Meta, MIT) — https://github.com/facebook/react
- The Collimate **renderer-hooks** runtime (`/api/sdk/react.js`), which is
  MIT from this SDK.

Additional libraries specific renderers load (all in the frozen graph):

- **KaTeX** (MIT) — loaded by the milkdown and todo renderers (through
  Milkdown Crepe's math support) for LaTeX math rendering.
  https://github.com/KaTeX/KaTeX
- **js-yaml** (MIT) — loaded by the schema_form and todo renderers.
  https://github.com/nodeca/js-yaml

### What this means for redistributors

A build of Collimate that ships the renderers (the `collimate` wheel)
redistributes the Collimate wrappers (MIT)
and the frozen module graph in `module-snapshot/` (vendored into the
app as `mirror/`): third-party packages under their own licenses, listed
package by package in its `LICENSES.json`. Ship that file with the
distribution and honour each license it names (for MIT, BSD, ISC and
similar, keep the copyright and license notices; for MPL-2.0 and EPL-2.0,
the source of those packages is at the `source` URL recorded for them).
A module the app's mirror fetches later, for an authored renderer, is
fetched by that installation, not redistributed by the SDK.
