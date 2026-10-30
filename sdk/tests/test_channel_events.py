"""The channel fixture enumerates the SDK records consumed by client dispatch."""
import json
from pathlib import Path

from collimate._host.channel_events import (
    COMMIT_CHANGE_OPS, EVENT_TYPES, AnnotationsChanged, CommitChange, StateDelta, StateSnapshot,
)

_FIXTURE = Path(__file__).parents[1] / "shared/contracts/channel-events.json"


def test_channel_event_fixture_enumerates_every_record__F0308_F1716():
    samples = json.loads(_FIXTURE.read_text())
    assert {sample["type"] for sample in samples} == set(EVENT_TYPES)
    assert len(samples) == len(EVENT_TYPES)
    for sample in samples:
        payload = {k: v for k, v in sample.items() if k != "type"}
        assert EVENT_TYPES[sample["type"]](**payload).to_dict() == sample


def test_channel_event_envelope_never_copies_payload__F0308():
    state = {"channels": [{"cells": [object()]}]}
    assert StateSnapshot("fixture:1", state).to_dict()["state"] is state


def test_flush_batch_envelope_preserves_order_and_identity__F0140():
    from collimate._host.channel_events import RunEvents
    events=[{'id':1,'type':'stdout','payload':'one','created_at':'now'}, {'id':2,'type':'log','payload':'two','created_at':'now'}]
    value=RunEvents('run',events).to_dict()
    assert value=={'type':'run_events','run_id':'run','events':events}
    assert value['events'] is events


def test_delta_envelope_preserves_operation_identity__P4F03():
    patches = [{'op': 'set', 'path': ['field'], 'value': object()}]
    event = StateDelta('before', 'after', 'channel', patches).to_dict()
    assert event['patches'] is patches
    assert event['base_revision'] == 'before' and event['revision'] == 'after'


def test_lineage_changed_and_run_status_end_fields_O04():
    from collimate._host.channel_events import LineageChanged, RunStatus
    ids = ["cell-1", "cell-2"]
    event = LineageChanged(ids).to_dict()
    assert event == {"type": "lineage_changed", "cell_ids": ids} and event["cell_ids"] is ids
    ended = RunStatus("run", "completed", None, "2026-10-06T14:05:00+00:00", "probe passed", "pass").to_dict()
    assert ended == {"type": "run_status", "run_id": "run", "status": "completed", "failure_reason": None,
                     "ended_at": "2026-10-06T14:05:00+00:00", "note": "probe passed", "outcome": "pass"}
    assert RunStatus("run", "stopped").to_dict()["ended_at"] is None


def test_run_started_carries_the_opened_surfaces_GN03():
    from collimate._host.channel_events import RunStarted
    bare = RunStarted("run", "cell", None, None, "x-11111111", "_runs/x-11111111", "x").to_dict()
    assert bare["opens"] == []
    paths = ["_runs/x-11111111/chat/conversation.chat"]
    event = RunStarted("run", "cell", "x", "X", "x-11111111", "_runs/x-11111111", "x",
                       stack_id="stack", opens=paths).to_dict()
    assert event["opens"] is paths and event["stack_id"] == "stack"


def test_surface_changed_is_a_typed_channel_event():
    from collimate._host.channel_events import SurfaceChanged
    assert SurfaceChanged("cell", "notes/board.panel").to_dict() == {
        "type": "surface_changed", "cell_id": "cell", "path": "notes/board.panel",
    }


def test_annotations_changed_is_the_context_doorbell():
    assert AnnotationsChanged("cell-1", "run-1", 3).to_dict() == {
        "type": "annotations_changed", "cell_id": "cell-1", "context_id": "run-1", "rev": 3,
    }
    samples = json.loads(_FIXTURE.read_text())
    assert samples[-1]["type"] == "annotations_changed"
    assert list(EVENT_TYPES)[-1] == "annotations_changed"


def test_commit_run_event_payload_lists_its_changes():
    samples = json.loads(_FIXTURE.read_text())
    [batch] = [s for s in samples if s["type"] == "run_events"]
    [commit] = [e for e in batch["events"] if e["type"] == "commit"]
    changes = json.loads(commit["payload"])["changes"]
    assert changes
    for change in changes:
        assert set(change) == set(CommitChange.__annotations__)
        assert change["op"] in COMMIT_CHANGE_OPS
        assert isinstance(change["path"], str) and isinstance(change["folder"], bool)
