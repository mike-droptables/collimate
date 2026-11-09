# SPDX-License-Identifier: MIT
"""Characterize both public run entrypoints before F1296 preparation extraction."""
import json
from pathlib import Path
import sys

import pytest

import collimate.testing as testing
from collimate import native


@pytest.fixture(params=['path', 'callable'])
def invoke(request, tmp_path):
    def call(source, **kwargs):
        entrypoint = tmp_path / 'synthetic' / 'entry.py'
        entrypoint.parent.mkdir(exist_ok=True)
        entrypoint.write_text(source)
        if request.param == 'path':
            return testing.run_generator(entrypoint, **kwargs)
        namespace = {}
        exec(compile(source, str(entrypoint), 'exec'), namespace)
        return testing.run_generator_callable(namespace['run'], **kwargs)
    return call


@pytest.fixture
def created(monkeypatch, native_bridge):
    actual = testing.make_test_bridge
    records = []
    def create(workspace, **kwargs):
        bridge = actual(workspace, **kwargs)
        records.append((Path(workspace), bridge))
        return bridge
    monkeypatch.setattr(testing, 'make_test_bridge', create)
    return records


def test_seeded_knobs_and_owned_workspace_cleanup__F1296__P6F01(invoke, created, native_bridge):
    source = '''
def run():
    import gen, json
    seen = {'input': gen.read_file('input.txt'), 'config': gen.config('settings.set_yaml'),
            'folder': gen.config_folder('scripts'), 'prompt': gen.prompt,
            'packages': gen.packages()}
    gen.update_file('seen.json', json.dumps(seen))
'''
    before = list(sys.path)
    result = invoke(source, inputs={'input.txt': 'input'}, config={'settings.set_yaml': {'key': 'value'}},
                    config_folders={'scripts': {'setup.sh': 'echo setup'}}, prompt='prompt',
                    packages={'synthetic-tool': '1.2'}, autocancel_on_serve=False)
    assert result.ok, result.error
    assert json.loads(result.files['seen.json']) == {
        'input': 'input', 'config': {'key': 'value'}, 'folder': {'setup.sh': 'echo setup'},
        'prompt': 'prompt', 'packages': {'synthetic-tool': '1.2'}}
    [record] = created
    workspace, bridge = record
    assert not workspace.exists()
    assert bridge._stop_pipe_r == bridge._stop_pipe_w == -1
    assert native.current_bridge() is native_bridge
    assert sys.path == before


@pytest.mark.parametrize('failure', ['error', 'timeout', 'interrupt'])
def test_failure_restores_bridge_and_closes_resources__F1296__P6F01(invoke, created, native_bridge, failure):
    body = {'error': "raise ValueError('synthetic failure')", 'timeout': 'await asyncio.sleep(10)',
            'interrupt': 'raise KeyboardInterrupt'}[failure]
    source = f'import asyncio\nasync def run():\n    {body}\n'
    if failure == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            invoke(source, timeout=0.01)
    else:
        result = invoke(source, timeout=0.01)
        assert not result.ok
        assert ('synthetic failure' if failure == 'error' else 'timed out after') in result.error
    [(workspace, bridge)] = created
    assert not workspace.exists()
    assert bridge._stop_pipe_r == bridge._stop_pipe_w == -1
    assert native.current_bridge() is native_bridge


def test_external_workspace_survives_and_bad_seed_never_creates_bridge__F1296(
        invoke, created, native_bridge, tmp_path):
    workspace = tmp_path / 'external'
    result = invoke("def run():\n    import gen\n    gen.update_file('output.txt', 'output')\n", workspace=workspace)
    assert result.ok and (workspace / 'output.txt').read_text() == 'output'
    assert len(created) == 1 and created[0][1]._stop_pipe_r == -1
    bad = invoke('def run():\n    pass\n', config={'bad': ['not a mapping']})
    assert not bad.ok and 'workspace seeding failed' in bad.error
    assert len(created) == 1
    assert native.current_bridge() is native_bridge


def test_path_import_and_missing_run_failures_cleanup__F1296(tmp_path, created, native_bridge):
    entrypoint = tmp_path / 'entry.py'
    before = list(sys.path)
    for source, error in [("raise ValueError('bad import')", 'import of entry.py failed'),
                          ('value = 1', 'defines no top-level run()')]:
        entrypoint.write_text(source)
        result = testing.run_generator(entrypoint)
        assert not result.ok and error in result.error
        workspace, bridge = created[-1]
        assert not workspace.exists() and bridge._stop_pipe_r == -1
        assert native.current_bridge() is native_bridge
        assert sys.path == before


def test_execution_restores_absent_bridge__P6F01():
    native._clear_bridge()
    result = testing.run_generator_callable(lambda: None)
    assert result.ok, result.error
    assert native.current_bridge() is None


def test_runtime_support_owners_are_not_generator_exports__P6F01(native_bridge):
    import gen
    for name in ('current_bridge', 'set_bridge', 'require_bridge',
                 'final_flush', 'deliver_permission_answer'):
        assert callable(getattr(native, name))
        assert name not in native.__all__
        with pytest.raises(AttributeError):
            getattr(gen, name)
    for name in ('_set_bridge', '_require_bridge', '_final_flush',
                 '_deliver_permission_answer'):
        assert not hasattr(native, name)
    assert native.current_bridge() is native_bridge
    assert native.require_bridge() is native_bridge
    native.set_bridge(None)
    assert native.current_bridge() is None
    with pytest.raises(RuntimeError, match='outside a generator run'):
        native.require_bridge()
    native.set_bridge(native_bridge)
