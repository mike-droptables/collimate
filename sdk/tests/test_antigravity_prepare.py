# SPDX-License-Identifier: MIT
"""F1461/F1475: actual Antigravity admission consumes native preparation facts.

The ACP leg runs Google's ``antigravity-acp`` server (settings.json, ``_meta``,
environment); the terminal leg runs the ``agy`` CLI (settings.json, argv)."""
import asyncio
import json
from pathlib import Path
import stat
import threading

import pytest

import _agent_shared as engine
from _agent_shared import acp_run, harness_homes, login_store
from _agent_shared.harnesses.antigravity import backend as agy
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from tests._commands_matrix import TerminalRecorded, recording_runtime  # noqa: F401


def context(**values):
    return {'backend': 'antigravity', 'auth': 'api_key', 'provider': 'gemini',
        'model': 'gemini-3.5-flash-lite', 'commands': 'ask', 'enable_file_tools': True,
        'websearch': False, 'system_prompt': 'native context', 'runtime': 'docker', **values}


async def admit(ctx, leg):
    if leg == 'acp':
        profile = await acp_run._PROFILES['antigravity'](ctx)
        await profile.spawn()
        return profile
    with pytest.raises(TerminalRecorded):
        await engine.surface_runner('antigravity', 'terminal')(ctx, pointer='own.terminal',
            extra_system=ctx['system_prompt'])
    return None


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('file_tools', [False, True])
def test_antigravity_actual_admission_preserves_per_leg_policy_and_selected_api_F1461_F1475(recording_runtime, leg, commands, file_tools):  # noqa: F811
    ctx = context(commands=commands, enable_file_tools=file_tools)
    profile = asyncio.run(admit(ctx, leg))
    assert len(recording_runtime.ensures) == 1
    name, admission = recording_runtime.ensures[0]
    assert name == 'antigravity' and admission['build_dir'] == 'antigravity'
    assert json.loads(admission['readonly_files'][agy.HOOKS_PATH]) == agy.hooks_json(commands)
    home = Path(harness_homes.harness_home('antigravity'))
    assert list(admission['volumes'].values()) == [
        {'bind': f'{agy.ANTIGRAVITY_HOME}/{d}', 'mode': 'rw'} for d in (agy.STATE_DIR, agy.ACP_STATE_DIR)]
    launch = recording_runtime.spawn if leg == 'acp' else recording_runtime.terminal
    variables = launch['env_vars'] if leg == 'acp' else launch['env']
    assert variables['GEMINI_API_KEY'] == 'matrix-fake-key'  # Qualified existing native env route.
    assert 'env_vars' not in admission
    if leg == 'acp':
        # The server's own settings pick the key auth; the CLI's files are not written.
        assert json.loads((home / agy.ACP_STATE_DIR / 'settings.json').read_text()) == {
            'auth': {'type': 'gemini-api-key'}}
        assert not (home / agy.STATE_DIR / 'settings.json').exists()
        assert launch['backend'] == 'antigravity' and launch['argv'] == ['/opt/agy-acp/agy_acp_server.par', '--uid=']
        assert variables['GOOGLE_GEMINI_BASE_URL'] == 'https://generativelanguage.googleapis.com'
        assert not [key for key in variables if key.startswith('COLLIMATE_AGY')]
        assert profile.model_applied is False and profile.fold_system_prompt is False
        unasked = [] if commands == 'allow' else list(agy.SERVER_UNASKED_TOOLS)
        shell = ['run_command'] if commands == 'deny' else []
        assert profile.session_meta == {'antigravity': {
            'disabledTools': ['view_file', 'search_web', 'read_url_content', *unasked, *shell],
            'eagerMcpServers': ['collimate'], 'systemPrompt': {'append': 'native context'}}}
        assert recording_runtime.files.get(agy.CONTEXT_SEAM_PATH) is None
    else:
        settings = json.loads((home / agy.STATE_DIR / 'settings.json').read_text())
        assert settings['modelProvider'] == 'gemini'
        assert settings['trustedWorkspaces'] == ['/workspace']
        assert all(settings[key] == value for key, value in agy.permission_settings(commands).items())
        assert json.loads((home / agy.STATE_DIR / 'cache/onboarding.json').read_text()) == agy.onboarding_json()
        assert launch['argv'] == ['agy', '--model', 'collimate:model:' + ctx['model'], *(['--dangerously-skip-permissions'] if commands == 'allow' else [])]
        assert json.loads(recording_runtime.files[agy.MCP_CONFIG_PATH]) == {'mcpServers': {}}


