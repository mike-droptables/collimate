"""P4 known selected credentials never persist through agent-owned sinks."""
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

SECRET = 'selected-test-secret-0123456789'


@pytest.fixture
def hygiene():
    m = importlib.import_module('_agent_shared.secret_hygiene')
    m.clear()
    yield m
    m.clear()


def test_logs_scrub_selected_nested_secrets_without_mutating_snapshot(hygiene, monkeypatch):
    from _agent_shared import logging
    account = {'secrets': {'api_key': SECRET, 'login': json.dumps({'access_token': SECRET + '-nested'})}}
    hygiene.remember_account(account)
    logs = []
    monkeypatch.setattr(logging.gen, 'log', logs.append)
    logging.safe_log('error: ' + SECRET + '-nested')
    assert logs == ['error: [redacted]']
    assert account['secrets']['api_key'] == SECRET


def test_streaming_scrub_every_split_and_json_escape(hygiene):
    secret = SECRET + '"\\\nλ'
    hygiene.remember(secret)
    raw = json.dumps({'text': secret}, ensure_ascii=True).encode()
    for split in range(len(raw) + 1):
        stream = hygiene.ByteScrubber()
        got = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.finish()
        assert json.loads(got)['text'] == '[redacted]'
    hygiene.remember(SECRET)
    stream = hygiene.ByteScrubber()
    assert stream.feed(b'\xff' + SECRET.encode()) + stream.finish() == b'\xff[redacted]'


def test_capture_and_log_exports_are_scrubbed_copies(hygiene, monkeypatch):
    from _agent_shared.harnesses.claude_code import backend as c
    hygiene.remember(SECRET)
    raw = json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': SECRET}]}}) + '\n'
    monkeypatch.setattr(c, 'pull_claude_transcript', lambda *a, **kw: ([{'role': 'assistant', 'content': SECRET}], raw))
    snapshot = c._capture_snapshot('id', store_dir='synthetic', project='/synthetic/workspace')
    assert len(snapshot) == 3
    assert all(SECRET not in value for value in snapshot.values())
    assert SECRET in raw


def test_adapter_stderr_split_andfinal_flush(hygiene, native_bridge):
    from _agent_shared import acp_client as ac
    hygiene.remember(SECRET)
    log = ac.register_adapter_log('synthetic')
    sink = ac._adapter_log_writer(log)
    sink(SECRET[:8].encode())
    sink(SECRET[8:].encode() + b'\xff tail')
    sink.close()
    assert Path(log).read_bytes() == b'[redacted]\xff tail'


def test_session_text_initial_append_and_new_secret_revision(hygiene, tmp_path):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    path = source / 'history.jsonl'
    hygiene.remember(SECRET)
    path.write_text(json.dumps({'text': SECRET}) + '\n')
    hh._copy_session_state(str(source), str(target), 'codex')
    with path.open('a') as stream:
        stream.write(json.dumps({'text': SECRET + '-later'}) + '\n')
    hh._copy_session_state(str(source), str(target), 'codex')
    assert SECRET not in (target / path.name).read_text()
    hygiene.remember('-later')
    hh._copy_session_state(str(source), str(target), 'codex')
    assert '-later' not in (target / path.name).read_text()
    assert SECRET in path.read_text()


def test_contaminated_database_group_omitted_with_old_cache(hygiene, tmp_path):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    for base in (source, target):
        (base / 'sessions.db').write_bytes(b'SQLite format 3\x00clean')
        (base / 'sessions.db-wal').write_bytes(SECRET.encode())
    hygiene.remember(SECRET)
    hh._copy_session_state(str(source), str(target), 'goose')
    assert not (target / 'sessions.db').exists()
    assert not (target / 'sessions.db-wal').exists()
    assert (source / 'sessions.db').exists()


