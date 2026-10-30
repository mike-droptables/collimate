"""Key-manager-owned authentication replaces startup/channel login probing."""
from __future__ import annotations
from datetime import datetime, timedelta
import json
import pytest
from collimate.account_taxonomy import MODEL_HARNESS_RULES, MODEL_LOGIN_PLANS, PLAN_DEFAULT_HARNESS, PLAN_LOGINS
import accounts
import account_adapters
import plan_logins
from test_pytest_accounts import registry as registry

def test_service_catalog_covers_current_auth_families_without_probing(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('implicit service check'))
    rows = {row['id']: row for row in accounts.list_accounts()['services']}
    assert {'openai', 'anthropic', 'gemini', 'github', 'gitlab', 'git', 'copilot', 'xai', 'custom'} <= set(rows)
    assert rows['openai']['auth_types'] == ['api_key', 'cli']
    assert rows['anthropic']['auth_types'] == ['api_key', 'subscription']
    assert rows['copilot']['auth_types'] == ['cli']
    assert rows['custom']['auth_types'] == ['api_key', 'basic', 'none']
    assert not any(auth in {'device', 'token', 'password'} for row in rows.values() for auth in row['auth_types'])
    for row in rows.values():
        for auth in row['auth_types']:
            assert row['help'][auth] and 'Sign in' not in row['help'][auth] and 'Pull' not in row['help'][auth], (row['id'], auth)
    assert 'claude setup-token' in rows['anthropic']['help']['subscription']
    assert 'paste the contents of the login file' in rows['openai']['help']['cli']

def test_plan_login_catalog_names_each_app_file_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('CLINE_PROVIDER_SETTINGS_PATH', str(tmp_path / 'cline.json'))
    monkeypatch.delenv('PI_CODING_AGENT_DIR', raising=False)
    catalog = accounts.list_accounts()['plan_logins']
    assert {k: catalog['codex'][k] for k in ('command', 'store', 'field')} == {'command': 'codex login', 'store': str(tmp_path / 'codex-home' / 'auth.json'), 'field': 'credential_json'}
    assert str(tmp_path / 'codex-home' / 'auth.json') in catalog['codex']['hint']
    assert catalog['opencode']['store'] == str(tmp_path / 'data' / 'opencode' / 'auth.json')
    assert catalog['cline']['store'] == str(tmp_path / 'cline.json')
    assert catalog['pi']['store'].endswith('/.pi/agent/auth.json')
    # Antigravity's Google login is the official ACP server's own token
    # document, and Copilot's is a token no sign-in writes.
    assert catalog['antigravity']['field'] == 'credential_json'
    assert catalog['antigravity']['store'].endswith('/.gemini/antigravity-acp/acp_token.json')
    assert 'own risk' in catalog['antigravity']['hint']
    assert (catalog['copilot']['field'], catalog['copilot']['store']) == ('credential_file', '')
    assert 'claude-code' not in catalog, 'Claude is a pasted token, not a document'
    assert set(catalog) == set(PLAN_LOGINS)

def test_the_key_manager_is_served_the_taxonomy_harnesses_and_plan_defaults(registry):
    listing = accounts.list_accounts()
    assert listing['model_harnesses'] == list(MODEL_HARNESS_RULES)
    assert listing['login_plans'] == {service: {'plan': plan, 'harness': PLAN_DEFAULT_HARNESS[plan]}
                                      for service, plan in MODEL_LOGIN_PLANS.items()}
    # The Copilot plan starts on GitHub's own CLI, as every other plan starts on its vendor's.
    assert listing['login_plans']['copilot'] == {'plan': 'github_copilot', 'harness': 'copilot'}
    services = {row['id'] for row in listing['services']}
    assert set(listing['login_plans']) <= services

def test_pasted_document_does_not_mistake_api_key_for_subscription():
    assert plan_logins.document_problem('{"OPENAI_API_KEY":"key"}', 'codex') is not None
    assert plan_logins.document_problem('{"openai":{"type":"api","key":"key"}}', 'opencode') is not None
    assert plan_logins.document_problem('{"openai":{"type":"oauth","refresh":"refresh"}}', 'opencode') is None

def test_native_auth_routes_belong_only_to_key_manager(client):
    from routes.accounts import router
    routes = {getattr(route, 'path', '') for route in router.routes}
    assert not any('login' in path or 'logout' in path for path in routes), 'credentials are pasted and verified; nothing signs in or out'
    assert {'/api/accounts', '/api/accounts/check', '/api/accounts/{identity}', '/api/accounts/{identity}/models'} == routes


