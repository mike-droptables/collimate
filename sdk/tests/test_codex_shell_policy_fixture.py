# SPDX-License-Identifier: MIT
"""F1560/OW-P4-01: portable live gate setup, with no native/install/provider run."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


from collimate import host_harness
from tests._commands_matrix import recording_runtime  # noqa: F401
from tests_live import test_live_codex_shell_policy as live
from _agent_shared import host_adapters
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _env_shared import upstream

ACTUAL_ADAPTER_RESOLVER = host_adapters.resolve_adapter_version


def _found(version='0.164.0', *, snap=False):
    return host_harness.HostHarness('codex', 'codex', '/fixture/native-cli/codex', version, snap, ())


@pytest.mark.parametrize('found', [None, _found('', snap=False), _found(snap=True)])
def test_missing_or_unusable_native_cli_is_named_prerequisite_F1560(monkeypatch, found):
    queried = []
    monkeypatch.setattr(host_harness, 'probe', lambda name: queried.append(name) or found)
    monkeypatch.setattr(live.engine, 'run_agent', lambda **kwargs: pytest.fail('missing prerequisite reached native execution'))
    monkeypatch.setattr(host_adapters, 'ensure_adapter', lambda *args: pytest.fail('missing prerequisite reached installer'))
    with pytest.raises(pytest.skip.Exception, match='requires a usable native Codex CLI0.164.0'):
        live.test_actual_codex_adapter_forwards_shell_environment_policy_OW_P4_01(SimpleNamespace(), monkeypatch)
    assert queried == ['codex']


def test_mismatched_actual_native_version_refuses_before_prepare_F1560(monkeypatch):
    monkeypatch.setattr(host_harness, 'probe', lambda name: _found('0.164.1'))
    monkeypatch.setattr(live.engine, 'run_agent', lambda **kwargs: pytest.fail('mismatch reached native execution'))
    monkeypatch.setattr(host_adapters, 'ensure_adapter', lambda *args: pytest.fail('mismatch reached installer'))
    with pytest.raises(pytest.fail.Exception, match='actual probed version is 0.164.1'):
        live.test_actual_codex_adapter_forwards_shell_environment_policy_OW_P4_01(SimpleNamespace(), monkeypatch)


@pytest.mark.parametrize('corruption', [None, 'nonce', 'parent', 'key', 'tool'])
def test_portable_fixture_reaches_actual_prepare_resolver_and_recorded_install_boundary_F1560_OW_P4_01(recording_runtime, monkeypatch, capsys, corruption):  # noqa: F811
    import gen
    queried, installed, prepared_legs = [], [], []
    monkeypatch.setattr(host_harness, 'probe', lambda name: queried.append(name) or _found())
    # These are offline external-operation boundaries, not live-fixture overrides.
    # The production resolve and prepare functions remain genuine.
    def install(name, version):
        installed.append((name, version))
        assert (name, version) == ('codex-acp', '2.2.2')
        return '/fixture/production-cache/codex-acp'
    monkeypatch.setattr(host_adapters, 'ensure_adapter', install)
    monkeypatch.setattr(host_adapters, 'resolve_adapter_version', ACTUAL_ADAPTER_RESOLVER)
    monkeypatch.setattr(upstream, 'latest_version', lambda *args: pytest.fail('explicit pin must never fetch'))
    monkeypatch.setattr(live, 'model_for', lambda provider: 'selected/model')
    monkeypatch.setattr(live, 'bind_model_account', lambda *args: None)
    monkeypatch.setattr(live, 'model_account', lambda *args, **kwargs: object())
    monkeypatch.setattr(live, 'run_on_test_loop', asyncio.run)
    # packages() is the run fact the genuine production adapter resolver reads.
    run = SimpleNamespace(permissions={}, packages={})
    monkeypatch.setattr(host_adapters.gen, 'packages', lambda: run.packages)
    async def recorded_agent(**kwargs):
        assert queried == ['codex'], 'usable public probe was never consulted'
        assert kwargs['host_harness'] == {'path': '/fixture/native-cli/codex', 'version': '0.164.0'}
        assert kwargs['runtime'] == 'standard' and kwargs['commands'] == 'allow'
        prepared = await _pipeline.prepare(_HARNESS['codex'], kwargs)
        prepared_legs.append(prepared)
        assert installed == [('codex-acp', '2.2.2')], 'live fixture replaced production installer routing'
        assert prepared.argv == ('/fixture/production-cache/codex-acp',)
        assert prepared.profile.adapter_version == '2.2.2'
        assert prepared.env['CODEX_PATH'] == '/fixture/native-cli/codex'
        config = json.loads(prepared.env['CODEX_CONFIG'])
        nonce = config['shell_environment_policy']['set']['COLLIMATE_POLICY_ALLOWED']
        assert len(nonce) == 32
        assert prepared.env['COLLIMATE_POLICY_PARENT'] == 'synthetic-parent-value'
        assert config['shell_environment_policy']['inherit'] == 'none'
        assert config['allow_login_shell'] is False
        assert config['features']['shell_snapshot'] is False
        assert 'Use your shell execution tool' in kwargs['prompt']
        # A recorded output satisfies only the fixture contract; no native shell
        # or tool execution is inferred from this synthetic observer file/trace.
        observation = {'allowed': nonce, 'parent_present': False, 'key_names': []}
        if corruption == 'nonce':
            observation['allowed'] = 'wrong-fresh-nonce'
        if corruption == 'parent':
            observation['parent_present'] = True
        if corruption == 'key':
            observation['key_names'] = ['SYNTHETIC_API_KEY']
        Path(gen.workspace_path(), 'codex-shell-policy.json').write_text(json.dumps(observation))
        return SimpleNamespace(trace=[] if corruption == 'tool' else [{'type': 'tool_use'}])
    monkeypatch.setattr(live.engine, 'run_agent', recorded_agent)
    try:
        if corruption is None:
            live.test_actual_codex_adapter_forwards_shell_environment_policy_OW_P4_01(run, monkeypatch)
        else:
            with pytest.raises(AssertionError):
                live.test_actual_codex_adapter_forwards_shell_environment_policy_OW_P4_01(run, monkeypatch)
    except pytest.skip.Exception as exc:
        pytest.fail(f'Portable actual probe was supplied but fixture rejected it: {exc}')
    assert len(prepared_legs) == 1
    assert run.packages == {'codex-acp': '2.2.2'}
    output = capsys.readouterr().out
    assert ('codex0.164.0' in output) is (corruption is None)

