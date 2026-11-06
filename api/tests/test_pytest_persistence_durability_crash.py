"""Native SQLite boundaries and external-parent real HTTP crash/replay."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from persistence_durability_crash import compile_native, run_case


@pytest.fixture(scope='module')
def native_library(tmp_path_factory):
    return compile_native(tmp_path_factory.mktemp('p5-native-library'))


@pytest.mark.parametrize('directory_error', [False, True])
def test_installed_sqlite_native_sync_and_recreated_wal(native_library, tmp_path, directory_error):
    trace = tmp_path / 'native-trace.jsonl'
    with trace.open('wb') as markers:
        env = dict(os.environ, LD_PRELOAD=str(native_library), P5_NATIVE_MARKER_FD=str(markers.fileno()))
        command = [sys.executable, str(Path(__file__).with_name('persistence_durability_native.py')), str(tmp_path / 'db')]
        if directory_error:
            command.append('--directory-error')
        result = subprocess.run(command, env=env, pass_fds=(markers.fileno(),), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    (tmp_path / 'native-observation.json').write_text(json.dumps(observed, indent=2))
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    wal_returns = [r for r in rows if r['boundary'] == 'sqlite_native_sync_return' and r['path_kind'] == 'wal']
    assert len(wal_returns) == 6, 'Installed SQLite sync interception was not observed'
    assert all(r['result'] == 0 and r['inode'] and r['fd'] >= 0 for r in wal_returns)
    assert {r['wal_ordinal'] for r in wal_returns} == {1, 2}
    directory_returns = [r for r in rows if r['boundary'] == 'sqlite_native_sync_return' and r['path_kind'] == 'directory']
    assert len(directory_returns) == 3
    if directory_error:
        assert all(r['result'] == -1 and r['errno'] == 5 for r in directory_returns)
    else:
        assert all(r['result'] == 0 for r in directory_returns)
    assert all(not cycle['wal_before_open'] and not cycle['wal_after_last_close'] for cycle in observed['cycles'])
    assert [cycle['accepted_rows'] for cycle in observed['cycles']] == [[0], [0, 1], [0, 1, 2]]
    assert all(cycle['effective_pragmas']['synchronous'] == 2 for cycle in observed['cycles'])


@pytest.mark.parametrize('kind,boundary,ordinal', [
    ('new', 'cas_temp_written', 1),
    ('new', 'cas_file_sync_returned', 1),
    ('new', 'cas_rename_returned', 1),
    ('new', 'cas_directory_sync_returned', 1),
    ('new', 'sql_transaction_staged', 1),
    ('new', 'sqlite_native_sync_enter', 1),
    ('new', 'sqlite_native_sync_return', 1),
    ('new', 'sqlite_native_sync_enter', 2),
    ('new', 'sqlite_native_sync_return', 2),
    ('new', 'sql_commit_returned', 1),
    ('new', 'client_ack_observed', 1),
    ('dedupe', 'cas_file_sync_returned', 1),
    ('dedupe', 'cas_directory_sync_returned', 1),
    ('dedupe', 'sql_transaction_staged', 1),
    ('dedupe', 'sqlite_native_sync_enter', 2),
    ('dedupe', 'sqlite_native_sync_return', 2),
    ('dedupe', 'sql_commit_returned', 1),
    ('dedupe', 'client_ack_observed', 1),
    ('rename', 'sql_transaction_staged', 1),
    ('rename', 'sqlite_native_sync_enter', 2),
    ('rename', 'sqlite_native_sync_return', 2),
    ('rename', 'sql_commit_returned', 1),
    ('rename', 'client_ack_observed', 1),
    ('conflict', 'sql_transaction_staged', 1),
    ('conflict', 'sqlite_native_sync_enter', 2),
    ('conflict', 'sqlite_native_sync_return', 2),
    ('conflict', 'sql_commit_returned', 1),
    ('conflict', 'client_ack_observed', 1),
    ('global', 'sql_transaction_staged', 1),
    ('global', 'sqlite_native_sync_enter', 2),
    ('global', 'sqlite_native_sync_return', 2),
    ('global', 'sql_commit_returned', 1),
    ('global', 'client_ack_observed', 1),
])
def test_external_parent_kill_exact_receipt_replay(native_library, tmp_path, kind, boundary, ordinal):
    result = run_case(tmp_path, kind, boundary, native_library, ordinal=ordinal)
    assert result['selected_boundary'] == boundary


@pytest.mark.parametrize('kind', ['new', 'rename', 'conflict'])
def test_acknowledged_restart_gc_retains_typed_bodies_and_exact_receipts(native_library, tmp_path, kind):
    result = run_case(tmp_path, kind, 'client_ack_observed', native_library, gc=True)
    assert result['parent_ack_observed']
    assert result['after_gc']