def _jwt(exp):
    import base64
    body = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'h.{body}.s'


def test_plan_login_check_reads_the_saved_document_only(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('a codex login has no API to probe'))
    live = datetime.now().timestamp() + 3600
    saved = accounts.create_account({'name': 'ChatGPT', 'kind': 'model', 'service': 'openai',
        'protocol': 'cli', 'auth_type': 'cli', 'model': 'chosen',
        'metadata': {'harness': 'codex', 'plan': 'chatgpt'},
        'secrets': {'credential_json': json.dumps({'tokens': {'access_token': _jwt(live), 'refresh_token': 'private'}})}})
    result = account_adapters.check_account(accounts.private_account(saved['id']))
    assert result['valid'] is True and 'expires' in result['message'] and 'private' not in result['message']
    expired = json.dumps({'tokens': {'access_token': _jwt(live - 7200), 'refresh_token': 'private'}})
    accounts.update_account(saved['id'], {'revision': accounts.get_account(saved['id'])['revision'], 'secrets': {'credential_json': expired}})
    result = account_adapters.check_account(accounts.private_account(saved['id']))
    assert result['valid'] is True and 'expired' in result['message'] and 'refreshes it on first use' in result['message']
    result = account_adapters.check_draft({'name': 'Fresh', 'kind': 'model', 'service': 'openai', 'protocol': 'cli',
        'auth_type': 'cli', 'model': 'chosen', 'metadata': {'harness': 'codex', 'plan': 'chatgpt'}, 'secrets': {}})
    assert result['valid'] is False and "Paste the app's login document" in result['message']


def test_claude_login_check_is_one_bearer_request(registry, monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs['headers']['Authorization'], kwargs.get('params')))
        return {'data': [{'id': 'claude-x'}]}
    monkeypatch.setattr(account_adapters, 'request_service', request)
    # Verify's claude line probes the machine's `claude`; keep the test off
    # whatever this machine has installed.
    from collimate import host_harness
    monkeypatch.setattr(host_harness, 'probe', lambda harness, **kw: None)
    saved = accounts.create_account({'name': 'Claude', 'kind': 'model', 'service': 'anthropic',
        'protocol': 'cli', 'auth_type': 'subscription', 'model': 'claude-x',
        'metadata': {'harness': 'claude-code', 'plan': 'claude_code'},
        'secrets': {'access_token': 'sk-ant-oat01-private'}})
    snapshot = accounts.private_account(saved['id'])
    result = account_adapters.check_account(snapshot)
    assert result['valid'] is True and 'private' not in result['message']
    assert "'claude-x' is in the service's model list" in result['message'] and 'claude-code can run it' in result['message']
    # One request: the model list answers both the login and the id.
    assert calls == [('GET', 'https://api.anthropic.com/v1/models', 'Bearer sk-ant-oat01-private', {'limit': 100})]
    listed = account_adapters.discover_models(snapshot)
    assert listed['supported'] is True and listed['models'][0]['id'] == 'claude-x'
    assert calls[-1][2] == 'Bearer sk-ant-oat01-private' and len(calls) == 2
    # The login alone (a base account, not a model entry) is the one-token probe.
    base = accounts.private_account(accounts.create_account({'name': 'Claude plan', 'kind': 'account', 'service': 'anthropic',
        'protocol': 'cli', 'auth_type': 'subscription', 'metadata': {'harness': 'claude-code', 'plan': 'claude_code'},
        'secrets': {'access_token': 'sk-ant-oat01-private'}})['id'])
    assert account_adapters.check_account(base)['message'] == 'Claude token verified against the API'
    assert calls[-1][3] == {'limit': 1}


def test_plan_without_a_model_api_offers_the_plan_default(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('no request'))
    saved = accounts.create_account({'name': 'xAI plan', 'kind': 'model', 'service': 'xai',
        'protocol': 'cli', 'auth_type': 'cli', 'model': 'grok-4.6',
        'metadata': {'harness': 'opencode', 'plan': 'xai'},
        'secrets': {'credential_json': '{"xai":{"type":"oauth","refresh":"private"}}'}})
    listed = account_adapters.discover_models(accounts.private_account(saved['id']))
    assert listed['supported'] is False and listed['models'][0]['id'] == 'grok-4.6'
    assert 'private' not in listed['message']


