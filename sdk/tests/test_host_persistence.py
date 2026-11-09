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
                'files': [{'path': 'a.txt', 'file_revision': 9}],
                'conflicts': [_conflict('a.txt', payload, current=9)]}
    restarted.emit_and_wait = conflict
    try:
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(restarted, writes=['a.txt'])
        assert seen == [pending]
        assert raised.value.paths == {'a.txt'}
        # The competing revision is not adopted: a later write of a.txt is
        # another candidate, never a replacement of the other writer's save.
        assert restarted._persistence['revisions'].get('a.txt', 0) == 0
        # The conflict is the host's final answer, so it is not replayed.
        assert restarted._persistence['pending'] is None
        assert (tmp_path / 'a.txt').read_text() == 'candidate'
    finally:
        restarted.close()


def _conflict(path, payload, *, current, conflict_id=None, earlier=(), writer=None):
    conflict_id = conflict_id or 'conflict-' + path
    return {'conflict_id': conflict_id, 'path': path,
            'base_revision': payload['base_revisions'][path], 'current_revision': current,
            'candidate_revision': current + 1, 'candidate_hash': 'h', 'writer_id': payload['writer_id'],
            'edit_seq': payload['edit_seq'], 'current_writer': writer or {'kind': 'user'},
            'writer_candidates': [*earlier, conflict_id]}


def test_a_conflicted_flush_never_blocks_later_writes(tmp_path):
    """A conflict answers its operation: the next flush sends only its own
    paths, and they land."""
    bridge = make_test_bridge(tmp_path)
    sent = []
    def host(payload, **_):
        sent.append(json.loads(json.dumps(payload)))
        if 'a.txt' in payload['base_revisions']:
            return {'ok': True, 'outcome': 'conflict', 'operation_id': payload['operation_id'],
                    'files': [], 'conflicts': [_conflict('a.txt', payload, current=2)], 'refused': []}
        return accepted(payload, 5)
    bridge.emit_and_wait = host
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        assert isinstance(raised.value, PermissionError) and raised.value.paths == {'a.txt'}
        assert 'a.txt' in raised.value.errors
        bridge.stage_file('b.txt', 'unrelated', category='file')
        HostBridge._commit_update(bridge, writes=['b.txt'])
        assert [sorted(p['base_revisions']) for p in sent] == [['a.txt'], ['b.txt']]
        assert bridge._persistence['revisions'] == {'b.txt': 5}
        assert bridge._persistence['pending'] is None
    finally:
        bridge.close()


