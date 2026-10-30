"""Transient command resources must be observed before their turn closes."""
from copy import deepcopy
import subprocess
import sys
import time

import pytest

from tests_live import test_live_generator_tool as live


def test_short_lived_command_container_is_observed_before_completed_turn_D23(monkeypatch, tmp_path):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.15)'])
    row = {'id': 'actual-short-lived-process', 'hostname': str(process.pid),
           'kind': 'agent_sandbox', 'mounts': [{'source': str(tmp_path), 'target': '/work'}]}
    def poll(run):
        assert run == 'actual-parent-run'
        return [row] if process.poll() is None else []
    monkeypatch.setattr(live, '_run_containers', poll)
    try:
        result, observed = live._observed_turn('actual-parent-run', lambda: process.wait(timeout=5))
        assert result == 0 and process.poll() == 0
        assert poll('actual-parent-run') == [], 'Resource has genuinely ended before post-turn inspection'
        matching = [item for item in observed if item['hostname'] == str(process.pid)]
        assert matching == [row]
        assert live._workspace(matching) == tmp_path
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize('damage', ['missed', 'hostname', 'kind', 'mount', 'id', 'duplicate'])
def test_missing_or_wrong_actual_container_identity_never_passes_D23(damage):
    row = {'id': 'actual-id', 'hostname': 'actual-hostname', 'kind': 'agent_sandbox',
           'mounts': [{'source': '/actual/source', 'target': '/work'}]}
    rows = [deepcopy(row)]
    if damage == 'missed':
        rows = []
    elif damage == 'duplicate':
        rows.append({**row, 'id': 'other-id'})
    elif damage == 'hostname':
        rows[0]['hostname'] = 'wrong-hostname'
    elif damage == 'kind':
        rows[0]['kind'] = 'other-kind'
    elif damage == 'mount':
        rows[0]['mounts'] = [{'source': '/actual/source', 'target': '/other'}]
    elif damage == 'id':
        rows[0]['id'] = ''
    with pytest.raises(AssertionError):
        live._observed_container(rows, {'hostname': 'actual-hostname', 'workspace': '/work'}, 'agent_sandbox')


def test_observer_thread_failure_is_not_silently_ignored_D23(monkeypatch):
    calls = []
    def poll(run):
        calls.append(run)
        if len(calls) > 1:
            raise RuntimeError('actual metadata lookup failed')
        return []
    monkeypatch.setattr(live, '_run_containers', poll)
    with pytest.raises(AssertionError, match='observation failed') as caught:
        live._observed_turn('parent', lambda: time.sleep(.05), interval=.005)
    assert isinstance(caught.value.__cause__, RuntimeError)


def test_observer_deadline_is_not_silently_ignored_D23(monkeypatch):
    monkeypatch.setattr(live, '_run_containers', lambda _: [])
    with pytest.raises(AssertionError, match='observation failed') as caught:
        live._observed_turn('parent', lambda: time.sleep(.05), interval=.005, timeout=.01)
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_observer_retains_actual_metadata_snapshot_after_mutation_D23(monkeypatch):
    row = {'id': 'actual-id', 'hostname': 'actual-hostname', 'kind': 'agent_sandbox',
           'mounts': [{'source': '/actual/source', 'target': '/work'}]}
    monkeypatch.setattr(live, '_run_containers', lambda _: [row])
    def operation():
        row['mounts'].clear()
        return 'completed'
    result, observed = live._observed_turn('parent', operation)
    assert result == 'completed' and row['mounts'] == []
    assert observed[0]['mounts'] == [{'source': '/actual/source', 'target': '/work'}]


def _actual_docker_collection(monkeypatch, disappeared_error):
    """Real SDK collection over controlled list/inspect API replies; no daemon."""
    import docker
    client = docker.DockerClient(base_url='unix:///nonexistent-d23-observer', version='1.51')
    def listing(**kwargs):
        assert kwargs['filters'] == {'label': 'com.collimate.run_id=actual-parent-run'}
        return [{'Id': 'removed-id'}, {'Id': 'live-id'}]
    def inspect(identity):
        if identity == 'removed-id':
            raise disappeared_error
        assert identity == 'live-id'
        return {'Id': identity, 'Config': {'Hostname': 'actual-live-host',
                'Labels': {'com.collimate.kind': 'agent_sandbox'}},
                'Mounts': [{'Type': 'bind', 'Source': '/actual/work', 'Destination': '/work'}]}
    def from_env(**kwargs):
        assert kwargs == {'timeout': 2}
        return client
    monkeypatch.setattr(client.api, 'containers', listing)
    monkeypatch.setattr(client.api, 'inspect_container', inspect)
    monkeypatch.setattr(docker, 'from_env', from_env)
    return client


def test_actual_sdk_disappeared_entry_preserves_other_live_identity_D23(monkeypatch):
    import docker
    _actual_docker_collection(monkeypatch, docker.errors.NotFound('removed before inspect'))
    assert live._run_containers('actual-parent-run') == [{
        'id': 'live-id', 'hostname': 'actual-live-host', 'kind': 'agent_sandbox',
        'mounts': [{'source': '/actual/work', 'target': '/work'}]}]


def test_actual_sdk_unexpected_docker_error_is_not_ignored_D23(monkeypatch):
    import docker
    _actual_docker_collection(monkeypatch, docker.errors.APIError('unexpected Docker failure'))
    with pytest.raises(docker.errors.APIError, match='unexpected Docker failure'):
        live._run_containers('actual-parent-run')
