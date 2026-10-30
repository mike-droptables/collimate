"""D10 partial refusals preserve local edits until their bytes change."""

from collimate import _mirror as mirror_runtime
import asyncio

import pytest

from collimate import native
from collimate._host.bridge import PartialRefusal
from collimate._host.ipc import refused_message
from tests.test_final_output_ack import Endpoint


def test_refusal_message_contains_verifier_error__D10():
    text = refused_message([{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'needs good'}])
    assert 'a.checked' in text and 'needs good' in text
    assert 'locked' not in text


def test_mirror_holds_refused_bytes_until_next_edit__D10(native_bridge):
    async def run():
        local, remote = Endpoint({'bad': 'old', 'ok': 'old'}), Endpoint({'bad': 'old', 'ok': 'old'})
        mirror = mirror_runtime.Mirror(local, remote, own_folder='_runs/test')
        await mirror._tick()
        local.files.update(bad='rejected', ok='accepted')
        remote.refuse.add('bad')
        with pytest.raises(PartialRefusal):
            await mirror.flush()
        remote.refuse.clear()
        remote.files.update(bad='remote changed', ok='user edit')
        await mirror.flush()
        assert local.files == {'bad': 'rejected', 'ok': 'user edit'}
        assert remote.files == {'bad': 'remote changed', 'ok': 'user edit'}
        assert len(remote.commits) == 1
        local.files['bad'] = 'corrected'
        await mirror.flush()
        assert remote.files['bad'] == 'corrected'
        assert len(remote.commits) == 2
    asyncio.run(run())


def test_callback_once_and_edit_during_ack_releases_attempted_hash__D10(native_bridge):
    async def run():
        local, remote = Endpoint({'a': 'old'}), Endpoint({'a': 'old'})
        calls = []
        mirror = mirror_runtime.Mirror(local, remote, own_folder='_runs/test', on_refused=lambda *args:calls.append(args))
        await mirror._tick()
        local.files['a'] = 'rejected'
        remote.refuse.add('a')
        remote.started, remote.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(mirror.flush())
        await remote.started.wait()
        local.files['a'] = 'corrected during ack'
        remote.release.set()
        with pytest.raises(PartialRefusal):
            await task
        assert len(calls) == 1 and calls[0][0] == 'a'
        remote.refuse.clear()
        await mirror.flush()
        assert remote.files['a'] == 'corrected during ack'
        assert len(calls) == 1
    asyncio.run(run())


def test_held_deletion_is_not_restored_or_retried__D10(native_bridge):
    async def run():
        local, remote = Endpoint({'a': 'old', 'keep': 'same'}), Endpoint({'a': 'old', 'keep': 'same'})
        calls = []
        mirror = mirror_runtime.Mirror(local, remote, own_folder='_runs/test', on_refused=lambda *args:calls.append(args))
        await mirror._tick()
        del local.files['a']
        remote.refuse.add('a')
        with pytest.raises(PartialRefusal):
            await mirror.flush()
        await mirror.flush()
        assert 'a' not in local.files and remote.files['a'] == 'old'
        assert len(remote.commits) == len(calls) == 1
        local.files['a'] = 'new'
        remote.refuse.clear()
        await mirror.flush()
        assert remote.files['a'] == 'new'
    asyncio.run(run())


def test_cancelled_partial_ack_still_holds_refused_path__D10(native_bridge):
    async def run():
        class CancelledEndpoint(Endpoint):
            async def commit(self):
                cancel = asyncio.CancelledError()
                cancel._flush_landed = True
                cancel._flush_refusal = PartialRefusal('bad document', paths=['a'])
                raise cancel
        local, remote = Endpoint({'a': 'old'}), CancelledEndpoint({'a': 'old'})
        mirror = mirror_runtime.Mirror(local, remote, own_folder='_runs/test')
        await mirror._tick()
        local.files['a'] = 'rejected'
        with pytest.raises(asyncio.CancelledError):
            await mirror.flush()
        await mirror.flush()
        assert local.files['a'] == 'rejected' and remote.files['a'] == 'old'
    asyncio.run(run())


def test_public_live_files_passes_sync_callback__D10(native_bridge):
    def callback(path, error):
        pass
    assert native.live_files(on_refused=callback)._on_refused is callback


def test_actual_bridge_ack_exposes_each_verifier_error__D10(tmp_path):
    from collimate._host.bridge import HostBridge
    from tests.test_run_folders import _wire, make_test_bridge
    bridge = make_test_bridge(tmp_path)
    _wire(bridge, {'cell_id': 'version', 'refused': [
        {'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'needs good'},
        {'path': 'b.checked', 'reason': 'handler_invalid', 'error': 'missing sibling'},
    ]})
    try:
        with pytest.raises(PartialRefusal) as result:
            HostBridge.update_cell(bridge, writes=[])
        assert result.value.errors == {'a.checked': 'needs good', 'b.checked': 'missing sibling'}
        assert result.value.paths == {'a.checked', 'b.checked'}
        assert 'needs good' in str(result.value) and 'missing sibling' in str(result.value)
    finally:
        bridge.close()


def test_native_cancelled_update_keeps_partial_ack_details__D10(native_bridge, monkeypatch):
    import threading
    started, release = threading.Event(), threading.Event()
    refused = PartialRefusal('needs good', paths=['a.checked'], refused=[
        {'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'needs good'}])
    def commit(**kwargs):
        started.set()
        assert release.wait(2)
        raise refused
    monkeypatch.setattr(native_bridge, 'update_cell', commit)
    async def run():
        native.update_file('a.checked', 'rejected')
        task = asyncio.create_task(native.commit())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as result:
            await task
        assert result.value._flush_landed is True
        assert result.value._flush_refusal is refused
        assert not native_bridge.tracked_writes
    asyncio.run(run())


def test_launch_refusal_is_skipped_but_transport_failure_surfaces__D10(monkeypatch):
    from tests.test_generator_internet_grants import load
    module = load('launch/launch/run.py')
    logs = []
    monkeypatch.setattr(module.gen, 'log', logs.append)
    async def refused(**kwargs):
        raise PartialRefusal('a.checked: needs good', paths=['a.checked'])
    monkeypatch.setattr(module.gen, 'commit', refused)
    asyncio.run(module._flush())
    assert any('Skipped' in message and 'needs good' in message for message in logs)
    async def failed(**kwargs):
        raise RuntimeError('transport down')
    monkeypatch.setattr(module.gen, 'commit', failed)
    with pytest.raises(RuntimeError, match='transport down'):
        asyncio.run(module._flush())
