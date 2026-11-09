"""Run-write verdicts use actual cell commits and accepted synthetic handlers."""
import pytest

import test_pytest_run_folders as support
from test_pytest_run_folders import _mem, _accepted_files, _accept_file, _emit, _operation

client, cell, live = support.client, support.cell, support.live


def handler(source, ext='.checked'):
    """Add a synthetic handler with the verifier ``source`` on ``ext`` to the
    Capabilities stack, its build accepted as Review and accept stores it."""
    import yaml
    from collimate.handlers import derived_hash
    from conftest import add_capabilities_cell
    from collimate_server.handlers import save_derived_skill
    from collimate_server.models import Cell, VirtualFile
    from collimate_server.persistence import publish_files
    from collimate_server.state import find_active_channel
    files = {'verify.ts': source.encode()}
    manifest = {'id': 'gate-test', 'name': 'Gate test', 'description': 'Synthetic verifier',
                'types': [{'ext': ext}], 'guidance': '', 'examples': [], 'derived': derived_hash(files)}
    definition = Cell(files=[
        VirtualFile(name='gate/type.handler', content=yaml.safe_dump(manifest)),
        VirtualFile(name='gate/derived/verify.ts', content=source),
    ])
    save_derived_skill(manifest['id'], manifest['derived'], files)
    channel=find_active_channel()
    add_capabilities_cell(channel,definition)
    publish_files(channel,str(definition.id),{},persist=False)


def test_run_refuses_invalid_file_keeps_accepted_user_edit_and_commits_sibling__D10__P6F01(cell, live):
    manager = live.start(cell)
    _emit(manager,artifacts=[_mem('a.checked', 'old')])
    _accept_file(cell, 'a.checked', 'user accepted edit')
    handler('export default input => ({valid:input.text === "good",error:"needs good"});')
    version, refused = _emit(manager,artifacts=[_mem('a.checked', 'bad'), _mem('b.txt', 'kept')])
    assert version
    assert refused == [{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'needs good'}]
    assert _accepted_files(cell)['b.txt'] == 'kept'
    assert _accepted_files(cell)['a.checked'] == 'user accepted edit'


def test_run_actual_postflush_package_and_full_paths__D10(cell, live):
    manager = live.start(cell)
    handler('export default input => ({valid:input.package["code.txt"] === "new" && input.cell_paths.includes("other.txt"),error:"missing candidate"});')
    _, refused = _emit(manager,artifacts=[_mem('pkg/type.checked', 'definition'), _mem('pkg/code.txt', 'new'), _mem('other.txt', 'outside')])
    assert refused == []
    assert _accepted_files(cell)['pkg/type.checked'] == 'definition'
    _, refused = _emit(manager,artifacts=[_mem('pkg/type.checked', 'changed')], deleted_files=['pkg/code.txt'])
    assert refused == [{'path': 'pkg/type.checked', 'reason': 'handler_invalid', 'error': 'missing candidate'}]
    assert _accepted_files(cell)['pkg/type.checked'] == 'definition'
    assert 'pkg/code.txt' not in _accepted_files(cell)


def test_run_previous_value_is_current_accepted_body__D10(cell, live):
    manager = live.start(cell)
    _emit(manager,artifacts=[_mem('a.checked', 'committed')])
    _accept_file(cell, 'a.checked', 'accepted edit')
    handler('export default input => ({valid:input.previous_text === "accepted edit" && input.text === "new",error:"wrong baseline"});')
    _, refused = _emit(manager,artifacts=[_mem('a.checked', 'new')])
    assert refused == []
    assert _accepted_files(cell)['a.checked'] == 'new'


