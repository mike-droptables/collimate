"""Credential-free checks of actual owner-live fixture startup contracts."""
import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._workspace import GENERATORS

from collimate.permissions import Grant

LIVE = Path(__file__).resolve().parents[1] / "tests_live"


@pytest.fixture(autouse=True)
def _engine_import_path(monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))


def _tree(file):
    return ast.parse((LIVE / file).read_text())


def _literal(file, name):
    node = next(n for n in _tree(file).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
    return ast.literal_eval(node.value)


def test_live_default_grant_has_only_current_axes__D03_D06():
    grant = Grant.from_dict(_literal("conftest.py", "LIVE_GRANT"))
    assert (grant.docker, grant.internet, grant.commands) == ("none", "full", "deny")


def test_live_public_configs_reach_parser_without_removed_settings__D06():
    """The live suite's default Agent config is a one-shot annotation run
    with file tools off, and the parser takes it as written."""
    import _agent_shared as engine

    node = next(n for n in _tree("conftest.py").body
                if isinstance(n, ast.FunctionDef) and n.name == "agent_config")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "live-config-only", "exec"), namespace)
    cfg = namespace["agent_config"]()
    grant = Grant.from_dict({"docker": "none", "internet": "full", "commands": "deny"})
    parsed = engine.parse_agent_config(cfg, grant=grant, can_ask=False)
    assert (parsed["surface"], parsed["session"]) == ("annotation", "oneshot")
    assert parsed["backend"] == "pydantic-ai"
    assert parsed["enable_file_tools"] is False


def test_the_live_chat_config_reaches_the_chat_parser():
    """The live suite's chat runs carry no session: a chat is always
    interactive."""
    import _agent_shared as engine

    node = next(n for n in _tree("conftest.py").body
                if isinstance(n, ast.FunctionDef) and n.name == "agent_config")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "live-config-only", "exec"), namespace)
    cfg = namespace["agent_config"](surface="chat")
    grant = Grant.from_dict({"docker": "none", "internet": "full", "commands": "deny"})
    parsed = engine.parse_agent_config(cfg, grant=grant, can_ask=False)
    assert (parsed["session"], parsed["surface"]) == ("interactive", "chat")


@pytest.mark.parametrize("backend", ["codex", "goose", "opencode"])
@pytest.mark.parametrize("level", ["deny", "ask-reject", "ask-allow", "allow"])
def test_actual_host_commands_fixture_reaches_dispatch_guard__D06(native_bridge, monkeypatch, backend, level):
    _dispatch_host_fixture(native_bridge, monkeypatch, backend, level,
                          "test_host_harness_follows_the_commands_grant")


@pytest.mark.parametrize("answer", ["reject", "allow"])
def test_actual_host_escalation_fixture_reaches_dispatch_guard__D06(native_bridge, monkeypatch, answer):
    _dispatch_host_fixture(native_bridge, monkeypatch, "codex", "ask",
                          "test_host_codex_cards_the_escalation_its_sandbox_blocks")


def _dispatch_host_fixture(native_bridge, monkeypatch, backend, level, function):
    import _agent_shared as engine

    node = next(n for n in _tree("test_live_acp_conformance.py").body
                if isinstance(n, ast.FunctionDef) and n.name == function)
    # Execute only the actual permissions assignment. Importing the live module
    # would evaluate credential gates; running its body would launch a provider.
    bridge = SimpleNamespace(permissions={})
    assignments = [n for n in node.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Attribute) and t.attr == "permissions"
                           and isinstance(t.value, ast.Name) and t.value.id == "native_bridge"
                           for t in n.targets)]
    for assignment in assignments:
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), "live-grant-only", "exec"),
             {"native_bridge": bridge, "level": level})
    native_bridge.permissions = bridge.permissions
    commands = level.partition("-")[0]
    grant = Grant.from_dict(bridge.permissions)
    class DispatchBoundary(Exception):
        pass

    def reached():
        raise DispatchBoundary

    monkeypatch.setattr(engine, "discover_cell_caps", reached)
    with pytest.raises(DispatchBoundary):
        asyncio.run(engine.run_agent(prompt="fixture contract", backend=backend,
                    provider="openrouter", auth="api_key", model="fixture-model",
                    runtime="standard", commands=commands,
                    host_harness={"path": "/captured/vendor-cli"}))
    assert (grant.docker, grant.internet, grant.commands) == ("none", "full", commands)