@pytest.mark.parametrize('websearch,disabled', [(False, ['view_file', 'search_web', 'read_url_content']),
                                                (True, ['view_file'])])
def test_antigravity_acp_withholds_view_file_always_and_web_tools_without_internet_G6(recording_runtime, websearch, disabled):  # noqa: F811
    profile = asyncio.run(admit(context(websearch=websearch, commands='allow'), 'acp'))
    assert profile.session_meta['antigravity']['disabledTools'] == disabled


def test_antigravity_acp_carries_no_system_prompt_key_when_there_is_none_G6(recording_runtime):  # noqa: F811
    profile = asyncio.run(admit(context(system_prompt='  '), 'acp'))
    assert 'systemPrompt' not in profile.session_meta['antigravity']


def test_antigravity_acp_settings_writer_supports_agent_platform_though_no_key_manager_entry_selects_it():
    assert agy.acp_settings_json('agent_platform') == {'auth': {'type': 'agent-platform'}}
    assert agy.acp_settings_json('agent_platform', project='p', location='global') == {
        'auth': {'type': 'agent-platform'}, 'gcp': {'project': 'p', 'location': 'global'}}
    assert agy.acp_settings_json('google') == {'auth': {'type': 'oauth-personal'}}


def test_antigravity_google_terminal_is_this_machines_agy_and_takes_no_lease(recording_runtime, google_lease, monkeypatch):  # noqa: F811
    """The Google plan's terminal is the agy on this machine: no container, no
    lease of the pasted document, no key — and without an agy here, a
    refusal that names the binary."""
    import gen
    monkeypatch.setattr(gen, 'account', lambda role: {'id': 'google-entry', 'metadata': {'harness': 'antigravity',
                                                     'plan': 'google'}, 'service': 'gemini', 'auth_type': 'cli'})
    with pytest.raises(RuntimeError, match="this machine's agy, which was not found"):
        asyncio.run(admit(context(auth='google', model='default'), 'terminal'))
    found = {'path': '/opt/agy/agy', 'version': '1.4.7', 'binary': 'agy'}
    asyncio.run(admit({**context(auth='google', model='default'), 'host_harness': found}, 'terminal'))
    launch = recording_runtime.terminal
    assert launch['container_id'] is None and launch['argv'] == ['/opt/agy/agy']
    assert launch['env']['HOME'].endswith('/host-homes/antigravity/google-entry')
    assert recording_runtime.ensures == [] and 'GEMINI_API_KEY' not in launch['env']


@pytest.mark.parametrize('model', ['', 'default', 'gemini-3.5-flash-lite'])
def test_antigravity_actual_terminal_admission_preserves_blank_default_and_concrete_model_F1475(recording_runtime, model):  # noqa: F811
    asyncio.run(admit(context(model=model), 'terminal'))
    assert recording_runtime.terminal['argv'] == ['agy', *(['--model', 'collimate:model:' + model] if model and model != 'default' else [])]


def test_antigravity_acp_leaves_the_model_to_the_shared_config_option_G9(recording_runtime):  # noqa: F811
    """No static slug table and no env seam: the server lists its models at run
    time and the client's model control picks one; the sentinel keeps the
    server's own pick."""
    profile = asyncio.run(admit(context(model='gemini-3.8-flash'), 'acp'))
    assert profile.model_applied is False and profile.model_value == ''
    assert not [key for key in recording_runtime.spawn['env_vars'] if 'MODEL' in key]
    assert agy.model_matches('gemini-3.8-flash', 'default') is True
    assert agy.model_matches('gemini-3.8-flash', 'gemini-3.7-flash') is False


@pytest.mark.parametrize('leg,operation', [('acp', 'write_acp_settings'), ('acp', 'antigravity_volumes'),
                                           ('terminal', 'write_settings'), ('terminal', 'antigravity_volumes')])
def test_antigravity_actual_admission_offloads_blocking_setup_F1461(recording_runtime, monkeypatch, leg, operation):  # noqa: F811
    original = getattr(agy, operation)
    main_thread = threading.get_ident()
    seen = []
    def checked(*args, **kwargs):
        seen.append(threading.get_ident())
        assert seen[-1] != main_thread, 'blocking native setup ran on event loop thread'
        return original(*args, **kwargs)
    monkeypatch.setattr(agy, operation, checked)
    asyncio.run(admit(context(), leg))
    assert len(seen) == 1


