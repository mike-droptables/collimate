/**
 * Renderer-host conformance: one probe renderer (src/test/host-probe/, an
 * ordinary renderer package) on each of the three hosts a renderer runs in
 * (NativeRenderer, the pooled SandboxIframe and the fullscreen
 * StandaloneSandboxIframe), served both as a built bundle and as a cell
 * package the browser compiles (src/test/hostProbeServer.ts).
 *
 * The member table below is checked against the SDK's RendererAPI by the
 * compiler: a member added to or removed from types.ts fails the typecheck
 * until the table (and so the probe) follows it. At run time every host must
 * hand the renderer exactly the members its embed has, and each describe
 * ends by proving the probe called every one of them.
 *
 * Only the network edge is replaced: the file transport (requestJson), the
 * blob upload, a few read endpoints on fetch, and revealCellFile (the host's
 * navigation, observed). Everything between the renderer and those edges is
 * the app's own code.
 */
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {render} from 'vitest-browser-react';
import {useCallback, useLayoutEffect} from 'react';
import type * as Sdk from '../../../../sdk/packages/renderer-hooks/src/types';
import {NativeRenderer} from '../NativeRenderer';
import {SandboxIframe} from '../SandboxIframe';
import {StandaloneSandboxIframe} from '../StandaloneSandboxIframe';
import {GuestPoolProvider, useGuestPool, type GuestPoolAPI} from '@/contexts/GuestPoolContext';
import {useCollimateStore} from '@/stores/useCollimateStore';
import {useRendererStylesStore} from '@/stores/useRendererStylesStore';
import {fileFixture, fixtureBodyHash} from '@/test/fileFixtures';
import {HOST_PROBE_BUNDLED_ID, HOST_PROBE_IMPORTED_ID} from '@/test/rendererFixtures';
import {flushRendererEditors} from '@/lib/rendererFlush';
import {requestAnnotation, type AnnotationContext, type AnnotationRequest} from '@/lib/rendererAnnotation';
import {applyEditingSnapshot, readEditingSnapshot, type EditingSnapshot} from '@/lib/editingSnapshot';
import {rendererFontFamilies, type RendererFontFamily} from '@/lib/rendererStyles';
import type {ThemeFonts, ThemeSnapshot} from '@/lib/themeSnapshot';
import type {RendererStyleRecord} from '@/lib/api';
import type {FileMutation, FileOperation} from '@/lib/fileProtocol';
import type {DocumentViewEdit} from '@/lib/fileDocument';

const h = vi.hoisted(() => ({
    request: vi.fn(),
    reveal: vi.fn(),
    upload: vi.fn(async () => ({blob_hash: 'probe-blob', size: 3})),
}));
vi.mock('@/lib/api/client', async original => ({...await original<typeof import('@/lib/api/client')>(), getApiUrl: () => `${window.location.origin}/api`, requestJson: h.request}));
vi.mock('@/lib/api', async original => ({...await original<typeof import('@/lib/api')>(), getApiUrl: () => `${window.location.origin}/api`, uploadBlob: h.upload}));
vi.mock('@/lib/revealFile', () => ({revealCellFile: h.reveal, revealHandler: vi.fn()}));

// ---------------------------------------------------------------------------
// The member table: every RendererAPI member, and which hosts offer it. Only
// an optional member may be native-only (the compiler enforces that too).
// ---------------------------------------------------------------------------
type RendererAPI = Sdk.RendererAPI;
type Member = keyof RendererAPI;
type Optional<K extends Member> = undefined extends RendererAPI[K] ? true : false;
type Row<K extends Member> = {embed: Optional<K> extends true ? 'every' | 'native' : 'every'};
const MEMBERS = {
    getContent: {embed: 'every'}, getDocumentSnapshot: {embed: 'every'}, registerSnapshotCodec: {embed: 'every'},
    saveSnapshot: {embed: 'every'}, onDocumentSnapshot: {embed: 'every'}, getDiffContent: {embed: 'every'},
    getDiffLabels: {embed: 'every'}, getFilename: {embed: 'every'}, getCellId: {embed: 'every'},
    getSurface: {embed: 'every'}, onSurfaceChange: {embed: 'every'}, getFilePaths: {embed: 'every'},
    preview: {embed: 'every'},
    getSiblingFile: {embed: 'every'}, saveSiblingFile: {embed: 'every'}, getFileUrl: {embed: 'every'},
    reportChange: {embed: 'every'}, isReadOnly: {embed: 'every'}, canInteract: {embed: 'every'},
    onAnnotationRequest: {embed: 'every'}, onFilesChange: {embed: 'every'}, onSetContent: {embed: 'every'},
    onFlush: {embed: 'every'}, getVisible: {embed: 'native'}, onVisibleChange: {embed: 'native'},
    getFocused: {embed: 'native'}, onFocusChange: {embed: 'native'}, getTheme: {embed: 'every'},
    onThemeChange: {embed: 'every'}, getEditing: {embed: 'every'}, onEditingChange: {embed: 'every'},
    getStyle: {embed: 'every'}, onStyleChange: {embed: 'every'}, setStyle: {embed: 'every'},
    getFontFamilies: {embed: 'every'}, injectCss: {embed: 'every'}, loadCss: {embed: 'every'},
    ready: {embed: 'every'}, emitAction: {embed: 'native'}, stageDraftFile: {embed: 'native'},
    revealFile: {embed: 'every'}, desktop: {embed: 'native'},
} as const satisfies {[K in Member]-?: Row<K>};
type Embed = 'native' | 'iframe';
const ALL_MEMBERS = Object.keys(MEMBERS) as Member[];
const offered = (embed: Embed) => ALL_MEMBERS.filter(member => embed === 'native' || MEMBERS[member].embed === 'every');
/** A member as the probe calls it: the SDK's own signature. */
type Signature<M extends Member> = NonNullable<RendererAPI[M]> extends (...args: infer A) => infer R
    ? (...args: A) => R : () => RendererAPI[M];

