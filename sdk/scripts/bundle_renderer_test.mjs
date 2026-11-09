#!/usr/bin/env node
// SPDX-License-Identifier: MIT
//
// bundle_renderer_test.mjs — bundle a renderer entrypoint into a
// plain, Node-importable ESM module so a node:test harness can import the
// renderer's EXPORTED pure functions directly.
//
// Why this exists: production packages retain CDN and /api/sdk imports as
// externals. To test pure exports under plain Node we cannot `import` the .tsx
// (it pulls in React/js-yaml from a CDN and the SDK from /api/…), so we bundle
// it here, stubbing UI/framework imports while YAML uses the real parser.
// UI/framework stubs only have to make MODULE-TOP-LEVEL evaluation succeed — component bodies
// never run during import — so a Proxy that answers every member access with a
// callable no-op is sufficient, while the renderer's own `export function`s
// (canon, matches, …) survive as the bundle's real exports.
//
// This test-only tool produces a runnable bundle for pure exported functions.
// Production verification statically inspects the native compiler output in
// the accepted renderer handler and never evaluates authored renderer code.
//
// Usage:
//   node scripts/bundle_renderer_test.mjs <entrypoint.tsx> <outfile.mjs>
// Exit code: 0 on success, 1 on a bundle error, 2 on bad args / no esbuild.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { compiler as loadEsbuild, CONTRACT } from './renderer_bundle.mjs';

// ---------------------------------------------------------------------------
// Stub modules injected for the SDK / React / CDN / bare imports.
// ---------------------------------------------------------------------------
// The test stubs model renderer imports. They are emitted as CommonJS so esbuild's CJS→ESM
// interop accepts ANY named or default import without a static "no matching
// export" error. One subtlety drives the design: esbuild's `__toESM`
// materializes a namespace by copying a CJS module's OWN ENUMERABLE keys, so a
// member is only reachable as `import_x.member` if it is a real own property —
// a bare `get`-trap Proxy is NOT enough for a member that is CALLED AT MODULE
// TOP LEVEL. The one such call in every renderer entry is
// `export default defineRenderer({component})`, so `defineRenderer` MUST be a
// real property (SDK_STUB provides it). Members only touched inside component
// bodies (hooks, …) never run during import, so the generic
// callable-no-op Proxy covers them; unknown members still resolve to a no-op.
// The theme contract's pure members (tokens, THEME_VAR_NAMES, themeCss,
// resolveVar, toHex8, SYNTAX_ROLES, syntaxPalette), the cell-file drop
// helpers (CELL_FILES_MIME, isCellFileDrag, readCellFileDrop), the
// annotation rejection (annotationUnavailable) and the document view's
// (injectDarkReader, documentSrcDoc, frameBacking, …) are the SDK's real
// implementations, bundled from packages/renderer-hooks/src/index.tsx, so a
// renderer test that builds a frame's CSS or a library palette exercises the
// shipped code. useStyle reads all-null (every renderer default),
// useAnnotationRequest subscribes nothing and StyleControl renders nothing.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SDK_SOURCE = path.resolve(HERE, "../packages/renderer-hooks/src/index.tsx");
const SDK_STUB = `
const _theme = require(${JSON.stringify(SDK_SOURCE)});
const _api = new Proxy({}, { get: () => () => undefined });
function defineRenderer(def) {
  return { __collimate_renderer__: true, component: def && def.component, diffComponent: def && def.diffComponent };
}
const _sdk = {
  defineRenderer,
  useCollimateFile: () => ({ initialContent: "", content: "", saveContent() {}, isReadOnly: false, fileName: "", api: _api, diffContent: null }),
  useSiblingFile: () => ({ url: "", loading: false }),
  useTheme: () => ({ themeId: "unknown", scheme: "dark", known: false, vars: {}, fonts: { ui: "", mono: "", prose: "" }, onChange() { return () => {}; } }),
  useStyle: () => ({ theme: null, font: null, textSize: null, set() {}, onChange() { return () => {}; } }),
  StyleControl: () => null,
  useAnnotationRequest: () => {},
  annotationUnavailable: _theme.annotationUnavailable,
  useRemoteModule: () => ({ module: null, loading: false, error: null }),
  useRemoteScript: () => ({ loading: false, error: null }),
  // No app origin under Node, so no mirror: every URL is its own, as in a sealed preview.
  mirrorUrl: (url) => url,
  tokens: _theme.tokens,
  THEME_VAR_NAMES: _theme.THEME_VAR_NAMES,
  themeCss: _theme.themeCss,
  resolveVar: _theme.resolveVar,
  toHex8: _theme.toHex8,
  SYNTAX_ROLES: _theme.SYNTAX_ROLES,
  syntaxPalette: _theme.syntaxPalette,
  CELL_FILES_MIME: _theme.CELL_FILES_MIME,
  isCellFileDrag: _theme.isCellFileDrag,
  readCellFileDrop: _theme.readCellFileDrop,
};
// The document view's pure members, the real implementations too, so a
// renderer test sees the srcDoc chrome and the backing the app ships.
for (const name of ["DARKREADER_VERSION", "DARKREADER_PATH", "DARKREADER_MESSAGE",
  "HOST_THEME_WAIT_MS", "DOCUMENT_VIEW_STYLE", "darkReaderScriptUrl", "darkReaderPoles", "darkReaderOptions",
  "injectDarkReader", "documentSrcDoc", "hostThemeKnown", "themeSettled",
  "documentSkinKey", "documentSkinPost", "deliveredAfterLoad", "frameBacking"]) _sdk[name] = _theme[name];
module.exports = new Proxy(_sdk, { get: (t, k) => (k === "__esModule" ? false : (k in t ? t[k] : () => undefined)) });
`;

