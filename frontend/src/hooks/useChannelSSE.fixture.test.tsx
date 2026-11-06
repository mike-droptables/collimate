// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import samples from '../../../sdk/shared/contracts/channel-events.json';
import type { ChannelEvent } from '@/generated/channelEvents';

const handlers = vi.hoisted(() => ({
  attachStartedRun: vi.fn(), dispatchRunEvents: vi.fn(), dispatchRunPhase: vi.fn(), dispatchRunStatus: vi.fn(),
  recoverActiveRuns: vi.fn(() => Promise.resolve()),
}));
const pool = vi.hoisted(() => ({ broadcastToCell: vi.fn() }));
vi.mock('@/lib/runAttach', () => handlers);
vi.mock('@/contexts/GuestPoolContext', () => ({ useGuestPool: () => pool }));
vi.mock('@/lib/fileCommands', () => ({applyMutationResult:vi.fn(),recoverFileOperations:vi.fn(async()=>undefined)}));
vi.mock('@/lib/api', () => ({ getApiUrl: () => '/api', fetchState: vi.fn(async () => ({ channels: [], active_channel_id: null })) }));
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSSE } from './useChannelSSE';

class Source {
  static last: Source;
  onmessage?: (e: { data: string }) => void;
  close() {}
  readonly url: string;
  constructor(url: string) { this.url = url; Source.last = this; }
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('EventSource', Source);
  useCollimateStore.setState({ hasInitialized: true, applyStateEvent: vi.fn(() => true), queueReveal: vi.fn(), setShuttingDown: vi.fn(),
    refreshActiveRunsEverywhere: vi.fn(() => Promise.resolve()), overviewLineageChanged: vi.fn(), overviewRunChanged: vi.fn(), overviewResync: vi.fn(),
    annotationsChanged: vi.fn(), resyncAnnotations: vi.fn(),
    runs: {} });
});
afterEach(() => vi.unstubAllGlobals());

it('unregisters only the ended run’s proxy worker scopes P4F06', async () => {
  const paths = ['/proxy/11111111-1111-4111-8111-111111111111/ended/5100/', '/proxy/11111111-1111-4111-8111-111111111111/ended/5101/nested/',
    '/proxy/11111111-1111-4111-8111-111111111111/live/5100/', '/proxy/another/ended/5100/', '/', '/proxy/11111111-1111-4111-8111-111111111111/ended-other/5100/'];
  const registrations = paths.map(path => ({scope: new URL(path, location.origin).href, unregister: vi.fn(async () => true)}));
  vi.stubGlobal('navigator', {serviceWorker: {getRegistrations: vi.fn(async () => registrations)}});
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    await act(async () => Source.last.onmessage?.({data: JSON.stringify({type:'run_status', run_id:'ended', status:'completed', failure_reason:null})}));
    expect(registrations.map(registration => registration.unregister.mock.calls.length)).toEqual([1, 1, 0, 0, 0, 0]);
  } finally { hook.unmount(); }
});

it.each(samples)('SDK $type fixture reaches its frontend dispatcher (F0308, F1716)', async sample => {
  const changed = vi.fn();
  window.addEventListener('collimate:files-changed-broadcast', changed);
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    // The wire record, read as the hook reads it. The SDK pins the fixture
    // to channel_events.py, and ChannelEvent is generated from that file.
    const wire = JSON.stringify(sample);
    await act(async () => Source.last.onmessage?.({ data: wire }));
    const data: ChannelEvent = JSON.parse(wire);
    const store = useCollimateStore.getState();
    switch (data.type) {
      case 'connected': expect(pool.broadcastToCell).not.toHaveBeenCalled(); break;
      case 'overflow': expect(handlers.recoverActiveRuns).toHaveBeenCalledOnce(); break;
      case 'shutdown': expect(store.setShuttingDown).toHaveBeenCalledWith(true); break;
      case 'state_snapshot': case 'state_delta': expect(store.applyStateEvent).toHaveBeenCalledWith(data); break;
      case 'file_changes':
        expect(pool.broadcastToCell).toHaveBeenCalledWith(data.cell_id, { type:'FILES_CHANGED', cellId:data.cell_id, changedFiles:data.files.map(f=>f.path), files:data.files });
        expect(changed.mock.calls[0][0].detail).toEqual({cellId:data.cell_id,changedFiles:data.files.map(f=>f.path),files:data.files});
        break;
      case 'dag_commit':
        expect(pool.broadcastToCell).not.toHaveBeenCalled();
        expect(store.overviewRunChanged).toHaveBeenCalledWith(data.run_id, data.cell_id); break;
      case 'run_events': expect(handlers.dispatchRunEvents).toHaveBeenCalledWith(data.run_id, data.events); break;
      case 'run_status':
        expect(handlers.dispatchRunStatus).toHaveBeenCalledWith(data.run_id, data.status, data.failure_reason);
        expect(store.overviewRunChanged).toHaveBeenCalledWith(data.run_id, null); break;
      case 'run_phase': expect(handlers.dispatchRunPhase).toHaveBeenCalledWith(data.run_id, data.phase, data.message); break;
      case 'run_started': {
        const { type: _type, ...info } = data;
        expect(handlers.attachStartedRun).toHaveBeenCalledWith(info, '11111111-1111-4111-8111-111111111111');
        expect(store.overviewRunChanged).toHaveBeenCalledWith(data.run_id, data.cell_id); break;
      }
      case 'reveal_file': expect(store.queueReveal).toHaveBeenCalledWith(data.cell_id, { path: data.path, runId: data.run_id }); break;
      case 'surface_changed':
        expect(pool.broadcastToCell).toHaveBeenCalledWith(data.cell_id, {type:'SURFACE_CHANGED',cellId:data.cell_id,path:data.path}); break;
      case 'runs_active_changed': expect(store.refreshActiveRunsEverywhere).toHaveBeenCalledOnce(); break;
      case 'lineage_changed': expect(store.overviewLineageChanged).toHaveBeenCalledWith(data.cell_ids); break;
      case 'annotations_changed': expect(store.annotationsChanged).toHaveBeenCalledWith(data.cell_id, data.context_id, data.rev); break;
      default: { const exhaustive: never = data; throw new Error(String(exhaustive)); }
    }
  } finally {
    hook.unmount();
    window.removeEventListener('collimate:files-changed-broadcast', changed);
  }
});

