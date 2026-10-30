/**
 * The vitest dev server's side of the renderer-host conformance probe
 * (src/test/host-probe/, a renderer package; the suite is
 * components/__browser__/renderer-host-conformance.browser.test.tsx).
 *
 * One package, served the two ways the app serves a renderer:
 *  - `fixture-host-probe`, BUNDLED: `/source` answers the SDK's pinned
 *    compiler's bundle of the package directory, as the server answers a
 *    built-in or an admitted package;
 *  - `fixture-host-probe-imported`, IMPORTED: the package as a cell holds it
 *    before any bundle is stored. `/source` is a 404 until the browser has
 *    compiled `/package` in its worker and published it with `PUT /bundle`
 *    (lib/rendererBundle.loadRendererBundle), exactly the routes and checks
 *    of api/routes/blobs.py.
 * `/__host_probe/identity` reports the package's source hash, the server's
 * bundle hash and the browser's publications; `/__host_probe/reset` forgets
 * them; `/__host_probe/sheet.css` is a stylesheet that arrives late.
 */
import { createHash } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { pathToFileURL } from 'node:url';
import { HOST_PROBE_BUNDLED_ID, HOST_PROBE_IMPORTED_ID } from './rendererFixtures.ts';

interface Compiler {
    collectPackage(directory: string): Map<string, Uint8Array>;
    rendererSourceHash(files: Map<string, Uint8Array>, entrypoint: string): Promise<string>;
    bundleRenderer(input: { files: Map<string, Uint8Array>; entrypoint: string }): Promise<{ source_hash: string; code: string; bundle_hash: string }>;
}
interface Built { entrypoint: string; files: Map<string, Uint8Array>; source_hash: string; code: string; bundle_hash: string }
interface Publication { source_hash: string; bundle_hash: string; code: string }

export function hostProbeServer(packageDirectory: string, compilerModule: string) {
    let built: Promise<Built> | null = null;
    let published: Publication | null = null;
    let publishes: Publication[] = [];
    const build = () => built ??= import(/* @vite-ignore */ pathToFileURL(compilerModule).href).then(async (compiler: Compiler) => {
        const files = compiler.collectPackage(packageDirectory);
        const manifest = new TextDecoder().decode(files.get('host-probe.renderer'));
        const entrypoint = /^entrypoint:\s*(\S+)\s*$/m.exec(manifest)?.[1];
        if (!entrypoint) throw new Error('host-probe.renderer names no entrypoint');
        const bundle = await compiler.bundleRenderer({ files, entrypoint });
        return { entrypoint, files, ...bundle };
    });
    const send = (res: ServerResponse, status: number, type: string, body: string) => {
        res.statusCode = status;
        res.setHeader('Content-Type', type);
        res.setHeader('Cache-Control', 'no-store');
        res.end(body);
    };
    const json = (res: ServerResponse, status: number, value: unknown) => send(res, status, 'application/json', JSON.stringify(value));
    const body = (req: IncomingMessage) => new Promise<string>((resolve, reject) => {
        const chunks: Buffer[] = [];
        req.on('data', (chunk: Buffer) => chunks.push(chunk));
        req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
        req.on('error', reject);
    });

    /** Answers a probe route and returns true, or returns false. */
    return function handle(req: IncomingMessage, res: ServerResponse): boolean {
        const url = new URL(req.url ?? '/', 'http://fixture');
        if (url.pathname === '/__host_probe/sheet.css') {
            setTimeout(() => send(res, 200, 'text/css; charset=utf-8', '[data-host-probe-mark] { background-color: rgb(7, 8, 9); }\n'), 300);
            return true;
        }
        if (url.pathname === '/__host_probe/reset') {
            published = null;
            publishes = [];
            json(res, 200, { ok: true });
            return true;
        }
        if (url.pathname === '/__host_probe/identity') {
            void build().then(
                (b) => json(res, 200, { source_hash: b.source_hash, bundle_hash: b.bundle_hash, publishes: publishes.map(({ source_hash, bundle_hash }) => ({ source_hash, bundle_hash })) }),
                (error) => send(res, 500, 'text/plain', String(error)));
            return true;
        }
        const route = /^\/api\/renderers\/([^/]+)\/(source|package|bundle)$/.exec(url.pathname);
        if (!route || (route[1] !== HOST_PROBE_BUNDLED_ID && route[1] !== HOST_PROBE_IMPORTED_ID)) return false;
        const [, id, kind] = route;
        const version = url.searchParams.get('v');
        void build().then(async (b) => {
            if (kind === 'source') {
                if (version !== null && version !== b.source_hash) return json(res, 404, { detail: 'Renderer source version is no longer current' });
                const code = id === HOST_PROBE_BUNDLED_ID ? b.code : published?.code;
                if (code === undefined) return json(res, 404, { detail: 'Renderer bundle is not built' });
                res.setHeader('ETag', `"${b.source_hash}"`);
                res.setHeader('X-Entrypoint', 'module.js');
                return send(res, 200, 'application/javascript', code);
            }
            if (kind === 'package') {
                if (req.method !== 'GET' || version !== b.source_hash) return json(res, 404, { detail: 'Renderer source version is no longer current' });
                const files = Object.fromEntries([...b.files].map(([name, bytes]) => [name, Buffer.from(bytes).toString('base64')]));
                return json(res, 200, { source_hash: b.source_hash, entrypoint: b.entrypoint, files });
            }
            if (req.method !== 'PUT') return json(res, 405, { detail: 'Method Not Allowed' });
            const upload = JSON.parse(await body(req)) as { source_hash?: unknown; code?: unknown };
            if (typeof upload.code !== 'string' || upload.source_hash !== b.source_hash) return json(res, 409, { detail: 'Renderer source version is no longer current' });
            const publication = { source_hash: b.source_hash, code: upload.code, bundle_hash: createHash('sha256').update(upload.code, 'utf8').digest('hex') };
            publishes.push(publication);
            // The first distinct upload for a source identity is kept (renderer_bundles.publish_client_bundle).
            published ??= publication;
            return json(res, 200, { source_hash: published.source_hash, bundle_hash: published.bundle_hash });
        }).catch((error) => send(res, 500, 'text/plain', String(error)));
        return true;
    };
}