// The declarations the hosts implement against, compared by the compiler:
// the optional members (every other one is in every host), and the records a
// host hands a renderer, which the app declares on its own side.
type Equal<A, B> = (<T>() => T extends A ? 1 : 2) extends (<T>() => T extends B ? 1 : 2) ? true : false;
type OptionalMembers = {[K in Member]-?: Optional<K> extends true ? K : never}[Member];
const DECLARATIONS: Record<string, true> = {
    optionalMembers: true satisfies Equal<OptionalMembers, 'getDiffLabels' | 'onAnnotationRequest' | 'onFlush' | 'getVisible'
        | 'onVisibleChange' | 'getFocused' | 'onFocusChange' | 'ready' | 'emitAction' | 'stageDraftFile' | 'desktop'>,
    theme: true satisfies Equal<keyof Sdk.RendererTheme, keyof ThemeSnapshot>,
    themeFonts: true satisfies Equal<Sdk.ThemeFonts, ThemeFonts>,
    editing: true satisfies Equal<Sdk.RendererEditing, EditingSnapshot>,
    style: true satisfies Equal<Sdk.RendererStyle, RendererStyleRecord>,
    fontFamily: true satisfies Equal<Sdk.RendererFontFamily, RendererFontFamily>,
    annotationRequest: true satisfies Equal<Sdk.AnnotationRequest, AnnotationRequest>,
    annotationContext: true satisfies Equal<Sdk.AnnotationContext, AnnotationContext>,
};

// ---------------------------------------------------------------------------
// The probe, as the test sees it in the host's realm.
// ---------------------------------------------------------------------------
interface ProbeRecord {
    n: number; keys: string[]; cleaned: boolean; mark: HTMLElement;
    call(name: Member, ...args: unknown[]): unknown;
    post(message: unknown): void;
}
type ProbeWindow = Window & typeof globalThis & {__hostProbe?: {mounts: ProbeRecord[]}};
const plain = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
const settle = (ms = 300) => new Promise(resolve => setTimeout(resolve, ms));
// An error from a frame is another realm's Error: compare its fields.
const failure = async (promise: unknown): Promise<{message: string; status?: number}> => {
    try { await promise; } catch (error) { return {message: String((error as Error).message), status: (error as {status?: number}).status}; }
    throw new Error('resolved');
};
const thrownBy = (run: () => unknown): string | null => {
    try { run(); } catch (error) { return String((error as Error).message); }
    return null;
};

// ---------------------------------------------------------------------------
// The cell, the network edge and the hosts.
// ---------------------------------------------------------------------------
const CHANNEL = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
const FILE = 'notes.probe';
const BODY = 'probe body';
const SESSION = {state: 'live', chat_id: 'probe-chat', ws_path: '/ws/probe', token: 'probe-token', label: 'Probe', run_id: 'probe-run'};
const PATHS = [FILE, 'two.probe', 'other.txt', 'binary.bin'];
let mutations: FileMutation[] = [];
let actions: unknown[] = [];
let fetchSpy: {mockRestore(): void} | null = null;

function seedCell(): string {
    const cellId = crypto.randomUUID();
    const files = [fileFixture(FILE, BODY), fileFixture('two.probe', BODY), fileFixture('other.txt', 'other body'),
        fileFixture('binary.bin', '', {encoding: 'binary', language: 'binary'})];
    const cell = {id: cellId, name: 'Probe', files};
    const channel = {id: CHANNEL, name: 'Probe', nav_stacks: [], main_stacks: [{id: 'probe-stack', name: '', cells: [cell]}], nav_selection_id: null, main_selection_id: 'probe-stack'};
    useCollimateStore.setState({cellCache: {[cellId]: cell}, activeChannel: channel, state: {active_channel_id: CHANNEL, channels: [channel]}} as never);
    return cellId;
}

beforeEach(() => {
    mutations = []; actions = [];
    h.reveal.mockReset(); h.upload.mockClear(); h.request.mockReset();
    h.request.mockImplementation(async (path: string, method: string, operation?: FileOperation) => {
        if (method === 'GET') return {files: [], conflicts: []};
        // A style commit persists the channel's renderer_styles section.
        if (method === 'PUT' && path.endsWith('/settings')) return operation;
        if (method !== 'POST' || !operation?.mutations) throw new Error(`Unexpected transport ${method} ${path}`);
        mutations.push(...operation.mutations);
        return {operation_id: operation.operation_id, outcome: 'accepted', conflicts: [], files: operation.mutations.map(mutation => ({
            path: mutation.path, file_revision: mutation.base_revision + 1, body_hash: mutation.blob_hash ?? fixtureBodyHash(mutation.content_text ?? ''),
            language: mutation.language ?? 'plaintext', encoding: mutation.encoding ?? 'utf-8', deleted: false, writer_id: mutation.writer_id, edit_seq: mutation.edit_seq,
        }))};
    });
    const original = window.fetch.bind(window);
    fetchSpy = vi.spyOn(window, 'fetch').mockImplementation(async (input, init) => {
        const url = String(input instanceof Request ? input.url : input);
        if (url.includes('/surfaces/')) return Response.json(SESSION);
        if (url.includes('/file-paths')) return Response.json({paths: PATHS});
        if (url.endsWith('/actions') && init?.method === 'POST') { actions.push(JSON.parse(String(init.body))); return Response.json({ok: true}); }
        return original(input, init);
    });
    delete (window as ProbeWindow).__hostProbe;
});
afterEach(() => { fetchSpy?.mockRestore(); fetchSpy = null; });

