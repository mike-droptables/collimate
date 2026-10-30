"""Crash cleanup requires positive ownership and dead-process evidence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_workspace_cleanup as cleanup


def owned_tree(cwd, *, kind='ws'):
    run_id = str(uuid.uuid4())
    directory = cwd / '.collimate' / 'tmp' / f'{kind}_{run_id[:8]}_probe'
    directory.mkdir(parents=True)
    (directory / 'keep.txt').write_text('owned workspace bytes')
    cleanup.stamp_directory(directory, run_id)
    return directory


def change_receipt(directory, **changes):
    marker = directory / cleanup.MARKER
    receipt = json.loads(marker.read_text())
    receipt.update(changes)
    marker.write_text(json.dumps(receipt))


def dead_owner(monkeypatch):
    monkeypatch.setattr(cleanup, '_alive', lambda pid: False)


def test_live_server_retains_its_staged_workspaces(fresh_cwd):
    directory = owned_tree(fresh_cwd)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}
    assert (directory / 'keep.txt').read_text() == 'owned workspace bytes'


def test_unreadable_start_ticks_cannot_disprove_a_live_owner(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    change_receipt(directory, server_start='1234')
    monkeypatch.setattr(cleanup, '_start_ticks', lambda pid: None)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}
    assert directory.exists()


def test_reused_pid_with_distinct_observed_start_can_be_reclaimed(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    change_receipt(directory, server_start='1234')
    monkeypatch.setattr(cleanup, '_start_ticks', lambda pid: '5678')
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 1, 'retained': 0}
    assert not directory.exists()


@pytest.mark.parametrize('changes', [
    {'version': True}, {'version': 2}, {'phase': 'unknown'},
    {'run_id': 'deadbeef-not-a-uuid'}, {'server_pid': 0},
    {'server_pid': -1}, {'server_pid': True}, {'server_pid': 2**64},
    {'server_start': []}, {'server_start': 'unknown'},
    {'process_group': 0}, {'process_group': -1}, {'process_group': True},
    {'process_group': 2**64}, {'data_dir': '/unrelated'},
])
def test_malformed_ownership_is_retained(fresh_cwd, monkeypatch, changes):
    directory = owned_tree(fresh_cwd)
    change_receipt(directory, **changes)
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}
    assert directory.exists()


@pytest.mark.parametrize('phase', ['launching', 'running'])
def test_unresolved_launch_or_missing_running_group_is_retained(fresh_cwd, monkeypatch, phase):
    directory = owned_tree(fresh_cwd)
    change_receipt(directory, phase=phase)
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}


def test_live_group_then_observed_exit_controls_reclamation(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        change_receipt(directory, phase='running', process_group=process.pid)
        dead_owner(monkeypatch)
        assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 1, 'retained': 0}
    assert not directory.exists()


def test_real_dead_server_staged_receipt_is_reclaimed(fresh_cwd):
    directory = owned_tree(fresh_cwd)
    code = "import sys; from run_workspace_cleanup import stamp_directory; stamp_directory(sys.argv[1],sys.argv[2])"
    receipt = json.loads((directory / cleanup.MARKER).read_text())
    env = os.environ | {'PYTHONPATH': str(Path(cleanup.__file__).parent)}
    subprocess.run([sys.executable, '-c', code, str(directory), receipt['run_id']], check=True, env=env)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 1, 'retained': 0}
    assert not directory.exists()


@pytest.mark.parametrize('mount_kind', ['tree', 'child', 'parent', 'unknown'])
def test_container_reference_or_unknown_inventory_retains_tree(fresh_cwd, monkeypatch, mount_kind):
    directory = owned_tree(fresh_cwd)
    dead_owner(monkeypatch)
    if mount_kind == 'unknown':
        monkeypatch.setattr(cleanup, '_container_mounts', lambda: None)
        result = cleanup.reclaim_abandoned_directories()
    else:
        mount = {'tree': directory, 'child': directory / 'child', 'parent': directory.parent}[mount_kind]
        result = cleanup.reclaim_abandoned_directories(container_mounts=[mount])
    assert result == {'reclaimed': 0, 'retained': 1}
    assert directory.exists()


def test_symbolic_directory_or_marker_is_never_followed(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    marker = directory / cleanup.MARKER
    outside = fresh_cwd / 'outside-receipt'
    marker.rename(outside)
    marker.symlink_to(outside)
    link = directory.parent / ('app_' + str(uuid.uuid4())[:8] + '_link')
    link.symlink_to(directory, target_is_directory=True)
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 2}
    assert directory.exists() and outside.exists()


def test_symbolic_private_parent_is_rejected_on_stamp_and_reclaim(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    marker = directory / cleanup.MARKER
    run_id = json.loads(marker.read_text())['run_id']
    outside = fresh_cwd / 'outside-private-parent'
    marker.parent.rename(outside)
    marker.parent.symlink_to(outside, target_is_directory=True)
    original = (outside / marker.name).read_bytes()

    with pytest.raises(OSError):
        cleanup.stamp_directory(directory, run_id, phase='launching')
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}
    assert (outside / marker.name).read_bytes() == original
    assert directory.exists()


def test_symbolic_final_marker_is_rejected_on_stamp(fresh_cwd):
    directory = owned_tree(fresh_cwd)
    marker = directory / cleanup.MARKER
    run_id = json.loads(marker.read_text())['run_id']
    sentinel = fresh_cwd / 'sentinel-receipt'
    marker.rename(sentinel)
    marker.symlink_to(sentinel)

    with pytest.raises(ValueError, match='symbolic marker'):
        cleanup.stamp_directory(directory, run_id, phase='launching')
    assert json.loads(sentinel.read_text())['phase'] == 'staged'
    assert marker.is_symlink()


def test_stamp_pins_private_parent_during_concurrent_swap(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    marker = directory / cleanup.MARKER
    run_id = json.loads(marker.read_text())['run_id']
    detached = fresh_cwd / 'detached-private-parent'
    outside = fresh_cwd / 'outside-private-parent'
    outside.mkdir()
    sentinel = outside / marker.name
    sentinel.write_text('unrelated bytes')
    replace = cleanup.os.replace

    def swap_then_replace(source, target, **kwargs):
        marker.parent.rename(detached)
        marker.parent.symlink_to(outside, target_is_directory=True)
        return replace(source, target, **kwargs)

    monkeypatch.setattr(cleanup.os, 'replace', swap_then_replace)
    cleanup.stamp_directory(directory, run_id, phase='launching')
    assert sentinel.read_text() == 'unrelated bytes'
    assert json.loads((detached / marker.name).read_text())['phase'] == 'launching'


def test_stamp_does_not_follow_fixed_temporary_marker_symlink(fresh_cwd):
    directory = owned_tree(fresh_cwd)
    receipt = json.loads((directory / cleanup.MARKER).read_text())
    sentinel = fresh_cwd / 'sentinel'
    sentinel.write_text('unrelated bytes')
    (directory / (cleanup.MARKER + '.tmp')).symlink_to(sentinel)
    cleanup.stamp_directory(directory, receipt['run_id'], phase='launching')
    assert sentinel.read_text() == 'unrelated bytes'
    assert json.loads((directory / cleanup.MARKER).read_text())['phase'] == 'launching'


def test_unknown_or_truncated_marker_does_not_block_another_cleanup(fresh_cwd, monkeypatch):
    invalid = owned_tree(fresh_cwd)
    valid = owned_tree(fresh_cwd, kind='app')
    (invalid / cleanup.MARKER).write_text('{')
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 1, 'retained': 1}
    assert invalid.exists() and not valid.exists()


def test_unknown_phase_with_dead_group_is_retained(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    change_receipt(directory, phase='unrecognized', process_group=os.getpid())
    dead_owner(monkeypatch)
    def gone(group, signal):
        raise ProcessLookupError()
    monkeypatch.setattr(cleanup.os, 'killpg', gone)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 1}


def test_missing_docker_client_with_configured_daemon_is_unknown(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'unix:///synthetic-daemon.sock')
    monkeypatch.setitem(sys.modules, 'docker', None)
    assert cleanup._container_mounts() is None


def test_symbolic_ancestor_is_not_a_cleanup_root(fresh_cwd, monkeypatch):
    directory = owned_tree(fresh_cwd)
    original = fresh_cwd / '.collimate'
    outside = fresh_cwd / 'elsewhere'
    original.rename(outside)
    original.symlink_to(outside, target_is_directory=True)
    dead_owner(monkeypatch)
    assert cleanup.reclaim_abandoned_directories(container_mounts=[]) == {'reclaimed': 0, 'retained': 0}
    assert directory.exists()


def test_manager_stamps_both_owned_trees_before_and_after_spawn(fresh_cwd):
    workspace = owned_tree(fresh_cwd)
    run_id = json.loads((workspace / cleanup.MARKER).read_text())['run_id']
    app = workspace.parent / f'app_{run_id[:8]}_source'
    source = app / 'synthetic'
    source.mkdir(parents=True)
    manager = SimpleNamespace(app_dir=str(source), workspace_dir=str(workspace), run_id=run_id, process=None)
    cleanup.stamp_manager(manager, 'launching')
    for directory in (app, workspace):
        receipt = json.loads((directory / cleanup.MARKER).read_text())
        assert (receipt['run_id'], receipt['phase'], receipt['process_group']) == (run_id, 'launching', None)
        assert not (directory / '.collimate-run-owner.json').exists()
    manager.process = SimpleNamespace(pid=os.getpid())
    cleanup.stamp_manager(manager, 'running')
    for directory in (app, workspace):
        receipt = json.loads((directory / cleanup.MARKER).read_text())
        assert (receipt['phase'], receipt['process_group']) == ('running', os.getpgrp())
