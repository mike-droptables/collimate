"""Plan logins are PASTED documents (or Claude's pasted token); Verify reads
them and never starts a process."""
from __future__ import annotations

import base64
import inspect
import json
from datetime import datetime

import pytest

from collimate.account_taxonomy import PLAN_LOGINS

import accounts
import account_adapters
import plan_logins
from test_pytest_accounts import registry as registry, model


def _jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return f'h.{body}.s'


def test_plan_login_module_starts_nothing_and_parses_no_cli_output():
    source = inspect.getsource(plan_logins)
    for word in ('subprocess', 'docker', 'Popen', 'pty', 'os.system', 'httpx', 'threading'):
        assert word not in source, word
    assert not hasattr(plan_logins, 'start_login') and not hasattr(plan_logins, 'pull_local_login')


@pytest.mark.parametrize('providers', [None, [], 17, 'private-bad-login'])
def test_malformed_cline_document_returns_invalid_login_P4F32(providers, monkeypatch):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('offline shape validation'))
    account = {'metadata': {'harness': 'cline', 'plan': 'chatgpt'}, 'auth_type': 'cli',
               'secrets': {'credential_json': json.dumps({'providers': providers})}}
    result = plan_logins.check_plan_login(account)
    assert result['valid'] is False
    assert 'not a chatgpt login' in result['message']
    assert 'private-' not in result['message']


def test_codex_document_check_reads_shape_and_expiry_offline(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('a codex login has no API to probe'))
    live = datetime.now().timestamp() + 3600
    entry = model(service='openai', protocol='cli', auth_type='cli', endpoint='', metadata={'harness': 'codex', 'plan': 'chatgpt'},
                  secrets={'credential_json': json.dumps({'tokens': {'access_token': _jwt(live), 'refresh_token': 'private'}})})
    result = account_adapters.check_draft(entry)
    assert result['valid'] is True and 'expires' in result['message'] and 'private' not in result['message']
    # An access token past its expiry is refreshed on first use, not a failure.
    lapsed = {**entry, 'secrets': {'credential_json': json.dumps({'tokens': {'access_token': _jwt(live - 7200), 'refresh_token': 'private'}})}}
    result = account_adapters.check_draft(lapsed)
    assert result['valid'] is True and 'refreshes it on first use' in result['message']
    stale = {**entry, 'secrets': {'credential_json': json.dumps({'tokens': {'refresh_token': 'private'}, 'last_refresh': '2026-01-01T00:00:00Z'})}}
    result = account_adapters.check_draft(stale)
    assert result['valid'] is True and 'has not refreshed this login since 2026-01-01' in result['message']
    token_login = {**entry, 'secrets': {'credential_json': json.dumps({'OPENAI_API_KEY': None, 'personal_access_token': 'at-private'})}}
    result = account_adapters.check_draft(token_login)
    assert result['valid'] is True and 'access-token login' in result['message'] and 'private' not in result['message']
    other_mode = {**entry, 'secrets': {'credential_json': json.dumps({'auth_mode': 'chatgptAuthTokens', 'tokens': {'refresh_token': 'private'}})}}
    assert account_adapters.check_draft(other_mode)['valid'] is False
    foreign = {**entry, 'secrets': {'credential_json': '{"OPENAI_API_KEY":"private-key"}'}}
    result = account_adapters.check_draft(foreign)
    assert result['valid'] is False and 'not a chatgpt login' in result['message']
    empty = {**entry, 'secrets': {}}
    result = account_adapters.check_draft(empty)
    assert result['valid'] is False and "Paste the app's login document" in result['message']