type HostName = 'native' | 'pooled' | 'standalone';
type Variant = 'bundled' | 'imported';
interface HostProps {
    rendererId: string; rendererHash?: string; cellId?: string; channel?: string; fileName: string; fileContent: string;
    documentId?: string; fileHash?: string; readOnly?: boolean; isActiveFocus: boolean; visible?: boolean;
    documentSnapshot?: {content: string; editSeq: number; materializedSeq: number; viewAcks: Record<string, number>};
    onContentChange?: (content: string, view?: DocumentViewEdit) => void;
    onSnapshotChange?: (snapshot: unknown, view?: DocumentViewEdit) => void;
}

function PoolOverlay({onPool}: {onPool: (pool: GuestPoolAPI) => void}) {
    const pool = useGuestPool();
    useLayoutEffect(() => onPool(pool));
    const ref = useCallback((element: HTMLDivElement | null) => pool.registerOverlay(element), [pool]);
    return <div ref={ref} data-pool-overlay style={{position: 'fixed', left: 0, top: 0, width: 0, height: 0}}/>;
}
function Host({host, props}: {host: HostName; props: HostProps}) {
    const {documentId, fileHash, visible, documentSnapshot, onSnapshotChange, ...common} = props;
    const snapshot = {documentSnapshot, onSnapshotChange: onSnapshotChange as never};
    if (host === 'native') return <NativeRenderer {...common} {...snapshot} visible={visible}/>;
    const Iframe = host === 'pooled' ? SandboxIframe : StandaloneSandboxIframe;
    return <Iframe {...common} {...snapshot} documentId={documentId} fileHash={fileHash ?? ''}/>;
}

/** One host on the page, inside a guest pool (the pooled host's frames live
 *  in its overlay) and a box it can lay out in. */
async function mountHost(host: HostName, initial: HostProps) {
    let pool: GuestPoolAPI | null = null;
    let props = initial;
    let show = true;
    const ui = () => (
        <GuestPoolProvider>
            <PoolOverlay onPool={next => { pool = next; }}/>
            <div data-host-box style={{width: 320, height: 200}}>{show ? <Host host={host} props={props}/> : null}</div>
        </GuestPoolProvider>
    );
    const view = await render(ui());
    const frameElement = () => (host === 'pooled'
        ? (props.documentId ? pool!.getElement(props.documentId) : view.container.querySelector('[data-pool-overlay] iframe'))
        : view.container.querySelector('[data-standalone-renderer] iframe')) as HTMLIFrameElement | null;
    const realm = (): ProbeWindow | null => (host === 'native' ? window as ProbeWindow : (frameElement()?.contentWindow ?? null) as ProbeWindow | null);
    const cover = () => view.container.querySelector('[data-host-box] .z-20') as HTMLElement | null;
    return {
        view, realm, frameElement,
        pool: () => pool!,
        lifted: () => cover()?.style.opacity === '0',
        covered: () => cover()?.style.opacity === '1',
        update: async (patch: Partial<HostProps>) => { props = {...props, ...patch}; await view.rerender(ui()); },
        setShown: async (next: boolean) => { show = next; await view.rerender(ui()); },
        /** The n-th probe mount in the host's current realm. */
        probe: async (n = 1): Promise<ProbeRecord> => {
            await vi.waitFor(() => expect(realm()?.__hostProbe?.mounts.length ?? 0).toBeGreaterThanOrEqual(n), {timeout: 20000});
            return realm()!.__hostProbe!.mounts[n - 1];
        },
        unmount: () => view.unmount(),
    };
}
type Mounted = Awaited<ReturnType<typeof mountHost>>;

/** What useChannelSSE does with a commit and a surface change: the pool's
 *  cell-scoped broadcast (pooled frames) and the window event (the native
 *  host and the fullscreen frame). */
function deliverFilesChanged(m: Mounted, cellId: string, changedFiles: string[]) {
    const files = useCollimateStore.getState().cellCache[cellId].files.filter(file => changedFiles.includes(file.name))
        .map(({name, ...file}) => ({path: name, deleted: false, ...file}));
    m.pool().broadcastToCell(cellId, {type: 'FILES_CHANGED', cellId, changedFiles, files});
    window.dispatchEvent(new CustomEvent('collimate:files-changed-broadcast', {detail: {cellId, changedFiles, files}}));
}
function deliverSurfaceChanged(m: Mounted, cellId: string, path: string) {
    m.pool().broadcastToCell(cellId, {type: 'SURFACE_CHANGED', cellId, path});
    window.dispatchEvent(new CustomEvent('collimate:surface-changed', {detail: {channel: CHANNEL, cellId, path}}));
}
async function sourceHash(): Promise<string> {
    return (await (await fetch('/__host_probe/identity')).json()).source_hash;
}

const HOSTS: Array<{host: HostName; embed: Embed}> = [{host: 'native', embed: 'native'}, {host: 'pooled', embed: 'iframe'}, {host: 'standalone', embed: 'iframe'}];
const VARIANTS: Array<{variant: Variant; rendererId: string}> = [{variant: 'bundled', rendererId: HOST_PROBE_BUNDLED_ID}, {variant: 'imported', rendererId: HOST_PROBE_IMPORTED_ID}];