@pytest.mark.parametrize('leg,operation', [('acp', 'write_acp_settings'), ('acp', 'antigravity_volumes'),
                                           ('terminal', 'write_settings'), ('terminal', 'antigravity_volumes')])
def test_antigravity_prestartup_failure_never_reaches_native_admission_F1475(recording_runtime, monkeypatch, leg, operation):  # noqa: F811
    def failed(*args, **kwargs):
        raise OSError('controlled selected-native setup failure')
    monkeypatch.setattr(agy, operation, failed)
    with pytest.raises(OSError, match='controlled selected-native'):
        asyncio.run(admit(context(), leg))
    assert recording_runtime.ensures == [] and recording_runtime.spawn == {} and recording_runtime.terminal == {}


TOKEN = {'type': 'authorized_user', 'client_id': 'id', 'client_secret': 'secret',
         'refresh_token': 'refresh', 'token_uri': 'https://oauth2.googleapis.com/token'}


@pytest.fixture
def google_lease(monkeypatch, tmp_path):
    """The selected google account's leased ``acp_token.json``: ``(source, events)``."""
    import gen
    lease = tmp_path / 'selected-lease'
    source = lease / 'antigravity' / agy.ACP_TOKEN_FILE
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(TOKEN))
    source.chmod(0o600)
    snapshot = {'id': 'selected-account', 'revision': 'one', 'credential_revision': 'one',
        'service': 'gemini', 'auth_type': 'cli', 'metadata': {'harness': 'antigravity', 'plan': 'google'},
        'secrets': {'oauth_token': 'opaque-unused-host-fact'}}
    events = []
    monkeypatch.setattr(gen, 'account', lambda role: snapshot)
    monkeypatch.setattr(gen, 'materialize_account', lambda role: events.append('acquire') or {'path': str(lease), 'lease_id': 'selected-lease'})
    def release(role, identifier):
        events.append(('release', role, identifier, source.read_bytes()))
        return {'committed': True, 'revision': 'two'}
    monkeypatch.setattr(gen, 'release_account', release)
    async def forbidden_key(*args, **kwargs):
        raise AssertionError('Google leg requested an API credential')
    monkeypatch.setattr(_pipeline, 'account_api_key', forbidden_key)
    return source, events


@pytest.fixture
def server_process(monkeypatch):
    """A stand-in server process for the leg's spawn (no container): the
    sign-in guard wraps the real thing for the google plan."""
    from types import SimpleNamespace
    from _agent_shared.acp_client import AdapterProcess

    async def send(message):
        pass

    async def receive():
        await asyncio.Event().wait()

    async def close():
        pass

    process = AdapterProcess(transport=SimpleNamespace(send=send, receive=receive, close=close),
                             kill=lambda: None)
    spawned = []
    async def spawn(backend, target, argv, **kwargs):
        spawned.append({'backend': backend, 'argv': list(argv), **kwargs})
        return process
    monkeypatch.setattr(agy, 'spawn_container_adapter', spawn)
    return spawned, process


async def admit_real_spawn(ctx):
    profile = await acp_run._PROFILES['antigravity'](ctx)
    process = await profile.spawn()
    return profile, process


def test_antigravity_google_admission_copies_the_server_token_and_returns_its_refresh_F1475(recording_runtime, google_lease, server_process):  # noqa: F811
    source, events = google_lease
    original = source.read_bytes()
    refreshed = json.dumps({**TOKEN, 'token': 'new-access-token'}).encode()
    ctx = context(auth='google', model='default')
    asyncio.run(admit_real_spawn(ctx))
    home = Path(harness_homes.harness_home('antigravity'))
    copied = home / agy.ACP_STATE_DIR / agy.ACP_TOKEN_FILE
    assert copied.read_bytes() == original and stat.S_IMODE(copied.stat().st_mode) == 0o600
    copied.write_bytes(refreshed)
    copied.chmod(0o600)
    asyncio.run(admit_real_spawn(ctx))
    assert copied.read_bytes() == refreshed, 'preparing another leg must not overwrite the server refresh'
    assert source.read_bytes() == original and events == ['acquire']
    assert json.loads((home / agy.ACP_STATE_DIR / 'settings.json').read_text()) == {'auth': {'type': 'oauth-personal'}}
    spawned = server_process[0][-1]
    assert 'GEMINI_API_KEY' not in spawned['env_vars'] and 'GOOGLE_GEMINI_BASE_URL' not in spawned['env_vars']
    login_store.close_account_credentials()
    assert source.read_bytes() == refreshed
    assert events == ['acquire', ('release', 'model', 'selected-lease', refreshed)]


