# SPDX-License-Identifier: MIT
"""Characterize actual backend fixture policies before F1928 consolidation."""
import importlib
import inspect
import os
from types import SimpleNamespace

import pytest

# The fixtures whose terminal runs on this machine name their entry (its id
# keys the CLI's per-account home).
_ACCOUNTS = [
    ('codex', 'codex', 'openai', 'chatgpt', {}),
    ('claude_terminal', 'claude-code', None, None, {'id': 'entry-1'}),
    ('antigravity', 'antigravity', 'gemini', 'google', {'id': 'google-entry', 'protocol': 'cli'}),
    ('cline', 'cline', 'openai', 'chatgpt', {}),
    ('opencode', 'opencode', 'openai', 'chatgpt', {}),
    ('pi', 'pi', 'openai', 'chatgpt', {}),
]


@pytest.mark.parametrize('module_name,harness,service,plan,entry', _ACCOUNTS)
def test_actual_account_fixture_preserves_policy_and_cleanup__F1928(
        request, tmp_path, monkeypatch, module_name, harness, service, plan, entry):
    module = importlib.import_module('tests.test_backend_' + module_name)
    import gen
    from _agent_shared import login_store, harness_homes
    from collimate.account_taxonomy import PLAN_PROVIDERS

    closed = []
    monkeypatch.setattr(login_store, 'close_account_credentials', lambda: closed.append('credentials'))
    monkeypatch.setattr(harness_homes, 'close_harness_homes', lambda: closed.append('homes'))
    function = inspect.unwrap(module.selected_account_runtime)
    kwargs = {name: request.getfixturevalue(name) for name in inspect.signature(function).parameters}
    fixture = function(**kwargs)
    root = next(fixture)
    try:
        assert closed == ['credentials', 'homes']
        assert root == tmp_path / 'account-runtime' and root.is_dir()
        expected = {**entry, 'metadata': {'harness': harness}, 'secrets': {'oauth_token': 'test-subscription'}}
        if service is not None:
            expected.update(service=service, auth_type='cli')
            expected['metadata']['plan'] = plan
        assert gen.account('agent') == expected
        assert gen.materialize_account('agent') == {'path': str(root), 'lease_id': 'test-lease'}
        assert gen.release_account('test-lease') == {'committed': True}
        if module_name in {'opencode', 'pi'}:
            module._select_account_plan('not-a-plan')
            assert gen.account('agent') == expected and len(closed) == 2
            for selected in ('github_copilot', 'xai'):
                assert selected in PLAN_PROVIDERS
                module._select_account_plan(selected)
                snapshot = gen.account('agent')
                assert snapshot['service'] == PLAN_PROVIDERS[selected]
                assert snapshot['metadata'] == {'harness': harness, 'plan': selected}
                assert snapshot['secrets'] == expected['secrets']
            assert closed[-2:] == ['credentials', 'credentials']
    finally:
        assert next(fixture, None) is None
    assert closed[-2:] == ['credentials', 'homes']


@pytest.mark.parametrize('module_name', ['codex', 'antigravity', 'cline', 'opencode', 'pi'])
@pytest.mark.parametrize('existing', [False, True])
def test_actual_cache_fixture_preserves_bridge_ownership__F1928(
        request, tmp_path, monkeypatch, native_bridge, module_name, existing):
    importlib.import_module('tests.test_backend_' + module_name)
    from collimate import native

    monkeypatch.setattr(native, '_bridge', native_bridge if existing else None)
    finalizers = []
    kwargs = {'request': SimpleNamespace(addfinalizer=finalizers.append),
              'monkeypatch': monkeypatch, 'tmp_path': tmp_path}
    from tests.conftest import cache_root
    fixture = cache_root
    root = inspect.unwrap(fixture)(**kwargs)
    bridge = native.current_bridge()
    assert root == tmp_path and os.environ['XDG_CACHE_HOME'] == str(tmp_path)
    assert bridge.cell_id == 'cell-1'
    if existing:
        assert bridge is native_bridge and finalizers == []
    else:
        assert bridge is not native_bridge and len(finalizers) == 1
        assert bridge.workspace == str(tmp_path / 'run-ws')
        finalizers[0]()
        assert bridge._stop_pipe_r == bridge._stop_pipe_w == -1
