# Bundled renderers

Renderers are ES modules that mount UI for a file in one of two embed modes:
natively in the host document's shadow DOM (`embed: "native"` — most bundled
renderers, and the only mode handed `api.desktop`), or inside a sandboxed
iframe (`embed: "iframe"`). See [`sdk/shared/docs/how-to-write-a-renderer.md`](../../sdk/shared/docs/how-to-write-a-renderer.md) for the authoring guide.

## Layout

**The parent folders are the cells** (see `collimate._host.packaging.layout`):
each top-level directory is one seeded cell, and its `cell.yaml` carries the
cell's display name, icon, and order. These renderers are optional: a channel
seeds the ones its suggestion lists and can remove them.

```
renderers/
├── documents/     — cell "Documents": milkdown, monaco, and the llm-summary handler (.llm_summary, shown as markdown)
├── visual/        — cell "Visual": mermaid, image, html, excalidraw, slides, audio, video, pdf
└── utility/       — cell "Utility": schema_form, table
```

The handlers and renderers the app's own code names (the definition handlers,
the live surfaces and the plain text renderer) are not here: they are the app's
built-in cells in [`builtins/`](../../builtins/README.md). The Documents cell's
`llm-summary` handler is metadata only (no `derived/` folder, no checks); like
any handler here, it cannot take a type the built-in handlers reserve.

## Runtime dependencies

Renderers import React and other browser libraries by versioned CDN URLs (`https://esm.sh/react@19.3.1?target=es2022`). Authors write source files and a named `.renderer` definition; they have no manual build step. The SDK's pinned compiler bundles the entrypoint and package-relative imports, leaving CDN URLs and `/api/sdk/...` imports external. The app compiles a renderer when it admits the package and stores the bundle. After an edit, a cache miss compiles the exact current package bytes in the parent's local compiler worker and publishes the resulting bundle for its source hash. Compilation errors stay visible instead of running stale bytes.