def test_antigravity_google_spawn_wraps_the_server_in_the_sign_in_guard(recording_runtime, google_lease, server_process):  # noqa: F811
    async def run():
        _profile, process = await admit_real_spawn(context(auth='google', model='default'))
        try:
            assert isinstance(process.transport, agy.SignInGuard)
        finally:
            await process.transport.close()
    asyncio.run(run())


def test_antigravity_key_spawn_is_not_wrapped_in_the_sign_in_guard(recording_runtime, server_process):  # noqa: F811
    _profile, process = asyncio.run(admit_real_spawn(context()))
    assert process is server_process[1]


def test_antigravity_actual_selected_api_declares_native_model_without_persistent_key_F1475(recording_runtime):  # noqa: F811
    asyncio.run(admit(context(), 'terminal'))
    settings = json.loads((Path(harness_homes.harness_home('antigravity')) / agy.STATE_DIR / 'settings.json').read_text())
    assert settings['customModelsConfig']['customModels']['collimate:model:gemini-3.5-flash-lite'] == {
        'modelName': 'gemini-3.5-flash-lite', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    assert 'env_vars' not in recording_runtime.ensures[0][1]
    assert recording_runtime.terminal['env']['GEMINI_API_KEY'] == 'matrix-fake-key'


def test_antigravity_actual_api_model_refresh_preserves_user_native_map_F1475(recording_runtime):  # noqa: F811
    home = Path(harness_homes.harness_home('antigravity')) / agy.STATE_DIR
    home.mkdir(parents=True, exist_ok=True)
    user = {'modelName': 'user-picked', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI', 'maxTokens': 512}
    data = {'colorScheme': 'user-theme', 'customModelsConfig': {'userNativeFlag': 'unchanged',
        'customModels': {'user-entry': user, 'stale-api-model': {
            'modelName': 'stale-api-model', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}}}}
    (home / 'settings.json').write_text(json.dumps(data))
    asyncio.run(admit(context(), 'terminal'))
    got = json.loads((home / 'settings.json').read_text())
    assert got['colorScheme'] == 'user-theme'
    assert got['customModelsConfig']['userNativeFlag'] == 'unchanged'
    assert got['customModelsConfig']['customModels']['user-entry'] == user
    assert got['customModelsConfig']['customModels']['stale-api-model'] == data['customModelsConfig']['customModels']['stale-api-model']
    assert got['customModelsConfig']['customModels']['collimate:model:gemini-3.5-flash-lite'] == {
        'modelName': 'gemini-3.5-flash-lite', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}


def test_antigravity_actual_terminal_uses_own_native_bridge_config_F1475(recording_runtime, monkeypatch):  # noqa: F811
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from _agent_shared import cli_container
    closed = []
    @asynccontextmanager
    async def own_bridge(**kwargs):
        try:
            yield SimpleNamespace(url_container='http://host.docker.internal:9876/mcp-private-nonce')
        finally:
            closed.append(True)
    monkeypatch.setattr(cli_container, 'leg_bridge', own_bridge)
    asyncio.run(admit(context(), 'terminal'))
    assert json.loads(recording_runtime.files[agy.MCP_CONFIG_PATH]) == {'mcpServers': {
        'collimate': {'disabled': False, 'serverUrl': 'http://host.docker.internal:9876/mcp-private-nonce'}}}
    assert closed == [True]


@pytest.mark.parametrize('leg', ['acp', 'terminal'])
def test_antigravity_actual_selected_key_is_native_process_only_F1475(recording_runtime, leg):  # noqa: F811
    asyncio.run(admit(context(), leg))
    assert 'env_vars' not in recording_runtime.ensures[0][1], 'Selected key persisted into container admission'
    launch = recording_runtime.spawn if leg == 'acp' else recording_runtime.terminal
    assert (launch['env_vars'] if leg == 'acp' else launch['env'])['GEMINI_API_KEY'] == 'matrix-fake-key'


@pytest.mark.parametrize('model', ['selected-native-model', '', 'default'])
def test_antigravity_native_model_map_preserves_plain_foreign_declarations_F1475_P4R04(model):
    from copy import deepcopy
    plain = {'modelName': 'earlier', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    rich = {'modelName': 'user-rich', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI', 'maxTokens': 1024}
    foreign = {'modelName': 'other', 'apiProvider': 'API_PROVIDER_GOOGLE_VERTEX'}
    user_mismatch = {'modelName': 'different', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    existing = {'colorScheme': 'user-theme', 'customModelsConfig': {'native-user-setting': 'keep',
        'customModels': {'earlier': plain, 'user-rich': rich, 'other': foreign, 'user-alias': user_mismatch}}}
    saved = deepcopy(existing)
    got = agy.settings_json(existing, commands='ask', model=model)
    assert existing == saved, 'Pure native merge must not mutate caller nested settings'
    entries = got['customModelsConfig']['customModels']
    assert entries['earlier'] == plain
    assert entries['user-rich'] == rich and entries['other'] == foreign and entries['user-alias'] == user_mismatch
    assert got['customModelsConfig']['native-user-setting'] == 'keep' and got['colorScheme'] == 'user-theme'
    if model not in ('', 'default'):
        assert entries['collimate:model:' + model] == {'modelName': model, 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    else:
        assert set(entries) == {'earlier', 'user-rich', 'other', 'user-alias'}
    assert got['modelProvider'] == 'gemini'


@pytest.mark.parametrize('user', [
    {'modelName': 'selected-native-model', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI', 'modelFeatures': {'tools': False}},
    {'modelName': 'selected-native-model', 'apiProvider': 'API_PROVIDER_GOOGLE_VERTEX'},
])
def test_antigravity_selected_richer_or_other_provider_declaration_is_not_overwritten_F1475(user):
    got = agy.settings_json({'customModelsConfig': {'customModels': {'selected-native-model': user}}},
        commands='deny', model='selected-native-model')
    assert got['customModelsConfig']['customModels']['selected-native-model'] == user


@pytest.mark.parametrize('custom', ['opaque-invalid-native-config', {'customModels': ['invalid-native-list']}])
def test_antigravity_model_merge_preserves_unknown_native_config_for_native_validation_F1475(custom):
    got = agy.settings_json({'customModelsConfig': custom}, commands='deny', model='selected-native-model')
    assert got['customModelsConfig'] == custom


@pytest.mark.parametrize('commands', ['deny', 'ask', 'allow'])
@pytest.mark.parametrize('file_tools', [False, True])
def test_antigravity_terminal_rules_independent_of_files_P4F42(recording_runtime, commands, file_tools):  # noqa: F811
    # Exercise the actual terminal preparation and inspect its persisted
    # settings, rather than reproducing permission_settings in the test.
    asyncio.run(admit(context(commands=commands, enable_file_tools=file_tools), 'terminal'))
    home = Path(harness_homes.harness_home('antigravity'))
    settings = json.loads((home / agy.STATE_DIR / 'settings.json').read_text())
    expected = {'deny': {'deny': ['command(*)']}, 'ask': {'ask': ['command(*)']}, 'allow': {}}[commands]
    assert settings['permissions'] == expected
    assert ('--dangerously-skip-permissions' in recording_runtime.terminal['argv']) is (commands == 'allow')


@pytest.mark.parametrize('model', ['next-model', '', 'default'])
def test_antigravity_preserves_foreign_plain_models_and_removes_only_owned_P4R04(model):
    from copy import deepcopy
    def declaration(name):
        return {'modelName': name, 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    foreign = declaration('foreign-stale')
    existing = {'customModelsConfig': {'customModels': {
        'foreign-stale': foreign, 'collimate:model:old-model': declaration('old-model'),
        'collimate:model:modified': {**declaration('modified'), 'maxTokens': 512},
        'collimate:model:mismatch': declaration('different'),
    }, 'userFlag': True}, 'colorScheme': 'person'}
    before = deepcopy(existing)
    got = agy.settings_json(existing, commands='ask', model=model)
    assert existing == before
    entries = got['customModelsConfig']['customModels']
    assert entries['foreign-stale'] == foreign
    assert 'collimate:model:old-model' not in entries
    assert entries['collimate:model:modified'] == before['customModelsConfig']['customModels']['collimate:model:modified']
    assert entries['collimate:model:mismatch'] == declaration('different')
    assert got['customModelsConfig']['userFlag'] is True and got['colorScheme'] == 'person'
    if model not in ('', 'default'):
        assert entries['collimate:model:' + model] == declaration(model)
    else:
        assert len(entries) == 3


def test_antigravity_logs_every_removed_owned_model_after_settings_write_P4R04(tmp_path, monkeypatch):
    def declaration(name):
        return {'modelName': name, 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    path = tmp_path / agy.STATE_DIR / 'settings.json'
    path.parent.mkdir(parents=True)
    original = {'customModelsConfig': {'customModels': {
        'foreign-stale': declaration('foreign-stale'),
        'collimate:model:old-one': declaration('old-one'),
        'collimate:model:old-two': declaration('old-two')}}}
    path.write_text(json.dumps(original))
    logs = []
    def log(message):
        # A removal is reported only when it is present in the committed file.
        entries = json.loads(path.read_text())['customModelsConfig']['customModels']
        assert 'collimate:model:old-one' not in entries and 'collimate:model:old-two' not in entries
        logs.append(message)
    monkeypatch.setattr(agy, '_secret_log', log)
    agy.write_settings(str(tmp_path), commands='ask', model='next-model')
    entries = json.loads(path.read_text())['customModelsConfig']['customModels']
    assert entries['foreign-stale'] == declaration('foreign-stale')
    assert entries['collimate:model:next-model'] == declaration('next-model')
    assert len(logs) == 2
    for key in ('collimate:model:old-one', 'collimate:model:old-two'):
        assert sum(key in message and 'removed' in message for message in logs) == 1
    assert all('foreign-stale' not in message for message in logs)
    logs.clear()
    agy.write_settings(str(tmp_path), commands='ask', model='next-model')
    assert logs == [], 'Retained same-selection entry is not a removal'


def test_antigravity_failed_settings_write_does_not_report_model_removal_P4R04(tmp_path, monkeypatch):
    path = tmp_path / agy.STATE_DIR / 'settings.json'
    path.parent.mkdir(parents=True)
    original = {'customModelsConfig': {'customModels': {'collimate:model:old-model': {
        'modelName': 'old-model', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}}}}
    path.write_text(json.dumps(original))
    logs = []
    monkeypatch.setattr(agy, '_secret_log', logs.append)
    def fail(*args):
        raise OSError('synthetic settings write failure')
    monkeypatch.setattr(agy, '_write_json_private', fail)
    with pytest.raises(OSError, match='synthetic settings write failure'):
        agy.write_settings(str(tmp_path), commands='deny', model='new-model')
    assert logs == [] and json.loads(path.read_text()) == original


def test_antigravity_native_selection_resolves_owned_key_to_selected_provider_model_P4R04(recording_runtime):  # noqa: F811
    home = Path(harness_homes.harness_home('antigravity')) / agy.STATE_DIR
    home.mkdir(parents=True, exist_ok=True)
    selected = context()['model']
    foreign = {'modelName': 'different-foreign-model', 'apiProvider': 'API_PROVIDER_GOOGLE_VERTEX', 'maxTokens': 512}
    (home / 'settings.json').write_text(json.dumps({'customModelsConfig': {'customModels': {selected: foreign}}}))
    asyncio.run(admit(context(), 'terminal'))
    settings = json.loads((home / 'settings.json').read_text())
    entries = settings['customModelsConfig']['customModels']
    native_key = recording_runtime.terminal['argv'][2]
    assert native_key == 'collimate:model:' + selected
    # Native customModelsResolver.resolve looks up its incoming model by map
    # key; constructFromCustomModelConfig sends the declaration's modelName.
    # Source evidence: pinned agy1.2.11 resolver8d38d56-8d38d66, constructor
    # 8d3985e-8d39880, reflected modelName field at offset48.
    assert entries[native_key] == {'modelName': selected, 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}
    assert entries[selected] == foreign, 'Preserved foreign key must not control this run'


def test_antigravity_selected_owned_key_collision_refuses_without_overwrite_P4R04():
    original = {'customModelsConfig': {'customModels': {'collimate:model:selected': {
        'modelName': 'person-edited', 'apiProvider': 'API_PROVIDER_GOOGLE_GEMINI'}}}}
    with pytest.raises(ValueError, match='selected Collimate model entry was modified'):
        agy.settings_json(original, commands='deny', model='selected')
    assert original['customModelsConfig']['customModels']['collimate:model:selected']['modelName'] == 'person-edited'
