"""P1 public native contracts with direct behavior regressions."""

from collimate import _mirror as mirror_runtime
import asyncio
import pytest
from collimate import native


@pytest.mark.parametrize('method,args', [('start_terminal',{'container_id':'c','command':['sh']}),('terminal_status',{'terminal_id':'s'}),('surface_register',{'path':'_runs/dev-test/chat/c.chat','label':'Chat','transport_only':True,'broadcast':True})])
def test_host_only_surface_ops_explain_dev_mode__F1215_F1218(method,args):
    from collimate._host.bridge import HostBridge
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge.folder = "_runs/dev-test"
    bridge._sock_wfile = None
    bridge.emit_and_wait = lambda *a,**kw:{}
    with pytest.raises(RuntimeError,match='needs a host'):
        getattr(bridge,method)(**args)


def test_bridge_drains_deletions_without_a_caller_snapshot__F1213(native_bridge):
    native.update_file('old.txt','old')
    native.delete_file('old.txt')
    seen=[]
    native_bridge._commit_update=lambda **kw:seen.append(kw) or 'v'
    native_bridge.update_cell(writes=[])
    assert seen[0]['deleted_files']==['old.txt']
    assert not native_bridge.tracked_deletions


def test_bridge_stages_log_category_without_native_bookkeeping__F1213(native_bridge):
    native_bridge.stage_file('trace.txt','trace',category='log')
    assert native_bridge.log_paths=={'trace.txt'}
    assert native_bridge.tracked_deletions=={'trace.txt'}
    native_bridge.stage_file('trace.txt','public',category='file')
    assert not native_bridge.log_paths and not native_bridge.tracked_deletions


@pytest.mark.parametrize("failed_flush", [False, True])
def test_repeated_log_staging_preserves_cell_removal__F1213_P1F01(native_bridge, failed_flush):
    native_bridge.stage_file("trace.txt", "public", category="file")
    native_bridge.update_cell(writes=native_bridge.take_writes())
    native_bridge.stage_file("trace.txt", "log one", category="log")
    seen = []
    def commit(**kwargs):
        seen.append(kwargs)
        if failed_flush and len(seen) == 1:
            raise RuntimeError("commit unavailable")
        return "version"
    native_bridge._commit_update = commit
    if failed_flush:
        writes = native_bridge.take_writes()
        with pytest.raises(RuntimeError, match="commit unavailable"):
            native_bridge.update_cell(writes=writes)
        native_bridge.restage_writes(writes)
    native_bridge.stage_file("trace.txt", "log two", category="log")
    native_bridge.update_cell(writes=native_bridge.take_writes())
    assert seen[-1]["deleted_files"] == ["trace.txt"]
    assert native_bridge.log_paths == {"trace.txt"}
    assert not native_bridge.tracked_deletions


def test_ui_helpers_roundtrip_own_full_path__F1169(native_bridge):
    path=native.update_ui_file('panel.json','one')
    assert native.update_ui_file(path,'two')==path
    native.delete_ui_file(path)
    assert not native.has_file(path)
    with pytest.raises(ValueError):native.update_ui_file('_runs/other/panel.json','no')


def test_source_filter_reaches_waiter_and_iterator__F2030(native_bridge):
    seen=[]
    def poll(**kw):
        seen.append(kw)
        return {'action_id':'chosen','source':kw['source']}
    native_bridge.poll_renderer_action_event=poll
    async def run():
        assert await native.wait_for_action(source='panel',timeout=0)=='chosen'
        iterator=native.actions(source='panel',poll=0)
        assert await anext(iterator)=='chosen'
        await iterator.aclose()
    asyncio.run(run())
    assert all(row['source']=='panel' for row in seen)


def test_snapshot_and_mirror_share_skip_directory_contract__F1223(native_bridge):
    from collimate.fs import WORKSPACE_SKIP_DIRS
    for folder in WORKSPACE_SKIP_DIRS:
        native.update_file(f'{folder}/cache.txt','cache')
    native.update_file('visible.txt','yes')
    assert set(native.snapshot())=={'visible.txt'}
    mirror=native.live_files()
    assert mirror._skip_dirs==WORKSPACE_SKIP_DIRS
    assert len(native.list_files())==len(WORKSPACE_SKIP_DIRS)+1


def test_mirror_logs_three_failures_and_recovery__F1133(native_bridge):
    class Endpoint:
        label='fixture'
        async def hashes(self):raise RuntimeError('hash unavailable')
    mirror=mirror_runtime.Mirror(Endpoint(),Endpoint(),own_folder=native_bridge.folder)
    async def run():
        await mirror.__aenter__()  # one failed seed
        mirror._task.cancel()
        try:await mirror._task
        except asyncio.CancelledError:pass
        await mirror._reconcile() # second failure
        assert not any('live' in x for x in native_bridge._rec_logs)
        await mirror._reconcile() # third
        await mirror._reconcile() # fourth stays quiet
        async def good():pass
        mirror._tick=good
        await mirror.__aexit__(None,None,None)
    asyncio.run(run())
    lines=[x for x in native_bridge._rec_logs if 'live' in x]
    assert len(lines)==2 and 'hash unavailable' in lines[0] and 'recovered' in lines[1]


def test_live_mirror_skip_globs_are_declared_without_generator_knowledge__F1223(native_bridge):
    mirror=native.live_files(skip_globs=('_private/**',))
    assert mirror._rule('_private/log.json')=='skip'
    assert mirror._rule('_agent/trace.md')=='sync'


def test_result_reads_explicit_paths__F1228():
    from collimate._results import GeneratorResult
    result=GeneratorResult(ok=True,files={'reply.md':'hello','record.json':'{"x":1}'})
    assert result.output_text('reply.md')=='hello'
    assert result.structured('record.json')=={'x':1}


def test_chat_paths_and_surface_routes_are_explicit__F1233(native_bridge):
    import inspect
    parameters=inspect.signature(native.serve_chat).parameters
    for name in ('pointer_file','threads_dir'):
        assert parameters[name].default is inspect.Parameter.empty
    assert parameters['files'].default is True and parameters['broadcast'].default is False
