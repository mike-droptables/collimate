"""P4F03 channel transport: bounded changes with complete snapshot recovery."""
import asyncio
import json
import uuid
import time
import threading

import pytest

def _channel_id(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'https://collimate.test/channels/' + label))



async def _next_state(queue, collected=None):
    while True:
        event = await asyncio.wait_for(queue.get(), 5)
        if collected is not None:
            collected.append(event)
        if event.get('type', '').startswith('state_'):
            return event


@pytest.mark.parametrize("count", [20, 200, 2000])
async def test_actual_emit_wire_contains_only_changed_paths__P4F03(fresh_cwd, monkeypatch, count):
    import events
    import state
    import versions
    from database import get_db_connection
    from models import AppState, Cell, Channel, Stack, VirtualFile
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED

    phases = {'build_ms':[], 'diff_ms':[], 'encoding':[]}
    loop_thread = threading.get_ident()
    original_build, original_diff = state._state_wire_snapshot, state.state_patches
    original_encode = events.SerializedEvent.__init__
    def measured_build():
        started = time.perf_counter()
        result = original_build()
        phases['build_ms'].append(round((time.perf_counter()-started)*1000,3))
        return result
    def measured_diff(previous, current, path=None):
        if path is not None:
            return original_diff(previous,current,path)
        started = time.perf_counter()
        result = original_diff(previous,current)
        phases['diff_ms'].append(round((time.perf_counter()-started)*1000,3))
        return result
    def measured_encode(self, event):
        assert threading.get_ident() != loop_thread
        started = time.perf_counter()
        original_encode(self,event)
        phases['encoding'].append({'type':event['type'], 'ms':round((time.perf_counter()-started)*1000,3)})
    monkeypatch.setattr(state,'_state_wire_snapshot',measured_build)
    monkeypatch.setattr(state,'state_patches',measured_diff)
    monkeypatch.setattr(events.SerializedEvent,'__init__',measured_encode)
    files = [VirtualFile(name=f'f{i}.txt', content=str(i).zfill(8) + 'x' * ((112 if i % 20 == 0 else 48) * 1024 - 8)) for i in range(count)]
    cell = Cell(files=files)
    channel = Channel(name='delta-wire', main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    monkeypatch.setattr(state, 'state', AppState(channels=[channel], active_channel_id=channel.id))
    run = str(uuid.uuid4())
    conn = get_db_connection(str(channel.id))
    conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (run,))
    versions.synthesize_genesis_row(conn, str(cell.id), run, 0,
        files)
    history_depth = 500 if count == 20 else 0
    tip = str(cell.id)
    parent = versions.tip_file_map(conn, tip)
    for _ in range(history_depth):
        tip, _ = versions.insert_version(conn, tip_id=tip, parent_files=parent, run_id=run, files=parent,
            file_entries=versions.version_file_entries(conn,tip), version_name='history', source_type='generator', generator_name='scale', owner_id=str(cell.id))
    from persistence import initialize_files
    initialize_files(conn,str(cell.id),files)
    conn.commit()
    conn.close()
    events.capture_loop()
    queue = events.subscribe(str(channel.id))
    try:
        state.broadcast_state_update()
        initial = await _next_state(queue)
        assert initial["type"] == "state_snapshot"
        initial_bytes = len(initial.sse_data.encode())
        revision = initial["revision"]
        initial_snapshot = initial['state']
        initial_names = [file['name'] for file in initial_snapshot['channels'][0]['main_stacks'][0]['cells'][0]['files']]
        del initial
        manager = ProcessManager(channel=str(channel.id), run_id=run, cell_id=str(cell.id),
            app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint='unused.py',
            generator_name='scale', runner_id='scale', runner_name='Scale', permissions=UNRESTRICTED)
        measurements = []
        for index in range(6):
            started = time.perf_counter()
            _, refused = manager._emit_partial(artifacts=[
                {'type':'memory', 'cell_path':f'changed{index % 3}.txt', 'content':str(index) + 'a' * 1023}],operation_id=str(uuid.uuid4()),writer_id=run,edit_seq=index+1,base_revisions={f'changed{index % 3}.txt':index//3})
            assert not refused
            event = await _next_state(queue)
            assert event['type'] == 'state_delta'
            assert event['base_revision'] == revision
            revision = event['revision']
            encoded = event.sse_data.encode()
            assert len(encoded) < 8192, f'one 1KiB edit retransmitted {len(encoded)} SSE bytes'
            assert len(event['patches']) < 20
            assert 'x' * 100 not in event.sse_data
            measurements.append({'sse_bytes':len(encoded), 'patches':len(event['patches']),
                'emit_to_queue_ms':round((time.perf_counter()-started)*1000, 3)})
        if count == 20:
            second_run = str(uuid.uuid4())
            conn = get_db_connection(str(channel.id))
            conn.execute("INSERT INTO runs(id,status) VALUES (?,'running')", (second_run,))
            conn.commit()
            conn.close()
            second = ProcessManager(channel=str(channel.id), run_id=second_run, cell_id=str(cell.id),
                app_dir=str(fresh_cwd), workspace_dir=str(fresh_cwd), entrypoint='unused.py',
                generator_name='second', runner_id='second', runner_name='Second', permissions=UNRESTRICTED)
            received = []
            for writer in (second, manager):
                _, refused = writer._emit_partial(artifacts=[
                    {'type':'memory','cell_path':'_runs/dead-cccccccc/x','content':'forbidden-body'},
                    {'type':'memory','cell_path':f'accepted-{writer.run_id}.txt','content':writer.run_id}],operation_id=str(uuid.uuid4()),writer_id=writer.run_id,edit_seq=7,base_revisions={f'accepted-{writer.run_id}.txt':0})
                assert refused == [{'path':'_runs/dead-cccccccc/x','reason':'locked','owner':'dead-cccccccc'}]
                published = await _next_state(queue, received)
                assert published['type'] == 'state_delta' and published['base_revision'] == revision
                revision = published['revision']
                assert writer.run_id in published.sse_data
                assert 'forbidden-body' not in published.sse_data and 'dead-cccccccc' not in published.sse_data
            assert {event['run_id'] for event in received if event['type'] == 'run_events'} == {run,second_run}
        assert [file['name'] for file in initial_snapshot['channels'][0]['main_stacks'][0]['cells'][0]['files']] == initial_names
        assert [entry['type'] for entry in phases['encoding']].count('state_snapshot') == 1
        print('P4F03_WIRE ' + json.dumps({'files':count, 'history_depth':history_depth, 'content_bytes':sum(len(f.content.encode()) for f in files),
            'initial_sse_bytes':initial_bytes, 'edits':measurements, 'phases':phases}))
    finally:
        events.unsubscribe(str(channel.id), queue)


async def test_join_during_build_and_noop_join_receive_coherent_baselines__P4F03(monkeypatch):
    import events
    events.capture_loop()
    first = events.subscribe(_channel_id('race'))
    entered, release = threading.Event(), threading.Event()
    builds = []
    def build(channels):
        builds.append(1)
        if len(builds) == 1:
            entered.set()
            assert release.wait(5)
        return {name:{'channel_scope':_channel_id('scope'), 'channels':[], 'active_channel_id':None} for name in channels}
    second = third = None
    try:
        events.broadcast_coalesced(build, delay=0.001)
        assert await asyncio.to_thread(entered.wait, 5)
        second = events.subscribe(_channel_id('race'))
        events.broadcast_coalesced(build, delay=0.001)
        release.set()
        a, b = await asyncio.gather(_next_state(first), _next_state(second))
        assert a['type'] == b['type'] == 'state_snapshot'
        assert a['revision'] == b['revision']
        assert first.empty()  # the unchanged follow-up must not emit an empty delta
        third = events.subscribe(_channel_id('race'))
        events.broadcast_coalesced(build, delay=0.001)
        c = await _next_state(third)
        assert c['revision'] == a['revision']
        assert first.empty() and second.empty()
        assert len(builds) == 3
    finally:
        release.set()
        for queue in (first, second, third):
            if queue is not None:
                events.unsubscribe(_channel_id('race'), queue)


async def test_scope_change_reset_reconnect_and_sticky_overflow__P4F03():
    import events
    events.capture_loop()
    queue = events.subscribe(_channel_id('scope'))
    current = {'channel_scope':'a','channels':[], 'active_channel_id':'a'}
    def build(names):
        return {name:dict(current) for name in names}
    try:
        events.broadcast_coalesced(build, delay=0.001)
        before = await _next_state(queue)
        current.update(channel_scope='b', active_channel_id='b')
        events.broadcast_coalesced(build, delay=0.001)
        after = await _next_state(queue)
        assert before['type'] == after['type'] == 'state_snapshot'
        assert after['revision'] != before['revision']
        for index in range(600):
            events.broadcast(_channel_id('scope'), {'type':'run_events','index':index})
        assert queue.qsize() == 1
        assert queue.get_nowait() is events.OVERFLOW_SENTINEL
        events.broadcast(_channel_id('scope'), {'type':'dag_commit'})
        events.broadcast_all({'type':'shutdown'})
        assert queue.empty()  # even after sentinel consumption nothing may overwrite it
        events.unsubscribe(_channel_id('scope'), queue)
        queue = events.subscribe(_channel_id('scope'))
        events.broadcast_coalesced(build, delay=0.001)
        reconnect = await _next_state(queue)
        assert reconnect['type'] == 'state_snapshot'
        assert reconnect['state'] == current
        assert reconnect['revision'] != after['revision']
    finally:
        events.unsubscribe(_channel_id('scope'), queue)


async def test_retired_loop_worker_cannot_publish_into_new_epoch__P4F03(monkeypatch):
    import events
    events.capture_loop()
    old = events.subscribe(_channel_id('epoch'))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def build(names):
        entered.set()
        assert release.wait(5)
        finished.set()
        return {name:{'channel_scope':'old','channels':[]} for name in names}
    try:
        events.broadcast_coalesced(build, delay=0.001)
        assert await asyncio.to_thread(entered.wait, 5)
        old_epoch = events._state_epoch
        monkeypatch.setattr(events, '_main_loop', None)
        events.capture_loop()
        assert events._state_epoch != old_epoch
        new = events.subscribe(_channel_id('epoch'))
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        # Drain executor completion/call_soon callback without assuming wall-clock speed.
        await asyncio.sleep(0.02)
        assert new.empty() and old.empty() and not events._state_heads
        events.broadcast_coalesced(lambda names:{name:{'channel_scope':'new','channels':[]} for name in names},delay=0.001)
        assert (await _next_state(new))['state']['channel_scope'] == 'new'
        events.unsubscribe(_channel_id('epoch'),new)
    finally:
        release.set()
        events.unsubscribe(_channel_id('epoch'),old)


def test_compact_file_sort_preserves_literal_order_without_retransmitting_bodies__P4F03():
    from state import state_patches
    names = ['𐀀.txt','\ue000.txt','__proto__','constructor','目录/a~b.txt','2','10']
    files = [{'name':name, 'content':'x'*50_000} for name in names]
    before = {'files':files}
    after = {'files':sorted(files,key=lambda file:file['name'])}
    assert state_patches(before,after) == [{'op':'sort','path':['files']}]
    assert [file['name'] for file in files] == names
    added = {'files':[{'name':'0','content':'new'},*after['files']]}
    assert state_patches(after,added) == [{'op':'insert','path':['files',{'key':'name','value':'0'}], 'index':0, 'value':added['files'][0]}]
    assert state_patches(added,after) == [{'op':'remove','path':['files',{'key':'name','value':'0'}]}]
    reordered = {'files':[after['files'][-1],*after['files'][:-1]]}
    assert state_patches(after,reordered) == [{'op':'move','path':['files',{'key':'name','value':'𐀀.txt'}], 'index':0}]
    # Stack/cell order is intentional, even if it happens to become lexical.
    assert state_patches({'cells':[{'id':'b'},{'id':'a'}]}, {'cells':[{'id':'a'},{'id':'b'}]}) == [
        {'op':'move','path':['cells',{'key':'id','value':'a'}], 'index':0}]


def test_actual_diff_matches_canonical_sdk_delta_fixture__P4F03():
    from collimate.shared import shared_path
    from copy import deepcopy
    from state import state_patches
    samples = json.loads(shared_path('contracts','channel-events.json').read_text())
    before = next(event['state'] for event in samples if event['type'] == 'state_snapshot')
    after = deepcopy(before)
    cell = after['channels'][0]['main_stacks'][0]['cells'][0]
    delta = next(event for event in samples if event['type'] == 'state_delta')
    for patch in delta['patches']:
        selector = patch['path'][-1]
        if isinstance(selector, dict) and selector.get('key') == 'name':
            index = next(i for i, file in enumerate(cell['files']) if file['name'] == selector['value'])
            cell['files'][index] = deepcopy(patch['value'])
    cell['updated_at'] = '2026-01-01T00:00:01Z'
    patches = next(event['patches'] for event in samples if event['type'] == 'state_delta')
    # Field traversal order follows the snapshot, not an external JSON key order.
    assert sorted(state_patches(before,after),key=lambda p:json.dumps(p['path'])) == sorted(patches,key=lambda p:json.dumps(p['path']))


async def test_stream_route_bootstraps_overflows_and_releases_subscription__P4F03__F0349(fresh_cwd, monkeypatch):
    import events
    import state
    from models import AppState, Channel
    from routes.channels import stream_channel_state
    channel = Channel(name='actual-stream')
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    monkeypatch.setattr(state,'state',AppState(channels=[channel],active_channel_id=channel.id))
    from database import get_db_connection
    get_db_connection(str(channel.id)).close()
    response = await stream_channel_state(str(channel.id))
    stream = response.body_iterator
    try:
        assert json.loads((await anext(stream)).removeprefix('data: '))['type'] == 'connected'
        baseline = json.loads((await asyncio.wait_for(anext(stream),5)).removeprefix('data: '))
        assert baseline['type'] == 'state_snapshot'
        assert baseline['state']['channel_scope'] == str(channel.id)
        for _ in range(300):
            events.broadcast(str(channel.id),{'type':'dag_commit'})
        assert json.loads((await anext(stream)).removeprefix('data: '))['type'] == 'overflow'
        assert await anext(stream) == 'retry: 100\n\n'
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert str(channel.id) not in events._subscribers
    finally:
        await stream.aclose()


async def test_leave_and_rejoin_cannot_receive_retired_workers_snapshot__P4F03():
    import events
    events.capture_loop()
    old = events.subscribe(_channel_id('rejoin'))
    entered, release = threading.Event(), threading.Event()
    current, builds = {'channel_scope':'same','sequence':1}, []
    def build(names):
        captured = dict(current)
        builds.append(captured['sequence'])
        if len(builds) == 1:
            entered.set()
            assert release.wait(5)
        return {name:captured for name in names}
    new = None
    try:
        events.broadcast_coalesced(build,delay=0.001)
        assert await asyncio.to_thread(entered.wait,5)
        events.unsubscribe(_channel_id('rejoin'),old)
        new = events.subscribe(_channel_id('rejoin'))
        current['sequence'] = 2
        events.broadcast_coalesced(build,delay=0.001)
        release.set()
        event = await _next_state(new)
        assert event['type'] == 'state_snapshot' and event['state']['sequence'] == 2
        assert old.empty() and builds == [1,2]
        assert events._state_heads[_channel_id('rejoin')] == (event['revision'],event['state'])
    finally:
        release.set()
        events.unsubscribe(_channel_id('rejoin'),old)
        if new is not None:
            events.unsubscribe(_channel_id('rejoin'),new)


async def test_retired_coalesce_timer_cannot_adopt_new_loop_subscribers__P4F03(monkeypatch):
    import events
    events.capture_loop()
    old = events.subscribe(_channel_id('timer'))
    builds = []
    def old_build(names):
        builds.append('retired')
        return {name:{'channel_scope':'old','channels':[]} for name in names}
    events.broadcast_coalesced(old_build,delay=0.01)
    monkeypatch.setattr(events,'_main_loop',None)
    events.capture_loop()
    new = events.subscribe(_channel_id('timer'))
    try:
        await asyncio.sleep(0.03)
        assert builds == [] and new.empty() and old.empty()
        events.broadcast_coalesced(lambda names:{name:{'channel_scope':'new','channels':[]} for name in names},delay=0.001)
        assert (await _next_state(new))['state']['channel_scope'] == 'new'
    finally:
        events.unsubscribe(_channel_id('timer'),old)
        events.unsubscribe(_channel_id('timer'),new)
