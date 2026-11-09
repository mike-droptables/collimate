# SPDX-License-Identifier: MIT
"""P4 final names at real preparation and grant-routing boundaries."""
import asyncio
from pathlib import Path

import pytest
from collimate.permissions import Grant

import _agent_shared as engine
from _agent_shared.harnesses import HARNESS, Terminal, resolve
from _agent_shared.terminal_runner import TerminalRequest, run_terminal
from tests._commands_matrix import recording_runtime  # noqa: F401


def test_claude_code_terminal_prepare_runs_this_machines_cli_with_no_image(recording_runtime, monkeypatch):  # noqa: F811
    import gen
    from _agent_shared.harnesses import image, pipeline
    row = HARNESS['claude-code']
    assert image(row) is None
    monkeypatch.setattr(gen, 'account', lambda role: {'id': 'entry-1', 'secrets': {}})
    cfg = {'backend': 'claude-code', 'auth': 'api_key', 'provider': 'anthropic', 'model': 'haiku',
           'commands': 'ask', 'runtime': 'standard', 'system_prompt': '', 'enable_file_tools': False,
           'websearch': False, 'host_harness': {'path': '/opt/bin/claude', 'version': '2.1.281', 'binary': 'claude'}}
    prepared = asyncio.run(pipeline.prepare(row, cfg, leg='terminal', request=TerminalRequest('', '', '')))
    assert prepared.env_obj is None and recording_runtime.ensures == []
    assert prepared.argv[0] == '/opt/bin/claude'


@pytest.mark.parametrize('backend', ['qwen-code', 'codex'])
def test_registry_real_prepare_uses_backend_image_identity_F1550_F1549_F1561(recording_runtime, backend):  # noqa: F811
    row = HARNESS[backend]
    cfg = {'backend': backend, 'auth': 'api_key', 'provider': 'openrouter',
           'model': 'fixture/model',
           'commands': 'deny', 'runtime': 'docker', 'host_harness': None,
           'system_prompt': '', 'enable_file_tools': False, 'websearch': False}
    kwargs = {'leg': 'terminal'}
    if row.terminal.prepare_with_request:
        kwargs['request'] = TerminalRequest('', '', '')
    from _agent_shared.harnesses import image, pipeline
    prepared = asyncio.run(pipeline.prepare(row, cfg, **kwargs))
    assert prepared.env_obj is recording_runtime
    assert recording_runtime.ensures[-1][0] == backend
    assert recording_runtime.ensures[-1][1]['build_dir'] == backend
    found = image(row)
    assert found.kind == backend and found.folder == f"harnesses/{backend.replace('-', '_')}/image"
    assert found.name_prefix == 'collimate-' + backend
    assert prepared.argv[0] == {'qwen-code': 'qwen', 'codex': 'codex'}[backend]


def test_final_module_targets_and_shared_terminal_have_no_old_dispatch_F1550_F1549_F1561():
    owner = Path(engine.__file__).parent
    for module, folder in [('qwen_backend', 'qwen'), ('claude_terminal', 'claude_terminal')]:
        assert not (owner / (module + '.py')).exists()
        assert not (owner / folder).exists()
    assert not hasattr(engine, 'resolve_runner')
    for backend, row in HARNESS.items():
        assert callable(resolve(row.prepare))
        if row.terminal:
            assert isinstance(row.terminal, Terminal)
            assert callable(resolve(row.terminal.native_context))
            assert engine.surface_runner(backend, 'terminal') is run_terminal


@pytest.mark.parametrize('docker,expected', [('full', 'docker'), ('none', 'standard')])
def test_runtime_config_key_preserves_real_grant_routing_F1428(monkeypatch, docker, expected):
    from collimate import host_harness
    from collimate.account_taxonomy import HARNESS_HOST_BINARY
    def _found(harness, path, version):
        return host_harness.HostHarness(harness, HARNESS_HOST_BINARY[harness], path, version, False, ("/fixture",))
    monkeypatch.setattr(host_harness, 'probe', lambda *args, **kwargs: _found('codex', '/fixture/codex', '0.114.0'))
    cfg = engine.parse_agent_config({"surface": "annotation", 'backend': 'codex'}, grant=Grant(docker=docker, internet='full'), can_ask=True)
    assert cfg['runtime'] == expected
    assert 'placement' not in cfg
    assert engine.run_needs_container('codex', 'chat', runtime=cfg['runtime']) is (docker == 'full')


def test_backend_spec_has_no_dead_terminal_compatibility_surface_F1404_F1550():
    from dataclasses import fields
    from _agent_shared.harnesses import BACKENDS, BackendSpec
    assert 'terminal' not in {field.name for field in fields(BackendSpec)}
    for name, row in HARNESS.items():
        assert BACKENDS[name] is row.spec
        assert not hasattr(BACKENDS[name], 'terminal')
        if row.terminal:
            assert isinstance(row.terminal, Terminal)
            assert engine.surface_runner(name, 'terminal') is run_terminal
