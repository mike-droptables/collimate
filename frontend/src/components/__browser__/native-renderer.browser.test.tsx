import { fileFixture } from '@/test/fileFixtures';
import { describe, it, expect, vi } from "vitest";
import { render } from "vitest-browser-react";
import { page } from "vitest/browser";
import { NativeRenderer } from "../NativeRenderer";
import { useCollimateStore } from "@/stores/useCollimateStore";
import { Profiler, useLayoutEffect } from "react";
import { uploadBlob } from "@/lib/api";
import { cellFileCap } from "@/lib/cellFileCap";
import type { DocumentViewEdit } from '@/lib/fileDocument';
import type { FileDocument } from '@/lib/fileDocument';
import {getFileDocument} from '@/lib/fileDocuments';
import {
  MOUNTED_TEXT,
  FIXTURE_BASIC_ID,
  FIXTURE_REPORT_CHANGE_ID,
  FIXTURE_PROBE_ID,
  FIXTURE_PROBE_SLOW_ID,
  type ProbeState,
} from "../../test/rendererFixtures";

// stageDraftFile's network half, stubbed so a staged draft resolves true.
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  uploadBlob: vi.fn(async () => ({ blob_hash: "h", size: 1 })),
}));

// The cell-file capability a mount with a cell awaits (the vitest dev server
// has no file_cap route).
vi.mock("@/lib/cellFileCap", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/cellFileCap")>()),
  cellFileCap: vi.fn(async () => "testcap"),
}));

vi.mock('@/lib/fileDocuments',{spy:true});

vi.mock("@/hooks/useRendererId", () => ({
  useRendererId: () => ({renderer:{
    id: FIXTURE_PROBE_ID, name: "Probe", extensions: [".txt"],
    supportsDiff: false, liveRefresh: false, embed: "native", contentHash: "probe",
  },error:null}),
}));

const freshProbe = (): ProbeState => {
  const p: ProbeState = { mounts: 0, cleanups: 0, apis: [], fileUrlsAtMount: [], vis: [], set: [] };
  (window as unknown as { __probe: ProbeState }).__probe = p;
  return p;
};
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const probeApi = (p: ProbeState, i = 0) => p.apis[i] as any;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Browser-mode integration test for NativeRenderer.
 *
 * Exercises the real shadow-DOM pipeline end-to-end:
 *   1. NativeRenderer mounts a shadow root on its host div
 *   2. Fetches `/api/renderers/<id>/source` — served by the vitest dev
 *      server via rendererFixturePlugin (vitest.config.ts) with an
 *      X-Entrypoint header ending in `.js`, so the fixture runs as written
 *   3. Dynamic-imports the same URL — which is why a fetch mock isn't
 *      enough and the fixtures are served over real HTTP
 *   4. Calls `renderer.mount(mountEl, api)` — mount writes into the
 *      shadow DOM and calls `api.ready()` to lift the overlay
 *
 * Each test uses its own renderer id: NativeRenderer memoises loaded
 * modules per renderer for the whole session.
 *
 * If any of these steps breaks in a real browser, this test fails —
 * which is the payoff for running in browser mode rather than jsdom.
 */

