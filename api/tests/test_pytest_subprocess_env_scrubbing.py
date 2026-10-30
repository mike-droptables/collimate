"""A generator's env: the run's identity and grant, and only the selected
account snapshots' credentials."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest


def _mgr(tmp_path, manifest=None, accounts=None):
    from runner_permissions import RunnerPermissions
    app_dir = tmp_path / 'gen'
    app_dir.mkdir(exist_ok=True)
    if manifest is not None:
        (app_dir / 'choice.generator').write_text(manifest)
    return SimpleNamespace(app_dir=str(app_dir), channel='channel', run_id='run-1',
                           permissions=RunnerPermissions.from_definition(None), packages={},
                           runner_id='synthetic-runner', runner_name='Synthetic runner',
                           label='synthetic-runner-run-1', folder='_runs/synthetic-runner-run-1',
                           account_context=accounts or {}, log_buffer=[])


RUNNER_ENV = {'runner': 'synthetic-runner', 'name': 'Synthetic runner',
              'label': 'synthetic-runner-run-1', 'folder': '_runs/synthetic-runner-run-1'}


def _build(manager):
    from process_manager.manager import ProcessManager
    return ProcessManager._build_subprocess_env(manager)


def _account(service='openai', auth='api_key', secrets=None, endpoint=''):
    return {'id': service + '-selected', 'service': service, 'protocol': 'openai' if service == 'openai' else service,
            'auth_type': auth, 'endpoint': endpoint, 'model': 'chosen-model', 'metadata': {}, 'secrets': secrets or {}}


def test_arbitrary_host_secrets_are_scrubbed(tmp_path, monkeypatch):
    for name in ('AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'RANDOM_DEV_VAR'):
        monkeypatch.setenv(name, 'host-secret')
    env = _build(_mgr(tmp_path))
    assert not {'AWS_SECRET_ACCESS_KEY', 'GITHUB_TOKEN', 'OPENAI_API_KEY', 'RANDOM_DEV_VAR'} & env.keys()


def test_child_private_run_directory_is_not_inherited_by_host_runs__D23(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLIMATE_RUN_DIR', '/another-child/private-config')
    assert 'COLLIMATE_RUN_DIR' not in _build(_mgr(tmp_path, 'env_passthrough: [COLLIMATE_RUN_DIR]\n'))


def test_child_ipc_capabilities_are_not_inherited_by_host_runs__D23(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLIMATE_IPC_OPS', '["read_cell_files"]')
    assert 'COLLIMATE_IPC_OPS' not in _build(_mgr(tmp_path, 'env_passthrough: [COLLIMATE_IPC_OPS]\n'))


def test_static_allowlist_and_locale_prefixes_survive(tmp_path, monkeypatch):
    expected = {'HOME': '/home/test', 'PATH': '/usr/local/bin:/usr/bin', 'TZ': 'America/Los_Angeles',
                'XDG_CACHE_HOME': '/tmp/nonsecret-cache', 'COLLIMATE_FOO': 'bar', 'LC_ALL': 'en_US.UTF-8'}
    for name, value in expected.items(): monkeypatch.setenv(name, value)
    env = _build(_mgr(tmp_path))
    assert all(env[name] == value for name, value in expected.items())


def test_selected_snapshot_injects_key_endpoint_without_registry_or_keyring_read(tmp_path, monkeypatch):
    import accounts
    import keystore
    monkeypatch.setattr(accounts, 'resolve_accounts', lambda *a, **k: pytest.fail('snapshot re-resolved'))
    monkeypatch.setattr(keystore, 'get_key', lambda *a, **k: pytest.fail('unexpected keyring read'))
    monkeypatch.setenv('OPENAI_API_KEY', 'wrong-host-key')
    context = {'model': _account(secrets={'api_key': 'selected-key'}, endpoint='https://chosen.example/v1')}
    env = _build(_mgr(tmp_path, accounts=context))
    assert env['OPENAI_API_KEY'] == 'selected-key'
    assert env['OPENAI_BASE_URL'] == 'https://chosen.example/v1'
    assert not any('selected' in name for name in env)


def _git_snapshot(service='github', auth='api_key', secrets=None, endpoint='https://github.com'):
    return {**_account(service, auth, secrets or {'api_key': 'selected-git-token'}, endpoint), 'kind': 'git',
            'capabilities': ['git'], 'metadata': {'author_name': 'Ada Lovelace', 'author_email': 'ada@example.org'}}


def test_git_account_adds_nothing_and_host_tokens_stay_scrubbed(tmp_path, monkeypatch):
    """A bound Git account reaches a run through gen.account("git"), never
    the generator's environment, and the host's own git tokens stay out."""
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GITLAB_TOKEN'):
        monkeypatch.setenv(name, 'host-token')
    env = _build(_mgr(tmp_path, accounts={'git': _git_snapshot()}))
    assert not {'GH_TOKEN', 'GITHUB_TOKEN', 'GITLAB_TOKEN', 'GITLAB_HOST', 'OPENAI_API_KEY'} & env.keys()
    assert 'selected-git-token' not in json.dumps(env)


@pytest.mark.parametrize('snapshot', [
    _git_snapshot(),
    _git_snapshot('gitlab', endpoint='https://gitlab.com'),
    _git_snapshot('git', 'basic', {'password': 'selected-git-token'}, 'https://git.example.org'),
    _git_snapshot('github', 'ssh_key', {'private_key': '-----BEGIN OPENSSH PRIVATE KEY-----\nx\n'}, ''),
])
def test_git_account_exports_nothing(snapshot):
    import accounts
    assert accounts.account_environment({'git': snapshot}) == {}


def test_different_model_roles_do_not_implicitly_choose_one_credential(tmp_path):
    context = {'thinker': _account(secrets={'api_key': 'thinking-key'}),
               'writer': _account(secrets={'api_key': 'writing-key'})}
    assert 'OPENAI_API_KEY' not in _build(_mgr(tmp_path, accounts=context))


def test_empty_credentials_are_not_synthesized(tmp_path):
    assert 'OPENAI_API_KEY' not in _build(_mgr(tmp_path, accounts={'model': _account()}))


def test_claude_subscription_env_has_no_api_billing_key(tmp_path, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ambient-key')
    context = {'model': _account('anthropic', 'subscription', {'access_token': 'subscription-token'})}
    env = _build(_mgr(tmp_path, accounts=context))
    assert env['CLAUDE_CODE_OAUTH_TOKEN'] == 'subscription-token'
    assert 'ANTHROPIC_API_KEY' not in env


def test_native_cli_document_is_available_only_through_account_ipc(tmp_path):
    context = {'model': _account('openai', 'cli', {'credential_json': '{"tokens":{"refresh_token":"private"}}'})}
    env = _build(_mgr(tmp_path, accounts=context))
    assert 'private' not in str(env)
    assert 'OPENAI_API_KEY' not in env


def test_manifest_nonsecret_passthrough_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv('CUSTOM_TOOL_MODE', 'fast')
    monkeypatch.setenv('UNRELATED_VAR', 'omit')
    manifest = 'id: test-gen\nenv_passthrough: [CUSTOM_TOOL_MODE, MISSING_TOOL_VAR]\n'
    env = _build(_mgr(tmp_path, manifest))
    assert env['CUSTOM_TOOL_MODE'] == 'fast'
    assert 'UNRELATED_VAR' not in env and 'MISSING_TOOL_VAR' not in env


def test_passthrough_reads_the_manifest_start_already_loaded(tmp_path, monkeypatch):
    from process_manager.manager import ProcessManager
    monkeypatch.setenv('CUSTOM_TOOL_MODE', 'fast')
    env = ProcessManager._build_subprocess_env(_mgr(tmp_path), {'env_passthrough': ['CUSTOM_TOOL_MODE']})
    assert env['CUSTOM_TOOL_MODE'] == 'fast'


def test_the_static_allowlist_carries_no_harness_flag(tmp_path, monkeypatch):
    # IS_SANDBOX is the claude CLI's own flag. The static allowlist is what
    # every generator needs, so a harness flag is never a row there.
    from process_manager.manager import ProcessManager
    monkeypatch.setenv('IS_SANDBOX', '1')
    assert 'IS_SANDBOX' not in ProcessManager._ENV_ALLOWLIST
    assert 'IS_SANDBOX' not in _build(_mgr(tmp_path, 'id: test-gen\n'))


def test_passthrough_cannot_overwrite_selected_account_key(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'host-key')
    env = _build(_mgr(tmp_path, 'env_passthrough: [OPENAI_API_KEY]\n',
                      {'model': _account(secrets={'api_key': 'selected-key'})}))
    assert env['OPENAI_API_KEY'] == 'selected-key'


@pytest.mark.parametrize('manifest', [None, '{unparsable', 'env_passthrough: invalid'])
def test_absent_or_damaged_manifest_keeps_base_allowlist(tmp_path, monkeypatch, manifest):
    monkeypatch.setenv('HOME', '/home/test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'omit')
    env = _build(_mgr(tmp_path, manifest))
    assert env['HOME'] == '/home/test'
    assert 'AWS_SECRET_ACCESS_KEY' not in env


def test_permission_grant_roundtrip(tmp_path):
    from runner_permissions import RunnerPermissions
    grant = RunnerPermissions.from_definition({'docker': 'full', 'internet': 'full',
                                               })
    manager = _mgr(tmp_path)
    manager.permissions = grant
    encoded = _build(manager)['COLLIMATE_PERMISSIONS']
    assert encoded == grant.to_env()
    assert RunnerPermissions.from_definition(json.loads(encoded)) == grant


def test_host_and_manifest_cannot_override_saved_permission_grant(tmp_path, monkeypatch):
    from runner_permissions import RunnerPermissions
    monkeypatch.setenv('COLLIMATE_PERMISSIONS', '{}')
    manager = _mgr(tmp_path, 'env_passthrough: [COLLIMATE_PERMISSIONS]\n')
    manager.permissions = RunnerPermissions.from_definition({'docker': 'none'})
    env = _build(manager)
    assert env['COLLIMATE_PERMISSIONS'] == manager.permissions.to_env()


def test_absent_saved_execution_fields_do_not_inherit_server_values(tmp_path, monkeypatch):
    names = ['COLLIMATE_PACKAGES', 'COLLIMATE_IPC_HOST']
    for name in [*names, 'COLLIMATE_RUNNER']:
        monkeypatch.setenv(name, 'unrelated-parent-execution')
    manager = _mgr(tmp_path, 'env_passthrough: [' + ', '.join([*names, 'COLLIMATE_RUNNER']) + ']\n')
    env = _build(manager)
    assert not set(names) & env.keys()
    # The run's identity is always its own, never the server's.
    assert json.loads(env['COLLIMATE_RUNNER']) == RUNNER_ENV


def test_every_env_carries_the_run_identity_and_every_grant_axis(tmp_path, monkeypatch):
    # The SDK stops a host run without its run id, channel or grant, or with
    # a grant that is not a JSON object or names an unknown axis, or without
    # its runner identity (label + folder); packages are a JSON object when
    # present. Neither the
    # server's env nor a manifest passthrough may stand in for them.
    from collimate.permissions import AXES, Grant
    from conftest import sdk_grant
    from runner_permissions import UNRESTRICTED, RunnerPermissions
    for name in ('COLLIMATE_RUN_ID', 'COLLIMATE_CHANNEL', 'COLLIMATE_PERMISSIONS'):
        monkeypatch.setenv(name, 'server-value')
    manager = _mgr(tmp_path, 'env_passthrough: [COLLIMATE_RUN_ID, COLLIMATE_CHANNEL]\n')
    proxy = RunnerPermissions.from_definition({
        'docker': 'limited', 'internet': 'proxy', 'internet_allow': ['b.com', 'a.com'],
        'commands': 'ask', 'update_packages': False})
    for grant in (UNRESTRICTED, RunnerPermissions.from_definition({'commands': 'ask'}), proxy):
        manager.permissions = grant
        env = _build(manager)
        assert (env['COLLIMATE_RUN_ID'], env['COLLIMATE_CHANNEL']) == ('run-1', 'channel')
        sent = json.loads(env['COLLIMATE_PERMISSIONS'])
        assert set(sent) == set(AXES)
        assert Grant.from_dict(sent) == sdk_grant(grant)
    manager.packages = {'synthetic-package': 'latest'}
    env = _build(manager)
    assert json.loads(env['COLLIMATE_PACKAGES']) == {'synthetic-package': 'latest'}
    assert json.loads(env['COLLIMATE_RUNNER']) == RUNNER_ENV


def test_a_docker_limited_container_receives_the_run_identity_and_grant(tmp_path):
    # The docker:limited launch forwards the run env minus a host-only denylist;
    # the identity and grant must cross into the container.
    from process_manager.docker_runner import forward_env
    from runner_permissions import RunnerPermissions
    manager = _mgr(tmp_path)
    manager.permissions = RunnerPermissions.from_definition({'docker': 'limited', 'internet': 'proxy'})
    manager.packages = {'synthetic-package': 'latest'}
    env = _build(manager)
    forwarded = forward_env(env, {'COLLIMATE_CONTAINERIZED': '1'})
    for name in ('COLLIMATE_RUN_ID', 'COLLIMATE_CHANNEL', 'COLLIMATE_PERMISSIONS',
                 'COLLIMATE_PACKAGES', 'COLLIMATE_RUNNER'):
        assert forwarded[name] == env[name], name


def test_docker_coordinates_follow_permission_grant(tmp_path, monkeypatch):
    from runner_permissions import UNRESTRICTED, RunnerPermissions
    monkeypatch.setenv('DOCKER_HOST', 'unix:///synthetic.sock')
    manager = _mgr(tmp_path)
    manager.permissions = RunnerPermissions.from_definition({'docker': 'none'})
    assert 'DOCKER_HOST' not in _build(manager)
    manager.permissions = RunnerPermissions.from_definition({'docker': 'full', 'internet': 'full'})
    assert _build(manager)['DOCKER_HOST'] == 'unix:///synthetic.sock'
    manager.permissions = UNRESTRICTED
    assert _build(manager)['DOCKER_HOST'] == 'unix:///synthetic.sock'


def test_server_auth_token_never_reaches_a_generator(tmp_path, monkeypatch):
    # COLLIMATE_TOKEN authenticates the whole API; the COLLIMATE_ prefix
    # passthrough and a manifest's env_passthrough must both leave it out.
    monkeypatch.setenv('COLLIMATE_TOKEN', 'server-bearer-FAKE')
    monkeypatch.setenv('COLLIMATE_CONNECT_TOKEN', 'connect-FAKE')
    monkeypatch.setenv('COLLIMATE_FOO', 'bar')
    env = _build(_mgr(tmp_path, 'env_passthrough: [COLLIMATE_TOKEN, COLLIMATE_CONNECT_TOKEN]\n'))
    assert 'COLLIMATE_TOKEN' not in env and 'COLLIMATE_CONNECT_TOKEN' not in env
    assert env['COLLIMATE_FOO'] == 'bar'