def _actual_model_selector_namespace():
    import os
    from collimate.account_taxonomy import PROVIDER_DEFAULT_MODELS
    selector = next(node for node in _tree('conftest.py').body
                    if isinstance(node, ast.FunctionDef) and node.name == 'model_for')
    namespace = {'os': os, 'eng': SimpleNamespace(PROVIDER_DEFAULT_MODELS=PROVIDER_DEFAULT_MODELS)}
    exec(compile(ast.Module(body=[selector], type_ignores=[]), 'actual-owner-model-selector', 'exec'), namespace)
    return namespace


def _actual_antigravity_model_namespace(filename):
    """Execute the live selector, if present, with the real model_for owner."""
    namespace = _actual_model_selector_namespace()
    helpers = [node for node in _tree(filename).body
               if isinstance(node, ast.FunctionDef) and node.name == '_antigravity_model']
    exec(compile(ast.Module(body=helpers, type_ignores=[]), 'actual-agy-model-selection', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('override', ['', '   ', 'gemini-3.5-flash-lite', '  gemini-3.5-flash-lite  '])
def test_actual_antigravity_api_roundtrip_honors_only_explicit_model_F1560(monkeypatch, override):
    from copy import deepcopy
    filename = 'test_live_acp_conformance.py'
    node = deepcopy(next(node for node in _tree(filename).body
                         if isinstance(node, ast.FunctionDef) and node.name == 'test_antigravity_acp_round_trip'))
    node.decorator_list = []  # Record the real request boundary without starting a container.
    monkeypatch.setenv('COLLIMATE_LIVE_MODEL_GEMINI', override)
    monkeypatch.setenv('COLLIMATE_LIVE_MODEL_OPENROUTER', 'foreign-model')
    calls = []
    namespace = _actual_antigravity_model_namespace(filename)
    namespace['_container_round_trip'] = lambda *args, **kwargs: calls.append((args, kwargs))
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-agy-acp-request', 'exec'), namespace)
    namespace[node.name](SimpleNamespace(packages={}))
    assert calls == [(('antigravity', 'gemini'), {'model': override.strip() or 'default'})]


@pytest.mark.parametrize('override', ['', '   ', 'gemini-3.5-flash-lite', '  gemini-3.5-flash-lite  '])
@pytest.mark.parametrize('available', [False, True])
def test_actual_antigravity_commands_model_preserves_key_guard_F1560(monkeypatch, override, available):
    filename = 'test_live_acp_conformance.py'
    node = next(node for node in _tree(filename).body
                if isinstance(node, ast.FunctionDef) and node.name == '_commands_leg')
    monkeypatch.setenv('COLLIMATE_LIVE_MODEL_GEMINI', override)
    namespace = _actual_antigravity_model_namespace(filename)
    checked = []
    namespace['has_key'] = lambda provider: checked.append(provider) or available
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-agy-commands-request', 'exec'), namespace)
    assert namespace[node.name]('antigravity') == (
        ('gemini', override.strip() or 'default') if available else ('', ''))
    assert checked == ['gemini']


def test_actual_antigravity_api_roundtrip_pins_acquired_native_versions_F1560(monkeypatch):
    from copy import deepcopy
    filename = 'test_live_acp_conformance.py'
    node = deepcopy(next(n for n in _tree(filename).body
                         if isinstance(n, ast.FunctionDef) and n.name == 'test_antigravity_acp_round_trip'))
    node.decorator_list = []
    namespace = _actual_antigravity_model_namespace(filename)
    namespace['_container_round_trip'] = lambda *args, **kwargs: None
    bridge = SimpleNamespace(packages={})
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-agy-pinned-roundtrip', 'exec'), namespace)
    namespace[node.name](bridge)
    assert bridge.packages == {'agy': '1.4.7', 'mcp-remote': '0.15.2'}


@pytest.mark.parametrize('available,unpinned', [(True, False), (True, True), (False, False)])
def test_actual_antigravity_commands_pins_preserve_key_and_unpinned_guards_F1560(monkeypatch, available, unpinned):
    from collimate import native
    filename = 'test_live_acp_conformance.py'
    tree = _tree(filename)
    table = next(n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == '_COMMANDS_PINS' for t in n.targets))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'_commands_leg', '_container_commands_leg'}]
    bridge = SimpleNamespace(packages={})
    monkeypatch.setattr(native, '_bridge', bridge)
    namespace = _actual_antigravity_model_namespace(filename)
    namespace.update(has_key=lambda provider: available, UNPINNED=unpinned, pytest=pytest)
    exec(compile(ast.Module(body=[table, *functions], type_ignores=[]), 'actual-agy-pinned-command-admission', 'exec'), namespace)
    if available:
        namespace['_container_commands_leg']('antigravity')
    else:
        with pytest.raises(pytest.skip.Exception):
            namespace['_container_commands_leg']('antigravity')
    assert bridge.packages == ({'agy': '1.4.7', 'mcp-remote': '0.15.2'} if available and not unpinned else {})


