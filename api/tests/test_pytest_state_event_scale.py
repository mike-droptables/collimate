import asyncio
import hashlib
import uuid
import threading


async def test_multichannel_burst_builds_once_and_scopes_metadata__F0012_F0967(fresh_cwd, monkeypatch):
    import events
    import state
    from models import AppState, Cell, Channel, Stack, VirtualFile
    channels = [Channel(name=f"channel-{i}", main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name="a.txt", content=f"private-{i}")])])]) for i in range(2)]
    current = AppState(channels=channels, active_channel_id=channels[0].id)
    monkeypatch.setattr(state, "state", current)
    from database import get_db_connection
    from persistence import initialize_files
    for channel in channels:
        connection = get_db_connection(str(channel.id))
        try:
            cell = channel.main_stacks[0].cells[0]
            initialize_files(connection, str(cell.id), cell.files)
            connection.commit()
        finally:
            connection.close()
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()
    queues = [events.subscribe(str(ch.id)) for ch in channels]
    counts = []
    original = AppState.model_dump
    def dump(self, *args, **kwargs):
        if self is current:
            counts.append(1)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(AppState, "model_dump", dump)
    try:
        for _ in range(100):
            state.broadcast_state_update()
        replies = await asyncio.wait_for(asyncio.gather(*(q.get() for q in queues)), 5)
        assert len(counts) == 1
        for index, reply in enumerate(replies):
            wire = reply["state"]
            assert wire["channel_scope"] == str(channels[index].id)
            assert "main_stacks" not in wire["channels"][1-index]
            descriptor = wire["channels"][index]["main_stacks"][0]["cells"][0]["files"][0]
            assert descriptor["name"] == "a.txt"
            assert descriptor["body_hash"] == hashlib.sha256(f"private-{index}".encode()).hexdigest()
            assert descriptor["encoding"] == "utf-8"
            assert descriptor["file_revision"] == 1
            assert descriptor["deleted"] is False
            assert "content" not in descriptor and "blob_url" not in descriptor
    finally:
        for channel, q in zip(channels, queues):
            events.unsubscribe(str(channel.id), q)


async def test_shared_encoding_and_update_during_build_are_preserved__F0012(monkeypatch):
    import events
    events._subscribers.clear()
    events._pending_coalesced.clear()
    events.capture_loop()
    channel_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "shared-encoding"))
    queues = [events.subscribe(channel_id) for _ in range(2)]
    entered, release = threading.Event(), threading.Event()
    builds, encodings = [], []
    original = events.SerializedEvent.__init__
    def encode(self, event):
        encodings.append(event["type"])
        original(self, event)
    monkeypatch.setattr(events.SerializedEvent, "__init__", encode)
    def build(channels):
        sequence = len(builds) + 1
        builds.append(sequence)
        if sequence == 1:
            entered.set()
            assert release.wait(5)
        return {channel: {"channel_scope": channel, "sequence": sequence} for channel in channels}
    try:
        events.broadcast_coalesced(build, delay=0.001)
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(100):
            events.broadcast_coalesced(build, delay=0.001)
        release.set()
        first = await asyncio.wait_for(asyncio.gather(*(q.get() for q in queues)), 5)
        second = await asyncio.wait_for(asyncio.gather(*(q.get() for q in queues)), 5)
        assert first[0] is first[1] and second[0] is second[1]
        assert first[0]["state"]["sequence"] == 1
        assert second[0]["base_revision"] == first[0]["revision"]
        assert second[0]["patches"] == [{"op": "set", "path": ["sequence"], "value": 2}]
        assert builds == [1, 2]
        assert encodings == ["state_snapshot", "state_delta"]
        assert '"value": 2' in second[0].sse_data
    finally:
        release.set()
        for q in queues:
            events.unsubscribe(channel_id, q)
