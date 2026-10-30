"""Account catalog characterization; no provider calls."""
import pytest

import account_adapters
import account_catalog as catalog
import accounts
from test_pytest_accounts import registry as registry


def test_catalog_help_and_endpoint_defaults_are_static_F0426_F0455(monkeypatch):
    monkeypatch.setattr(accounts, '_read', lambda: pytest.fail('catalog must not read account authority'))
    monkeypatch.setattr(account_adapters, 'request_service', lambda *args, **kwargs: pytest.fail('catalog must not contact providers'))
    rows = catalog.service_catalog()
    assert len({row['id'] for row in rows}) == len(rows)
    for row in rows:
        assert row['protocols'] == [row['protocol']]
        assert row['help'] == {auth: catalog.auth_help(row['id'], auth) for auth in row['auth_types']}
        assert all(isinstance(help_text, str) and help_text for help_text in row['help'].values())
        assert catalog.endpoint({'service': row['id'], 'endpoint': ''}) == row['endpoint'].rstrip('/')
    assert catalog.endpoint({'service': 'openai', 'endpoint': 'https://synthetic.example/v1/'}) == 'https://synthetic.example/v1'
    assert catalog.endpoint({'service': 'unlisted', 'endpoint': ''}) == ''


def test_registry_consumes_catalog_defaults_and_refuses_unsupported_auth_F0426_F0455(registry):
    row = next(row for row in catalog.service_catalog() if row['id'] == 'tavily')
    saved = accounts.create_account({'name': 'Synthetic search', 'kind': 'account', 'service': row['id'],
                                     'secrets': {'api_key': 'synthetic-key'}})
    assert (saved['protocol'], saved['endpoint'], saved['auth_type'], saved['capabilities']) == (
        row['protocol'], row['endpoint'], row['auth_types'][0], row['capabilities'],
    )
    before = accounts.CONFIG_PATH.read_bytes(), dict(registry)
    with pytest.raises(accounts.AccountError, match='Unsupported authentication'):
        accounts.update_account(saved['id'], {'auth_type': 'basic', 'secrets': {'password': 'never-written'}})
    assert (accounts.CONFIG_PATH.read_bytes(), registry) == before


def test_static_catalog_has_no_runtime_owner_imports_F0426_F0455():
    import ast
    from pathlib import Path

    source = Path(catalog.__file__).read_text()
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))]
    modules = {node.module if isinstance(node, ast.ImportFrom) else alias.name
               for node in imports for alias in node.names}
    assert modules == {'__future__', 'collimate.providers'}
    assert not any(hasattr(account_adapters, name) for name in ('service_catalog', 'auth_help', 'endpoint'))


def test_every_model_provider_is_a_service_F0426():
    from collimate.providers import PROVIDERS
    rows = {row['id']: row for row in catalog.service_catalog()}
    for provider in PROVIDERS.values():
        row = rows[provider.id]
        assert (row['name'], row['endpoint']) == (provider.label, provider.endpoint)
        assert ('api_key' in row['auth_types']) is provider.api_key
        assert bool(set(row['auth_types']) & {'cli', 'subscription'}) is bool(provider.login_plan)
    assert rows['mistral']['protocol'] == 'openai-compatible' and rows['mistral']['auth_types'] == ['api_key']
    assert (rows['copilot']['protocol'], rows['copilot']['auth_types']) == ('cli', ['cli'])
    assert 'Copilot Requests' in rows['copilot']['help']['cli']


def test_git_rows_offer_their_methods_kinds_and_labels():
    from collimate import git_hosts
    rows = {row['id']: row for row in catalog.service_catalog()}
    assert tuple(row['id'] for row in catalog.service_catalog() if 'git' in row['kinds']) == git_hosts.GIT_SERVICES
    expected = {
        'github': (['api_key', 'ssh_key'], 'https://github.com',
                   {'api_key': 'Personal access token', 'ssh_key': 'SSH key'}),
        'gitlab': (['api_key', 'ssh_key'], 'https://gitlab.com',
                   {'api_key': 'Personal access token', 'ssh_key': 'SSH key'}),
        'git': (['ssh_key', 'basic', 'none'], '',
                {'basic': 'Username and token', 'ssh_key': 'SSH key', 'none': 'Public (no credentials)'}),
    }
    for service, (auth_types, endpoint, labels) in expected.items():
        row = rows[service]
        assert (row['auth_types'], row['endpoint'], row['auth_labels']) == (auth_types, endpoint, labels)
        assert (row['kinds'], row['capabilities'], row['protocol']) == (['git'], ['git'], service)
    assert catalog.auth_label('github', 'api_key') == 'Personal access token'
    assert catalog.auth_label('git', 'basic') == 'Username and token'
    assert catalog.auth_label('openai', 'api_key') == 'API key'
    assert catalog.auth_label('custom', 'basic') == 'Username and password'


def test_kinds_and_capabilities_come_from_the_rows():
    rows = {row['id']: row for row in catalog.service_catalog()}
    assert (rows['tavily']['kinds'], rows['tavily']['capabilities']) == (['account'], ['search'])
    assert (rows['custom']['kinds'], rows['custom']['capabilities']) == (['custom', 'account', 'model'], [])
    assert (rows['openai']['kinds'], rows['openai']['capabilities']) == (['account', 'model'], ['text'])


def test_every_auth_label_names_an_offered_auth_type():
    for row in catalog.service_catalog():
        assert set(row['auth_labels']) <= set(row['auth_types']), row['id']
        assert all(isinstance(label, str) and label for label in row['auth_labels'].values())