def test_jsonl_with_secret_stays_incremental_and_invalidates_on_rotation(hygiene, tmp_path, monkeypatch):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    path = source / 'history.jsonl'
    path.write_bytes((b'{"text":"' + SECRET.encode() + b'"}\n') * 100000)
    hygiene.remember(SECRET)
    hh._copy_session_state(str(source), str(target), 'codex')
    reads = []
    original = os.pread
    def read(fd, size, offset):
        value = original(fd, size, offset)
        reads.append(len(value))
        return value
    monkeypatch.setattr(os, 'pread', read)
    with path.open('ab') as stream:
        stream.write(b'{"text":"' + SECRET.encode() + b'"}\n')
    hh._copy_session_state(str(source), str(target), 'codex')
    assert sum(reads) < 150000, sum(reads)
    print({'source_bytes': path.stat().st_size, 'append_bytes_read': sum(reads)})
    assert SECRET.encode() not in (target / path.name).read_bytes()


def test_real_host_adapter_stderr_is_scrubbed_and_drained(hygiene, native_bridge):
    import asyncio
    from _agent_shared import acp_client as ac
    hygiene.remember(SECRET)
    source = "import os; os.write(2, b'%s'); os.write(2, b'%s tail'); print('{}')" % (SECRET[:11], SECRET[11:])
    async def exercise():
        adapter = await ac.spawn_host_adapter('synthetic-host', [sys.executable, '-c', source],
                                               env={}, cwd=native_bridge.workspace)
        assert await adapter.transport.receive() == {}
        await adapter.proc.wait()
        await adapter.transport.close()
    asyncio.run(exercise())
    log = Path(native_bridge.workspace, '_agent/acp-synthetic-host.log')
    assert log.read_bytes() == b'[redacted] tail'


def test_refresh_reads_selected_lease_only_and_refuses_link(hygiene, monkeypatch, tmp_path):
    from _agent_shared import login_store as ls
    from collimate.account_taxonomy import PLAN_LOGINS
    root = tmp_path / 'lease'
    root.mkdir()
    harness, login = 'codex', PLAN_LOGINS['codex']
    path = root.joinpath(harness, *login.lease)
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(ls, '_DIRECTORY', str(root))
    monkeypatch.setattr(ls, '_ACCOUNT', {'metadata': {'harness': harness}})
    monkeypatch.setattr(ls, '_COPIES', {})
    monkeypatch.setattr(ls, '_REFRESH_SIGNATURES', {})
    path.write_text(json.dumps({'access_token': SECRET}))
    hygiene.refresh()
    assert hygiene.scrub(SECRET) == '[redacted]'
    rotated = SECRET + '-rotated'
    path.write_text(json.dumps({'refresh_token': rotated}))
    hygiene.refresh()
    assert hygiene.scrub(rotated) == '[redacted]'
    outside = tmp_path / 'unselected.json'
    outside.write_text(json.dumps({'access_token': 'unselected-outside-secret'}))
    path.unlink()
    path.symlink_to(outside)
    hygiene.refresh()
    assert hygiene.scrub('unselected-outside-secret') == 'unselected-outside-secret'


def test_unchanged_transcript_is_scrubbed_when_key_becomes_known(hygiene, native_bridge):
    from _agent_shared import transcript_merge as tm
    from collimate.transcript import Message
    tm.write_merged_transcript([Message(role='assistant', content=SECRET)])
    hygiene.remember(SECRET)
    tm.write_merged_transcript([])
    assert SECRET not in Path(native_bridge.workspace, tm.TERMINAL_TRANSCRIPT).read_text()


def test_jsonl_secret_split_across_sync_and_failed_append_rollback(hygiene, tmp_path, monkeypatch):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    path = source / 'history.jsonl'
    hygiene.remember(SECRET)
    path.write_text('{"text":"' + SECRET[:10])
    hh._copy_session_state(str(source), str(target), 'codex')
    with path.open('a') as out:
        out.write(SECRET[10:] + '"}\n')
    hh._copy_session_state(str(source), str(target), 'codex')
    assert json.loads((target / path.name).read_text()) == {'text': '[redacted]'}
    before = (target / path.name).read_bytes()
    with path.open('ab') as out:
        out.write(b'x' * 150000)
    read = os.pread
    source_inode = path.stat().st_ino
    def fail(fd, size, offset):
        if os.fstat(fd).st_ino == source_inode and offset > 65536:
            raise OSError('synthetic read failure')
        return read(fd, size, offset)
    monkeypatch.setattr(os, 'pread', fail)
    with pytest.raises(OSError, match='synthetic'):
        hh._copy_session_state(str(source), str(target), 'codex')
    assert (target / path.name).read_bytes() == before