it('runs_active_changed refetches every channel’s live runs, and every connect does too; nothing polls (C04)', async () => {
  vi.useFakeTimers();
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    const refresh = useCollimateStore.getState().refreshActiveRunsEverywhere;
    const source = Source.last as Source & { onopen?: () => void };
    await act(async () => source.onopen?.());
    expect(refresh).toHaveBeenCalledTimes(1);
    // Every connect also refetches the status panes' annotation contexts.
    expect(useCollimateStore.getState().resyncAnnotations).toHaveBeenCalledTimes(1);
    // Another channel's run started: the event names it, on this channel's stream.
    await act(async () => source.onmessage?.({ data: JSON.stringify({ type: 'runs_active_changed', channel: 'elsewhere' }) }));
    expect(refresh).toHaveBeenCalledTimes(2);
    await act(async () => { vi.advanceTimersByTime(10 * 60_000); });
    expect(refresh).toHaveBeenCalledTimes(2);
  } finally {
    hook.unmount();
    vi.useRealTimers();
  }
});

it('run_status carries how the run ended to the overview: its cell, read before the record ends (O04)', async () => {
  useCollimateStore.setState({ runs: { 'run-9': { runId: 'run-9', cellId: 'cell-9', channel: '11111111-1111-4111-8111-111111111111', runnerId: null,
    runnerName: null, generatorName: 'g', label: 'g-run-9', folder: '_runs/g-run-9' } } });
  handlers.dispatchRunStatus.mockImplementation(() => useCollimateStore.setState({ runs: {} }));
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    const event: ChannelEvent = { type: 'run_status', run_id: 'run-9', status: 'completed', failure_reason: null,
      ended_at: '2026-10-06T14:05:00+00:00', note: 'probe passed', outcome: 'pass' };
    await act(async () => Source.last.onmessage?.({ data: JSON.stringify(event) }));
    expect(useCollimateStore.getState().overviewRunChanged).toHaveBeenCalledWith('run-9', 'cell-9');
  } finally {
    hook.unmount();
    handlers.dispatchRunStatus.mockReset();
  }
});

it('every (re)connect rereads the overview, which may have missed a lineage_changed while the stream was down (O04)', async () => {
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    const source = Source.last as Source & { onopen?: () => void };
    await act(async () => source.onopen?.());
    expect(useCollimateStore.getState().overviewResync).toHaveBeenCalledTimes(1);
  } finally { hook.unmount(); }
});

it('a dag_commit refreshes the open journal that tells its cell (O06)', async () => {
  const hook = renderHook(() => useChannelSSE('11111111-1111-4111-8111-111111111111'));
  try {
    const event: ChannelEvent = { type: 'dag_commit', cell_id: 'a', files: ['out.txt'], run_id: null, generator_name: null };
    await act(async () => Source.last.onmessage?.({ data: JSON.stringify(event) }));
    expect(useCollimateStore.getState().overviewRunChanged).toHaveBeenCalledWith(null, 'a');
  } finally { hook.unmount(); }
});


it('uses the exact captured UUID for SSE and preserves the stream across a display rename', async () => {
  const hook = renderHook(({id}) => useChannelSSE(id), {initialProps:{id:'11111111-1111-4111-8111-111111111111'}});
  const original = Source.last;
  expect(original.url).toBe('/api/channels/11111111-1111-4111-8111-111111111111/state/stream');
  useCollimateStore.setState({activeChannel:{id:'11111111-1111-4111-8111-111111111111',name:'Renamed'} as never});
  await hook.rerender({id:'11111111-1111-4111-8111-111111111111'});
  expect(Source.last).toBe(original);
  await hook.rerender({id:'22222222-2222-4222-8222-222222222222'});
  expect(Source.last).not.toBe(original);
  expect(Source.last.url).toBe('/api/channels/22222222-2222-4222-8222-222222222222/state/stream');
  hook.unmount();
});
