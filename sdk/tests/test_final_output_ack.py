"""D07 final output acknowledgment of the live mirror, on synthetic endpoints without a provider (the Agent one-shot's final push is tests/test_agent_annotation.py)."""

from collimate import _mirror as mirror_runtime
import asyncio
import hashlib

import pytest

from collimate._host.bridge import PartialRefusal


class Endpoint(mirror_runtime.FileEndpoint):
    label = "synthetic"

    def __init__(self, files):
        self.files = dict(files)
        self.staged = {}
        self.refuse = set()
        self.commits = []
        self.started = self.release = None
        self.read_error = None

    async def hashes(self):
        return {p: hashlib.sha256(v.encode("utf-8") if isinstance(v, str) else v).hexdigest() for p, v in self.files.items()}

    async def read(self, path):
        if self.read_error == path:
            raise OSError(f"cannot read {path}")
        return self.files[path]

    async def write(self, path, content):
        self.staged[path] = content

    async def delete(self, path):
        self.staged[path] = None

    async def commit(self):
        writes, self.staged = self.staged, {}
        self.commits.append(dict(writes))
        if self.started:
            self.started.set()
            await self.release.wait()
        refused = self.refuse & writes.keys()
        for path, value in writes.items():
            if path not in refused:
                if value is None:
                    self.files.pop(path, None)
                else:
                    self.files[path] = value
        if refused:
            exc = PartialRefusal("refused " + ", ".join(sorted(refused)))
            exc.paths = frozenset(refused)
            raise exc


def test_last_write_flush_waits_for_ack_and_serializes_ticks__D07(native_bridge):
    async def run():
        local, remote = Endpoint({"a": "old"}), Endpoint({"a": "old"})
        mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
        await mirror._tick()
        local.files["a"] = "last write"
        remote.started, remote.release = asyncio.Event(), asyncio.Event()
        flush = asyncio.create_task(mirror.flush())
        await remote.started.wait()
        background = asyncio.create_task(mirror._reconcile())
        await asyncio.sleep(0)
        assert not flush.done() and not background.done()
        assert len(remote.commits) == 1
        remote.release.set()
        await asyncio.gather(flush, background)
        assert remote.files["a"] == "last write"
        assert len(remote.commits) == 1
    asyncio.run(run())


def test_partial_refusal_holds_until_edit_and_preserves_user_edit__D07_D10(native_bridge):
    async def run():
        local = Endpoint({"ok": "old", "bad": "old"})
        remote = Endpoint(local.files)
        mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
        await mirror._tick()
        local.files.update(ok="accepted", bad="desired")
        remote.refuse.add("bad")
        with pytest.raises(PartialRefusal):
            await mirror._tick()
        remote.files["ok"] = "user edit"
        remote.refuse.clear()
        await mirror._tick()
        assert local.files == {"ok": "user edit", "bad": "desired"}
        assert remote.files == {"ok": "user edit", "bad": "old"}
        local.files['bad'] = 'corrected'
        await mirror._tick()
        assert remote.files == local.files == {"ok": "user edit", "bad": "corrected"}
        assert remote.commits == [{"ok": "accepted", "bad": "desired"}, {"bad": "corrected"}]
    asyncio.run(run())


def test_background_failure_is_observed_beforefinal_flush_can_succeed__D07(native_bridge):
    async def run():
        local, remote = Endpoint({"a": "old"}), Endpoint({"a": "old"})
        mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
        await mirror._tick()
        local.files["a"] = "new"
        remote.refuse.add("a")
        await mirror._reconcile()
        remote.refuse.clear()
        with pytest.raises(PartialRefusal, match="a"):
            await mirror.flush()
        await mirror.flush()
        assert remote.files["a"] == "old"
        assert local.files['a'] == 'new'
        local.files['a'] = 'corrected'
        await mirror.flush()
        assert remote.files['a'] == 'corrected'
    asyncio.run(run())


def test_read_failure_commits_good_sibling_but_cannot_ack_final_output__D07(native_bridge):
    async def run():
        local, remote = Endpoint({"ok": "new", "bad": "new"}), Endpoint({})
        mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
        local.read_error = "bad"
        with pytest.raises(OSError, match="bad"):
            await mirror.flush()
        assert remote.files == {"ok": "new"}
        local.read_error = None
        await mirror.flush()
        assert remote.files == local.files
    asyncio.run(run())


def test_transient_empty_snapshot_is_not_a_final_ack__D07(native_bridge):
    async def run():
        local, remote = Endpoint({"a": "old"}), Endpoint({"a": "old"})
        mirror = mirror_runtime.Mirror(local, remote, own_folder="_runs/test")
        await mirror._tick()
        local.files.clear()
        with pytest.raises(RuntimeError, match="empty"):
            await mirror.flush()
        assert remote.files == {"a": "old"}
    asyncio.run(run())


def test_guard_disabled_flush_is_noop__D07(native_bridge):
    mirror = mirror_runtime.Mirror(None, None, own_folder="_runs/test", guard=lambda: False)
    asyncio.run(mirror.flush())


def test_host_partial_refusal_keeps_machine_readable_paths__D07(native_bridge, monkeypatch):
    from collimate._host.bridge import HostBridge
    native_bridge.fs_write_any("good.md", "good")
    native_bridge.fs_write_any("candidate/result.md", "candidate")
    monkeypatch.setattr(native_bridge, "emit_and_wait", lambda payload, **kw: {
        "ok": True, "cell_id": "cell", "operation_id":payload["operation_id"],
        "outcome":"accepted", "files":[{'path':'good.md', 'file_revision':1,
            'writer_id':payload['writer_id'], 'edit_seq':payload['edit_seq'], 'deleted':False}], "refused": [
            {"path": "candidate/result.md", "reason": "locked", "owner": "other"},
        ],
    })
    with pytest.raises(PartialRefusal) as caught:
        HostBridge._commit_update(native_bridge, writes=["good.md", "candidate/result.md"])
    assert caught.value.paths == {"candidate/result.md"}