it('the imported package compiles in the browser to the bytes the server bundles', async () => {
    await fetch('/__host_probe/reset', {method: 'POST'});
    const cellId = seedCell();
    const m = await mountHost('native', {rendererId: HOST_PROBE_IMPORTED_ID, rendererHash: await sourceHash(), cellId, channel: CHANNEL, fileName: FILE, fileContent: BODY, isActiveFocus: true});
    try {
        await m.probe();
        const identity = await (await fetch('/__host_probe/identity')).json();
        expect(identity.publishes).toEqual([{source_hash: identity.source_hash, bundle_hash: identity.bundle_hash}]);
    } finally { await m.unmount(); }
}, 40000);

for (const {host, embed} of HOSTS) for (const {variant, rendererId} of VARIANTS) {
    describe(`${host} host, ${variant} probe`, () => {
        const covered = new Set<Member>();
        const call = <M extends Member>(probe: ProbeRecord, member: M, ...args: Parameters<Signature<M>>) => {
            covered.add(member);
            return probe.call(member, ...args) as ReturnType<Signature<M>>;
        };
        const iframe = embed === 'iframe';

        async function setup(patch: Partial<HostProps> = {}) {
            const cellId = seedCell();
            const acks: Record<string, number> = {};
            const edits: Array<{content: string; view?: DocumentViewEdit}> = [];
            const snapshots: Array<{snapshot: unknown; view?: DocumentViewEdit}> = [];
            const ack = (view?: DocumentViewEdit) => { if (view) acks[view.viewId] = Math.max(acks[view.viewId] ?? 0, view.viewSeq); };
            const m = await mountHost(host, {
                rendererId, rendererHash: await sourceHash(), cellId, channel: CHANNEL, fileName: FILE, fileContent: BODY,
                documentId: iframe ? crypto.randomUUID() : undefined, isActiveFocus: true,
                documentSnapshot: {content: BODY, editSeq: 0, materializedSeq: 0, viewAcks: {}},
                onContentChange: (content, view) => { ack(view); edits.push({content, view}); },
                onSnapshotChange: (snapshot, view) => { ack(view); snapshots.push({snapshot: plain(snapshot), view}); },
                ...patch,
            });
            return {m, cellId, acks, edits, snapshots};
        }
        async function readied(m: Mounted, probe: ProbeRecord) {
            call(probe, 'ready');
            await vi.waitFor(() => expect(m.lifted()).toBe(true), {timeout: 5000});
            // An iframe host pushes its theme, editing and style on ready: let
            // them land before a test subscribes.
            if (iframe) await settle(200);
        }

        it('hands the renderer exactly its embed\'s members, and each read answers as the contract says', async () => {
            const {m, cellId} = await setup();
            try {
                const p = await m.probe();
                expect(p.keys).toEqual([...offered(embed)].sort());
                await readied(m, p);
                expect(await call(p, 'getContent')).toBe(BODY);
                expect(plain(await call(p, 'getDocumentSnapshot'))).toMatchObject({content: BODY, editSeq: 0, materializedSeq: 0});
                expect(await call(p, 'getDiffContent')).toBeNull();
                expect(call(p, 'getDiffLabels')).toBeNull();
                expect(call(p, 'getFilename')).toBe(FILE);
                expect(call(p, 'getCellId')).toBe(cellId);
                expect([call(p, 'isReadOnly'), call(p, 'canInteract')]).toEqual([false, true]);
                // The cell's capability URL, pinned to the accepted revision; the
                // same string in every host.
                const base = `${location.origin}/api/cellcap/testcap/channels/${CHANNEL}/cells/${cellId}/files/`;
                expect(call(p, 'getFileUrl', '')).toBe(base);
                expect(call(p, 'getFileUrl', 'other.txt')).toBe(`${base}other.txt?revision=1`);
                expect(thrownBy(() => call(p, 'getFileUrl', 'absent.txt'))).toBe('File absent.txt is unavailable.');
                expect(plain(await call(p, 'getFilePaths'))).toEqual(PATHS);
                expect(plain(await call(p, 'getSurface'))).toEqual(SESSION);
                expect(await failure(call(p, 'registerSnapshotCodec', {id: 'probe', version: 1, implementationHash: '0'.repeat(64), source: 'probe'})))
                    .toMatchObject({message: 'Snapshot codec implementation hash does not match its source.'});
                const files = {'main.js': new TextEncoder().encode('export default {mount(){}};')};
                const preview = call(p, 'preview');
                const compiled = plain(await preview.compile(files, 'main.js'));
                expect(compiled.source_hash).toMatch(/^[0-9a-f]{64}$/);
                expect(compiled.bundle_hash).toMatch(/^[0-9a-f]{64}$/);
                expect(compiled.code).toContain('mount');
                await preview.prepareDependencies(files, 'main.js');
                expect(await call(p, 'getSiblingFile', 'other.txt')).toBe('other body');
                expect(await failure(call(p, 'getSiblingFile', 'absent.txt'))).toMatchObject({status: 404});
            } finally { await m.unmount(); }
        }, 60000);

        it('sibling writes, edits, snapshots and flushes round-trip to the host', async () => {
            const {m, cellId, acks, edits, snapshots} = await setup();
            try {
                const p = await m.probe();
                await readied(m, p);
                // A committed text write is read back at once, through the
                // document owner the host shares with every other view.
                expect(await call(p, 'saveSiblingFile', 'other.txt', 'saved body')).toBe(true);
                expect(mutations).toMatchObject([{kind: 'put', path: 'other.txt', content_text: 'saved body', base_revision: 1}]);
                expect(await call(p, 'getSiblingFile', 'other.txt')).toBe('saved body');
                // A refused write resolves false; it never rejects.
                expect(await call(p, 'saveSiblingFile', 'binary.bin', 'text over bytes')).toBe(false);
                expect(mutations).toHaveLength(1);
                call(p, 'reportChange', 'edited body');
                await vi.waitFor(() => expect(edits.map(edit => edit.content)).toEqual(['edited body']));
                expect(edits[0].view).toMatchObject({viewSeq: 1});
                const recovery = {codecHash: 'probe-codec', snapshotId: 'probe-1', payload: {n: 1}};
                call(p, 'saveSnapshot', recovery);
                await vi.waitFor(() => expect(snapshots).toHaveLength(1));
                expect(snapshots[0]).toEqual({snapshot: recovery, view: {viewId: edits[0].view!.viewId, viewSeq: 2}});
                let flushes = 0;
                const offFlush = call(p, 'onFlush', () => { flushes++; });
                await flushRendererEditors(cellId, FILE);
                expect(flushes).toBe(1);
                const offRefusal = call(p, 'onFlush', () => Promise.reject(new Error('flush refused')));
                await expect(flushRendererEditors(cellId, FILE)).rejects.toThrow('flush refused');
                offRefusal(); offFlush();
                await flushRendererEditors(cellId, FILE);
                expect(flushes).toBe(2);
                if (!iframe) {
                    expect(await call(p, 'stageDraftFile', 'upload.bin', new Uint8Array([1, 2, 3]))).toBe(true);
                    expect(h.upload).toHaveBeenCalledWith(new Uint8Array([1, 2, 3]));
                    expect(mutations.at(-1)).toMatchObject({kind: 'put', path: 'upload.bin', blob_hash: 'probe-blob', encoding: 'binary'});
                    expect(await call(p, 'emitAction', 'probe-action', {answer: 42})).toBe(true);
                    expect(actions).toEqual([{action_id: 'probe-action', source: FILE, payload: {answer: 42}}]);
                }
                // The owner's accepted body comes back as a push the renderer
                // applies in place (it saw every emission it acknowledges).
                const pushedSnapshots: unknown[] = [];
                const pushedContent: string[] = [];
                const offSnapshot = call(p, 'onDocumentSnapshot', (snapshot: unknown) => pushedSnapshots.push(plain(snapshot)));
                const offContent = call(p, 'onSetContent', (content: string) => pushedContent.push(content));
                const pushed = {content: 'accepted body', editSeq: 3, materializedSeq: 3, viewAcks: {...acks}};
                await m.update({fileContent: pushed.content, documentSnapshot: pushed});
                await vi.waitFor(() => expect(pushedContent).toEqual(['accepted body']));
                expect(pushedSnapshots).toContainEqual(expect.objectContaining({content: 'accepted body', editSeq: 3, materializedSeq: 3}));
                offSnapshot(); offContent();
            } finally { await m.unmount(); }
        }, 60000);

        it('host pushes reach their subscriptions: files, surface, annotations, theme, editing, style and view state', async () => {
            const {m, cellId} = await setup();
            const root = document.documentElement;
            const sheet2 = root.style.getPropertyValue('--sheet-2');
            const editing = readEditingSnapshot();
            try {
                const p = await m.probe();
                await readied(m, p);
                const filesEvents: unknown[] = [];
                const offFiles = call(p, 'onFilesChange', (detail: unknown) => filesEvents.push(plain(detail)));
                deliverFilesChanged(m, cellId, ['other.txt']);
                await vi.waitFor(() => expect(filesEvents).toEqual([expect.objectContaining({cellId, changedFiles: ['other.txt']})]));
                offFiles();
                let surfaces = 0;
                const offSurface = call(p, 'onSurfaceChange', () => { surfaces++; });
                deliverSurfaceChanged(m, cellId, 'other.txt');
                deliverSurfaceChanged(m, cellId, FILE);
                await vi.waitFor(() => expect(surfaces).toBe(1));
                await settle(100);
                expect(surfaces).toBe(1);
                offSurface();

                // An annotation request arrives with exactly the SDK's fields,
                // and the renderer's answer comes back as the outcome.
                const requests: Sdk.AnnotationRequest[] = [];
                const context = {label: 'notes.probe › mark', text: 'the probe mark', anchor: {mark: 1}};
                const offAnnotation = call(p, 'onAnnotationRequest', async request => { requests.push(plain(request)); return context; });
                await expect(requestAnnotation(cellId, FILE, 'selection', {hint: 'probe hint'})).resolves.toEqual({status: 'ok', path: FILE, rendererId, context});
                expect(requests).toEqual([{id: expect.stringMatching(/^[0-9a-f-]{36}$/), mode: 'selection', hint: 'probe hint'}]);
                offAnnotation();
                await expect(requestAnnotation(cellId, FILE, 'selection')).resolves.toMatchObject({status: 'unavailable', reason: 'not-capable'});

                // Theme, editing and style: a snapshot at once, then every change.
                await vi.waitFor(() => expect(plain(call(p, 'getTheme')).known).toBe(true));
                expect(Object.keys(call(p, 'getTheme')).sort()).toEqual(['fonts', 'known', 'scheme', 'themeId', 'vars']);
                const themes: Sdk.RendererTheme[] = [];
                const offTheme = call(p, 'onThemeChange', theme => themes.push(plain(theme)));
                expect(themes).toHaveLength(1);
                root.style.setProperty('--sheet-2', 'rgb(1, 2, 3)');
                window.dispatchEvent(new CustomEvent('collimate:theme-changed'));
                await vi.waitFor(() => expect(themes.at(-1)?.vars['--sheet-2']).toBe('rgb(1, 2, 3)'));
                offTheme();
                expect(plain(call(p, 'getEditing'))).toEqual(editing);
                const editings: unknown[] = [];
                const offEditing = call(p, 'onEditingChange', (value: unknown) => editings.push(plain(value)));
                applyEditingSnapshot({spellcheck: !editing.spellcheck});
                await vi.waitFor(() => expect(editings).toEqual([editing, {spellcheck: !editing.spellcheck}]));
                expect(plain(call(p, 'getEditing'))).toEqual({spellcheck: !editing.spellcheck});
                if (iframe) expect(p.mark.ownerDocument.documentElement.getAttribute('spellcheck')).toBe(String(!editing.spellcheck));
                offEditing();
                expect(plain(call(p, 'getStyle'))).toEqual({theme: null, font: null, textSize: null});
                // A copy: changing it changes nothing in the host.
                call(p, 'getStyle').textSize = 9;
                expect(call(p, 'getStyle').textSize).toBeNull();
                const styles: unknown[] = [];
                const offStyle = call(p, 'onStyleChange', (style: unknown) => styles.push(plain(style)));
                call(p, 'setStyle', {textSize: 1.25});
                await vi.waitFor(() => expect(styles).toEqual([{theme: null, font: null, textSize: null}, {theme: null, font: null, textSize: 1.25}]));
                expect(useRendererStylesStore.getState().styles[rendererId]).toEqual({theme: null, font: null, textSize: 1.25});
                expect(plain(call(p, 'getStyle'))).toEqual({theme: null, font: null, textSize: 1.25});
                if (iframe) {
                    // A patch always lands under the frame's own renderer id,
                    // never one the message names.
                    p.post({type: 'SET_STYLE_PATCH', rendererId: 'someone-else', patch: {textSize: 1.5}});
                    await vi.waitFor(() => expect(useRendererStylesStore.getState().styles[rendererId]?.textSize).toBe(1.5));
                    expect(useRendererStylesStore.getState().styles['someone-else']).toBeUndefined();
                }
                offStyle();
                expect(plain(call(p, 'getFontFamilies'))).toEqual(rendererFontFamilies());

                if (!iframe) {
                    const visibility: boolean[] = [];
                    const focus: boolean[] = [];
                    const offVisible = call(p, 'onVisibleChange', (value: boolean) => visibility.push(value));
                    const offFocus = call(p, 'onFocusChange', (value: boolean) => focus.push(value));
                    await m.update({visible: false, isActiveFocus: false});
                    await vi.waitFor(() => expect([visibility, focus]).toEqual([[true, false], [true, false]]));
                    expect([call(p, 'getVisible'), call(p, 'getFocused')]).toEqual([false, false]);
                    offVisible(); offFocus();
                    await m.update({visible: true, isActiveFocus: true});
                    expect([call(p, 'getVisible'), call(p, 'getFocused'), visibility.length, focus.length]).toEqual([true, true, 2, 2]);
                    // No desktop shell in a browser: the surface is absent.
                    expect(call(p, 'desktop')).toBeUndefined();
                }
            } finally {
                await m.unmount();
                root.style.setProperty('--sheet-2', sheet2);
                applyEditingSnapshot(editing);
                await useRendererStylesStore.getState().commitStyle(rendererId, {textSize: null});
            }
        }, 60000);

        it('styles land in the renderer\'s own root and a reveal is the host\'s navigation, never its focus', async () => {
            const {m, cellId} = await setup();
            try {
                const p = await m.probe();
                await readied(m, p);
                const view = p.mark.ownerDocument.defaultView!;
                call(p, 'injectCss', '[data-host-probe-mark] { color: rgb(4, 5, 6); }');
                expect(view.getComputedStyle(p.mark).color).toBe('rgb(4, 5, 6)');
                // The sheet answers after 300 ms: both calls wait for it, and
                // the second takes the first one's link instead of adding one.
                const href = '/__host_probe/sheet.css';
                const first = call(p, 'loadCss', href);
                const second = call(p, 'loadCss', href);
                const links = [...(p.mark.getRootNode() as ParentNode).querySelectorAll('link')].filter(link => link.getAttribute('href') === href);
                expect(links).toHaveLength(1);
                expect(links[0].sheet).toBeNull();
                await second;
                expect(links[0].sheet).not.toBeNull();
                await first;
                expect(view.getComputedStyle(p.mark).backgroundColor).toBe('rgb(7, 8, 9)');
                // A renderer asking for the main focus area too: that is the host's alone.
                call(p, 'revealFile', 'other.txt', {newTab: true, focus: true} as {newTab: boolean});
                await vi.waitFor(() => expect(h.reveal).toHaveBeenCalledWith(cellId, 'other.txt', {newTab: true}));
                expect(h.reveal).toHaveBeenCalledTimes(1);
            } finally { await m.unmount(); }
        }, 40000);

        it('ready lifts the cover once, and only for the current mount', async () => {
            const {m} = await setup();
            const readyFrom = new Map<unknown, number>();
            const countReady = (event: MessageEvent) => { if (event.data?.type === 'RENDERER_READY') readyFrom.set(event.source, (readyFrom.get(event.source) ?? 0) + 1); };
            window.addEventListener('message', countReady);
            try {
                const first = await m.probe();
                const firstWindow = m.realm()!;
                expect(m.covered()).toBe(true);
                call(first, 'ready'); call(first, 'ready');
                await vi.waitFor(() => expect(m.lifted()).toBe(true));
                if (iframe) {
                    await settle();
                    expect(readyFrom.get(firstWindow)).toBe(1);
                }
                if (host === 'pooled') {
                    // A host re-acquiring the parked frame pings it; the frame,
                    // ready already, answers at once (no 3 s safety net).
                    await m.setShown(false);
                    const shown = performance.now();
                    await m.setShown(true);
                    await vi.waitFor(() => expect(m.lifted()).toBe(true), {timeout: 2500});
                    expect(performance.now() - shown).toBeLessThan(2500);
                    expect(readyFrom.get(firstWindow)).toBe(2);
                    expect(firstWindow.__hostProbe!.mounts).toHaveLength(1);
                }
                // A new mount covers again; the superseded one cannot lift it.
                await m.update({fileName: 'two.probe', ...(iframe ? {documentId: crypto.randomUUID()} : {})});
                const second = await m.probe(iframe ? 1 : 2);
                expect(second).not.toBe(first);
                expect(m.covered()).toBe(true);
                if (host === 'native') call(first, 'ready');
                // The pooled host's first frame is parked in the pool, alive.
                if (host === 'pooled') first.post({type: 'RENDERER_READY'});
                // The fullscreen host's first frame is gone; its last message
                // can still be in flight.
                if (host === 'standalone') window.dispatchEvent(new MessageEvent('message', {source: firstWindow, origin: location.origin, data: {type: 'RENDERER_READY'}}));
                await settle();
                expect(m.covered()).toBe(true);
                call(second, 'ready');
                await vi.waitFor(() => expect(m.lifted()).toBe(true));
            } finally { window.removeEventListener('message', countReady); await m.unmount(); }
        }, 40000);

        it('the read-only lock reaches the renderer live, and a locked view writes nothing', async () => {
            const {m, cellId, edits, snapshots} = await setup({readOnly: true});
            try {
                const p = await m.probe();
                await readied(m, p);
                expect([call(p, 'isReadOnly'), call(p, 'canInteract')]).toEqual([true, false]);
                call(p, 'reportChange', 'locked edit');
                call(p, 'saveSnapshot', {codecHash: 'probe-codec', snapshotId: 'locked', payload: {}});
                expect(await call(p, 'saveSiblingFile', 'other.txt', 'locked write')).toBe(false);
                if (!iframe) {
                    expect(await call(p, 'stageDraftFile', 'upload.bin', new Uint8Array([1]))).toBe(false);
                    expect(await call(p, 'emitAction', 'probe-action')).toBe(false);
                }
                // The host sends a locked view no annotation request.
                let asked = 0;
                const off = call(p, 'onAnnotationRequest', async () => { asked++; return {label: 'x', text: 'x'}; });
                await expect(requestAnnotation(cellId, FILE, 'selection')).resolves.toMatchObject({status: 'unavailable', reason: 'read-only'});
                off();
                // A style is the view's, not the file's: a locked view sets it.
                call(p, 'setStyle', {textSize: 1.75});
                await vi.waitFor(() => expect(useRendererStylesStore.getState().styles[rendererId]?.textSize).toBe(1.75));
                await settle();
                expect([edits, snapshots, mutations, actions, asked, h.upload.mock.calls.length]).toEqual([[], [], [], [], 0, 0]);
                await m.update({readOnly: false});
                await vi.waitFor(() => expect([call(p, 'isReadOnly'), call(p, 'canInteract')]).toEqual([false, true]));
                await m.update({readOnly: true});
                await vi.waitFor(() => expect([call(p, 'isReadOnly'), call(p, 'canInteract')]).toEqual([true, false]));
            } finally {
                await m.unmount();
                await useRendererStylesStore.getState().commitStyle(rendererId, {textSize: null});
            }
        }, 40000);

        if (iframe) it('ignores messages from a foreign window, in both directions', async () => {
            const {m} = await setup();
            const foreign = document.createElement('iframe');
            // Same origin as the host and the frame, so only the source tells
            // its messages apart.
            foreign.srcdoc = `<script>addEventListener('message', e => {
                if (e.source !== parent) return;
                if (e.data.to === 'host') parent.postMessage(e.data.message, '*');
                else for (const f of Array.from(parent.frames)) { try { if (f.__hostProbe) f.postMessage(e.data.message, '*'); } catch {} }
            });</script>`;
            const foreignLoaded = new Promise(resolve => { foreign.onload = resolve; });
            document.body.append(foreign);
            try {
                const p = await m.probe();
                await foreignLoaded;
                const frame = m.realm()!;
                const contents: string[] = [];
                const styles: unknown[] = [];
                let asked = 0;
                call(p, 'onSetContent', (content: string) => contents.push(content));
                call(p, 'onStyleChange', (style: unknown) => styles.push(plain(style)));
                call(p, 'onAnnotationRequest', async () => { asked++; return null; });
                const toHost = [{type: 'RENDERER_READY'}, {type: 'SIBLING_WRITE', id: 1, path: 'forged.txt', content: 'forged'},
                    {type: 'REVEAL_FILE', path: 'forged.txt'}, {type: 'SET_STYLE_PATCH', patch: {textSize: 2}}];
                const toFrame = [{type: 'SET_DOCUMENT', content: 'forged', editSeq: 0, materializedSeq: 0},
                    {type: 'ANNOTATION_REQUEST', requestId: 'forged', mode: 'selection'}, {type: 'SET_STYLE', style: {textSize: 2}}];
                for (const message of toHost) {
                    foreign.contentWindow!.postMessage({to: 'host', message}, '*');
                    window.dispatchEvent(new MessageEvent('message', {source: frame, origin: 'https://foreign.invalid', data: message}));
                }
                for (const message of toFrame) {
                    foreign.contentWindow!.postMessage({to: 'frame', message}, '*');
                    frame.dispatchEvent(new frame.MessageEvent('message', {source: window, origin: 'https://foreign.invalid', data: message}));
                }
                await settle();
                expect(m.covered()).toBe(true);
                expect([mutations, h.reveal.mock.calls, useRendererStylesStore.getState().styles[rendererId], contents, asked, styles.length])
                    .toEqual([[], [], undefined, [], 0, 1]);
                // The same events from the right window and origin are heard.
                window.dispatchEvent(new MessageEvent('message', {source: frame, origin: location.origin, data: {type: 'RENDERER_READY'}}));
                await vi.waitFor(() => expect(m.lifted()).toBe(true));
                frame.dispatchEvent(new frame.MessageEvent('message', {source: window, origin: location.origin, data: {type: 'SET_DOCUMENT', content: 'genuine', editSeq: 0, materializedSeq: 0}}));
                await vi.waitFor(() => expect(contents).toEqual(['genuine']));
            } finally { foreign.remove(); await m.unmount(); }
        }, 40000);

        if (variant === 'bundled') it('a renderer that never calls ready is uncovered by the 3 s safety net, not before', async () => {
            const started = performance.now();
            const {m} = await setup();
            try {
                await m.probe();
                expect(m.covered()).toBe(true);
                await vi.waitFor(() => expect(m.lifted()).toBe(true), {timeout: 8000, interval: 25});
                expect(performance.now() - started).toBeGreaterThanOrEqual(2950);
            } finally { await m.unmount(); }
        }, 20000);

        if (iframe && variant === 'bundled') it('a view with no cell takes the lock from the sandbox URL', async () => {
            const fileHash = URL.createObjectURL(new Blob([BODY]));
            try {
                for (const readOnly of [true, false]) {
                    const m = await mountHost(host, {rendererId, rendererHash: await sourceHash(), fileName: FILE, fileContent: BODY, fileHash, readOnly, isActiveFocus: true});
                    try {
                        const p = await m.probe();
                        expect(m.frameElement()!.src.includes('readonly=1')).toBe(readOnly);
                        expect(await call(p, 'getContent')).toBe(BODY);
                        expect(call(p, 'isReadOnly')).toBe(readOnly);
                    } finally { await m.unmount(); }
                }
            } finally { URL.revokeObjectURL(fileHash); }
        }, 40000);

        it('the probe called every member this host offers', () => {
            expect(Object.values(DECLARATIONS)).not.toContain(false);
            expect(offered(embed).filter(member => !covered.has(member))).toEqual([]);
            expect(covered.size).toBe(embed === 'native' ? ALL_MEMBERS.length : offered('iframe').length);
        });
    });
}