def test_verification_retries_tip_race_outside_all_commit_locks__D10(cell, live, monkeypatch):
    import threading
    import collimate_server.verification as verification
    first, second = live.start(cell), live.start(cell)
    _emit(first,artifacts=[_mem('pkg/code.txt', 'old')])
    handler('export default input => ({valid:input.package["code.txt"] === "new",error:"stale"});')
    actual, calls = verification.verify_source, []
    errors = []
    def verify(*args, **kwargs):
        calls.append(args[1]['package']['code.txt'])
        if len(calls) == 1:
            def mutate():
                try:
                    _emit(second,artifacts=[_mem('pkg/code.txt', 'new')])
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=mutate)
            thread.start()
            thread.join(2)
            assert not thread.is_alive(), 'verification held a cell/state lock'
            assert not errors
        return actual(*args, **kwargs)
    monkeypatch.setattr(verification, 'verify_source', verify)
    _, refused = _emit(first,artifacts=[_mem('pkg/type.checked', 'definition')])
    assert refused == [] and calls == ['old', 'new']
    assert _accepted_files(cell)['pkg/code.txt'] == 'new'


@pytest.mark.parametrize('advisory_overlap', [False, True])
def test_accepted_revision_race_retries_and_second_race_refuses_only_verified_writes__D10_P4F48(cell, live, monkeypatch, advisory_overlap):
    import collimate_server.verification as verification
    import threading
    import collimate_server.user_validation as user_validation
    from collimate_server.state import find_active_channel
    manager = live.start(cell)
    handler('export default () => ({valid:true});')
    actual, calls = verification.verify_source, []
    advisory_calls, advisory_errors = [], []
    from collimate_server.cell_placement import capabilities_stack_cells
    handler_cell = capabilities_stack_cells(find_active_channel())[-1]
    if advisory_overlap:
        _accept_file(str(handler_cell.id),'gate/advisory.checked','advisory input')
    def verify(source, value, **kwargs):
        # Only the run's owned lease belongs to this race injection. Advisory
        # validation remains real and must not mutate this test's target cell.
        if kwargs.get('lease') is None:
            result = actual(source, value, **kwargs)
            advisory_calls.append((value['path'], result))
            return result
        calls.append(kwargs['deadline'])
        if advisory_overlap and len(calls) == 1:
            def advisory():
                try:
                    user_validation._service._validate((manager.channel, str(handler_cell.id)))
                except BaseException as exc:
                    advisory_errors.append(exc)
            thread = threading.Thread(target=advisory, name='forced-user-validation')
            thread.start()
            thread.join(5)
            assert not thread.is_alive(), 'advisory validation did not complete'
            assert not advisory_errors
        _accept_file(cell, 'user.txt', f'accepted {len(calls)}')
        return {'valid': True}
    monkeypatch.setattr(verification, 'verify_source', verify)
    _, refused = _emit(manager,artifacts=[_mem('a.checked', 'unverified'), _mem('plain.txt', 'kept')])
    assert len(calls) == 2 and calls[0] == calls[1]
    assert refused == [{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'cell changed during verification; retry'}]
    assert 'a.checked' not in _accepted_files(cell)
    assert _accepted_files(cell)['plain.txt'] == 'kept'
    assert _accepted_files(cell)['user.txt'] == 'accepted 2'

    if advisory_overlap:
        assert ('gate/advisory.checked', {'valid': True}) in advisory_calls


def test_seal_during_verification_never_commits_late_write__D10(cell, live, monkeypatch):
    import collimate_server.verification as verification
    manager = live.start(cell)
    _emit(manager,artifacts=[_mem('plain.txt', 'before')])
    handler('export default () => ({valid:true});')
    def verify(*args, **kwargs):
        manager.sealed = True
        return {'valid': True}
    monkeypatch.setattr(verification, 'verify_source', verify)
    with pytest.raises(RuntimeError, match='ended'):
        _emit(manager,artifacts=[_mem('a.checked', 'bad'), _mem('plain.txt', 'after')])
    assert _accepted_files(cell)['plain.txt'] == 'before'
    assert 'a.checked' not in _accepted_files(cell)