def test_selected_secret_revision_scrubs_old_cached_sessions_once(hygiene, tmp_path, monkeypatch):
    from _agent_shared import harness_homes as hh
    state = tmp_path / 'state'
    state.mkdir()
    path = state / 'history.jsonl'
    path.write_text(json.dumps({'old': SECRET}) + '\n')
    hygiene.remember(SECRET)
    hh._scrub_cached_state(str(state), 'codex')
    assert SECRET not in path.read_text()
    monkeypatch.setattr(hh, '_copy_session_state', lambda *a: pytest.fail('unchanged token revision rescanned cache'))
    hh._scrub_cached_state(str(state), 'codex')


@pytest.mark.parametrize('contaminated', [False, True])
def test_real_sqlite_wal_group_preserved_or_omitted_without_vendor_mutation(hygiene, tmp_path, contaminated):
    import sqlite3
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    connection = sqlite3.connect(source / 'sessions.db')
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE history(text)')
        value = SECRET if contaminated else 'clean history'
        connection.execute('INSERT INTO history VALUES (?)', (value,))
        connection.commit()
        hygiene.remember(SECRET)
        hh._copy_session_state(str(source), str(target), 'goose')
        assert connection.execute('SELECT text FROM history').fetchone() == (value,)
        if contaminated:
            assert list(target.iterdir()) == []
        else:
            with sqlite3.connect(target / 'sessions.db') as cached:
                assert cached.execute('SELECT text FROM history').fetchone() == (value,)
    finally:
        connection.close()


def test_opaque_session_blob_omits_without_byte_replacement(hygiene, tmp_path):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    (source / 'sessions').mkdir(parents=True)
    target.mkdir()
    body = b'valid-binary-header' + b'x' * 1000 + SECRET.encode() + b'\x00\xff'
    path = source / 'sessions' / 'opaque.blob'
    path.write_bytes(body)
    hygiene.remember(SECRET)
    hh._copy_session_state(str(source), str(target), 'codex')
    assert not (target / 'sessions' / path.name).exists()
    assert path.read_bytes() == body


@pytest.mark.parametrize('secret', ['"', ':', '\\'])
def test_json_session_scrub_preserves_structure_for_punctuation_credentials(hygiene, tmp_path, secret):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    path = source / 'history.jsonl'
    hygiene.remember(secret)
    raw = json.dumps({'text': secret, 'number': 42}) + '\n'
    path.write_text(raw)
    hh._copy_session_state(str(source), str(target), 'codex')
    assert json.loads((target / path.name).read_text()) == {'text': '[redacted]', 'number': 42}
    with path.open('a') as stream:
        stream.write(raw)
    hh._copy_session_state(str(source), str(target), 'codex')
    assert [json.loads(line) for line in (target / path.name).read_text().splitlines()] == [{'text': '[redacted]', 'number': 42}] * 2


def test_json_stream_and_incremental_lexer_state_at_every_boundary(hygiene, tmp_path):
    from _agent_shared import harness_homes as hh
    secret = SECRET + '\\"λ'
    hygiene.remember(secret)
    raw = json.dumps({'text': secret, 'nested': [secret]}).encode() + b'\n'
    for split in range(len(raw) + 1):
        stream = hygiene.JsonByteScrubber()
        cleaned = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.finish()
        assert json.loads(cleaned) == {'text': '[redacted]', 'nested': ['[redacted]']}
        source, target = tmp_path / f's{split}', tmp_path / f't{split}'
        source.mkdir()
        target.mkdir()
        path = source / 'history.jsonl'
        path.write_bytes(raw[:split])
        hh._copy_session_state(str(source), str(target), 'codex')
        with path.open('ab') as out:
            out.write(raw[split:])
        hh._copy_session_state(str(source), str(target), 'codex')
        assert json.loads((target / path.name).read_bytes()) == {'text': '[redacted]', 'nested': ['[redacted]']}