it('a frame no host has answered reads the placeholders, and its sibling write resolves false', async () => {
    const code = await (await fetch(`/api/renderers/${HOST_PROBE_BUNDLED_ID}/source`)).text();
    const frame = document.createElement('iframe');
    // A parent that hands over the bundle and answers nothing else.
    const answer = (event: MessageEvent) => {
        if (event.source === frame.contentWindow && event.data?.type === 'RENDERER_BUNDLE_REQUEST')
            frame.contentWindow!.postMessage({type: 'RENDERER_BUNDLE_RESULT', token: event.data.token, code}, location.origin);
    };
    window.addEventListener('message', answer);
    frame.src = `/sandbox.html?renderer=${HOST_PROBE_BUNDLED_ID}&hash=&apiBase=`;
    document.body.append(frame);
    try {
        await vi.waitFor(() => expect((frame.contentWindow as ProbeWindow | null)?.__hostProbe?.mounts).toHaveLength(1), {timeout: 15000});
        const p = (frame.contentWindow as ProbeWindow).__hostProbe!.mounts[0];
        // Until the host's first theme, editing and style arrive.
        expect(plain(p.call('getTheme'))).toEqual({themeId: 'unknown', scheme: 'dark', known: false, vars: {}, fonts: {ui: '', mono: '', prose: ''}});
        expect(plain([p.call('getEditing'), p.call('getStyle'), p.call('getFontFamilies')])).toEqual([{spellcheck: true}, {theme: null, font: null, textSize: null}, []]);
        // A theme is known only when the host says so; its tokens land on :root by name.
        frame.contentWindow!.postMessage({type: 'SET_THEME', themeId: 'probe', scheme: 'light', vars: {'--sheet-2': 'rgb(9, 8, 7)'}, fonts: {}}, location.origin);
        await vi.waitFor(() => expect(plain(p.call('getTheme'))).toMatchObject({themeId: 'probe', scheme: 'light', known: false}));
        expect(frame.contentWindow!.getComputedStyle(frame.contentDocument!.documentElement).getPropertyValue('--sheet-2').trim()).toBe('rgb(9, 8, 7)');
        frame.contentWindow!.postMessage({type: 'SET_THEME', themeId: 'probe', scheme: 'light', known: true, vars: {}, fonts: {}}, location.origin);
        await vi.waitFor(() => expect(plain(p.call('getTheme'))).toMatchObject({known: true}));
        const started = performance.now();
        await expect(p.call('saveSiblingFile', 'other.txt', 'unanswered')).resolves.toBe(false);
        expect(performance.now() - started).toBeGreaterThanOrEqual(9900);
    } finally { window.removeEventListener('message', answer); frame.remove(); }
}, 30000);