def test_run_rechecks_definition_after_sibling_change_and_keeps_sibling__D10(cell, live, monkeypatch):
    # Catalog substitution supplies an accepted verifier for the reserved
    # definition type; discovery, input, native evaluation and commit are real.
    from types import SimpleNamespace
    import collimate_server.handlers as handlers
    manager = live.start(cell)
    monkeypatch.setattr(handlers, 'catalog', lambda _:SimpleNamespace(for_path=lambda p:None))
    _emit(manager,artifacts=[_mem('pkg/type.renderer', 'definition'), _mem('pkg/code.txt', 'good')])
    source = b'export default input => ({valid:input.package["code.txt"] === "good",error:"bad code"});'
    record = SimpleNamespace(accepted_files={'verify.ts': source})
    monkeypatch.setattr(handlers, 'catalog', lambda _:SimpleNamespace(for_path=lambda p:record if p.endswith('.renderer') else None))
    _, refused = _emit(manager,artifacts=[_mem('pkg/code.txt', 'bad')])
    assert refused == [{'path': 'pkg/type.renderer', 'reason': 'handler_invalid', 'error': 'bad code'}]
    assert _accepted_files(cell)['pkg/code.txt'] == 'bad'
    assert _accepted_files(cell)['pkg/type.renderer'] == 'definition'


def test_candidate_is_rechecked_after_another_write_is_refused__D10(cell, live):
    manager = live.start(cell)
    handler('export default input => ({valid:input.path.endsWith("a.checked") && input.package["b.checked"] === "new",error:"dependency refused"});')
    _, refused = _emit(manager,artifacts=[_mem('pkg/a.checked', 'definition'), _mem('pkg/b.checked', 'new'), _mem('plain.txt', 'kept')])
    assert {r['path'] for r in refused} == {'pkg/a.checked', 'pkg/b.checked'}
    assert _accepted_files(cell)['plain.txt'] == 'kept'
    assert not any(p.endswith('.checked') for p in _accepted_files(cell))


def test_plain_flush_does_not_start_worker_or_read_verification_bodies__D10(cell, live, monkeypatch):
    import collimate_server.run_verification as run_verification
    import collimate_server.verifier_pool as verifier_pool
    monkeypatch.setattr(run_verification, '_input', lambda *a:pytest.fail('plain flush reads verifier input'))
    monkeypatch.setattr(verifier_pool, 'get_verifier_pool', lambda:pytest.fail('plain flush starts worker'))
    _, refused = _emit(live.start(cell),artifacts=[_mem('plain.txt', 'kept')])
    assert refused == []


def test_expired_flush_refuses_verified_paths_but_commits_plain_siblings__D10(cell, live, monkeypatch):
    import collimate_server.run_verification as run_verification
    handler('export default () => ({valid:true});')
    original = run_verification.RunVerifier.__init__
    def expired(self, manager):
        original(self, manager)
        self.deadline = 0
    monkeypatch.setattr(run_verification.RunVerifier, '__init__', expired)
    _, refused = _emit(live.start(cell),artifacts=[_mem('a.checked', 'bad'), _mem('b.txt', 'kept')])
    assert refused == [{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'verifier budget exceeded'}]
    assert _accepted_files(cell)['b.txt'] == 'kept'


def test_accepted_verifier_rechecks_definition_after_code_change__D10(cell, live, monkeypatch):
    manager = live.start(cell)
    from types import SimpleNamespace
    import collimate_server.handlers as handlers
    monkeypatch.setattr(handlers,'catalog',lambda _:SimpleNamespace(for_path=lambda p:None))
    definition = 'Synthetic definition'
    valid = 'export default defineRenderer({component:()=>null});'
    _, refused = _emit(manager,artifacts=[_mem('pkg/sample.renderer', definition), _mem('pkg/main.tsx', valid)])
    assert refused == []
    source=b'export default input => ({valid:input.package["main.tsx"].includes("defineRenderer"),error:"missing renderer definition"});'
    record=SimpleNamespace(accepted_files={'verify.ts':source})
    monkeypatch.setattr(handlers,'catalog',lambda _:SimpleNamespace(for_path=lambda p:record if p.endswith('.renderer') else None))
    _, refused = _emit(manager,artifacts=[_mem('pkg/main.tsx', 'export default 42;')])
    assert refused == [{'path':'pkg/sample.renderer','reason':'handler_invalid','error':'missing renderer definition'}]
    assert _accepted_files(cell)['pkg/main.tsx'] == 'export default 42;'
    assert _accepted_files(cell)['pkg/sample.renderer'] == definition


