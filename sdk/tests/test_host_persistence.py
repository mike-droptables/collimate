"""Run flushes preserve their bytes and provenance across uncertain ACKs."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import pytest
from collimate._host.bridge import FileConflictError, HostBridge, IPCError, IPCTimeoutError
from collimate.testing import make_test_bridge
from collimate._results import AnnotationNotice


def accepted(payload, revision=1):
    return {"ok": True, "operation_id": payload["operation_id"], "outcome": "accepted",
            "cell_id": "history", "files": [
                {"path": path, "file_revision": revision, "writer_id":payload['writer_id'],
                 "edit_seq":payload['edit_seq'], "deleted":path in payload.get('deleted_files', [])}
                for path in payload["base_revisions"]]}


def test_uncertain_flush_replays_frozen_bytes_before_new_workspace_edit(tmp_path):
    bridge = make_test_bridge(tmp_path)
    bridge.stage_file('a.txt', 'first', category='file')
    sent = []
    def emit(payload, **_):
        snapshot = Path(bridge.workspace) / payload['artifacts'][0]['local_path']
        sent.append((json.loads(json.dumps(payload)), snapshot.read_text()))
        if len(sent) == 1:
            raise IPCTimeoutError('update_cell', 1)
        return accepted(payload, len(sent) - 1)
    bridge.emit_and_wait = emit
    try:
        with pytest.raises(IPCTimeoutError):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        bridge.stage_file('a.txt', 'second', category='file')
        HostBridge._commit_update(bridge, writes=['a.txt'])
        assert [body for _, body in sent] == ['first', 'first', 'second']
        assert sent[0][0] == sent[1][0]
        assert sent[2][0]['operation_id'] != sent[0][0]['operation_id']
        assert sent[2][0]['base_revisions'] == {'a.txt': 1}
        assert sent[2][0]['edit_seq'] > sent[0][0]['edit_seq']
        assert bridge._persistence['pending'] is None
    finally:
        bridge.close()


def test_pending_flush_survives_new_bridge_and_does_not_adopt_competing_inventory(tmp_path):
    bridge = make_test_bridge(tmp_path)
    bridge.stage_file('a.txt', 'candidate', category='file')
    def timeout(*_, **__):
        raise IPCTimeoutError('update_cell', 1)
    bridge.emit_and_wait = timeout
    with pytest.raises(IPCTimeoutError):
        HostBridge._commit_update(bridge, writes=['a.txt'])
    pending = bridge._persistence['pending']['payload']
    bridge.close()
    restarted = make_test_bridge(tmp_path)
    restarted._initialize_persistence()
    seen = []
    def conflict(payload, **_):
        seen.append(payload)
        return {'ok': True, 'outcome': 'conflict', 'operation_id': payload['operation_id'],
                'files': [{'path': 'a.txt', 'file_revision': 9}], 'conflicts': ['candidate']}
    restarted.emit_and_wait = conflict
    try:
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(restarted, writes=['a.txt'])
        assert seen == [pending]
        assert restarted._persistence['revisions'].get('a.txt', 0) == 0
        assert restarted._persistence['pending']['payload'] == pending
        assert (tmp_path / 'a.txt').read_text() == 'candidate'
    finally:
        restarted.close()


def test_reads_check_hash_and_revision_and_do_not_rebase_staged_edits(tmp_path):
    bridge = make_test_bridge(tmp_path)
    bridge.stage_file('a.txt', 'local edit', category='file')
    item = {'content': base64.b64encode(b'server').decode(),
            'body_hash': hashlib.sha256(b'server').hexdigest(),
            'file_revision': 7, 'encoding': 'utf-8'}
    bridge.emit_and_wait = lambda *_, **__: {'files': {'a.txt': item}}
    try:
        assert HostBridge.read_cell_files(bridge, paths=['a.txt'], revisions={'a.txt': 7}) == {'a.txt': 'server'}
        assert bridge._persistence['revisions'].get('a.txt', 0) == 0
        with pytest.raises(Exception, match='wrong revision'):
            HostBridge.read_cell_files(bridge, revisions={'a.txt': 6})
        item['content'] = base64.b64encode(b'corrupt').decode()
        with pytest.raises(Exception, match='corrupt'):
            HostBridge.read_cell_files(bridge)
    finally:
        bridge.close()


def _wire_message(message_id: str, body: str) -> dict:
    return {"id": message_id, "context_id": "run", "parent_id": None, "author": "user",
            "author_label": None, "kind": "message", "body": body, "context_items": [],
            "refs": [], "mode": "queued", "position": 1.0, "held": False, "revision": 1,
            "created_at": "2026-10-24 12:00:00", "edited_at": None,
            "read_at": "2026-10-24 12:00:01"}


def test_uncertain_annotation_pull_replays_the_same_operation_after_restart(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []

    def lose(op, **fields):
        sent.append((op, dict(fields)))
        raise IPCTimeoutError(op, 1)

    bridge._annotations_ack = lose
    with pytest.raises(IPCTimeoutError):
        bridge.annotations_pull()
    assert bridge._persistence["pending_annotation_pull"] == sent[0][1]
    bridge.close()
    restarted = make_test_bridge(tmp_path)

    def replay(op, **fields):
        sent.append((op, dict(fields)))
        return {"ok": True, "operation_id": fields["operation_id"],
                "messages": [_wire_message("a_" + "1" * 16, "hi")],
                "rev": 4, "unread": 0, "interrupt_seq": 1}

    restarted._annotations_ack = replay
    try:
        batch, notice = restarted.annotations_pull()
        assert sent[0] == sent[1] and sent[0][0] == "annotations_pull"
        assert [m.text for m in batch] == ["hi"] and notice == AnnotationNotice(4, 0, 1)
        assert restarted._persistence["pending_annotation_pull"] is None
        restarted.annotations_pull()
        assert sent[2][1]["operation_id"] != sent[0][1]["operation_id"]
    finally:
        restarted.close()


def test_uncertain_annotation_post_replays_the_same_message_after_restart(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []

    def lose(op, **fields):
        sent.append((op, dict(fields)))
        raise IPCTimeoutError(op, 1)

    bridge._annotations_ack = lose
    with pytest.raises(IPCTimeoutError):
        bridge.annotations_post("One reply", reply_to="a_" + "2" * 16)
    bridge.close()
    restarted = make_test_bridge(tmp_path)

    def accept(op, **fields):
        sent.append((op, dict(fields)))
        return {"ok": True, "operation_id": fields["operation_id"], "id": fields["message_id"]}

    restarted._annotations_ack = accept
    try:
        result = restarted.annotations_post("One reply", reply_to="a_" + "2" * 16)
        assert len(sent) == 2 and sent[0] == sent[1]
        assert result == sent[0][1]["message_id"]
        assert restarted._persistence["pending_annotation_post"] is None
        later = restarted.annotations_post("Another")
        assert later != result and sent[-1][1]["operation_id"] != sent[0][1]["operation_id"]
        assert "reply_to" not in sent[-1][1]
    finally:
        restarted.close()


def test_a_retried_post_returns_the_replayed_id_with_one_more_frame(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []
    answers = iter([IPCTimeoutError("annotations_post", 1), None])

    def flaky(op, **fields):
        sent.append(dict(fields))
        failure = next(answers)
        if failure is not None:
            raise failure
        return {"ok": True, "operation_id": fields["operation_id"], "id": fields["message_id"]}

    bridge._annotations_ack = flaky
    try:
        with pytest.raises(IPCTimeoutError):
            bridge.annotations_post("Same text")
        result = bridge.annotations_post("Same text")
        assert len(sent) == 2 and sent[0] == sent[1] and result == sent[0]["message_id"]
    finally:
        bridge.close()


@pytest.mark.parametrize("error, exc", [("empty_text", ValueError), ("sealed", RuntimeError)])
def test_a_definitive_post_refusal_drops_the_pending_post(tmp_path, error, exc):
    bridge = make_test_bridge(tmp_path)
    bridge._annotations_ack = lambda op, **fields: {"ok": False, "error": error}
    try:
        with pytest.raises(exc, match=error):
            bridge.annotations_post("text")
        assert bridge._persistence["pending_annotation_post"] is None
    finally:
        bridge.close()


def test_a_sealed_pull_drops_the_pending_pull_and_other_refusals_keep_it(tmp_path):
    from collimate._host.bridge import AnnotationsSealed

    bridge = make_test_bridge(tmp_path)
    try:
        bridge._annotations_ack = lambda op, **fields: {"ok": False, "error": "unknown_context"}
        with pytest.raises(RuntimeError, match="unknown_context"):
            bridge.annotations_pull()
        kept = bridge._persistence["pending_annotation_pull"]
        assert kept is not None
        bridge._annotations_ack = lambda op, **fields: {"ok": False, "error": "sealed"}
        with pytest.raises(AnnotationsSealed):
            bridge.annotations_pull()
        assert bridge._persistence["pending_annotation_pull"] is None
    finally:
        bridge.close()


def test_utf8_valid_binary_artifact_keeps_explicit_encoding(tmp_path):
    bridge = make_test_bridge(tmp_path)
    bridge.stage_file('empty.bin', b'', category='file')
    sent = []
    def emit(payload, **_):
        sent.append(payload)
        return accepted(payload)
    bridge.emit_and_wait = emit
    try:
        HostBridge._commit_update(bridge, writes=['empty.bin'])
        assert sent[0]['artifacts'][0]['encoding'] == 'binary'
    finally:
        bridge.close()


def test_observed_tombstone_advances_clean_baseline_but_not_pending_intent(tmp_path):
    bridge=make_test_bridge(tmp_path)
    try:
        tombstone={'path':'gone.txt','file_revision':8,'body_hash':None,'deleted':True}
        bridge._accept_clean_file_revisions([tombstone])
        assert bridge._persistence['revisions']['gone.txt']==8
        bridge.stage_file('gone.txt','recreated',category='file')
        bridge._accept_clean_file_revisions([{**tombstone,'file_revision':10}])
        assert bridge._persistence['revisions']['gone.txt']==8
    finally:
        bridge.close()


@pytest.mark.parametrize('damage', ['missing', 'sequence', 'writer', 'revision', 'deletion'])
def test_invalid_file_receipt_keeps_exact_pending_bytes(tmp_path, damage):
    bridge = make_test_bridge(tmp_path)
    bridge.stage_file('a.txt', 'must remain recoverable', category='file')
    sent = []
    def emit(payload, **_):
        sent.append(json.loads(json.dumps(payload)))
        receipt = accepted(payload)
        if len(sent) == 1:
            if damage == 'missing':
                receipt['files'] = []
            else:
                key, value = {'sequence':('edit_seq',0), 'writer':('writer_id','another'),
                              'revision':('file_revision',0), 'deletion':('deleted',True)}[damage]
                receipt['files'][0][key] = value
        return receipt
    bridge.emit_and_wait = emit
    try:
        with pytest.raises(IPCError, match='invalid file receipt'):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        pending = bridge._persistence['pending']
        assert pending['payload'] == sent[0]
        assert (Path(bridge.workspace) / pending['payload']['artifacts'][0]['local_path']).read_text() == 'must remain recoverable'
        HostBridge._commit_update(bridge, writes=['a.txt'])
        assert sent[1] == sent[0]
        assert bridge._persistence['pending'] is None
    finally:
        bridge.close()
