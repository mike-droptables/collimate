"""The Key manager accepts only a service's offered authentication methods."""
import pytest

import account_catalog
import accounts
from collimate.git_hosts import GIT_SERVICES
from test_pytest_accounts import registry as registry

AUTHOR = {'author_name': 'Octo', 'author_email': 'octo@example.com'}


@pytest.mark.parametrize('service,auth_type', [
    ('openai', 'device'), ('openai', 'oauth'), ('anthropic', 'cli'),
    ('github', 'cli'), ('github', 'basic'), ('gitlab', 'basic'), ('gitlab', 'none'), ('git', 'api_key'),
    ('copilot', 'api_key'), ('tavily', 'basic'), ('unknown', 'none'),
])
@pytest.mark.parametrize('operation', ['create', 'draft', 'update'])
def test_service_refuses_unsupported_auth_before_writing__F1557_F1544(registry, service, auth_type, operation):
    original = accounts.create_account({'name': 'Keep', 'kind': 'account', 'service': 'openai',
                                       'secrets': {'api_key': 'kept-secret'}})
    before_config = accounts.CONFIG_PATH.read_bytes()
    before_keyring = dict(registry)
    body = {'name': 'Rejected', 'kind': 'account', 'service': service, 'auth_type': auth_type}
    if service in GIT_SERVICES:
        body.update(kind='git', metadata=AUTHOR)
    with pytest.raises(accounts.AccountError, match='Unsupported authentication|Unknown account service'):
        if operation == 'create':
            accounts.create_account(body)
        elif operation == 'draft':
            accounts.draft_account(body)
        else:
            accounts.update_account(original['id'], body)
    assert accounts.CONFIG_PATH.read_bytes() == before_config
    assert registry == before_keyring


@pytest.mark.parametrize('service,auth_type', [
    (row['id'], auth_type)
    for row in account_catalog.service_catalog()
    for auth_type in row['auth_types']
])
def test_every_offered_service_auth_can_be_saved__F1557_F1544(registry, service, auth_type):
    body = {'name': 'Offered', 'kind': 'account', 'service': service, 'auth_type': auth_type}
    if service in GIT_SERVICES:
        body.update(kind='git', metadata=AUTHOR)
    saved = accounts.create_account(body)
    assert (saved['service'], saved['auth_type'], saved['kind']) == (service, auth_type, body['kind'])