def test_partial_ack_follows_commit_and_carries_verifier_error__D10(cell, live):
    import asyncio
    manager = live.start(cell)
    handler('export default () => ({valid:false,error:"needs repair"});')
    sent = []
    async def ack(body, request):
        assert _accepted_files(cell)['plain.txt'] == 'kept'
        assert 'a.checked' not in _accepted_files(cell)
        sent.append(body)
    manager._send_ack = ack
    asyncio.run(manager._op_update_cell(_operation(manager,artifacts=[_mem('a.checked', 'bad'), _mem('plain.txt', 'kept')])))
    assert sent[0]['ok'] is True and sent[0]['cell_id']
    assert sent[0]['refused'] == [{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'needs repair'}]
    assert any('needs repair' in row['payload'] for row in manager.log_buffer)


def test_verification_cannot_publish_collected_staged_cas_reference__D10(cell, live, monkeypatch):
    import hashlib
    from pathlib import Path
    import collimate_server.cas as cas
    import collimate_server.verification as verification
    manager = live.start(cell)
    _emit(manager,artifacts=[_mem('plain.txt', 'before')])
    handler('export default () => ({valid:true});')
    digest = hashlib.sha256(b'after').hexdigest()
    def verify(*args, **kwargs):
        Path(cas.channel_store(manager.channel).blob_path(digest)).unlink()
        return {'valid': True}
    monkeypatch.setattr(verification, 'verify_source', verify)
    with pytest.raises(OSError,match="Missing run artifact"):
        _emit(manager,artifacts=[_mem('a.checked', 'typed'), _mem('plain.txt', 'after')])
    assert _accepted_files(cell)['plain.txt'] == 'before'
    assert 'a.checked' not in _accepted_files(cell)


def test_run_write_keeps_unrelated_accepted_revisions_and_tombstones__D10(cell, live):
    from collimate_server.persistence import current_file
    from collimate_server.database import get_db_connection
    manager = live.start(cell)
    _accept_file(cell,'user.txt','user edit')
    _accept_file(cell,'deleted.txt','old')
    _accept_file(cell,'deleted.txt',None)
    with get_db_connection(manager.channel) as conn:
        before={p:current_file(conn,cell,p) for p in ('user.txt','deleted.txt')}
    _, refused = _emit(manager,artifacts=[_mem('plain.txt','accepted')])
    assert refused == []
    with get_db_connection(manager.channel) as conn:
        assert {p:current_file(conn,cell,p) for p in before} == before
    assert _accepted_files(cell)['user.txt'] == 'user edit'
    assert 'deleted.txt' not in _accepted_files(cell)
    assert _accepted_files(cell)['plain.txt'] == 'accepted'


def test_run_write_does_not_rehash_unrelated_large_accepted_body__D10_F0010(cell, live, monkeypatch):
    import threading
    import collimate_server.cas as cas
    manager = live.start(cell)
    large = 'unrelated accepted body\n' * (256 * 1024)
    _accept_file(cell,'large.txt',large)
    stored=[]
    original=cas.BodyStore.store_bytes
    def store(self, body):
        # Only the run write's own stores: the user-validation thread, woken
        # by the save above, may be publishing its compiled verifier's bundle
        # (about half a megabyte) meanwhile.
        if threading.current_thread() is threading.main_thread():
            stored.append(len(body))
        return original(self, body)
    monkeypatch.setattr(cas.BodyStore,'store_bytes',store)
    _, refused=_emit(manager,artifacts=[_mem('plain.txt','x' * 1024)])
    assert refused == [] and stored and max(stored) <= 1024, stored
    assert _accepted_files(cell)['large.txt'] == large