def _actual_docker_fixture_P4F39(monkeypatch, *, fail_start=False):
    import uuid

    from _agent_shared import acp_run, cli_container

    events, placements = [], []

    class Container:
        id = 'recorded-layout-container'

        def remove(self, *, force):
            assert force
            events.append('remove')
            for source, binding in placements[-1]['volumes'].items():
                if binding['mode'] == 'ro':
                    assert Path(source).exists(), 'Policy files must outlive their container'

    def run(*args, **kwargs):
        placements.append(kwargs)
        if fail_start:
            raise RuntimeError('recorded container start failure')
        return Container()

    async def close_sessions():
        events.append('sessions')

    monkeypatch.setattr(acp_run, 'close_acp_sessions', close_sessions)
    monkeypatch.setattr(cli_container, 'close_all', lambda: events.append('close'))
    bridge = SimpleNamespace(env_close=lambda env_id: {"ok": True, "error": None})
    bridge.stub_env_place = lambda fn: setattr(bridge, 'place', fn)
    bridge.stub_env_exec = lambda fn: setattr(bridge, 'execute', fn)
    client = SimpleNamespace(containers=SimpleNamespace(run=run))
    node = next(n for n in _tree('conftest.py').body
                if isinstance(n, ast.FunctionDef) and n.name == 'docker_env_bridge')
    node.decorator_list = []
    namespace = {'docker_client': lambda: client, 'ensure_image': lambda *args: events.append('image'),
                 'uuid': uuid, 'Path': Path, 'live_container_labels': lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-live-docker-fixture', 'exec'), namespace)
    fixture = namespace['docker_env_bridge'](bridge, SimpleNamespace(run=asyncio.run), monkeypatch)
    assert next(fixture) is bridge
    return fixture, bridge, placements, events


def test_actual_live_readonly_files_scope_mode_and_lifetime_P4F39(tmp_path, monkeypatch):
    fixture, bridge, placements, events = _actual_docker_fixture_P4F39(monkeypatch)
    try:
        ack = bridge.place({'workspace': str(tmp_path), 'image': 'recorded-image',
                            'readonly_files': {'/etc/run-policy.json': '{"enabled":true}',
                                               '/etc/second-policy.json': 'policy \u2603\n'}})
        mounted = {binding['bind']: (Path(source), binding)
                   for source, binding in placements[-1]['volumes'].items()}
        assert set(mounted) == {'/workspace', '/etc/run-policy.json', '/etc/second-policy.json'}
        for target, content in {'/etc/run-policy.json': '{"enabled":true}',
                                '/etc/second-policy.json': 'policy \u2603\n'}.items():
            path, binding = mounted[target]
            assert binding == {'bind': target, 'mode': 'ro'}
            assert path.read_text(encoding='utf-8') == content
            assert path.stat().st_mode & 0o777 == 0o444
            assert path.parent.stat().st_mode & 0o777 == 0o700
            assert not path.is_relative_to(tmp_path)
        assert ack['container_id'] == 'recorded-layout-container'
        paths = [mounted[target][0] for target in ('/etc/run-policy.json', '/etc/second-policy.json')]
    finally:
        fixture.close()
    assert events[-3:] == ['sessions', 'close', 'remove']
    assert all(not path.exists() and not path.parent.exists() for path in paths)


def test_actual_live_readonly_files_start_failure_cleans_sources_P4F39(tmp_path, monkeypatch):
    fixture, bridge, placements, events = _actual_docker_fixture_P4F39(monkeypatch, fail_start=True)
    try:
        with pytest.raises(RuntimeError, match='recorded container start failure'):
            bridge.place({'workspace': str(tmp_path), 'image': 'recorded-image',
                          'readonly_files': {'/etc/run-policy.json': '{}'}})
        paths = [Path(source) for source, binding in placements[-1]['volumes'].items()
                 if binding['mode'] == 'ro']
        assert len(paths) == 1
        assert all(not path.exists() and not path.parent.exists() for path in paths)
    finally:
        fixture.close()
    assert 'remove' not in events


@pytest.mark.parametrize('readonly,volumes', [
    ([], {}), ({'relative': '{}'}, {}), ({'/etc/policy': {'not': 'text'}}, {}),
    ({'/workspace/policy': '{}'}, {}), ({'/': '{}'}, {}),
    ({'/etc/../policy': '{}'}, {}), ({'/etc/policy:rw': '{}'}, {}),
    ({'/etc/policy': '{}', '/etc/policy/child': '{}'}, {}),
    ({'/vendor/policy': '{}'}, {'source': {'bind': '/vendor', 'mode': 'rw'}}),
    ({'//workspace/policy': '{}'}, {}), ({'//': '{}'}, {}),
])
def test_actual_live_readonly_files_invalid_or_overlapping_refuse_before_placement_P4F39(
        tmp_path, monkeypatch, readonly, volumes):
    fixture, bridge, placements, events = _actual_docker_fixture_P4F39(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match='readonly_files'):
            bridge.place({'workspace': str(tmp_path), 'image': 'recorded-image',
                          'readonly_files': readonly, 'volumes': volumes})
        assert not placements and not events
    finally:
        fixture.close()


@pytest.mark.parametrize('occupied', ['workspace', 'volume'])
def test_actual_live_readonly_files_double_slash_occupied_mount_refuses_overlap_P4F39(
        tmp_path, monkeypatch, occupied):
    fixture, bridge, placements, events = _actual_docker_fixture_P4F39(monkeypatch)
    spec = {'workspace': str(tmp_path), 'image': 'recorded-image'}
    if occupied == 'workspace':
        spec.update(mount='//workspace', readonly_files={'/workspace/policy': '{}'})
    else:
        spec.update(volumes={'source': {'bind': '//vendor', 'mode': 'rw'}},
                    readonly_files={'/vendor/policy': '{}'})
    try:
        with pytest.raises(RuntimeError, match='readonly_files'):
            bridge.place(spec)
        assert not placements and not events
    finally:
        fixture.close()


def _actual_claude_live_namespace(monkeypatch):
    import os
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-selected-api')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'synthetic-selected-plan')
    namespace = {'os': os, 'KEY_ENV': {'anthropic': 'ANTHROPIC_API_KEY'},
                 'PLAN_TOKEN_ENV': {'claude_code': 'CLAUDE_CODE_OAUTH_TOKEN'}}
    owner = _tree('conftest.py')
    funcs = [n for n in owner.body if isinstance(n, ast.FunctionDef)
             and n.name in {'wire_protocol', 'model_account'}]
    exec(compile(ast.Module(body=funcs, type_ignores=[]), 'actual-live-model-account', 'exec'), namespace)
    calls = []
    namespace['bind_model_account'] = calls.append
    namespace['_CLAUDE_MODEL'] = 'haiku'
    tree = _tree('test_live_acp_conformance.py')
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {'_bind_claude_plan', '_bind_claude_account', '_events_collector', '_claude_live_host'}]
    namespace['_host_harness_or_skip'] = lambda backend, commands: {
        'path': '/synthetic/claude', 'binary': 'claude', 'version': '2.1.281'}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), 'actual-claude-live-selection', 'exec'), namespace)
    return namespace, calls