def test_manager_check_never_touches_the_runtime_lease(registry):
    from pathlib import Path
    saved = accounts.create_account({'name': 'ChatGPT', 'kind': 'model', 'service': 'openai',
        'protocol': 'cli', 'auth_type': 'cli', 'model': 'chosen',
        'metadata': {'harness': 'codex', 'plan': 'chatgpt'},
        'secrets': {'credential_json': '{"tokens":{"refresh_token":"first"}}'}})
    snapshot = accounts.private_account(saved['id'])
    running = accounts.materialize_cli_account(snapshot, 'active-run')
    path = Path(running['path']) / 'codex/auth.json'
    try:
        before = path.read_text()
        assert account_adapters.check_account(snapshot)['valid'] is True
        assert path.read_text() == before
        assert accounts.private_account(saved['id'])['credential_generation'] == snapshot['credential_generation']
    finally:
        accounts.release_cli_account(running['lease_id'], 'active-run')
    assert not path.exists()


def test_pasted_plan_document_must_match_the_selected_plan():
    assert plan_logins.document_problem('{"xai":{"type":"oauth","refresh":"private"}}', 'opencode', 'chatgpt') is not None
    assert plan_logins.document_problem('{"openai":{"type":"oauth","refresh":"private"}}', 'opencode', 'chatgpt') is None


def test_a_shared_store_is_checked_under_the_taxonomy_entry_of_the_plan():
    # pi keeps the ChatGPT plan under its own provider id, openai-codex: the
    # entry is the SDK row's store key, the one the generator reads.
    assert PLAN_LOGINS['pi'].store_keys['chatgpt'] == 'openai-codex'
    assert plan_logins.document_problem('{"openai-codex":{"type":"oauth","refresh":"private"}}', 'pi', 'chatgpt') is None
    problem = plan_logins.document_problem('{"openai":{"type":"oauth","refresh":"private"}}', 'pi', 'chatgpt')
    assert problem and "under 'openai-codex'" in problem
    expires = int((datetime.now() + timedelta(days=1)).timestamp() * 1000)
    note = plan_logins.document_note(json.dumps({'openai-codex': {'type': 'oauth', 'expires': expires}}), 'pi', 'chatgpt')
    assert 'the harness refreshes it as it runs' in note
    # Antigravity's token document is an authorized_user JSON, checked by key presence.
    assert PLAN_LOGINS['antigravity'].field == 'credential_json'
    assert plan_logins.document_problem('not json at all', 'antigravity') is not None
    authorized = {'client_id': 'id', 'client_secret': 'private', 'refresh_token': 'private',
                  'token_uri': 'https://oauth2.googleapis.com/token'}
    assert plan_logins.document_problem(json.dumps(authorized), 'antigravity', 'google') is None
    problem = plan_logins.document_problem(json.dumps({**authorized, 'refresh_token': ''}), 'antigravity', 'google')
    assert problem and 'private' not in problem


@pytest.mark.parametrize('operation', ['clear', 'replace'])
def test_manager_auth_change_cannot_be_replaced_by_active_cli_refresh(registry, operation):
    from pathlib import Path
    saved = accounts.create_account({'name': 'ChatGPT', 'kind': 'model', 'service': 'openai',
        'protocol': 'cli', 'auth_type': 'cli', 'model': 'chosen',
        'metadata': {'harness': 'codex'},
        'secrets': {'credential_json': '{"tokens":{"refresh_token":"old"}}'}})
    snapshot = accounts.private_account(saved['id'])
    running = accounts.materialize_cli_account(snapshot, 'active-run')
    document = None if operation == 'clear' else '{"tokens":{"refresh_token":"new-login"}}'
    accounts.update_account(saved['id'], {'revision': saved['revision'], 'secrets': {'credential_json': document}})
    path = Path(running['path']) / 'codex/auth.json'
    path.write_text('{"tokens":{"refresh_token":"old-chain-refresh"}}')
    assert accounts.release_cli_account(running['lease_id'], 'active-run')['committed'] is False
    current = accounts.private_account(saved['id'])
    assert current['credential_generation'] != snapshot['credential_generation']
    if operation == 'clear':
        assert 'credential_json' not in current['secrets']
    else:
        assert 'new-login' in current['secrets']['credential_json']