def test_verifier_sees_current_accepted_empty_body_and_tombstone__D10(cell, live):
    manager=live.start(cell)
    _accept_file(cell,'pkg/user.txt','accepted edit')
    _accept_file(cell,'pkg/empty.txt','')
    _accept_file(cell,'pkg/deleted.txt','old')
    _accept_file(cell,'pkg/deleted.txt',None)
    handler('export default input => ({valid:input.package["user.txt"] === "accepted edit" && input.package["empty.txt"] === "" && !("deleted.txt" in input.package) && !input.cell_paths.includes("pkg/deleted.txt"),error:"wrong accepted view"});')
    _, refused=_emit(manager,artifacts=[_mem('pkg/a.checked','accepted')])
    assert refused == []
    assert _accepted_files(cell) == {'pkg/user.txt':'accepted edit','pkg/empty.txt':'','pkg/a.checked':'accepted'}


def _current_revision(cell_id, path):
    from collimate_server.database import get_db_connection
    from collimate_server.persistence import current_file
    with get_db_connection(support.CHANNEL) as conn:
        return current_file(conn, cell_id, path)['revision']


def _candidate(manager, cell_id, path, text):
    """The run's write of ``path`` on its stale base: a conflict, whose
    candidate id it returns."""
    stale = _current_revision(cell_id, path) - 1
    op = _operation(manager, artifacts=[_mem(path, text)])
    op['base_revisions'][path] = stale
    manager._emit_partial(**op)
    receipt = manager._last_file_receipt
    assert receipt['outcome'] == 'conflict', receipt
    return receipt['conflicts'][0]['conflict_id']


def _open_candidates(cell_id, path):
    from collimate_server.database import get_db_connection
    with get_db_connection(support.CHANNEL) as conn:
        return [row[0] for row in conn.execute(
            'SELECT conflict_id FROM file_conflicts WHERE cell_id=? AND path=? '
            'AND resolution_operation_id IS NULL', (cell_id, path))]


def test_an_acknowledged_write_must_name_each_paths_base_revision(cell, live):
    manager = live.start(cell)
    _emit(manager, artifacts=[_mem('a.md', 'run')])
    _accept_file(cell, 'a.md', 'user save')
    candidate = _candidate(manager, cell, 'a.md', 'run edit')
    current = _current_revision(cell, 'a.md')
    for replaces in ({'a.md': current + 1}, {'a.md': current - 1}, {'other.md': current}, {'a.md': '3'}):
        op = _operation(manager, artifacts=[_mem('a.md', 'run over the save')])
        with pytest.raises(ValueError, match="replaces must name each path's base revision"):
            manager._emit_partial(**dict(op, resolves=[candidate], replaces=replaces))
    assert _accepted_files(cell)['a.md'] == 'user save'
    assert _open_candidates(cell, 'a.md') == [candidate]


def test_a_refused_acknowledged_path_keeps_its_candidates_while_its_sibling_lands(cell, live):
    """The handler refuses the acknowledged write of a.checked: its run
    candidates stay open (nothing replaced the save) and carry no
    replacement mark, and the rest of the write lands."""
    import json
    from collimate_server.database import get_db_connection
    manager = live.start(cell)
    _emit(manager, artifacts=[_mem('a.checked', 'good'), _mem('b.txt', 'old')])
    _accept_file(cell, 'a.checked', 'user save')
    candidate = _candidate(manager, cell, 'a.checked', 'run edit')
    handler('export default input => ({valid:input.text !== "bad",error:"no bad"});')
    current = _current_revision(cell, 'a.checked')
    version, refused = manager._emit_partial(**dict(
        _operation(manager, artifacts=[_mem('a.checked', 'bad'), _mem('b.txt', 'kept')]),
        resolves=[candidate], replaces={'a.checked': current}))
    assert version
    assert refused == [{'path': 'a.checked', 'reason': 'handler_invalid', 'error': 'no bad'}]
    files = _accepted_files(cell)
    assert (files['a.checked'], files['b.txt']) == ('user save', 'kept')
    assert _open_candidates(cell, 'a.checked') == [candidate]
    with get_db_connection(support.CHANNEL) as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='commit' ORDER BY id DESC LIMIT 1",
            (manager.run_id,)).fetchone()[0])
    assert payload['cell_id'] == version
    assert [(c['path'], c.get('replaced')) for c in payload['changes']] == [('b.txt', None)]
