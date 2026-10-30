# SPDX-License-Identifier: MIT
"""F1402/F1425: actual host/image boundaries share the pure adapter owner."""
from __future__ import annotations

import builtins
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

from _agent_shared import adapter_artifacts as owner, cli_container as cc, host_adapters as host
from _agent_shared.harnesses import HARNESS
from _env_shared import git_account, upstream


def test_pure_owner_has_no_harness_or_runtime_import_F1402(monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert not any(word in name for word in ('collimate', '_agent_shared', 'harness', 'gen'))
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    spec = importlib.util.spec_from_file_location('_adapter_owner_isolated', owner.__file__)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert set(module.ADAPTERS) == {'claude-agent-acp', 'codex-acp', 'pi-acp'}


@pytest.mark.parametrize('name', ['codex-acp', 'claude-agent-acp'])
def test_one_row_controls_actual_host_resolution_install_and_executable_F1402_F1425(monkeypatch, tmp_path, name):
    row = replace(owner.ADAPTERS[name], package='@fixture/selected-adapter',
                  executable='selected-adapter', fallback_version='8.9.10')
    monkeypatch.setitem(owner.ADAPTERS, name, row)
    monkeypatch.setattr(host.gen, 'packages', lambda: {})
    monkeypatch.setattr(host.gen, 'grant', lambda: SimpleNamespace(update_packages=False))
    monkeypatch.setattr(upstream, 'latest_version', lambda source, github_token=None: pytest.fail('no network permitted'))
    assert host.resolve_adapter_version(name) == row.fallback_version
    monkeypatch.setattr(host, 'find_host_node', lambda: host.HostNode('/fixture/node', '/fixture/npm', '', '22.22.1'))
    monkeypatch.setattr(host, '_host_node_env', lambda _: {'PATH': '/fixture'})
    monkeypatch.setattr(host, '_log', lambda *args: None)
    monkeypatch.setattr(host, '_PREFLIGHTED', set())
    binary = host.adapter_binary(name, row.fallback_version)
    assert Path(binary).name == row.executable
    calls = []
    def recorded(argv, **kwargs):
        calls.append(argv)
        if argv[0] == '/fixture/npm':
            Path(binary).parent.mkdir(parents=True, exist_ok=True)
            Path(binary).write_text('not an executable; recorded subprocess boundary')
        else:
            assert argv == [binary, '--version']
        return SimpleNamespace(returncode=0, stdout=row.fallback_version, stderr='')
    monkeypatch.setattr(host.subprocess, 'run', recorded)
    assert host.ensure_adapter(name, host.resolve_adapter_version(name)) == binary
    assert calls[0][-1] == row.package + '@' + row.fallback_version
    assert calls[1] == [binary, '--version']


@pytest.mark.parametrize('folder,name', [('codex', 'codex-acp'), ('pi', 'pi-acp')])
def test_one_row_supplies_actual_container_build_fallback_F1425(monkeypatch, folder, name):
    row = replace(owner.ADAPTERS[name], fallback_version='8.9.10')
    monkeypatch.setitem(owner.ADAPTERS, name, row)
    monkeypatch.setattr(cc.gen, 'packages', lambda: {})
    monkeypatch.setattr(cc.gen, 'grant', lambda: SimpleNamespace(update_packages=False))
    monkeypatch.setattr(upstream, 'latest_version', lambda source, github_token=None: pytest.fail('no network permitted'))
    import _env_shared
    calls = []
    monkeypatch.setattr(_env_shared, 'layered_image_spec', lambda *args, **kw: calls.append(kw) or kw)
    cc.image_spec(folder, folder)
    assert calls[0]['build_args'][row.build_arg] == row.fallback_version


@pytest.mark.parametrize('folder,name', [('codex', 'codex-acp'), ('pi', 'pi-acp')])
def test_real_registry_metadata_is_derived_from_artifact_row_F1402(folder, name):
    row = owner.ADAPTERS[name]
    assert HARNESS[folder].artifacts[name] == owner.image_artifact(name)
    assert cc._IMAGE_ARTIFACTS[folder][name] == {'source': 'npm:' + row.package, 'arg': row.build_arg}
    docker = Path(cc.__file__).parent / 'harnesses' / folder / 'image' / 'Dockerfile'
    text = docker.read_text()
    assert 'ARG ' + row.build_arg + '\n' in text
    assert 'ARG ' + row.build_arg + '=' not in text
    assert ': "${' + row.build_arg + ':?cli_container must supply ' + row.build_arg + '}" && npm install' in text
    guard = next(line[4:].split(' && npm install')[0] for line in text.splitlines() if line.startswith('RUN : '))
    missing = subprocess.run(['/bin/sh', '-c', guard], env={}, capture_output=True, text=True)
    assert missing.returncode != 0 and row.build_arg in missing.stderr
    present = subprocess.run(['/bin/sh', '-c', guard], env={row.build_arg: row.fallback_version}, capture_output=True)
    assert present.returncode == 0


@pytest.mark.parametrize('side', ['host', 'container'])
@pytest.mark.parametrize('selection', ['fallback', 'pin', 'cached-latest', 'fresh-latest'])
def test_actual_resolver_precedence_F1425(monkeypatch, side, selection):
    name = 'codex-acp'
    row = owner.ADAPTERS[name]
    packages = {} if selection == 'fallback' else {name: '6.7.8' if selection == 'pin' else 'latest'}
    monkeypatch.setattr(host.gen, 'packages', lambda: packages)
    monkeypatch.setattr(host.gen, 'grant', lambda: SimpleNamespace(update_packages=selection == 'fresh-latest'))
    # Shared actual resolver cache and admission logic; only the registry HTTP boundary is recorded.
    seen = []
    def latest(source, github_token=None):
        seen.append(source)
        assert source == 'npm:' + row.package
        return '7.8.9'
    monkeypatch.setattr(upstream, 'latest_version', latest)
    # Outside a run there is no account to bind; release lookups go anonymous.
    monkeypatch.setattr(git_account, 'release_token', lambda log=None: None)
    if selection == 'cached-latest':
        upstream.remember_resolution('npm:' + row.package, '5.6.7')
    if side == 'host':
        value = host.resolve_adapter_version(name)
    else:
        # Other native CLI latest lookup must remain absent/offline in this focused probe.
        monkeypatch.setattr(cc, '_IMAGE_ARTIFACTS', {'codex': {name: owner.image_artifact(name)}})
        value = cc._requested_versions('codex')[row.build_arg]
    assert value == {'fallback': row.fallback_version, 'pin': '6.7.8', 'cached-latest': '5.6.7', 'fresh-latest': '7.8.9'}[selection]
    assert seen == (['npm:' + row.package] if selection == 'fresh-latest' else [])


def test_host_eligibility_refuses_before_node_or_npm_F1402(monkeypatch):
    monkeypatch.setattr(host, 'find_host_node', lambda: pytest.fail('host ineligible adapter reached node'))
    monkeypatch.setattr(host.subprocess, 'run', lambda *args, **kw: pytest.fail('host ineligible adapter reached npm'))
    for name in ['pi-acp', 'codex', 'unregistered-adapter']:
        with pytest.raises(KeyError):
            host.ensure_adapter(name, '1.2.3')


def test_image_filters_only_host_adapters_and_keeps_unknown_diagnostics_F1402(monkeypatch):
    monkeypatch.setattr(cc.gen, 'packages', lambda: {'claude-agent-acp': '1.2.3', 'unknown-artifact': '1.2.3'})
    monkeypatch.setattr(cc.gen, 'grant', lambda: SimpleNamespace(update_packages=False))
    messages = []
    monkeypatch.setattr(cc, 'safe_log', messages.append)
    result = cc._requested_versions('pi')
    assert result['PI_ACP_VERSION'] == owner.ADAPTERS['pi-acp'].fallback_version
    assert any('unknown-artifact' in line for line in messages)
    assert all('claude-agent-acp' not in line for line in messages)


def test_package_and_build_argument_row_reaches_actual_registry_and_builder_F1402(monkeypatch):
    row = replace(owner.ADAPTERS['codex-acp'], package='@fixture/owned-adapter', build_arg='OWNED_ADAPTER_VERSION')
    monkeypatch.setitem(owner.ADAPTERS, 'codex-acp', row)
    # Import the actual adapter afresh, without replacing the live module used by other tests.
    import _agent_shared.harnesses.codex.adapter as declared
    spec = importlib.util.spec_from_file_location('_agent_shared.harnesses.codex._artifact_registry_probe',
                                                  declared.__file__)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    artifacts = dict(module.ADAPTER.artifacts)
    assert artifacts['codex-acp'] == {'source': 'npm:' + row.package, 'arg': row.build_arg}
    monkeypatch.setattr(cc, '_IMAGE_ARTIFACTS', {'codex': artifacts})
    monkeypatch.setattr(cc.gen, 'packages', lambda: {'codex-acp': '6.7.8'})
    monkeypatch.setattr(cc.gen, 'grant', lambda: SimpleNamespace(update_packages=False))
    import _env_shared
    calls = []
    monkeypatch.setattr(_env_shared, 'layered_image_spec', lambda *args, **kw: calls.append(kw) or kw)
    cc.image_spec('codex', 'codex')
    assert calls[0]['build_args'][row.build_arg] == '6.7.8'
    assert 'CODEX_ACP_VERSION' not in calls[0]['build_args']
