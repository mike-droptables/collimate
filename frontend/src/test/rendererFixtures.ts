/**
 * Renderer-source fixtures for browser-mode tests.
 *
 * NativeRenderer fetches `/api/renderers/<id>/source` to read the
 * X-Entrypoint header, then — for plain-JS entrypoints — dynamic-imports the
 * SAME URL. A test-side `fetch` mock only intercepts the first request; the
 * module import goes to the network. So the vitest dev server itself serves
 * these fixtures via the `rendererFixturePlugin` in vitest.config.ts, making
 * both the fetch and the import resolve against real HTTP.
 *
 * Each test uses its own renderer id because NativeRenderer memoises loaded
 * modules per `${rendererId}::${rendererHash}` for the whole session — two
 * tests sharing an id would both get whichever module loaded first.
 */

export const MOUNTED_TEXT = 'mounted-from-test-fixture';

export const FIXTURE_FLUSH_ID = 'fixture-sdk-flush';

export const FIXTURE_BASIC_ID = 'fixture-basic';
export const FIXTURE_REPORT_CHANGE_ID = 'fixture-report-change';
export const FIXTURE_SCRIM_ID = 'fixture-scrim';
// A column taller than its cell, for the scrim's first-wheel replay.
export const FIXTURE_WHEEL_ID = 'fixture-wheel';
// Probe fixtures: record each mount on `window.__probe` (set by the test) and
// expose the mount's api so a test can drive it directly, along with the
// `api.getFileUrl('x')` it saw as it mounted. The slow variant holds its
// mount open for 150ms, widening the async-mount window.
export const FIXTURE_PROBE_ID = 'fixture-probe';
// Imports a module and loads a stylesheet that exist only on the test
// mirror (vitest.config.ts MIRROR_FIXTURES); mounts the module's text in an
// element the stylesheet colours.
export const FIXTURE_MIRROR_PROBE_ID = 'fixture-mirror-probe';
export const MIRROR_PROBE_MODULE = 'https://esm.sh/mirror-probe@1.0.0?target=es2022';
export const MIRROR_PROBE_CSS = 'https://cdn.jsdelivr.net/npm/mirror-probe@1.0.0/probe.css';
export const FIXTURE_PROBE_SLOW_ID = 'fixture-probe-slow';
// The renderer-host conformance probe (src/test/host-probe/, a renderer
// package): served as a built bundle and as a cell package the browser
// compiles (src/test/hostProbeServer.ts).
export const HOST_PROBE_BUNDLED_ID = 'fixture-host-probe';
export const HOST_PROBE_IMPORTED_ID = 'fixture-host-probe-imported';

export interface ProbeState {
    mounts: number;
    cleanups: number;
    apis: Array<Record<string, (...args: never[]) => unknown>>;
    fileUrlsAtMount: string[];
    urlPath?: string;
    vis: string[];
    set: string[];
}

const probeSource = (delayMs: number) => `
    export default {
        mount: async (container, api) => {
            const p = window.__probe;
            const n = ++p.mounts;
            p.apis.push(api);
            if(p.urlPath !== undefined) p.fileUrlsAtMount.push(api.getFileUrl(p.urlPath));
            // Deliberately never unsubscribed: the host must drop these per mount.
            api.onVisibleChange((v) => p.vis.push(n + ':' + v));
            api.onSetContent((c) => p.set.push(c));
            ${delayMs ? `await new Promise((r) => setTimeout(r, ${delayMs}));` : ''}
            container.textContent = 'probe-' + n;
            api.ready();
            return () => { p.cleanups++; };
        }
    };
`;

const FIXTURES: Record<string, string> = {
    [FIXTURE_MIRROR_PROBE_ID]: `
        import {probe} from '${MIRROR_PROBE_MODULE}';
        export default {
            mount: async (container, api) => {
                await api.loadCss('${MIRROR_PROBE_CSS}');
                const text = document.createElement('div');
                text.className = 'mirror-probe';
                text.textContent = probe;
                container.append(text);
                api.ready();
                return () => text.remove();
            }
        };
    `,
    [FIXTURE_SCRIM_ID]: `
        export default {
            mount: async (container, api) => {
                const button = document.createElement('button');
                button.textContent = 'Renderer action';
                button.style.cssText = 'margin:40px;width:180px;height:50px';
                let clicks = 0;
                button.addEventListener('click', event => {
                    button.dataset.event = JSON.stringify({clicks:++clicks,
                        x:event.clientX,y:event.clientY,trusted:event.isTrusted,
                        bubbles:event.bubbles,composed:event.composed,cancelable:event.cancelable});
                    api.reportChange(String(clicks));
                });
                container.append(button);
                api.ready();
                return () => button.remove();
            }
        };
    `,
    [FIXTURE_WHEEL_ID]: `
        export default {
            mount: async (container, api) => {
                const column = document.createElement('div');
                column.dataset.wheelColumn = '';
                column.style.cssText = 'height:100%;overflow-y:auto';
                const tall = document.createElement('div');
                tall.style.height = '2000px';
                column.append(tall);
                container.append(column);
                api.ready();
                return () => column.remove();
            }
        };
    `,
    'fixture-renderer-alias': `
        import React from 'https://esm.sh/react@19.3.1?target=es2022';
        import {defineRenderer,useCollimateFile} from '/api/sdk/react.js';
        function Probe() {
            const {api,content}=useCollimateFile();
            window.__aliasApi=api;window.__aliasContent=content;
            React.useEffect(()=>{
                window.__aliasEvents=[];
                const off=api.onFilesChange(detail=>window.__aliasEvents.push(detail));
                window.__aliasSubscribed=api;
                return ()=>{off();delete window.__aliasApi;delete window.__aliasSubscribed;};
            },[api]);
            return React.createElement('div',null,content);
        }
        export default defineRenderer({component:Probe});
    `,
    [FIXTURE_FLUSH_ID]: `
        import React from 'https://esm.sh/react@19.3.1?target=es2022';
        import {defineRenderer,useCollimateFile} from '/api/sdk/react.js';
        function Editor() {
            const {content,saveContent,api}=useCollimateFile();
            const [text,setText]=React.useState('');
            React.useEffect(()=>{setText(content)},[content]);
            const save=saveContent;
            React.useEffect(()=>{const off=api.onFlush(()=>window.__fixtureFlushGate);window.__fixtureFlushReady=true;return ()=>{window.__fixtureFlushReady=false;off();};},[api]);
            return React.createElement('textarea',{'aria-label':'SDK pending editor',value:text,
                onChange:e=>{setText(e.target.value);save(e.target.value)}});
        }
        export default defineRenderer({component:Editor});
    `,
    [FIXTURE_BASIC_ID]: `
        export default {
            mount: async (container, api) => {
                const content = await api.getContent();
                container.textContent = ${JSON.stringify(MOUNTED_TEXT)} + ':' + content;
                api.ready();
                return () => {};
            }
        };
    `,
    [FIXTURE_REPORT_CHANGE_ID]: `
        export default {
            mount: async (container, api) => {
                api.reportChange('new-content');
                api.ready();
                return () => {};
            }
        };
    `,
    [FIXTURE_PROBE_ID]: probeSource(0),
    [FIXTURE_PROBE_SLOW_ID]: probeSource(150),
};

export function rendererFixtureSource(id: string): string | null {
    return FIXTURES[id] ?? null;
}