const REACT_STUB = `
const React = { createElement: () => null, Fragment: Symbol("Fragment"), Component: class {}, PureComponent: class {} };
const _react = Object.assign({}, React, {
  useState: (v) => [typeof v === "function" ? v() : v, () => {}],
  useEffect: () => {}, useLayoutEffect: () => {},
  useRef: (v) => ({ current: v === undefined ? null : v }),
  useMemo: (f) => (typeof f === "function" ? f() : undefined),
  useCallback: (f) => f, useContext: () => ({}),
  createContext: (v) => ({ Provider: () => null, Consumer: () => null, _v: v }),
  memo: (c) => c, forwardRef: (c) => c,
});
module.exports = new Proxy(_react, { get: (t, k) => (k === "__esModule" ? false : (k in t ? t[k] : () => undefined)) });
`;

// Generic callable-no-op module for CDN / bare / other /api specifiers. Any
// member access (including `.default`) returns a no-op function; safe because
// these are only invoked inside component bodies, never at module top level.
const EMPTY_STUB = `module.exports = new Proxy({}, { get: (t, k) => (k === "__esModule" ? false : () => undefined) });`;

function stubPlugin() {
  return {
    name: "collimate-renderer-test-stubs",
    setup(build) {
      build.onResolve({ filter: /.*/ }, (args) => {
        // Never stub the entry itself (it arrives as an absolute path).
        if (args.kind === "entry-point") return undefined;
        const p = args.path;
        if (p === "/api/sdk/react.js" || p.endsWith("/api/sdk/react.js")) {
          return { path: "sdk", namespace: "colstub" };
        }
        // Other virtual SDK / api modules are framework-free — the generic
        // callable-proxy stub covers them.
        if (p.includes("/api/")) {
          return { path: "empty", namespace: "colstub" };
        }
        const isReact = p === "react" || p === "react-dom" ||
          /(^https?:\/\/)[^\s]*\breact(-dom)?(@[^/]*)?(\/|$)/.test(p);
        if (isReact) return { path: "react", namespace: "colstub" };
        if (path.isAbsolute(p) && fs.existsSync(p)) return {path:p,namespace:"file"};
        if (p.includes("js-yaml@")) return {path:"yaml", namespace:"colstub"};
        if (p.startsWith("http://") || p.startsWith("https://")) {
          return { path: "empty", namespace: "colstub" };
        }
        // Bare specifier (js-yaml, …): not relative, not an absolute on-disk
        // path. Everything else (./relative, /absolute on disk) is resolved
        // from disk by esbuild.
        if (!(p.startsWith(".") || p.startsWith("/"))) {
          return { path: "empty", namespace: "colstub" };
        }
        return undefined;
      });
      build.onLoad({ filter: /.*/, namespace: "colstub" }, (args) => {
        // js-yaml 5 has named exports only (no default); they map to the real parser.
        if (args.path === "yaml") return {contents: `import {parse,stringify} from ${JSON.stringify(path.resolve(HERE, "node_modules/yaml/browser/index.js"))}; export const load = parse; export const dump = stringify;`, loader:"js"};
        if (args.path === "sdk") return { contents: SDK_STUB, loader: "js" };
        if (args.path === "react") return { contents: REACT_STUB, loader: "js" };
        return { contents: EMPTY_STUB, loader: "js" };
      });
    },
  };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  const [entrypoint, outfile] = process.argv.slice(2);
  if (!entrypoint || !outfile) {
    console.error("usage: bundle_renderer_test.mjs <entrypoint> <outfile>");
    process.exit(2);
  }
  const entryAbs = path.resolve(entrypoint);
  if (!fs.existsSync(entryAbs)) {
    console.error(`entrypoint does not exist: ${entryAbs}`);
    process.exit(2);
  }

  let esbuild;
  try {
    esbuild = await loadEsbuild();
  } catch (e) {
    console.error(String(e && e.message ? e.message : e));
    process.exit(2);
  }

  let outText;
  try {
    const result = await esbuild.build({
      ...CONTRACT.options,
      entryPoints: [entryAbs],
      bundle: true,
      write: false,
      format: "esm",
      platform: "neutral",
      logLevel: "silent",
      plugins: [stubPlugin()],
    });
    outText = result.outputFiles[0].text;
  } catch (e) {
    const msgs = (e && e.errors && e.errors.length)
      ? e.errors.map((d) => `${d.location ? d.location.file + ":" + d.location.line + " " : ""}${d.text}`)
      : [String(e && e.message ? e.message : e)];
    console.error(`bundle failed:\n  ${msgs.join("\n  ")}`);
    process.exit(1);
  }

  const outAbs = path.resolve(outfile);
  fs.mkdirSync(path.dirname(outAbs), { recursive: true });
  fs.writeFileSync(outAbs, outText, "utf8");
}

main().catch((e) => {
  console.error(`bundle_renderer_test: ${e && e.stack ? e.stack : e}`);
  process.exit(2);
});