def test_claude_token_is_pasted_and_verified_against_the_api__P6F01(registry, monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((url, kwargs['headers']['Authorization'], kwargs.get('params')))
        return {'data': [{'id': 'claude-x'}]}
    monkeypatch.setattr(account_adapters, 'request_service', request)
    base = {'name': 'Claude plan', 'kind': 'account', 'service': 'anthropic', 'protocol': 'cli', 'auth_type': 'subscription',
            'metadata': {'harness': 'claude-code', 'plan': 'claude_code'}, 'secrets': {'access_token': 'sk-ant-oat01-private'}}
    result = account_adapters.check_draft(base)
    assert result == {'valid': True, 'message': 'Claude token verified against the API'}
    assert calls == [('https://api.anthropic.com/v1/models', 'Bearer sk-ant-oat01-private', {'limit': 1})]
    # No token: the CLI surface runs this machine's claude, which signs in
    # itself; the token is what the Annotation and Chat surfaces need.
    result = account_adapters.check_draft({**base, 'secrets': {}})
    assert result['valid'] is True and 'claude setup-token' in result['message']
    assert "the CLI surface runs Claude Code's claude on this machine, which signs in itself" in result['message']
    saved = accounts.create_account(base)
    assert saved['configured'] is True
    assert account_adapters.discover_models(accounts.private_account(saved['id']))['models'][0]['id'] == 'claude-x'


def test_codex_store_is_the_file_that_exists_on_this_machine(tmp_path, monkeypatch):
    """A Snap install keeps CODEX_HOME in its own home without exporting it;
    the form names the file that is actually there (existence only)."""
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setattr(plan_logins.Path, 'home', staticmethod(lambda: tmp_path))
    codex = PLAN_LOGINS['codex']
    assert plan_logins.store_path(codex) == tmp_path / '.codex' / 'auth.json'
    snap = tmp_path / 'snap' / 'codex' / 'current' / 'auth.json'
    snap.parent.mkdir(parents=True)
    snap.write_text('{}')
    assert plan_logins.store_path(codex) == snap
    (tmp_path / '.codex').mkdir()
    (tmp_path / '.codex' / 'auth.json').write_text('{}')
    assert plan_logins.store_path(codex) == tmp_path / '.codex' / 'auth.json'
    catalog = plan_logins.plan_login_catalog()['codex']
    assert catalog['store'] in catalog['hint'] and '--device-auth' in catalog['hint'] and 'cli_auth_credentials_store' in catalog['hint']


def test_a_cut_off_paste_is_named_as_such(registry, monkeypatch):
    """A file copied from a terminal with wrapping off ends every long line
    in '>' with its tokens truncated; Verify says so instead of 'not a
    login'."""
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('no request'))
    cut = ('{\n  "auth_mode": "chatgpt",\n  "OPENAI_API_KEY": null,\n  "tokens": {\n'
           '    "id_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6Im4wejZQcjEtdEItMTdXb1U0VGM5OHp1RD>\n'
           '    "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6Im4wejZQcjEtdEItMTdXb1U0VGM5OH>\n'
           '    "refresh_token": "rt.1.AAAA>\n    "account_id": "00000000-0000-0000-0000-000000000000"\n  },\n'
           '  "last_refresh": "2026-09-22T21:35:24.688020999Z"\n}')
    entry = model(service='openai', protocol='cli', auth_type='cli', endpoint='', metadata={'harness': 'codex', 'plan': 'chatgpt'},
                  secrets={'credential_json': cut})
    result = account_adapters.check_draft(entry)
    assert result['valid'] is False and 'cut off' in result['message'] and "lines end with '>'" in result['message']
    assert 'AAAA' not in result['message']
    whole = {**entry, 'secrets': {'credential_json': json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
        'tokens': {'id_token': 'a.b.c', 'access_token': 'a.b.c', 'refresh_token': 'rt.1.private', 'account_id': 'id'},
        'last_refresh': '2026-09-22T21:35:24Z'})}}
    result = account_adapters.check_draft(whole)
    assert result['valid'] is True and result['message'].startswith('codex login present') and 'private' not in result['message']
    api_key_file = {**entry, 'secrets': {'credential_json': '{"OPENAI_API_KEY": "sk-private", "tokens": null}'}}
    result = account_adapters.check_draft(api_key_file)
    assert result['valid'] is False and 'API-key file' in result['message'] and 'private' not in result['message']
    hint = plan_logins.plan_login_catalog()['codex']['hint']
    assert 'copy from the opening { to the closing }' in hint



@pytest.fixture
def forbid_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(account_adapters, 'request_service', lambda *args, **kwargs: pytest.fail('offline shape check must never request a provider'))
    monkeypatch.setattr(plan_logins, 'store_path', lambda login: tmp_path / 'synthetic-login.json')

@pytest.mark.parametrize('harness,key', [('opencode', 'openai'), ('pi', 'openai-codex')])
def test_actual_paste_unknown_plan_cannot_borrow_valid_oauth_F1403_F1404_P4F36(harness, key, forbid_requests):
    account = {'service': 'openai', 'auth_type': 'cli',
               'metadata': {'harness': harness, 'plan': 'unsupported-plan'},
               'secrets': {'credential_json': json.dumps({key: {'type': 'oauth'}})}}
    assert plan_logins.check_plan_login(account)['valid'] is False
    account['metadata']['plan'] = ''
    assert plan_logins.check_plan_login(account)['valid'] is True


@pytest.mark.parametrize('token,valid,says', [
    ('github_pat_11SYNTHETIC', True, 'copilot token present'),
    ('ghp_classicsynthetic', False, 'classic personal access token'),
    ('github_pat_ with space', False, 'one line'),
    ('', False, 'No copilot token is entered. Create a fine-grained personal access token'),
])
def test_a_copilot_plan_login_is_a_pasted_token_checked_offline(token, valid, says, monkeypatch):
    """Copilot's plan login is a token the user creates, not a file a
    sign-in writes: the form names no store and Verify checks its kind."""
    monkeypatch.setattr(account_adapters, 'request_service', lambda *a, **k: pytest.fail('offline token check'))
    login = PLAN_LOGINS['copilot']
    assert plan_logins.store_path(login) is None
    row = plan_logins.plan_login_catalog()['copilot']
    assert row['store'] == '' and row['field'] == 'credential_file' and 'Copilot Requests' in row['hint']
    account = {'service': 'copilot', 'auth_type': 'cli', 'metadata': {'harness': 'copilot', 'plan': 'github_copilot'},
               'secrets': {'credential_file': token}}
    result = plan_logins.check_plan_login(account)
    assert result['valid'] is valid and says in result['message']
    assert 'SYNTHETIC' not in result['message'] and 'classicsynthetic' not in result['message']


def test_the_catalog_marks_the_document_a_host_sign_in_makes_optional():
    catalog = plan_logins.plan_login_catalog()
    assert catalog['antigravity']['optional'] is True
    assert 'Optional, for the Annotation and Chat surfaces' in catalog['antigravity']['hint']
    assert 'At your own risk' in catalog['antigravity']['hint']
    assert all(catalog[name]['optional'] is False for name in catalog if name != 'antigravity')
