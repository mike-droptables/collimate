"""The live-files mirror reports the workspace paths its final reconcile
skipped, which the host's git harvest keeps out of a repository's
deletions (design §4.6), in bounded frames and only to a host whose
transport carries the op."""
import asyncio
from types import SimpleNamespace

from collimate import _mirror as mirror_runtime
from collimate._host.bridge import MIRROR_SKIPPED_PER_FRAME, HostBridge
from tests.test_final_output_ack import Endpoint


def test_the_final_reconcile_reports_the_paths_it_skipped(native_bridge, monkeypatch):
    reported = []
    monkeypatch.setattr(native_bridge, "report_mirror_skipped", lambda paths: reported.append(sorted(paths)))

    async def run():
        local = Endpoint({"a.txt": "a", "build.log": "log", "_agent/notes.md": "n", ".collrun/x": "band"})
        remote = Endpoint({})
        async with mirror_runtime.Mirror(local, remote, own_folder="_runs/test", exclude=(".log",),
                                         skip_globs=("_agent/**",), interval=60):
            pass
        return remote

    remote = asyncio.run(run())
    assert reported == [[".collrun/x", "_agent/notes.md", "build.log"]]
    assert set(remote.files) == {"a.txt"}


def test_the_report_goes_in_bounded_frames_and_only_where_the_transport_carries_it():
    calls = []
    bridge = SimpleNamespace(has_host=True, ipc_ops=None,
                             _call=lambda op, **values: calls.append((op, len(values["paths"]))))
    paths = [f"p{i:05d}" for i in range(MIRROR_SKIPPED_PER_FRAME * 2 + 3)]
    HostBridge.report_mirror_skipped(bridge, paths)
    assert calls == [("mirror_skipped", MIRROR_SKIPPED_PER_FRAME)] * 2 + [("mirror_skipped", 3)]
    calls.clear()
    HostBridge.report_mirror_skipped(SimpleNamespace(has_host=True, ipc_ops=frozenset({"update_cell"}),
                                                     _call=bridge._call), paths)
    HostBridge.report_mirror_skipped(SimpleNamespace(has_host=False, ipc_ops=None, _call=bridge._call), paths)
    assert calls == []
