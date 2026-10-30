"""D21 external file feeds identify actual pulled edits, not local writes."""

from collimate import _mirror as mirror_runtime
import asyncio

from collimate import native
from tests.test_final_output_ack import Endpoint


def test_external_changes_only_notify_pulled_edits_and_deletions_D21(native_bridge):
    async def run():
        local = Endpoint({'a': 'old', 'keep': 'same'})
        remote = Endpoint(dict(local.files))
        changed = []
        mirror = mirror_runtime.Mirror(local, remote, own_folder=native_bridge.folder,
                                on_changed=changed.append)
        await mirror._tick()
        local.files['a'] = 'model edit'
        await mirror.flush()
        assert not changed
        remote.files['a'] = 'user edit'
        await mirror.flush()
        assert changed == ['a'] and local.files['a'] == 'user edit'
        await mirror.flush()
        assert changed == ['a']
        del remote.files['a']
        await mirror.flush()
        assert changed == ['a', 'a'] and 'a' not in local.files
    asyncio.run(run())


def test_live_files_passes_external_change_callback_D21(native_bridge):
    def callback(path):
        pass
    assert native.live_files(on_changed=callback)._on_changed is callback