def test_malformed_plain_session_text_still_scrubs(hygiene, tmp_path):
    from _agent_shared import harness_homes as hh
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir()
    target.mkdir()
    path = source / 'history.jsonl'
    path.write_text(SECRET)
    hygiene.remember(SECRET)
    hh._copy_session_state(str(source), str(target), 'codex')
    assert (target / path.name).read_text() == '[redacted]'


@pytest.mark.parametrize(('secret', 'ordinary'), [
    ('n', '\n'), ('n-example-key', '\n-example-key'),
    ('t-example-key', '\t-example-key'), ('u03bb-example-key', 'λ-example-key'),
    ('03bb-example-key', 'λ-example-key'), ('b-example-key', 'λ-example-key'),
    ('\\', '\n'), ('λ-example-key', 'λ-example-key'),
])
def test_P4F15_json_encoded_character_boundaries_stream_and_checkpoint(hygiene, tmp_path, secret, ordinary):
    from _agent_shared import harness_homes as hh
    hygiene.remember(secret)
    raw = json.dumps({'message': ordinary, 'value': secret}).encode() + b'\n'
    expected = {'message': ordinary.replace(secret, '[redacted]'), 'value': '[redacted]'}
    assert json.loads(hygiene.scrub_persisted('trace.json', raw)) == expected
    for split in range(len(raw) + 1):
        stream = hygiene.JsonByteScrubber()
        cleaned = stream.feed(raw[:split]) + stream.feed(raw[split:]) + stream.finish()
        assert json.loads(cleaned) == expected, split
        source, target = tmp_path / f's{split}', tmp_path / f't{split}'
        source.mkdir()
        target.mkdir()
        path = source / 'history.jsonl'
        path.write_bytes(raw[:split])
        hh._copy_session_state(str(source), str(target), 'codex')
        with path.open('ab') as out:
            out.write(raw[split:])
        hh._copy_session_state(str(source), str(target), 'codex')
        assert json.loads((target / path.name).read_bytes()) == expected, split


def test_nested_session_groups_log_distinct_relative_identity_and_safe_reason_P4R06(hygiene, tmp_path, monkeypatch):
    from _agent_shared import harness_homes as hh, logging
    source, target = tmp_path / 'source', tmp_path / 'target'
    groups = ['sessions/first/sessions.db', 'sessions/second-' + SECRET + '/sessions.db',
              'sessions/clean/sessions.db']
    suffixes = ['', '-wal', '-shm', '-journal']
    originals = {}
    for group in groups:
        for suffix in suffixes:
            relative = group + suffix
            body = (b'SQLite format 3\x00original database' if not suffix else b'original sidecar ' + suffix.encode())
            if group != groups[-1] and suffix == '-wal':
                body += SECRET.encode()
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            originals[relative] = body
            old = target / relative
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(b'old cached group')
    hygiene.remember(SECRET)
    logs = []
    # Keep the actual safe_log scrub boundary, rather than replacing hh._log.
    monkeypatch.setattr(logging.gen, 'log', logs.append)
    hh._copy_session_state(str(source), str(target), 'codex')
    omissions = [message for message in logs if 'omitted session cache group' in message]
    assert len(omissions) == 2
    expected_names = ['sessions/first/sessions.db', 'sessions/second-[redacted]/sessions.db']
    for name in expected_names:
        assert sum(message.startswith(name + ': ') for message in omissions) == 1
    assert all('containing a selected credential' in message for message in omissions)
    assert all(SECRET not in message and str(source) not in message and str(target) not in message for message in logs)
    assert all('sessions/clean/sessions.db' not in message for message in omissions)
    for relative, body in originals.items():
        assert (source / relative).read_bytes() == body, 'Never edit native source database/sidecar bytes'
        if relative.startswith('sessions/clean/'):
            assert (target / relative).read_bytes() == body
        else:
            assert not (target / relative).exists(), 'Omit the entire contaminated database group and old cache'