@pytest.mark.parametrize('auth', ['api_key', 'claude_code'])
def test_actual_claude_live_binding_keeps_api_and_setup_token_accounts_distinct_F1560(monkeypatch, auth):
    namespace, calls = _actual_claude_live_namespace(monkeypatch)
    if '_bind_claude_account' in namespace:
        namespace['_bind_claude_account'](auth)
    else:
        namespace['_bind_claude_plan']()
    account = calls[0]
    assert account['service'] == 'anthropic' and account['model'] == 'haiku'
    assert account['metadata'] == ({'harness': 'claude-code'} if auth == 'api_key' else
                                   {'harness': 'claude-code', 'plan': 'claude_code'})
    assert account['secrets'] == ({'api_key': 'synthetic-selected-api'} if auth == 'api_key' else
                                {'oauth_token': 'synthetic-selected-plan'})


@pytest.mark.parametrize('function', ['test_claude_host_locus_round_trip_and_session_record',
    'test_claude_host_locus_session_carries_history', 'test_claude_host_locus_sees_the_collimate_mcp_tools',
    'test_claude_host_locus_cancel_interrupts_the_turn'])
@pytest.mark.parametrize('auth', ['api_key', 'claude_code'])
def test_actual_claude_host_live_request_selects_local_cli_and_named_auth_F1560(monkeypatch, function, auth):
    import gen
    from copy import deepcopy
    node = deepcopy(next(n for n in _tree('test_live_acp_conformance.py').body
                         if isinstance(n, ast.FunctionDef) and n.name == function))
    node.decorator_list = []
    namespace, accounts = _actual_claude_live_namespace(monkeypatch)
    requests = []
    class Boundary(Exception):
        pass
    def request(**kwargs):
        requests.append(kwargs)
        raise Boundary
    namespace.update(eng=SimpleNamespace(run_agent=request), asyncio=__import__('asyncio'),
                     run_on_test_loop=lambda value: value)
    monkeypatch.setattr(gen, 'update_file', lambda *args: None)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-claude-live-host-request', 'exec'), namespace)
    bridge = SimpleNamespace(packages={})
    with pytest.raises(Boundary):
        namespace[function](bridge, **({'auth': auth} if any(a.arg == 'auth' for a in node.args.args) else {}))
    assert requests[0]['auth'] == auth and requests[0].get('provider') == 'anthropic'
    assert requests[0]['model'] == 'haiku'
    assert requests[0].get('runtime') == 'standard'
    assert requests[0].get('host_harness') == {'path': '/synthetic/claude', 'binary': 'claude', 'version': '2.1.281'}
    assert bridge.packages == {'claude-code': '2.1.310', 'claude-agent-acp': '0.95.0'}
    assert accounts[0]['metadata'].get('plan', '') == ('' if auth == 'api_key' else 'claude_code')


