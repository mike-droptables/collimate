import { defineConfig, mergeConfig, type Plugin } from "vitest/config";
import type {BrowserCommandContext} from "vitest/node";
import { playwright } from "@vitest/browser-playwright";
import viteConfig from "./vite.config.ts";
import path from "path";
import { readFileSync } from "node:fs";
import {pathToFileURL} from "node:url";
import { MIRRORED_HOSTS } from "./src/lib/moduleMirror.ts";
import { rendererFixtureSource } from "./src/test/rendererFixtures.ts";
import { hostProbeServer } from "./src/test/hostProbeServer.ts";

// The module mirror for tests: /api/mirror/<host>/<path> answers from the
// SDK's frozen snapshot (the graph the shipped renderers import) and the
// mirror-only fixtures below, and 404s anything else as the app's mirror
// would, so no test reaches a module CDN. The browser tester's import map
// (src/test/browser-tester.html) sends both CDNs here.
const MIRROR_SNAPSHOT = path.resolve(import.meta.dirname, "../module-snapshot");
// Test attachments (browser failure screenshots included) go to the repo's
// tmp/; vitest writes only where the dev server may read.
const ATTACHMENTS = path.resolve(import.meta.dirname, "../tmp/vitest-attachments");
// Modules that exist only on the test mirror: a hit proves the mirror served it.
const MIRROR_FIXTURES: Record<string, { type: string; body: string }> = {
  "https://esm.sh/mirror-probe@1.0.0?target=es2022": {
    type: "application/javascript; charset=utf-8",
    body: "export * from './mirror-probe@1.0.0/es2022/mirror-probe.mjs';\n",
  },
  "https://esm.sh/mirror-probe@1.0.0/es2022/mirror-probe.mjs": {
    type: "application/javascript; charset=utf-8",
    body: "export const probe = 'served by the mirror';\n",
  },
  "https://cdn.jsdelivr.net/npm/mirror-probe@1.0.0/probe.css": {
    type: "text/css; charset=utf-8",
    body: ".mirror-probe { color: rgb(1, 2, 3); }\n",
  },
};
const mirrorHits = new Map<string, number>();
let mirrorIndex: { entries: Record<string, { hash: string; media_type: string }> } | null = null;
// collimate._host.module_mirror.mirror_key: printable ASCII escapes are decoded but for these.
const KEEP_ENCODED = new Set('%/?#&=+\\"<> ');
const canonicalPart = (text: string) => text.replace(/%([0-9A-Fa-f]{2})/g, (_escape, hex: string) => {
  const code = parseInt(hex, 16);
  const char = String.fromCharCode(code);
  return code >= 0x21 && code <= 0x7e && !KEEP_ENCODED.has(char) ? char : `%${hex.toUpperCase()}`;
});
function mirrorResponse(rawUrl: string): { status: number; type: string; body: Buffer | string } {
  const [rawPath, ...query] = rawUrl.slice("/api/mirror/".length).split("?");
  const key = `https://${canonicalPart(rawPath)}${query.length ? `?${canonicalPart(query.join("?"))}` : ""}`;
  mirrorHits.set(key, (mirrorHits.get(key) ?? 0) + 1);
  const fixture = MIRROR_FIXTURES[key];
  if (fixture) return { status: 200, type: fixture.type, body: fixture.body };
  mirrorIndex ??= JSON.parse(readFileSync(path.join(MIRROR_SNAPSHOT, "index.json"), "utf8"));
  const entry = mirrorIndex!.entries[key];
  if (entry) return { status: 200, type: entry.media_type, body: readFileSync(path.join(MIRROR_SNAPSHOT, "modules", entry.hash)) };
  return { status: 404, type: "application/json", body: JSON.stringify({ error: "upstream", status: 404, detail: `not on the test mirror: ${key}` }) };
}