At runtime nothing loads from the CDNs directly: the app mirrors `https://esm.sh/` and `https://cdn.jsdelivr.net/` on its own origin (`/api/mirror/<host>/…`). The host document's import map sends ES imports there, and `mirrorUrl(url)` from `/api/sdk/react.js` (with `api.loadCss`, which maps the same way) sends the rest: fetched stylesheets, fonts, wasm, AMD scripts, workers. Every upstream URL these renderers load is an exact pin (but the two ranges the milkdown renderer must share with Crepe's own graph), and their whole graph is frozen in the repository at `module-snapshot/`, with a `LICENSES.json` for every package it redistributes, so the shipped renderers work offline. `python -m collimate._host.module_mirror freeze` rewrites it from the sources (its roots are the quoted upstream URLs in each package's non-test sources, plus the files a `mirror_assets.json` beside the definition names for a loader that fetches by computed name); `check` verifies it (every root frozen; an edge the freeze could not fetch is reported, tolerated). The app's wheel ships it and the mirror serves it.

When public esm.sh cannot build a cold `deps=` combination (its builder answers 5xx, sometimes for days), freeze with a local esm.sh of the release the contract pins (`esm_builder_image` in `src/collimate/module_mirror_contract.json`, today `ghcr.io/esm-dev/esm.sh:v140`):

```bash
docker run -d --name collimate-esm-builder -p 127.0.0.1:18080:80 ghcr.io/esm-dev/esm.sh:v140
python -m collimate._host.module_mirror freeze --esm-builder http://127.0.0.1:18080
python -m collimate._host.module_mirror check
docker rm -f collimate-esm-builder
```

The freeze asks the local builder only for what public esm.sh answers with a 5xx, and for every not-yet-frozen module a locally built one reaches, so a family comes from one builder; keys and `final_url`s stay the public `https://esm.sh/…` URLs, bodies are rewritten exactly as public ones, already-frozen shared modules stay, and jsDelivr never falls back. The builder must report the pinned release (`/status.json`), or the freeze refuses it. A local body differs byte for byte from the public instance's, so `module-snapshot/builders.json` records each locally built key; a later freeze that fetches one publicly drops the mark. The app never talks to the builder. Move the image tag when public esm.sh moves (`gh api repos/esm-dev/esm.sh/releases`).

Every esm.sh import carries a version **and** `?target=es2022` (`&target=es2022` when the URL already has a query string). Unpinned, esm.sh picks the build target from the User-Agent, so the same specifier can resolve to a different build — and a different module instance — breaking anything that relies on two imports sharing one (React shared with `/api/sdk/react.js`, ProseMirror plugin classes shared with an editor). Pin every esm.sh import in a file, or none.

`deps=` splits instances the same way one level down. esm.sh resolves each URL's own imports against the list *that URL* declares, so a package two imports must share (React, ProseMirror, CodeMirror) has to be pinned with a **byte-identical** `deps=` list on every import that carries one — a single stale version or a package dropped out of the middle silently yields two copies, and the symptom is a plugin that mounts fine and then throws on first use. One file, one canonical list, repeated verbatim; a lower layer may keep only a leading run of it (`lucide-react` pins `react` but not the assistant-ui packages above it). A URL that *omits* `deps=` is doing so deliberately — that is how you land on the plain build a bundled dependency already imports, so the two share one instance. Comment that choice where you make it. `tests/test_renderer_cdn_pins.py` enforces all of this. The frozen snapshot then records what each list resolved, and the app serves that before any upstream fetch, so a third party's release cannot change a shipped renderer; its `check` also holds every package that must load once (`SINGLE_INSTANCE` in `collimate._host.module_mirror`: React, ProseMirror, CodeMirror, each plugin family) to one version.

A renderer package may contain sibling modules and assets. Relative imports must stay within that package; compiler policy and source identity come from the SDK. The host serves the SDK-built renderer runtime (`sdk/shared/renderer-runtime/`), generated from `sdk/packages/renderer-hooks/src/` by `sdk/scripts/build_renderer_runtime.mjs`; an installed SDK carries it prebuilt, so serving it needs no Node.

### Moving the pins

`python -m collimate._host.module_mirror bump [--major] [--dry-run] [--only PACKAGE ...] [--esm-builder URL]` (from an SDK checkout, with the network) moves the shipped renderers to newer releases:

- **Pins.** Every `https://esm.sh/<package>@<version>` and `https://cdn.jsdelivr.net/npm/<package>@<version>` literal with an exact version in a package's non-test sources and its `mirror_assets.json`, every `<package>@<version>` inside those URLs' `deps=` lists, and the runtime's React: `react` and `react_dom` in `sdk/shared/contracts/toolchain.json`, which the runtime build reads. A package pinned only inside `deps=` lists (`@milkdown/core`, `@assistant-ui/tap`) moves with its family; one with no family pinned on a URL (`prosemirror-model`, `codemirror`, `radix-ui`: floors a list holds on purpose) moves only when `--only` names it.
- **Candidates.** Each package's npm `latest`, classified as patch, minor or major by semver, with npm's caret rule for 0.x (0.14 → 0.15 is major). Patch and minor moves apply by default; when `latest` is a major, the package moves to the newest release of its own line, and `--major` takes the major.
- **Families.** A scope (`@milkdown/*`, `@embedpdf/*`, `@xterm/*`, `@assistant-ui/*`) and react with react-dom move together: members at one shared version move to one version every member publishes; members on their own versions move together or not at all. A move is skipped, with its family, when another pinned package's registry metadata declares a range its new version falls outside (the declared range wins).
- **Applying.** It rewrites every `<package>@<old>` in those files, the ones inside `deps=` lists too, so a family moves in its URLs and in every list that pins it at once (a package pinned in several files moves everywhere at once), and in the docs that quote them (`sdk/shared/docs/*.md`, this README, the renderer-hooks README; `sync_skill_references.py` runs after). A moved runtime React goes into `toolchain.json`; `sdk/scripts/sync_toolchain.py` then rewrites `sdk/packages/renderer-hooks`' manifest and lockfile from it, `npm ci` installs it there, and the renderer runtime is rebuilt from that install. The bump refreezes the snapshot with every floating key re-resolved, so the ranges the graph shares resolve at one moment (through `--esm-builder`'s local esm.sh for what public esm.sh cannot build), and runs `check`.
- **Atomic.** A moved family whose new roots are still not frozen after that freeze is reverted, the remaining moves are re-planned around it (a range it declares keeps holding them) and the snapshot is frozen again; the bump says which family it reverted, prints the final table, and exits 1. A bump never leaves a shipped renderer on pins the snapshot cannot serve.

It prints one row per package: old → new, class, latest, and applied or skipped with the reason. `--dry-run` prints the table and writes nothing; `--only` names packages, each bringing its family. Its rewrite never touches `node_modules`, test fixtures or snapshot bodies, so a test that asserts a version fails until you move it, and a moved graph needs the live checks (the offline renderers, chat typing, markdown lists) before it ships.