describe("NativeRenderer (browser mode)", () => {
  it("F0701 shares the cached module across mounts and evicts the prior hash Blob", async () => {
    const p = freshProbe();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const createSpy = vi.spyOn(URL, 'createObjectURL');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL');
    const props = { rendererId: FIXTURE_PROBE_ID, rendererHash: 'F0701-first', fileContent: 'cache', isActiveFocus: true };
    try {
      const view = await render(<><NativeRenderer {...props} fileName="one.md" /><NativeRenderer {...props} fileName="two.md" /></>);
      await vi.waitFor(() => expect(p.mounts).toBe(2));
      const sourceCalls = () => fetchSpy.mock.calls.filter(([url]) => String(url).includes(`/api/renderers/${FIXTURE_PROBE_ID}/source?v=F0701`));
      expect(sourceCalls()).toHaveLength(1);
      const blob = createSpy.mock.results.find(result => result.type === 'return')?.value;
      expect(blob).toMatch(/^blob:/);
      await view.unmount();
      const remount = await render(<NativeRenderer {...props} fileName="three.md" />);
      await vi.waitFor(() => expect(p.mounts).toBe(3));
      expect(sourceCalls()).toHaveLength(1);
      await remount.rerender(<NativeRenderer {...props} rendererHash="F0701-second" fileName="three.md" />);
      await vi.waitFor(() => expect(p.mounts).toBe(4));
      expect(sourceCalls()).toHaveLength(2);
      expect(revokeSpy).toHaveBeenCalledWith(blob);
    } finally { fetchSpy.mockRestore(); createSpy.mockRestore(); revokeSpy.mockRestore(); }
  });

  it("F0701 failed module loads are evicted so Retry performs a fresh load", async () => {
    const p = freshProbe();
    const realFetch = globalThis.fetch;
    let sourceReads = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((...args) => {
      if (String(args[0]).includes(`/api/renderers/${FIXTURE_PROBE_ID}/source?v=F0701-retry`)) {
        sourceReads++;
        if (sourceReads === 1) return Promise.reject(new Error('F0701 controlled first source failure'));
      }
      return realFetch(...args);
    });
    try {
      await render(<NativeRenderer rendererId={FIXTURE_PROBE_ID} rendererHash="F0701-retry"
        fileContent="retry" fileName="retry.md" isActiveFocus={true} />);
      await expect.element(page.getByText("This file's renderer failed to load.")).toBeInTheDocument();
      await page.getByRole('button', { name: 'Retry', exact: true }).click();
      await vi.waitFor(() => expect(p.mounts).toBe(1));
      expect(sourceReads).toBe(2);
    } finally { fetchSpy.mockRestore(); }
  });

  it("fetches source, dynamic-imports, and the renderer writes into the shadow root __F1858_F1094", async () => {
    await render(
      <NativeRenderer
        rendererId={FIXTURE_BASIC_ID}
        fileContent="HELLO"
        fileName="fixture.md"
        isActiveFocus={true}
      />,
    );

    // Text is inside an open shadow root. Playwright's locator engine
    // pierces open shadow roots automatically, so a plain getByText
    // works.
    await expect
      .element(page.getByText(new RegExp(MOUNTED_TEXT)))
      .toBeInTheDocument();
    await expect
      .element(page.getByText(/HELLO$/))
      .toBeInTheDocument();
  });

  it("delivers reportChange immediately to its content owner", async () => {
    const changed=vi.fn();
    await render(<NativeRenderer rendererId={FIXTURE_REPORT_CHANGE_ID} fileContent="" fileName="note.md" isActiveFocus={true} onContentChange={changed}/>);
    await vi.waitFor(()=>expect(changed).toHaveBeenCalledExactlyOnceWith('new-content',{
      viewId:expect.any(String),viewSeq:1,
    }));
  });

  it("a run superseded mid-load leaves exactly one live mount", async () => {
    const p = freshProbe();
    const el = (diff: string) => (
      <NativeRenderer
        rendererId={FIXTURE_PROBE_SLOW_ID}
        fileContent="X"
        fileName="race.md"
        isActiveFocus={true}
        diffContent={diff}
      />
    );
    const { rerender, unmount } = await render(el("a"));
    await rerender(el("b"));
    await expect.element(page.getByText(/^probe-\d$/)).toBeInTheDocument();
    await sleep(400);
    expect(p.mounts - p.cleanups).toBe(1);
    await unmount();
    expect(p.cleanups).toBe(p.mounts);
  });

  it("an unmount during an async mount still runs that mount's cleanup", async () => {
    const p = freshProbe();
    const { unmount } = await render(
      <NativeRenderer rendererId={FIXTURE_PROBE_SLOW_ID} fileContent="X" fileName="gone.md" isActiveFocus={true} />,
    );
    await vi.waitFor(() => expect(p.mounts).toBe(1));
    await unmount();
    await sleep(400);
    expect(p.cleanups).toBe(1);
  });

  it("a dead mount's visibility subscriber is dropped on remount", async () => {
    const p = freshProbe();
    const el = (fileName: string, visible = true) => (
      <NativeRenderer rendererId={FIXTURE_PROBE_ID} fileContent="X" fileName={fileName} isActiveFocus={true} visible={visible} />
    );
    const { rerender } = await render(el("one.md"));
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    await rerender(el("two.md"));
    await expect.element(page.getByText("probe-2")).toBeInTheDocument();
    p.vis.length = 0;
    await rerender(el("two.md", false));
    await vi.waitFor(() => expect(p.vis).toEqual(["2:false"]));
  });

  it("the renderer's own reportChange is not echoed back through onSetContent __F0810", async () => {
    const p = freshProbe();
    const el = (content: string) => (
      <NativeRenderer rendererId={FIXTURE_PROBE_ID} fileContent={content} fileName="echo.md" isActiveFocus={true} />
    );
    const { rerender } = await render(el("v0"));
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    probeApi(p).reportChange("v1");
    await rerender(el("v1")); // the host feeding the renderer's emit back
    await rerender(el("v2")); // a genuine host push
    await vi.waitFor(() => expect(p.set).toEqual(["v2"]));
  });

  it("a 50 KB native file preserves its mount over twenty edit echoes and accepts external bytes __F0810", async () => {
    const p=freshProbe();const changed=vi.fn();const durations:number[]=[];const initial='a'.repeat(50_000);
    const el=(content:string)=><Profiler id="native-editor" onRender={(_id,_phase,ms)=>durations.push(ms)}><NativeRenderer rendererId={FIXTURE_PROBE_ID} fileName="echo.txt" fileContent={content} onContentChange={changed} isActiveFocus/></Profiler>;
    const view=await render(el(initial));await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    for(let i=0;i<20;i++){probeApi(p).reportChange(initial+i);await view.rerender(el(initial+i));}
    expect(changed).toHaveBeenCalledTimes(20);expect(p.mounts).toBe(1);expect(p.set).toEqual([]);
    await view.rerender(el('another writer body'));await vi.waitFor(()=>expect(p.set).toEqual(['another writer body']));expect(p.mounts).toBe(1);
    console.info('F0810 native edit profile',JSON.stringify({edits:20,bytes:50_000,commits:durations.length,totalRenderMs:durations.reduce((sum,ms)=>sum+ms,0),ownEchoes:0,mounts:p.mounts}));
  });

  it("a newer edit between host commit and passive effects cannot receive an older echo __P3F08", async () => {
    const p = freshProbe();
    function CommitBoundary({ content, nextEdit }: { content: string; nextEdit?: string }) {
      // Parent layout work runs after the child's commit but before passive
      // effects. This deterministically represents a new editor callback in
      // the interval that a busy browser otherwise exposes intermittently.
      useLayoutEffect(() => {
        if (nextEdit !== undefined) probeApi(p).reportChange(nextEdit);
      }, [nextEdit]);
      return <NativeRenderer rendererId={FIXTURE_PROBE_ID} fileContent={content}
        fileName="rapid.txt" isActiveFocus={true} />;
    }
    const view = await render(<CommitBoundary content="initial" />);
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    probeApi(p).reportChange("edit one");
    await view.rerender(<CommitBoundary content="edit one" nextEdit="edit two" />);
    expect(p.set).toEqual([]);
    expect(await probeApi(p).getContent()).toBe("edit two");
    await view.rerender(<CommitBoundary content="edit two" />);
    expect(p.set).toEqual([]);
    await view.rerender(<CommitBoundary content="external replacement" />);
    await vi.waitFor(() => expect(p.set).toEqual(["external replacement"]));
    expect(await probeApi(p).getContent()).toBe("external replacement");
    expect(p.mounts).toBe(1);
  });

  it('a host snapshot that ACKs only an earlier native emission cannot overwrite newer content',async()=>{
    const p=freshProbe(),changed=vi.fn(),snapshots=vi.fn();
    const el=(content:string,viewAcks:Readonly<Record<string,number>>={})=><NativeRenderer
      rendererId={FIXTURE_PROBE_ID} fileName="ordered.txt" fileContent={content}
      documentSnapshot={{content,editSeq:1,materializedSeq:1,viewAcks}}
      onContentChange={changed} isActiveFocus/>;
    const view=await render(el('initial'));
    await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    const api=probeApi(p);api.onDocumentSnapshot(snapshots);
    api.reportChange('edit one');api.reportChange('edit two');
    const first=changed.mock.calls[0][1] as DocumentViewEdit;
    const second=changed.mock.calls[1][1] as DocumentViewEdit;
    expect(second).toEqual({viewId:first.viewId,viewSeq:2});
    await view.rerender(el('edit one',{[first.viewId]:first.viewSeq}));
    expect(p.set).toEqual([]);expect(snapshots).not.toHaveBeenCalled();
    expect(await api.getContent()).toBe('edit two');
    await view.rerender(el('edit two',{[second.viewId]:second.viewSeq}));
    expect(p.set).toEqual([]);expect(snapshots).toHaveBeenCalledOnce();
    await view.rerender(el('external replacement',{[second.viewId]:second.viewSeq}));
    expect(p.set).toEqual(['external replacement']);
    expect(await api.getContent()).toBe('external replacement');
    expect(p.mounts).toBe(1);
  });

  it('raw snapshots share the native view sequence and stale pushes still update permissions',async()=>{
    const p=freshProbe(),changed=vi.fn(),snapshotChanged=vi.fn(),snapshots=vi.fn();
    const el=(content:string,viewAcks:Readonly<Record<string,number>>={},readOnly=false)=><NativeRenderer
      rendererId={FIXTURE_PROBE_ID} fileName="snapshot.txt" fileContent={content}
      documentSnapshot={{content,editSeq:1,materializedSeq:1,viewAcks}}
      onContentChange={changed} onSnapshotChange={snapshotChanged} readOnly={readOnly} isActiveFocus/>;
    const view=await render(el('initial'));
    await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    const api=probeApi(p);api.onDocumentSnapshot(snapshots);
    api.reportChange('materialized');
    const origin=changed.mock.calls[0][1] as DocumentViewEdit;
    const recovery={codecHash:'codec',snapshotId:'newest',payload:'raw'};
    api.saveSnapshot(recovery);
    expect(snapshotChanged).toHaveBeenCalledExactlyOnceWith(recovery,{viewId:origin.viewId,viewSeq:2});
    await view.rerender(el('materialized',{[origin.viewId]:1},true));
    expect(p.set).toEqual([]);expect(snapshots).not.toHaveBeenCalled();
    expect(api.isReadOnly()).toBe(true);
    api.reportChange('refused');api.saveSnapshot({...recovery,snapshotId:'refused'});
    expect(changed).toHaveBeenCalledOnce();expect(snapshotChanged).toHaveBeenCalledOnce();
  });

  it('a replacement native mount starts a distinct view rather than inheriting a pending ACK',async()=>{
    const p=freshProbe(),changed=vi.fn();
    const el=(fileName:string,content:string)=><NativeRenderer rendererId={FIXTURE_PROBE_ID}
      fileName={fileName} fileContent={content} documentSnapshot={{content,editSeq:0,materializedSeq:0,viewAcks:{}}}
      onContentChange={changed} isActiveFocus/>;
    const view=await render(el('old.txt','old accepted'));
    await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    probeApi(p).reportChange('old pending');
    const prior=changed.mock.calls[0][1] as DocumentViewEdit;
    await view.rerender(el('new.txt','new accepted'));
    await expect.element(page.getByText('probe-2')).toBeInTheDocument();
    expect(await probeApi(p,1).getContent()).toBe('new accepted');
    probeApi(p,1).reportChange('new edit');
    const next=changed.mock.calls[1][1] as DocumentViewEdit;
    expect(next.viewId).not.toBe(prior.viewId);expect(next.viewSeq).toBe(1);
    probeApi(p).reportChange('retired callback');
    expect(changed).toHaveBeenCalledTimes(2);
  });

  it('actual disposal releases its captured document owner even after its registry path changes',async()=>{
    const p=freshProbe(),changed=vi.fn(),forgetView=vi.fn();
    vi.mocked(getFileDocument).mockReturnValueOnce({forgetView} as unknown as FileDocument);
    const view=await render(<NativeRenderer rendererId={FIXTURE_PROBE_ID} fileName="source.txt"
      cellId="retargeted" channel="test" fileContent="initial" onContentChange={changed} isActiveFocus/>);
    await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    probeApi(p).reportChange('new body');
    const origin=changed.mock.calls[0][1] as DocumentViewEdit;
    // The path lookup no longer returns the owner after retargeting it.
    expect(getFileDocument('test','retargeted','source.txt')).toBeUndefined();
    await view.unmount();
    expect(forgetView).toHaveBeenCalledExactlyOnceWith(origin.viewId);
    const calls=changed.mock.calls.length;
    probeApi(p).reportChange('queued dead callback');
    expect(changed).toHaveBeenCalledTimes(calls);
  });

  it('replacing the document owner at the same path retires the prior native view',async()=>{
    const p=freshProbe(),changed=vi.fn();
    const el=(ownerId:string,content:string)=><NativeRenderer rendererId={FIXTURE_PROBE_ID}
      fileName="recreated.txt" fileContent={content} documentSnapshot={{ownerId,content,editSeq:0,materializedSeq:0,viewAcks:{}}}
      onContentChange={changed} isActiveFocus/>;
    const view=await render(el('original-owner','original'));
    await expect.element(page.getByText('probe-1')).toBeInTheDocument();
    probeApi(p).reportChange('original owner pending');
    const old=changed.mock.calls[0][1] as DocumentViewEdit;
    await view.rerender(el('recreated-owner','recreated accepted'));
    await expect.element(page.getByText('probe-2')).toBeInTheDocument();
    expect(p.cleanups).toBe(1);expect(p.set).toEqual([]);
    expect(await probeApi(p,1).getContent()).toBe('recreated accepted');
    probeApi(p).reportChange('late original callback');
    expect(changed).toHaveBeenCalledOnce();
    probeApi(p,1).reportChange('recreated edit');
    const next=changed.mock.calls[1][1] as DocumentViewEdit;
    expect(next.viewId).not.toBe(old.viewId);expect(next.viewSeq).toBe(1);
  });

  it.each([
    { fileName: "next.txt" },
    { cellId: "next-cell" },
    { channel: "next-channel" },
    { versionId: "next-version" },
    { rendererId: FIXTURE_PROBE_SLOW_ID },
    { rendererHash: "next-hash" },
  ])("a new identity never sends its content to the retiring mount %j __P3F08", async (next) => {
    const p = freshProbe();
    const props = { rendererId: FIXTURE_PROBE_ID, rendererHash: "initial-hash",
      fileName: "initial.txt", cellId: "initial-cell", channel: "initial-channel",
      versionId: "initial-version", isActiveFocus: true };
    const view = await render(<NativeRenderer {...props} fileContent="initial content" />);
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    await view.rerender(<NativeRenderer {...props} {...next} fileContent="next identity content" />);
    await expect.element(page.getByText("probe-2")).toBeInTheDocument();
    expect(p.set).toEqual([]);
    expect(await probeApi(p, 1).getContent()).toBe("next identity content");
    expect(p.cleanups).toBe(1);
    await view.rerender(<NativeRenderer {...props} {...next} fileContent="new owner's update" />);
    await vi.waitFor(() => expect(p.set).toEqual(["new owner's update"]));
    expect(p.mounts).toBe(2);
  });

  it("identity replacement initializes unchanged prop bytes instead of prior optimistic edits __P3F08", async () => {
    const p = freshProbe();
    const el = (name: string) => <NativeRenderer rendererId={FIXTURE_PROBE_ID}
      fileName={name} fileContent="same stored bytes" isActiveFocus={true} />;
    const view = await render(el("old.txt"));
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    probeApi(p).reportChange("old file's pending edit");
    await view.rerender(el("new.txt"));
    await expect.element(page.getByText("probe-2")).toBeInTheDocument();
    expect(p.set).toEqual([]);
    expect(await probeApi(p, 1).getContent()).toBe("same stored bytes");
  });

  it("api surface: segment encoding and sibling status __F1695_F1703", async () => {
    const p = freshProbe();
    useCollimateStore.setState({cellCache:{'c#1':{id:'c#1',name:'API',files:[fileFixture('api.md','X'),fileFixture('a/b #%.png','',{encoding:'binary'})]}}});
    await render(
      <NativeRenderer
        rendererId={FIXTURE_PROBE_ID}
        fileContent="X"
        fileName="api.md"
        cellId="c#1"
        channel="ch"
        isActiveFocus={true}
        readOnly={true}
      />,
    );
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    const api = probeApi(p);
    await expect(api.loadCss('data:text/css,a"b')).resolves.toBeUndefined();
    // F1941: built on the cell's capability URL.
    expect(api.getFileUrl("a/b #%.png")).toContain("/cellcap/testcap/channels/ch/cells/c%231/files/a/b%20%23%25.png");
    const fetchMock = vi.spyOn(window, "fetch").mockResolvedValueOnce(new Response("absent", { status: 404 }));
    try { await expect(api.getSiblingFile("missing.txt")).rejects.toThrow(/does not exist/); }
    finally { fetchMock.mockRestore(); }
    expect(await api.stageDraftFile("a.txt", "x")).toBe(false);
    expect(uploadBlob).not.toHaveBeenCalled();
  });

  it("History native reads and URLs use the exact snapshot __P0F02", async () => {
    const p = freshProbe();
    await render(<NativeRenderer rendererId={FIXTURE_PROBE_ID} fileContent="root"
      fileName="history.md" cellId="root" versionId="root" channel="ch"
      isActiveFocus={true} readOnly />);
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    const api = probeApi(p);
    expect(api.getFileUrl("nested/image.png")).toContain(
      "/cellcap/testcap/channels/ch/versions/root/files/nested/image.png");
    const fetchMock = vi.spyOn(window, "fetch").mockResolvedValueOnce(new Response("root bytes"));
    try {
      await expect(api.getSiblingFile("nested/read.txt")).resolves.toBe("root bytes");
      expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(
        "/channels/ch/versions/root/files/nested/read.txt"),undefined);
    } finally { fetchMock.mockRestore(); }
  });

  it("the cell's capability is in api.getFileUrl before the renderer mounts (F1941)", async () => {
    // A renderer builds URLs while it mounts (the html preview's frame
    // src): a cap arriving later would leave those on the plain URL, which
    // the opaque preview frame is refused.
    vi.mocked(cellFileCap).mockImplementationOnce(
      () => new Promise((resolve) => setTimeout(() => resolve("latecap"), 80)),
    );
    const p = freshProbe();p.urlPath='x';
    useCollimateStore.setState({cellCache:{c2:{id:'c2',name:'Late',files:[fileFixture('late.md','X'),fileFixture('x','X')]}}});
    await render(
      <NativeRenderer
        rendererId={FIXTURE_PROBE_ID}
        fileContent="X"
        fileName="late.md"
        cellId="c2"
        channel="ch"
        isActiveFocus={true}
      />,
    );
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    expect(p.fileUrlsAtMount).toEqual([
      expect.stringContaining("/cellcap/latecap/channels/ch/cells/c2/files/x"),
    ]);
  });

  it("a mount with no channel mints no capability and still mounts (F1941)", async () => {
    vi.mocked(cellFileCap).mockClear();
    const p = freshProbe();p.urlPath='x';
    useCollimateStore.setState({state:null,activeChannel:null,cellCache:{c3:{id:'c3',name:'No channel',files:[fileFixture('nochannel.md','X'),fileFixture('x','X')]}}});
    await render(
      <NativeRenderer
        rendererId={FIXTURE_PROBE_ID}
        fileContent="X"
        fileName="nochannel.md"
        cellId="c3"
        isActiveFocus={true}
      />,
    );
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    expect(cellFileCap).not.toHaveBeenCalled();
    expect(p.fileUrlsAtMount[0]).not.toContain("/cellcap/");
  });

  it("the loading spinner unmounts once the overlay has faded out", async () => {
    freshProbe();
    await render(<NativeRenderer rendererId={FIXTURE_PROBE_SLOW_ID} fileContent="X" fileName="spin.md" isActiveFocus={true} />);
    await expect.element(page.getByText("probe-1")).toBeInTheDocument();
    await expect.element(page.getByText("Loading renderer…")).not.toBeInTheDocument();
  });
});