// Serves renderer-source fixtures for the browser-mode NativeRenderer tests.
// The shared loader fetches actual module code, which the component imports
// from a blob URL. The vitest dev server supplies those fixture responses. See src/test/rendererFixtures.ts.
const rendererFixturePlugin = (): Plugin => ({
  name: "collimate-renderer-fixtures",
  configureServer(server) {
    // The renderer-host conformance probe, bundled and imported (src/test/hostProbeServer.ts).
    const hostProbe = hostProbeServer(path.resolve(import.meta.dirname, "src/test/host-probe"), path.resolve(import.meta.dirname, "../sdk/scripts/renderer_bundle.mjs"));
    server.middlewares.use((req, res, next) => {
      if (hostProbe(req, res)) return;
      if (req.url?.startsWith("/api/mirror/")) {
        const answer = mirrorResponse(req.url);
        res.statusCode = answer.status;
        res.setHeader("Content-Type", answer.type);
        res.setHeader("Cache-Control", "no-store");
        res.end(answer.body);
        return;
      }
      if (req.url === "/__mirror_hits") {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify(Object.fromEntries(mirrorHits)));
        return;
      }
      const worker = req.url?.match(/^\/proxy\/dddddddd-dddd-4ddd-8ddd-dddddddddddd\/(ended|live)\/(5100|5101)\/nested\/sw.js$/);
      if (worker) {
        res.setHeader('Content-Type', 'application/javascript');
        res.setHeader('Service-Worker-Allowed', `/proxy/dddddddd-dddd-4ddd-8ddd-dddddddddddd/${worker[1]}/${worker[2]}/`);
        res.end("self.addEventListener('install',()=>self.skipWaiting());");return;
      }
      if(req.url==='/api/channels/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/cells/alias-cell/file_cap'
          || req.url==='/api/channels/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/cells/snapshot/file_cap'
          || /^\/api\/channels\/cccccccc-cccc-4ccc-8ccc-cccccccccccc\/cells\/[a-f0-9-]+\/file_cap$/.test(req.url??'')) {
        res.setHeader('Content-Type','application/json');res.end(JSON.stringify({cap:'testcap'}));return;
      }
      if(req.url?.startsWith('/__d14_pending_image')) {
        setTimeout(()=>{res.statusCode=404;res.end();},1500);return;
      }
      if(req.url?.startsWith('/__d14_navigation__')) {
        const token=new URL(req.url,'http://fixture').searchParams.get('token');
        const payload=JSON.stringify({tag:'collimate-preview',token,id:99,op:'save',path:'navigation-escape.txt',content:'replacement write'});
        res.setHeader('Content-Type','text/html');res.end(`<!doctype html><title>Synthetic preview navigation</title><body>Replacement document<script>addEventListener('message',event=>{if(event.ports[0])event.ports[0].postMessage(${payload})});parent.postMessage({...${payload},id:0,op:'connect-ready'},'*');parent.postMessage(${payload},'*')</script><img src='/__d14_pending_image'>`);return;
      }
      if(req.url === '/api/sdk/preview-common.json') {res.setHeader('Content-Type','application/json');res.end(readFileSync(path.resolve(import.meta.dirname,'../sdk/shared/renderer-runtime/common.json')));return;}
      if (['/api/sdk/react.js','/api/sdk/session.js'].includes(req.url?.split('?')[0] || '')) {
        res.setHeader('Content-Type', 'application/javascript');
        res.end(readFileSync(path.resolve(import.meta.dirname, '../sdk/shared/renderer-runtime/'+req.url!.split('?')[0].split('/').pop())));
        return;
      }
      if(req.url==='/__step6_weblinks.js') {
        res.setHeader('Content-Type','application/javascript');
        res.end(`import {WebLinksAddon as ActualWebLinksAddon} from 'https://esm.sh/@xterm/addon-web-links@0.12.0?deps=@xterm/xterm@6.0.0&target=es2022';
export class WebLinksAddon extends ActualWebLinksAddon {constructor(callback){super(callback);window.__step6TerminalLink=callback;}}`);return;
      }
      const step6 = req.url?.match(/^\/api\/renderers\/fixture-step6-(audio|video|terminal)\/source/);
      if(step6) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const kind=step6[1];
          const folder=kind==='audio'||kind==='video'?path.resolve(import.meta.dirname,'../content/renderers/visual',kind):path.resolve(import.meta.dirname,'../builtins/live',kind);
          const files=compiler.collectPackage(folder);
          const entrypoint='main.tsx';
          if(kind==='terminal') {
            // Observe the real addon callback, keeping the actual addon and viewer.
            const source=new TextDecoder().decode(files.get('main.tsx'));
            files.set('main.tsx',new TextEncoder().encode(source.replace('https://esm.sh/@xterm/addon-web-links@0.12.0?deps=@xterm/xterm@6.0.0&target=es2022',`http://${req.headers.host}/__step6_weblinks.js`)));

          }
          const result=await compiler.bundleRenderer({files,entrypoint});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-renderer-preview/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../builtins/definitions/renderer-example')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-html-base/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../content/renderers/visual/html')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/api/cellcap/testcap/channels/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/cells/html-cell/files/resource.txt')) {
        res.setHeader('Access-Control-Allow-Origin','*');res.end('cell-relative-resource');return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-table-bounds/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../content/renderers/utility/table')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-mermaid-debounce/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../content/renderers/visual/mermaid')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url==='/__monaco_schedule/loader.js') {
        res.setHeader('Content-Type','application/javascript');
        res.end('window.require=(_dependencies,ready)=>ready();window.require.config=()=>{};');return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-monaco-schedule/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const files=compiler.collectPackage(path.resolve(import.meta.dirname,'../content/renderers/documents/monaco'));
          const source=new TextDecoder().decode(files.get('main.tsx'));
          // Keep the actual component and SDK context. The parent layout
          // callback inserts a newer widget edit before passive effects.
          const boundary=`function ScheduleBoundary(){const {content}=useCollimateFile();React.useLayoutEffect(()=>{window.__monacoSchedule?.(content);});return React.createElement(MonacoEditor);}\n`;
          files.set('main.tsx',new TextEncoder().encode(source
            .replace(/https:\/\/cdn\.jsdelivr\.net\/npm\/monaco-editor@[^/]+\/min\/vs/,`http://${req.headers.host}/__monaco_schedule`)
            .replace('export default defineRenderer({',boundary+'export default defineRenderer({')
            .replace('component: MonacoEditor,','component: ScheduleBoundary,')));
          const result=await compiler.bundleRenderer({files,entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-website-url/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../builtins/live/website')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');res.end(result.code);
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      if(req.url?.startsWith('/__excal_callback_fixture.js')) {
        res.setHeader('Content-Type','application/javascript');
        res.end(`import React,{useEffect} from 'https://esm.sh/react@19.3.1?target=es2022';export const CaptureUpdateAction={NEVER:'NEVER',IMMEDIATELY:'IMMEDIATELY',EVENTUALLY:'EVENTUALLY'};export function Excalidraw(props){useEffect(()=>{props.onChange?.([],{theme:props.theme,viewBackgroundColor:'#fff'},{});},[]);return React.createElement('button',{onClick:()=>props.onChange?.([{id:'pending'}],{theme:props.theme,viewBackgroundColor:'#fff'},{})},'Queue drawing edit');}`);return;
      }
      if(req.url?.startsWith('/api/renderers/fixture-excal-ownership/source')) {
        void import(/* @vite-ignore */ pathToFileURL(path.resolve(import.meta.dirname,'../sdk/scripts/renderer_bundle.mjs')).href).then(async compiler=>{
          const result=await compiler.bundleRenderer({files:compiler.collectPackage(path.resolve(import.meta.dirname,'../content/renderers/visual/excalidraw')),entrypoint:'main.tsx'});
          res.setHeader('Content-Type','application/javascript');
          // Controlled widget callbacks; actual wrapper, runtime and host.
          res.end(result.code.replace('https://esm.sh/@excalidraw/excalidraw@0.18.1?deps=react@19.3.1,react-dom@19.3.1&target=es2022',`http://${req.headers.host}/__excal_callback_fixture.js`));
        }).catch(error=>{res.statusCode=500;res.end(String(error));});return;
      }
      const match = req.url?.match(/^\/api\/renderers\/([^/]+)\/source/);
      const source = match && rendererFixtureSource(decodeURIComponent(match[1]));
      if (!source) return next();
      res.setHeader("Content-Type", "application/javascript");
      res.setHeader("X-Entrypoint", "main.js");
      res.end(source);
    });
  },
});