def test_fresh_siblings_of_a_conflicted_path_land_and_resolve_their_candidates(tmp_path):
    """One stale path makes candidates of a whole operation; its fresh
    siblings go again at once, resolving their own candidates, and only
    the stale path is refused."""
    bridge = make_test_bridge(tmp_path)
    sent, bodies = [], []
    def host(payload, **_):
        sent.append(json.loads(json.dumps(payload)))
        bodies.append({a['cell_path']: (Path(bridge.workspace) / a['local_path']).read_text()
                       for a in payload['artifacts']})
        if len(sent) == 1:
            return {'ok': True, 'outcome': 'conflict', 'operation_id': payload['operation_id'],
                    'files': [], 'refused': [],
                    'conflicts': [_conflict('a.txt', payload, current=2),
                                  _conflict('b.txt', payload, current=0)]}
        return accepted(payload, 1)
    bridge.emit_and_wait = host
    try:
        bridge.stage_file('a.txt', 'stale', category='file')
        bridge.stage_file('b.txt', 'fresh', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt', 'b.txt'])
        assert raised.value.paths == {'a.txt'}
        first, again = sent
        assert again['operation_id'] != first['operation_id'] and again['edit_seq'] > first['edit_seq']
        assert again['base_revisions'] == {'b.txt': 0}
        assert again['resolves'] == ['conflict-b.txt']
        assert bodies == [{'a.txt': 'stale', 'b.txt': 'fresh'}, {'b.txt': 'fresh'}]
        assert bridge._persistence['revisions'] == {'b.txt': 1}
        assert bridge._persistence['pending'] is None
    finally:
        bridge.close()


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


def _user_save_host(bridge, sent, *, current=3, body='user save'):
    """A host whose a.txt holds the user's ``body`` at revision ``current``:
    a write based on an older revision is a candidate (one more each time),
    a write on ``current`` lands."""
    digest = hashlib.sha256(body.encode()).hexdigest()
    candidates = []
    def host(payload, **_):
        sent.append(json.loads(json.dumps(payload)))
        if payload['base_revisions'].get('a.txt', current) < current:
            candidates.append(f'candidate-{len(candidates) + 1}')
            return {'ok': True, 'outcome': 'conflict', 'operation_id': payload['operation_id'],
                    'refused': [], 'files': [{'path': 'a.txt', 'file_revision': current,
                                              'body_hash': digest, 'deleted': False, 'writer_id': 'browser'}],
                    'conflicts': [_conflict('a.txt', payload, current=current, conflict_id=candidates[-1],
                                            earlier=candidates[:-1])]}
        return accepted(payload, max([current, *payload['base_revisions'].values()]) + 1)
    return host


def test_a_conflict_refusal_names_the_newer_revision_its_writer_and_the_runs_candidates(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        conflict = raised.value.conflicts['a.txt']
        assert (conflict.revision, conflict.writer, conflict.writer_kind, conflict.candidates) == (
            3, 'the user', 'user', ('candidate-1',))
        for text in (str(raised.value), raised.value.errors['a.txt'], str(conflict)):
            assert 'revision 3' in text and 'the user' in text and 'candidate-1' in text
            assert 'replace_revision=3' in text
        assert bridge.file_conflicts()['a.txt'] == conflict
        # A plain retry is another candidate on the old base, resolving nothing.
        bridge.stage_file('a.txt', 'run edit again', category='file')
        with pytest.raises(FileConflictError) as again:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        assert sent[1]['base_revisions'] == {'a.txt': 0} and sent[1]['resolves'] == []
        assert 'replaces' not in sent[1]
        assert again.value.conflicts['a.txt'].candidates == ('candidate-1', 'candidate-2')
    finally:
        bridge.close()


def test_an_acknowledged_write_replaces_the_named_revision_and_closes_the_runs_candidates(tmp_path):
    from collimate import native
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    native.set_bridge(bridge)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        native.update_file('a.txt', 'run edit, over the user save', replace_revision=3)
        HostBridge._commit_update(bridge, writes=bridge.take_writes())
        ack = sent[-1]
        assert ack['base_revisions'] == {'a.txt': 3}
        assert ack['resolves'] == ['candidate-1'] and ack['replaces'] == {'a.txt': 3}
        assert bridge._persistence['revisions'] == {'a.txt': 4}
        assert bridge.file_conflicts() == {} and not bridge.replacement_staged('a.txt')
        # The acknowledgement is spent: a later write of a.txt is a normal one.
        native.update_file('a.txt', 'later')
        HostBridge._commit_update(bridge, writes=bridge.take_writes())
        assert sent[-1]['base_revisions'] == {'a.txt': 4} and 'replaces' not in sent[-1]
    finally:
        native._clear_bridge()
        bridge.close()


def test_a_replacement_must_name_the_revision_an_open_refusal_reported(tmp_path):
    from collimate import native
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    native.set_bridge(bridge)
    try:
        with pytest.raises(ValueError, match='no refused write'):
            native.update_file('a.txt', 'eager', replace_revision=3)
        assert not bridge.tracked_writes and not bridge.replacements
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        with pytest.raises(ValueError, match='named revision 3, not 2'):
            native.update_file('a.txt', 'stale guess', replace_revision=2)
        with pytest.raises(TypeError):
            native.update_file('a.txt', 'no number', replace_revision='3')
        bridge.stage_file('b.txt', 'other', category='file')
        with pytest.raises(ValueError, match='does not write'):
            import asyncio
            asyncio.run(native.commit(replace_revisions={'a.txt': 3}))
        assert not bridge.replacements and len(sent) == 1
    finally:
        native._clear_bridge()
        bridge.close()


def test_an_acknowledgement_over_a_revision_the_user_has_replaced_is_a_conflict_again(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        bridge.stage_replacement('a.txt', 3)
        bridge.emit_and_wait = _user_save_host(bridge, sent, current=5, body='user saved again')
        bridge.stage_file('a.txt', 'run edit over 3', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        assert sent[-1]['replaces'] == {'a.txt': 3}
        assert raised.value.conflicts['a.txt'].revision == 5
        assert bridge.file_conflicts()['a.txt'].revision == 5
        assert bridge._persistence['revisions'].get('a.txt', 0) == 0
    finally:
        bridge.close()


def test_writing_back_the_newer_revisions_bytes_accepts_it_and_closes_the_candidates(tmp_path):
    """Accepting the newer version is a conditional resolution, as the
    editor's "Use server version" is: the same bytes on that revision,
    resolving the run's candidates, never a silent change of base."""
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        bridge.stage_replacement('a.txt', 3)
        bridge.stage_file('a.txt', 'user save', category='file')
        bridge.stage_file('b.txt', 'sibling', category='file')
        HostBridge._commit_update(bridge, writes=['a.txt', 'b.txt'])
        assert sent[-1]['base_revisions'] == {'a.txt': 3, 'b.txt': 0}
        assert sent[-1]['resolves'] == ['candidate-1']
        assert [a['cell_path'] for a in sent[-1]['artifacts']] == ['a.txt', 'b.txt']
        # The same bytes replace nothing, whatever was staged.
        assert 'replaces' not in sent[-1] and not bridge.replacement_staged('a.txt')
        assert bridge._persistence['revisions']['a.txt'] == 4
        assert bridge.file_conflicts() == {}
    finally:
        bridge.close()


def test_accept_stages_the_newer_bytes_or_deletion_as_the_runs_answer(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        conflict = raised.value.conflicts['a.txt']
        bridge.read_cell_files = lambda **kw: {'a.txt': 'user save'}
        conflict.accept()
        assert bridge.fs_read_bytes('a.txt') == b'user save' and 'a.txt' in bridge.tracked_writes
        HostBridge._commit_update(bridge, writes=bridge.take_writes())
        assert sent[-1]['base_revisions'] == {'a.txt': 3} and sent[-1]['resolves'] == ['candidate-1']
        assert bridge.file_conflicts() == {}
        # A newer revision that deleted the path: accepting it is its deletion.
        bridge._persistence['conflicts']['a.txt'] = {'revision': 5, 'writer': {'kind': 'user'},
                                                     'candidates': ['candidate-9'], 'body_hash': None}
        bridge.file_conflicts()['a.txt'].accept()
        assert not (Path(bridge.workspace) / 'a.txt').exists() and 'a.txt' in bridge.tracked_deletions
        HostBridge._commit_update(bridge, writes=[], deleted_files=['a.txt'])
        assert sent[-1]['deleted_files'] == ['a.txt']
        assert sent[-1]['base_revisions'] == {'a.txt': 5} and sent[-1]['resolves'] == ['candidate-9']
    finally:
        bridge.close()


def test_an_observation_of_the_newer_revision_leaves_the_refusal_for_the_next_write(tmp_path):
    """Observing the newer revision (a mirror pull of its exact bytes) moves
    the base but answers nothing: the refusal stays open, and the run's next
    write of the path, on that revision, resolves its candidates."""
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        bridge._accept_clean_file_revisions([{'path': 'a.txt', 'file_revision': 3}])
        assert bridge._persistence['revisions']['a.txt'] == 3
        assert 'a.txt' in bridge.file_conflicts()
        bridge.stage_file('a.txt', 'built on the user save', category='file')
        HostBridge._commit_update(bridge, writes=bridge.take_writes())
        assert sent[-1]['base_revisions'] == {'a.txt': 3} and sent[-1]['resolves'] == ['candidate-1']
        assert 'replaces' not in sent[-1] and bridge.file_conflicts() == {}
    finally:
        bridge.close()


def test_collateral_retry_of_an_acknowledged_path_keeps_its_replacement(tmp_path):
    """An acknowledged path whose sibling went stale lands again at once,
    still over the named revision and still closing its candidates."""
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=['a.txt'])
        def host(payload, **_):
            sent.append(json.loads(json.dumps(payload)))
            if 'b.txt' in payload['base_revisions']:
                return {'ok': True, 'outcome': 'conflict', 'operation_id': payload['operation_id'],
                        'refused': [], 'files': [],
                        'conflicts': [_conflict('a.txt', payload, current=3, conflict_id='collateral-a'),
                                      _conflict('b.txt', payload, current=7)]}
            return accepted(payload, 4)
        bridge.emit_and_wait = host
        bridge.stage_replacement('a.txt', 3)
        bridge.stage_file('a.txt', 'run edit over 3', category='file')
        bridge.stage_file('b.txt', 'stale sibling', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt', 'b.txt'])
        assert raised.value.paths == {'b.txt'}
        retry = sent[-1]
        assert retry['base_revisions'] == {'a.txt': 3} and retry['replaces'] == {'a.txt': 3}
        assert retry['resolves'] == ['candidate-1', 'collateral-a']
        assert 'a.txt' not in bridge.file_conflicts()
    finally:
        bridge.close()


def test_reading_a_refusals_newer_version_does_not_make_it_the_runs_base(tmp_path):
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError) as raised:
            HostBridge._commit_update(bridge, writes=['a.txt'])
        def read(payload, **_):
            assert payload == {'op': 'read_cell_files', 'paths': ['a.txt'], 'revisions': {'a.txt': 3}}
            return {'files': {'a.txt': {'content': base64.b64encode(b'user save').decode(), 'file_revision': 3,
                                        'body_hash': hashlib.sha256(b'user save').hexdigest(),
                                        'encoding': 'utf-8'}}}
        bridge.emit_and_wait = read
        assert HostBridge.read_cell_files(bridge, paths=['a.txt'], revisions={'a.txt': 3}, observe=False) == {'a.txt': 'user save'}
        bridge.read_cell_files = lambda **kw: HostBridge.read_cell_files(bridge, **kw)
        assert raised.value.conflicts['a.txt'].read() == 'user save'
        assert bridge._persistence['revisions'].get('a.txt', 0) == 0
        assert 'a.txt' in bridge.file_conflicts()
    finally:
        bridge.close()


def test_deleting_a_path_drops_the_replacement_staged_for_its_write(tmp_path):
    """A replacement goes with the write it was staged for: deleting the path
    before the flush sends a plain deletion (another candidate), and no later
    write of the path inherits the replacement."""
    from collimate import native
    bridge = make_test_bridge(tmp_path)
    sent = []
    bridge.emit_and_wait = _user_save_host(bridge, sent)
    native.set_bridge(bridge)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        native.update_file('a.txt', 'run edit over 3', replace_revision=3)
        native.delete_file('a.txt')
        assert not bridge.replacement_staged('a.txt')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes(),
                                      deleted_files=sorted(bridge.tracked_deletions))
        assert sent[-1]['deleted_files'] == ['a.txt'] and 'replaces' not in sent[-1]
        bridge.tracked_deletions.clear()
        native.update_file('a.txt', 'later plain write')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        assert sent[-1]['base_revisions'] == {'a.txt': 0} and 'replaces' not in sent[-1]
    finally:
        native._clear_bridge()
        bridge.close()


def test_a_replacement_staged_over_an_unanswered_retrys_bytes_still_goes(tmp_path):
    """A plain retry whose answer was lost, then a replacement staged over the
    same bytes: replaying the retry is a conflict again, and the same bytes
    then go over the named revision rather than being skipped as already
    sent (which would leave the replacement staged for a later write)."""
    from collimate import native
    bridge = make_test_bridge(tmp_path)
    sent = []
    host = _user_save_host(bridge, sent)
    lost = []
    def emit(payload, **kw):
        if len(sent) == 1 and not lost:
            lost.append(payload['operation_id'])
            sent.append(json.loads(json.dumps(payload)))
            raise IPCTimeoutError('update_cell', 1)
        return host(payload, **kw)
    bridge.emit_and_wait = emit
    native.set_bridge(bridge)
    try:
        bridge.stage_file('a.txt', 'run edit', category='file')
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        native.update_file('a.txt', 'run edit again')
        with pytest.raises(IPCTimeoutError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        native.update_file('a.txt', 'run edit again', replace_revision=3)
        with pytest.raises(FileConflictError):
            HostBridge._commit_update(bridge, writes=bridge.take_writes())
        assert sent[-2]['operation_id'] == lost[0]
        assert sent[-1]['replaces'] == {'a.txt': 3} and sent[-1]['base_revisions'] == {'a.txt': 3}
        assert not bridge.replacement_staged('a.txt')
    finally:
        native._clear_bridge()
        bridge.close()
