// @vitest-environment jsdom
import 'fake-indexeddb/auto';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('@/lib/api/client', async (original) => ({ ...await original<Record<string, unknown>>(), requestJson: vi.fn() }));
import { requestJson } from '@/lib/api/client';
import { useCollimateStore } from '../useCollimateStore';
import { cellDisplayWrites } from '@/lib/cellDisplayWrites';
import { encodeCellDisplay } from '@/lib/cellDisplay';
import { createDefaultLayout, cycleRunsPaneSide, setFilesPane, setRunsPane, openFileTab, splitWithTab, allTabs, toggleRunsPane } from '@/lib/cellLayout';
import { fileFixture } from '@/test/fileFixtures';
import type { Channel, Cell } from '@/lib/types';
const channelId = '11111111-1111-4111-8111-111111111111', otherId = '22222222-2222-4222-8222-222222222222', cellId = '33333333-3333-4333-8333-333333333333';
const cell: Cell = { id: cellId, name: 'Display', files: [fileFixture('a.md', 'A'), fileFixture('b.py', 'B')], pinned: 'a.md' };
const channel: Channel = { id: channelId, name: 'Display channel', nav_stacks: [], main_stacks: [{ id: 'stack', stack_type: 'standard', name: '', cells: [cell] }], nav_selection_id: null, main_selection_id: 'stack' };
const seed = () => createDefaultLayout({ filePaths: ['a.md', 'b.py'], pinnedPath: 'a.md' });
let release!: (value: unknown) => void;
beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(requestJson).mockImplementation(async (path, method) => method === 'GET' && path.endsWith('/cell_display') ? new Promise(r => { release = r; }) : { ok: true });
    useCollimateStore.setState({ state: null, activeChannel: null, cellCache: {}, runs: {}, cellLayouts: {}, cellHistory: {}, _cellDisplayEdits: {}, _cellDisplayValues: {}, _cellDisplayFetch: 0, cellDisplaysReady: false, cellDisplayErrors: {}, cellDisplayLoadError: null });
    useCollimateStore.getState().applyServerState({ active_channel_id: channelId, channels: [channel] });
});
afterEach(() => { cellDisplayWrites.forget(channelId, [cellId]); cellDisplayWrites.forget(otherId, [cellId]); });
it('server seed restores splits while default and transient mount publications never write', async () => {
    const store = useCollimateStore.getState();
    const loading = store.hydrateCellDisplays();
    store.seedCellLayout(cellId, seed());
    expect(useCollimateStore.getState().cellLayouts[cellId]).toBeUndefined();
    store.updateCellLayout(cellId, l => setFilesPane(l, { currentFolder: 'folder' }), seed);
    expect(useCollimateStore.getState()._cellDisplayEdits[cellId]).toBeUndefined();
    let saved = openFileTab(seed(), 'b.py', null, 'code-editor', { newTab: true });
    saved = splitWithTab(saved, saved.activeLeafId, allTabs(saved).find(t => t.kind === 'file' && t.path === 'b.py')!.id, 'right');
    release({ [cellId]: encodeCellDisplay(saved, null) });
    await loading;
    expect(useCollimateStore.getState().cellLayouts[cellId].root.type).toBe('split');
    expect(allTabs(useCollimateStore.getState().cellLayouts[cellId])).toEqual(allTabs(saved));
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.every(call => call[1] === 'GET')).toBe(true);
});
it('an actual local display edit during hydration wins over its earlier server snapshot', async () => {
    const store = useCollimateStore.getState(), loading = store.hydrateCellDisplays();
    store.updateCellLayout(cellId, l => setFilesPane(l, { collapsed: false }), seed);
    release({ [cellId]: encodeCellDisplay(seed(), null) });
    await loading;
    expect(useCollimateStore.getState().cellLayouts[cellId].filesPane.collapsed).toBe(false);
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.find(call => call[1] === 'PUT')?.[2]).toMatchObject({ filesPane: { collapsed: false } });
});
it('old channel reads cannot seed a new channel and queued writes retain their UUID', async () => {
    const store = useCollimateStore.getState(), loading = store.hydrateCellDisplays();
    store.updateCellLayout(cellId, l => setFilesPane(l, { collapsed: false }), seed);
    store.applyServerState({ active_channel_id: otherId, channels: [channel, { ...channel, id: otherId, name: 'Other' }] });
    release({ [cellId]: encodeCellDisplay(seed(), null) });
    await loading;
    expect(useCollimateStore.getState().cellLayouts).toEqual({});
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.find(call => call[1] === 'PUT')?.[0]).toBe(`/channels/${channelId}/cell_display/${cellId}`);
});
it('failed hydration remains visible and retryable without blocking default editing', async () => {
    vi.mocked(requestJson).mockRejectedValueOnce(new Error('display read refused'));
    await useCollimateStore.getState().hydrateCellDisplays();
    expect(useCollimateStore.getState()).toMatchObject({ cellDisplaysReady: true, cellDisplayLoadError: 'display read refused' });
    useCollimateStore.getState().seedCellLayout(cellId, seed());
    expect(useCollimateStore.getState().cellLayouts[cellId]).toBeDefined();
    const retry = useCollimateStore.getState().hydrateCellDisplays();
    release({});
    await retry;
    expect(useCollimateStore.getState().cellDisplayLoadError).toBeNull();
});
it('a failed display flush retains its pending record and retry completes it', async () => {
    useCollimateStore.getState().updateCellLayout(cellId, l => setFilesPane(l, { collapsed: false }), seed);
    vi.mocked(requestJson).mockRejectedValueOnce(new Error('display write refused'));
    await expect(cellDisplayWrites.flush(channelId, [cellId])).rejects.toThrow('display write refused');
    expect(useCollimateStore.getState().cellDisplayErrors[cellId]).toBe('display write refused');
    await useCollimateStore.getState().retryCellDisplay(cellId);
    expect(useCollimateStore.getState().cellDisplayErrors[cellId]).toBeUndefined();
});
it('split sizing and the session-only folder do not write the durable display descriptor', async () => {
    useCollimateStore.setState({ cellDisplaysReady: true });
    useCollimateStore.getState().seedCellLayout(cellId, seed());
    useCollimateStore.getState().updateCellLayout(cellId, l => setFilesPane(l, { currentFolder: 'private', selection: { nodePath: 'a.md', isFolder: false } }));
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(requestJson).not.toHaveBeenCalled();
});
it('a dock side, width and height commit write the durable display descriptor (FP-01)', async () => {
    useCollimateStore.setState({ cellDisplaysReady: true });
    useCollimateStore.getState().seedCellLayout(cellId, seed());
    useCollimateStore.getState().updateCellLayout(cellId, l => setFilesPane(l, { side: 'top', width: 301, height: 144 }));
    expect(useCollimateStore.getState()._cellDisplayEdits[cellId]).toBe(1);
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.find(call => call[1] === 'PUT')?.[2]).toMatchObject({ filesPane: { side: 'top', width: 301, height: 144 } });
});
it('a runs pane change writes the durable display descriptor', async () => {
    useCollimateStore.setState({ cellDisplaysReady: true });
    useCollimateStore.getState().seedCellLayout(cellId, seed());
    useCollimateStore.getState().updateCellLayout(cellId, l => setRunsPane(l, { open: true, side: 'bottom', width: 300 }));
    expect(useCollimateStore.getState()._cellDisplayEdits[cellId]).toBe(1);
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.find(call => call[1] === 'PUT')?.[2]).toEqual(expect.objectContaining({ runsPane: { open: true, side: 'bottom', width: 300, height: null } }));
});
it('updateRunsPane seeds an unseeded cell from its default layout, as saveCellDisplay does, and writes the descriptor', async () => {
    useCollimateStore.setState({ cellDisplaysReady: true });
    useCollimateStore.getState().updateRunsPane(cellId, toggleRunsPane);
    const layout = useCollimateStore.getState().cellLayouts[cellId];
    expect(layout.runsPane).toEqual({ open: true, side: 'right', width: null, height: null });
    // The cell's pinned file leads its default tabs.
    expect(allTabs(layout)).toEqual([expect.objectContaining({ kind: 'file', path: 'a.md' })]);
    await cellDisplayWrites.flush(channelId, [cellId]);
    expect(vi.mocked(requestJson).mock.calls.find(call => call[1] === 'PUT')?.[2]).toMatchObject({ runsPane: { open: true, side: 'right' } });
    // A stored layout is the one it changes.
    useCollimateStore.getState().updateRunsPane(cellId, cycleRunsPaneSide);
    expect(useCollimateStore.getState().cellLayouts[cellId]).toMatchObject({ root: layout.root, runsPane: { open: true, side: 'bottom' } });
});
it('updateRunsPane leaves an unseeded cell alone until the displays load, and an unknown cell always', async () => {
    useCollimateStore.getState().updateRunsPane(cellId, toggleRunsPane);
    expect(useCollimateStore.getState().cellLayouts[cellId]).toBeUndefined();
    useCollimateStore.setState({ cellDisplaysReady: true });
    useCollimateStore.getState().updateRunsPane('no-such-cell', toggleRunsPane);
    expect(useCollimateStore.getState().cellLayouts).toEqual({});
});
it('hydration restores the stored sides and sizes, and the runs pane (FP-01)', async () => {
    const loading = useCollimateStore.getState().hydrateCellDisplays();
    release({ [cellId]: encodeCellDisplay(setRunsPane(setFilesPane(seed(), { side: 'top', width: 260, height: null }), { open: true, side: 'bottom', height: 180 }), null) });
    await loading;
    expect(useCollimateStore.getState().cellLayouts[cellId].filesPane).toMatchObject({ side: 'top', width: 260, height: null });
    expect(useCollimateStore.getState().cellLayouts[cellId].runsPane).toEqual({ open: true, side: 'bottom', width: null, height: 180 });
});
it('validates history selection and comparison inventories and reconciles missing paths', async () => {
    const layout = seed();
    if (layout.root.type !== 'leaf')
        throw new Error('fixture');
    const side = { channel: channelId, cellId, fileName: 'absent.md', label: 'Still a label' };
    layout.root.tabs.push({ id: 'missing', kind: 'file', path: 'absent.md' }, { id: 'old-history', kind: 'history', category: 'file', path: 'old.md' }, { id: 'compare', kind: 'compare', base: side, current: { ...side, fileName: 'a.md' } });
    const saved = encodeCellDisplay(layout, { nodeId: 'unavailable', versionId: 'gone', sectionsOpen: { config: true, files: true, logs: true } });
    vi.mocked(requestJson).mockImplementation(async (path) => path.endsWith('/cell_display') ? { [cellId]: saved } : { files: [{ path: 'a.md', deleted: false }] });
    const history = await import('@/lib/api/history');
    const fetchHistory = vi.spyOn(history, 'fetchCellHistory').mockResolvedValue({ nodes: [], total_versions: 0, current: { node_id: null, version_cell_id: 'missing' } });
    try {
        await useCollimateStore.getState().hydrateCellDisplays();
        expect(allTabs(useCollimateStore.getState().cellLayouts[cellId]).map(t => t.id)).not.toEqual(expect.arrayContaining(['missing', 'old-history', 'compare']));
        expect(useCollimateStore.getState().cellHistory[cellId]).toBeUndefined();
    }
    finally {
        fetchHistory.mockRestore();
    }
});
it('Remove refuses before issuing its consuming operation when display persistence fails', async () => {
    useCollimateStore.setState(prev => ({ overviewData: { ...prev.overviewData, channel: channel.name } }));
    useCollimateStore.getState().updateCellLayout(cellId, l => setFilesPane(l, { collapsed: false }), seed);
    vi.mocked(requestJson).mockRejectedValueOnce(new Error('display write refused'));
    expect(await useCollimateStore.getState().removeFromBoard(cellId)).toBe(false);
    expect(vi.mocked(requestJson).mock.calls.some(call => call[0].endsWith('/remove'))).toBe(false);
    expect(useCollimateStore.getState().overviewData.error).toContain('display write refused');
    expect(useCollimateStore.getState().locateCell(cellId)).not.toBeNull();
});
it('restores valid history artifact tabs and comparison coordinates',async()=>{
 const layout=seed();if(layout.root.type!=='leaf')throw new Error('fixture');
 const side={channel:channelId,cellId,fileName:'a.md',label:'Accepted file'};
 layout.root.tabs.push({id:'config-tab',kind:'history',category:'config',path:'settings.yml'},
  {id:'file-tab',kind:'history',category:'file',path:'historical.md',rendererId:'text'},
  {id:'log-tab',kind:'history',category:'log',path:'output.log'},
  {id:'compare-tab',kind:'compare',base:side,current:side});
 layout.root.activeTabId='file-tab';
 const selected={nodeId:'run:captured',versionId:'version-one',sectionsOpen:{config:true,files:false,logs:true}};
 vi.mocked(requestJson).mockImplementation(async(path)=>path.endsWith('/cell_display')?{[cellId]:encodeCellDisplay(layout,selected)}:{files:[{path:'a.md',deleted:false}]});
 const history=await import('@/lib/api/history');
 const artifacts=(path:string)=>[{path,blob_hash:'not-a-display-root',size:1}];
 const fetchHistory=vi.spyOn(history,'fetchCellHistory').mockResolvedValue({nodes:[{node_id:'run:captured',kind:'run',run:null,versions:[{cell_id:'version-one',index:0,created_at:null,name:null,file_count:1}],config:artifacts('settings.yml'),logs:artifacts('output.log')}],total_versions:1,current:{node_id:'run:captured',version_cell_id:'version-one'}});
 const filesTree=vi.spyOn(history,'fetchCellFilesTree').mockResolvedValue({cell_id:'version-one',files:[fileFixture('historical.md','historical body')]});
 try{
  await useCollimateStore.getState().hydrateCellDisplays();
  const restored=useCollimateStore.getState();
  expect(restored.cellHistory[cellId]).toEqual(selected);
  expect(allTabs(restored.cellLayouts[cellId]).map(t=>t.id)).toEqual(expect.arrayContaining(['config-tab','file-tab','log-tab','compare-tab']));
  expect(restored.cellLayouts[cellId].root).toMatchObject({activeTabId:'file-tab'});
  expect(filesTree).toHaveBeenCalledWith(channelId,'version-one');
 }finally{fetchHistory.mockRestore();filesTree.mockRestore();}
});