// Two projects keep fast pure-TS tests in a node env from paying for
// jsdom startup, while React component tests get a real DOM + RTL setup.
export default mergeConfig({
  ...viteConfig,
  // Test fixtures own /api responses; do not inherit the application's
  // development proxy to a separately running API server.
  server: { ...viteConfig.server, proxy: undefined, fs: { allow: [import.meta.dirname, ATTACHMENTS] } },
}, defineConfig({
  plugins: [rendererFixturePlugin()],
  optimizeDeps: {include: ["es-module-lexer/minimal/js", "esbuild-wasm/esm/browser.js", "@noble/hashes/sha2.js", "react-dom/client", "@radix-ui/react-slot", "class-variance-authority"]},
  test: {
    attachmentsDir: ATTACHMENTS,
    globals: true,
    projects: [
      {
        extends: true,
        test: {
          name: "node",
          environment: "node",
          include: ["src/lib/**/*.{test,spec}.{ts,tsx}"],
          // DOM-dependent lib tests route to the jsdom project below;
          // real-Chromium ones to the browser project.
          exclude: ["src/lib/**/api*.{test,spec}.{ts,tsx}", "src/**/*.browser.{test,spec}.{ts,tsx}"],
        },
      },
      {
        extends: true,
        test: {
          name: "jsdom",
          environment: "jsdom",
          setupFiles: ["./src/test/setup.ts"],
          include: [
            "src/components/**/*.{test,spec}.{ts,tsx}",
            "src/contexts/**/*.{test,spec}.{ts,tsx}",
            "src/hooks/**/*.{test,spec}.{ts,tsx}",
            "src/stores/**/*.{test,spec}.{ts,tsx}",
            "src/lib/**/api*.{test,spec}.{ts,tsx}",
          ],
          exclude: ["src/**/*.browser.{test,spec}.{ts,tsx}"],
        },
      },
      // Browser-mode project: real Chromium via Playwright. Used for
      // components jsdom can't faithfully host — shadow DOM, WebGL,
      // ResizeObserver-dependent layout, iframes. Opt in by suffixing
      // the filename `.browser.test.tsx`. Skipped unless
      // VITEST_BROWSER=1 so the unit suite stays fast.
      //
      // VITEST_CHROMIUM_PATH points the launch at a pre-installed browser
      // binary (e.g. /opt/pw-browsers/chromium in managed sandboxes) when
      // the Playwright-pinned headless-shell revision isn't downloadable.
      ...(process.env.VITEST_BROWSER
        ? [
            {
              extends: true as const,
              test: {
                name: "browser",
                include: ["src/**/*.browser.{test,spec}.{ts,tsx}"],
                browser: {
                  enabled: true,
                  // The app documents' mirror import map (index.html), so a
                  // renderer's module-CDN imports load from the test mirror.
                  testerHtmlPath: "src/test/browser-tester.html",
                  // Neither module CDN resolves: a load that bypasses the
                  // test mirror fails instead of reaching the network.
                  provider: playwright({ launchOptions: {
                    args: [`--host-resolver-rules=${[...MIRRORED_HOSTS].map(host => `MAP ${host} ~NOTFOUND`).join(", ")}`],
                    ...(process.env.VITEST_CHROMIUM_PATH ? { executablePath: process.env.VITEST_CHROMIUM_PATH } : {}),
                  } }),
                  headless: true,
                  commands: {
                    previewText: async (context: BrowserCommandContext) => context.iframe.frameLocator('iframe[title="Sealed renderer preview"]').locator('body').evaluate(body=>(body as HTMLElement).innerText+[...body.querySelectorAll('*')].map(node=>node.shadowRoot?.textContent??'').join('')),
                    previewInput: async (context: BrowserCommandContext, value: string) => {
                      await context.iframe.frameLocator('iframe[title="Sealed renderer preview"]').getByRole('textbox').fill(value);
                    },
                    previewClick: async (context: BrowserCommandContext, name: string) => {
                      await context.iframe.frameLocator('iframe[title="Sealed renderer preview"]').getByRole('button',{name,exact:true}).click({noWaitAfter:true});
                    },
                  },
                  instances: [
                    {
                      browser: "chromium" as const,
                    },
                  ],
                },
              },
            },
          ]
        : []),
    ],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["src/lib/**", "src/components/**", "src/hooks/**", "src/stores/**", "src/contexts/**"],
      exclude: [
        "src/**/__tests__/**",
        "src/**/*.{test,spec}.{ts,tsx}",
        "src/test/**",
        // Type-only modules have no runtime — excluding avoids 0%-drag.
        "src/lib/types.ts",
        "src/lib/generatorTypes.ts",
        // api.ts is a thin axios wrapper; each fetcher is covered
        // implicitly through e2e. Unit-testing every one is
        // busywork. getApiUrl has its own spec.
        "src/lib/api.ts",
        // Components without unit coverage. NativeRenderer is exercised by
        // the separate real-browser suite. Remove an entry when its unit
        // tests land; tested components and all contexts are measured.
        "src/components/NativeRenderer.tsx",
        "src/components/cellEditor/TabContextMenu.tsx",
        "src/components/ui/scroll-area.tsx",
        // Hooks — only the pure ones below are covered; the
        // DOM-orchestration hooks (useKeyboardHandler, useStackDrag,
        // useStackCamera, useStackLayout, useGeneratorRun,
        // useChannelSSE, etc.) still rely on e2e for coverage. Graduate
        // entries by removing them here as unit tests land.
        "src/hooks/useAppState.ts",
        "src/hooks/useChannelSSE.ts",
        "src/hooks/useDraftDebounce.ts",
        "src/hooks/useFileOperations.ts",
        "src/hooks/useGeneratorRun.ts",
        "src/hooks/useIdleCheckpoint.ts",
        "src/hooks/useKeyboardHandler.ts",
        "src/hooks/usePanZoomCamera.ts",
        "src/hooks/useSceneActions.ts",
        "src/hooks/useStackCamera.ts",
        "src/hooks/useStackDrag.ts",
        "src/hooks/useStackLayout.ts",
      ],
      thresholds: {
        // Ratchet policy: these floors reflect what the unit suite
        // currently achieves. A coverage regression fails CI. When a
        // new test raises a number, lift the floor in the same PR.
        "src/lib/**": { lines: 80, functions: 70, branches: 80, statements: 80 },
        "src/components/ui/**": { lines: 98, functions: 100, branches: 95, statements: 98 },
        "src/stores/**": { lines: 35, functions: 40, branches: 65, statements: 35 },
        "src/hooks/**": { lines: 95, functions: 95, branches: 90, statements: 95 },
        // F1919: measured88.73/64.67/74.44/82.38 after including contexts.
        "src/contexts/**": { lines: 88, functions: 64, branches: 74, statements: 82 },
      },
    },
  },
}));