def test_actual_claude_live_api_admission_does_not_require_setup_token_F1560():
    tree = _tree('test_live_acp_conformance.py')
    assignment = next(n for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_CLAUDE_AUTH_LEGS' for t in n.targets))
    namespace = {'os': SimpleNamespace(environ={}), 'pytest': pytest,
                 'has_key': lambda provider: provider == 'anthropic'}
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), 'actual-claude-auth-admission', 'exec'), namespace)
    entries = {p.values[0]: p for p in namespace['_CLAUDE_AUTH_LEGS']}
    assert entries['api_key'].marks[0].args == (False,)
    assert entries['claude_code'].marks[0].args == (True,)


@pytest.mark.parametrize('version', ['2.1.281', '', '2.1.280', '2.1.282'])
def test_actual_claude_live_host_version_guard_refuses_unmeasured_binary_F1560(monkeypatch, version):
    namespace, _ = _actual_claude_live_namespace(monkeypatch)
    namespace['_host_harness_or_skip'] = lambda *args: {'path': '/synthetic/claude', 'version': version}
    bridge = SimpleNamespace(packages={})
    if version == '2.1.281':
        assert namespace['_claude_live_host'](bridge)['version'] == version
        assert bridge.packages == {'claude-code': '2.1.310', 'claude-agent-acp': '0.95.0'}
    else:
        with pytest.raises(AssertionError):
            namespace['_claude_live_host'](bridge)
        assert bridge.packages == {}


def _actual_docker_close_fixture_P4F43(monkeypatch, native_bridge):
    """Execute the existing fixture with recorded Docker owners, no daemon."""
    import uuid
    from _agent_shared import acp_run, cli_container
    events, containers = [], []

    class Container:
        def __init__(self, identity):
            self.id = identity
            self.fail = False

        def remove(self, *, force):
            assert force
            events.append(('remove', self.id))
            if self.fail:
                raise RuntimeError('recorded owned close failure')

    def run(*args, **kwargs):
        item = Container('owned-' + str(len(containers)))
        containers.append(item)
        return item

    async def close_sessions():
        events.append(('sessions',))

    monkeypatch.setattr(acp_run, 'close_acp_sessions', close_sessions)
    monkeypatch.setattr(cli_container, 'close_all', lambda: events.append(('cache-sweep',)))
    node = next(n for n in _tree('conftest.py').body
                if isinstance(n, ast.FunctionDef) and n.name == 'docker_env_bridge')
    node.decorator_list = []
    namespace = {'docker_client': lambda: SimpleNamespace(containers=SimpleNamespace(run=run)),
                 'ensure_image': lambda *args: None, 'uuid': uuid,
                 'live_container_labels': lambda: {}, 'Path': Path}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-close-fixture', 'exec'), namespace)
    args = [native_bridge, SimpleNamespace(run=asyncio.run)]
    if any(arg.arg == 'monkeypatch' for arg in node.args.args):
        args.append(monkeypatch)
    fixture = namespace['docker_env_bridge'](*args)
    assert next(fixture) is native_bridge
    return fixture, containers, events


def test_actual_live_owned_close_retires_only_its_environment_P4F43(
        native_bridge, tmp_path, monkeypatch):
    from _agent_shared import cli_container
    from _env_shared import DockerEnvironment
    import gen
    fixture, containers, events = _actual_docker_close_fixture_P4F43(monkeypatch, native_bridge)
    try:
        first = native_bridge.env_place({'workspace': str(tmp_path), 'image': 'recorded'})
        second = native_bridge.env_place({'workspace': str(tmp_path), 'image': 'recorded'})
        env = DockerEnvironment.__new__(DockerEnvironment)
        env._env_id, env._closed = first['env_id'], False
        env._close_lock = threading.Lock()
        env._close_status = None
        cli_container._ENVS['close-control'] = env
        try:
            cli_container.close_env('close-control', env)
            assert events == [('remove', first['env_id'])]
            assert cli_container.cached_env('close-control') is None
            assert env._closed is True
            gen.close_environment(first['env_id'])  # Repeated close stays harmless.
            gen.close_environment('foreign-id')  # Never resolve/remove a foreign Docker owner.
            assert events == [('remove', first['env_id'])]
            assert native_bridge.env_ops[-1] == {'op': 'env_close', 'env_id': 'foreign-id'}
        finally:
            cli_container._ENVS.pop('close-control', None)
        assert second['env_id'] == containers[1].id
    finally:
        fixture.close()
    assert events[-3:] == [('sessions',), ('cache-sweep',), ('remove', second['env_id'])]


def test_actual_live_close_callback_failure_keeps_owner_for_final_sweep_P4F43(
        native_bridge, tmp_path, monkeypatch):
    fixture, containers, events = _actual_docker_close_fixture_P4F43(monkeypatch, native_bridge)
    try:
        placed = native_bridge.env_place({'workspace': str(tmp_path), 'image': 'recorded'})
        containers[0].fail = True
        with pytest.raises(RuntimeError, match='recorded owned close failure'):
            native_bridge.env_close(placed['env_id'])
        assert events == [('remove', placed['env_id'])]
        containers[0].fail = False
    finally:
        fixture.close()
    assert events[-3:] == [('sessions',), ('cache-sweep',), ('remove', placed['env_id'])]
