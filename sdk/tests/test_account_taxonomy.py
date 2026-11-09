"""Key Manager's metadata-only choices agree with the Agent's runtime matrix."""

import pytest


from collimate.account_taxonomy import (
    HARNESS_DEFAULT_MODEL_OVERRIDES, HARNESS_HOST_BINARY, HARNESS_MODEL_VOCABULARY,
    HARNESS_STANDARD_RUNTIME, KEY_DEFAULT_MODELS, MODEL_HARNESS_RULES, MODEL_LOGIN_PLANS, PLAN_DEFAULT_HARNESS,
    PLAN_DEFAULT_MODELS, PLAN_LOGINS, PLAN_PROVIDERS, UNCHECKED_MODEL_CONSEQUENCE, default_model_for,
    supported_model_harnesses,
)
from collimate.permissions import Grant

pytestmark = pytest.mark.usefixtures("no_machine_harness_probe")



def model(**changes):
    return {'kind': 'model', 'service': 'openai', 'protocol': 'openai',
            'auth_type': 'api_key', 'model': 'chosen-model', 'metadata': {}, **changes}


@pytest.mark.parametrize('service,protocol,expected', [
    ('openai', 'openai', {'pydantic-ai', 'codex', 'goose', 'opencode', 'qwen-code', 'cline', 'pi', 'hermes', 'vibe',
                          'copilot'}),
    ('anthropic', 'anthropic', {'pydantic-ai', 'claude-code', 'goose', 'opencode', 'qwen-code', 'cline', 'pi',
                                'hermes', 'copilot'}),
    ('gemini', 'gemini', {'pydantic-ai', 'antigravity', 'goose', 'opencode', 'qwen-code', 'cline', 'pi', 'hermes'}),
    ('xai', 'openai-compatible', {'pydantic-ai', 'hermes', 'copilot'}),
    ('mistral', 'openai-compatible', {'pydantic-ai', 'hermes', 'vibe', 'copilot'}),
])
def test_provider_choices(service, protocol, expected):
    assert set(supported_model_harnesses(model(service=service, protocol=protocol))) == expected


def test_custom_endpoint_limits_choices_to_direct_api():
    assert supported_model_harnesses(model(endpoint='https://proxy.example/v1')) == ['pydantic-ai']
    assert supported_model_harnesses(model(service='custom', endpoint='https://proxy.example/v1')) == ['pydantic-ai']
    assert supported_model_harnesses(model(service='custom')) == []


def test_local_unauthenticated_endpoint_requires_explicit_address():
    assert supported_model_harnesses(model(auth_type='none')) == []
    assert supported_model_harnesses(model(auth_type='none', endpoint='http://localhost:1234/v1')) == ['pydantic-ai']


def test_cli_login_ownership_and_new_account_choices__F1242_F1557():
    assert supported_model_harnesses(model(auth_type='cli', protocol='cli', metadata={'harness': 'codex'})) == ['codex']
    assert supported_model_harnesses(model(auth_type='cli', protocol='cli', metadata={'harness': 'claude-code'})) == []
    assert set(supported_model_harnesses(model(kind='account', auth_type='cli', protocol='cli'))) == {'codex', 'opencode', 'cline', 'pi'}
    assert supported_model_harnesses(model(service='copilot', auth_type='cli', protocol='cli', metadata={'harness': 'opencode'})) == ['opencode']
    assert supported_model_harnesses(model(service='copilot', auth_type='cli', protocol='cli', metadata={'harness': 'copilot'})) == ['copilot']
    assert supported_model_harnesses(model(kind='account', service='copilot', auth_type='cli', protocol='cli')) == ['opencode', 'copilot']
    assert supported_model_harnesses(model(service='copilot', auth_type='api_key', protocol='cli', metadata={'harness': 'opencode'})) == []


def test_service_default_endpoint_is_authoritative_when_protocol_differs():
    assert supported_model_harnesses(model(service='openai', protocol='anthropic')) == ['pydantic-ai']


@pytest.mark.parametrize('account', [
    None, [], {}, {'kind': []}, model(kind='custom'),
    model(service='github', protocol='github'), model(service='gitlab', protocol='gitlab'),
    model(service='git', protocol='git'), model(service='tavily', protocol='tavily'),
    model(protocol='unknown'), model(auth_type='basic'), model(auth_type=['api_key']),
    model(metadata='bad'), model(auth_type='cli', metadata={'plan': 'wrong'}),
])
def test_unsupported_or_malformed_metadata_has_no_harnesses(account):
    assert supported_model_harnesses(account) == []


def test_runtime_backend_specs_consume_public_rules():
    from _agent_shared import parse_agent_config
    from _agent_shared.harnesses import BACKENDS as _BACKENDS
    from _agent_shared.accounts import model_config

    assert set(_BACKENDS) == set(MODEL_HARNESS_RULES)
    for harness, rules in MODEL_HARNESS_RULES.items():
        assert _BACKENDS[harness].providers == rules['providers']
        assert _BACKENDS[harness].auths == rules['auths']
    for account in [model(), model(service='anthropic', protocol='anthropic'),
                    model(service='xai', protocol='openai-compatible'),
                    model(service='custom', endpoint='http://localhost:1234/v1'),
                    model(auth_type='cli', protocol='cli', metadata={'harness': 'codex'})]:
        for harness in supported_model_harnesses(account):
            selected = {**account, 'metadata': {**account['metadata'], 'harness': harness}}
            cfg = parse_agent_config({**model_config({}, selected), "surface": "annotation"}, grant=Grant.from_dict({}), can_ask=True)
            assert cfg['backend'] == harness


def test_every_harness_table_names_known_harnesses():
    harnesses = set(MODEL_HARNESS_RULES)
    assert set(HARNESS_HOST_BINARY) <= harnesses
    assert set(HARNESS_STANDARD_RUNTIME) <= set(HARNESS_HOST_BINARY)
    assert set(HARNESS_DEFAULT_MODEL_OVERRIDES) <= harnesses
    assert set(HARNESS_MODEL_VOCABULARY) <= harnesses
    assert set(UNCHECKED_MODEL_CONSEQUENCE) <= harnesses
    assert set(PLAN_LOGINS) <= harnesses
    assert set(PLAN_DEFAULT_HARNESS.values()) <= harnesses
    for harness, plan in PLAN_DEFAULT_MODELS:
        assert plan in MODEL_HARNESS_RULES[harness]['auths'], (harness, plan)
    for harness, provider in KEY_DEFAULT_MODELS:
        assert provider in MODEL_HARNESS_RULES[harness]['providers'], (harness, provider)


def test_plan_login_rows_are_plan_harnesses_with_a_safe_lease_path():
    from collimate.account_taxonomy import LOGIN_VALIDATORS
    for harness, login in PLAN_LOGINS.items():
        assert login.harness == harness
        assert set(MODEL_HARNESS_RULES[harness]['auths']) - {'api_key'}, harness
        assert login.field in {'credential_json', 'credential_file'}
        assert login.validator in LOGIN_VALIDATORS
        assert login.lease and all(part and part not in {'.', '..'} and '/' not in part for part in login.lease)
        assert not login.relative.startswith('/') and '..' not in login.relative.split('/')
        # A document a sign-in writes names where it is; a token the user
        # creates (copilot's) has no store.
        assert ('{store}' in login.hint) is bool(login.relative), harness


def test_a_shared_store_names_the_entry_of_every_plan_its_harness_takes():
    # opencode and pi keep several providers' logins in one document; every
    # other store is one login, so it names no entry.
    shared = {harness: login.store_keys for harness, login in PLAN_LOGINS.items() if login.store_keys}
    assert set(shared) == {'opencode', 'pi'}
    for harness, keys in shared.items():
        assert set(keys) == set(MODEL_HARNESS_RULES[harness]['auths']) - {'api_key'}, harness


def test_the_backends_store_keys_are_the_taxonomy_rows():
    # A backend's readiness check looks up the same entry the Key manager
    # checks a pasted document for.
    from _agent_shared.harnesses.opencode import backend as opencode
    from _agent_shared.harnesses.pi import backend as pi_backend
    assert opencode.OPENCODE_PLAN_KEYS == PLAN_LOGINS['opencode'].store_keys
    assert pi_backend.PI_PLAN_KEYS == PLAN_LOGINS['pi'].store_keys


def test_every_login_plan_starts_on_a_harness_that_takes_it():
    assert set(PLAN_DEFAULT_HARNESS) == set(MODEL_LOGIN_PLANS.values())
    for plan, harness in PLAN_DEFAULT_HARNESS.items():
        assert plan in MODEL_HARNESS_RULES[harness]['auths'], (plan, harness)


def test_no_harness_pins_a_model_vocabulary_antigravity_acp_lists_its_models_at_run_time():
    assert HARNESS_MODEL_VOCABULARY == {}


_PROTOCOL = {'anthropic': 'anthropic', 'gemini': 'gemini'}


def _blank_model_entries():
    """Every harness x provider x auth a model entry can select, with a blank
    model: each provider on its standard endpoint under an API key, and each
    plan login."""
    for harness, rules in MODEL_HARNESS_RULES.items():
        for auth in rules['auths']:
            if auth == 'api_key':
                for provider in rules['providers']:
                    yield pytest.param(
                        {'kind': 'model', 'service': provider, 'protocol': _PROTOCOL.get(provider, 'openai-compatible'),
                         'auth_type': 'api_key', 'endpoint': '', 'model': '', 'metadata': {'harness': harness}},
                        harness, provider, auth, id=f'{harness}-{provider}-api_key')
            else:
                provider = PLAN_PROVIDERS[auth]
                yield pytest.param(
                    {'kind': 'model', 'service': provider, 'protocol': 'cli', 'auth_type': 'cli', 'endpoint': '',
                     'model': '', 'metadata': {'harness': harness, 'plan': auth}},
                    harness, provider, auth, id=f'{harness}-{auth}')


@pytest.mark.parametrize('account,harness,provider,auth', list(_blank_model_entries()))
def test_a_blank_model_means_what_the_engine_resolves__F1241(account, harness, provider, auth):
    # The Key manager's Verify fills a blank model with default_model_for; the
    # engine's resolve_model is what the run uses for the same selection.
    from _agent_shared import resolve_model
    assert harness in supported_model_harnesses(account)
    assert default_model_for(account) == resolve_model(harness, provider, '', auth=auth)


@pytest.mark.parametrize('account', [
    # The ChatGPT-plan entry, with its plan named and with it inferred from
    # the service (a cli login on openai is the chatgpt plan).
    {'kind': 'model', 'service': 'openai', 'protocol': 'cli', 'auth_type': 'cli', 'endpoint': '', 'model': '',
     'metadata': {'harness': 'codex', 'plan': 'chatgpt'}},
    {'kind': 'model', 'service': 'openai', 'protocol': 'cli', 'auth_type': 'cli', 'endpoint': '', 'model': '',
     'metadata': {'harness': 'codex'}},
    # The openai API key on its standard endpoint.
    {'kind': 'model', 'service': 'openai', 'protocol': 'openai-compatible', 'auth_type': 'api_key', 'endpoint': '',
     'model': '', 'metadata': {'harness': 'codex'}},
], ids=['chatgpt-plan', 'chatgpt-inferred', 'openai-key'])
def test_a_blank_codex_model_on_openai_sends_no_model(account):
    # The service refuses the API default gpt-5 for a ChatGPT account and
    # codex's own catalogue dropped it, so codex picks its own default.
    assert default_model_for(account) == ''


def test_a_blank_codex_model_off_openai_keeps_the_provider_default():
    from collimate.providers import PROVIDER_DEFAULT_MODELS
    for provider in MODEL_HARNESS_RULES['codex']['providers']:
        if provider == 'openai':
            continue
        account = {'kind': 'model', 'service': provider, 'protocol': 'openai-compatible', 'auth_type': 'api_key',
                   'endpoint': '', 'model': '', 'metadata': {'harness': 'codex'}}
        assert default_model_for(account) == PROVIDER_DEFAULT_MODELS[provider], provider


def test_provider_maps_match_the_supported_services__F1240():
    from collimate.providers import PROVIDER_KEY_ENV, PROVIDERS, STANDARD_MODEL_ENDPOINTS
    key_billed = {p.id for p in PROVIDERS.values() if p.api_key}
    assert set(STANDARD_MODEL_ENDPOINTS) == key_billed == set(PROVIDER_KEY_ENV)
    billed = {provider for rules in MODEL_HARNESS_RULES.values() for provider in rules['providers']}
    assert billed <= key_billed


def test_placement_facts_match_actual_engine_dispatch__D06():
    from collimate.account_taxonomy import HARNESS_CONTAINERIZED, HARNESS_HOST_TWINS, HARNESS_TERMINAL
    from _agent_shared.harnesses import BACKENDS as _BACKENDS, HARNESS
    assert isinstance(HARNESS_CONTAINERIZED, tuple)
    assert set(HARNESS_CONTAINERIZED) == {name for name, spec in _BACKENDS.items() if spec.containerized}
    assert set(HARNESS_HOST_TWINS) == {name for name, row in HARNESS.items() if row.host_twin}
    assert set(HARNESS_TERMINAL) == {name for name, spec in _BACKENDS.items() if "terminal" in spec.uis}


@pytest.mark.parametrize('harness,plan,content,valid', [
    ('codex', 'chatgpt', '{"OPENAI_API_KEY":"key"}', False),
    ('codex', 'chatgpt', '{"tokens":{"refresh_token":"refresh"}}', True),
    ('opencode', 'chatgpt', '{"xai":{"type":"oauth"}}', False),
    ('opencode', 'chatgpt', '{"openai":{"type":"oauth"}}', True),
    ('pi', 'chatgpt', '{"openai-codex":{"type":"oauth"}}', True),
    ('cline', 'chatgpt', '{"providers":{"openai-codex":{"settings":{"auth":{"accessToken":"t"}}}}}', True),
    ('antigravity', 'google', '{"type":"authorized_user","refresh_token":"r","client_id":"i","client_secret":"s","token_uri":"https://oauth2.googleapis.com/token"}', True),
    ('antigravity', 'google', '{"refresh_token":"r","client_id":"i","client_secret":"s"}', False),
    ('antigravity', 'google', '{"refresh_token":"","client_id":"i","client_secret":"s","token_uri":"u"}', False),
    ('antigravity', 'google', 'opaque-token', False),
    ('copilot', 'github_copilot', 'github_pat_11ABCDEF', True),
    ('copilot', 'github_copilot', 'gho_abcdef', True),
    ('copilot', 'github_copilot', 'ghp_classic', False),
    ('copilot', 'github_copilot', 'not a token', False),
])
def test_plan_login_row_checks_its_document_F1403_F1404(harness, plan, content, valid):
    assert (PLAN_LOGINS[harness].document_problem(content, plan) is None) is valid


@pytest.mark.parametrize('depth', range(4))
@pytest.mark.parametrize('value', [None, [], 17, 'private-invalid-value'])
def test_cline_login_refuses_non_object_ancestors_P4F32(depth, value):
    import json

    path = ('providers', 'openai-codex', 'settings', 'auth')
    document = {'providers': {'openai-codex': {'settings': {'auth': {'accessToken': 'private-token'}}}}}
    parent = document
    for key in path[:depth]:
        parent = parent[key]
    parent[path[depth]] = value
    problem = PLAN_LOGINS['cline'].document_problem(json.dumps(document), 'chatgpt')
    assert problem == 'the file holds no openai-codex login; run `cline auth openai-codex` first'
    assert 'private-' not in problem


@pytest.mark.parametrize('token,valid', [
    (None, False), ('', False), (' ', False), ([], False), ({'secret': 'private'}, False),
    (17, False), (True, False), ('private-token', True),
])
def test_cline_login_requires_nonempty_string_token_P4F32(token, valid):
    import json

    document = {'providers': {'openai-codex': {'settings': {'auth': {'accessToken': token}}}}}
    problem = PLAN_LOGINS['cline'].document_problem(json.dumps(document), 'chatgpt')
    assert (problem is None) is valid
    assert problem is None or 'private' not in problem


def test_google_authorized_user_problem_names_missing_keys_and_never_echoes_values_F1404():
    document = '{"refresh_token":"private-refresh","client_id":"private-id"}'
    problem = PLAN_LOGINS['antigravity'].document_problem(document, 'google')
    assert 'client_secret' in problem and 'token_uri' in problem
    assert 'private-' not in problem
    assert PLAN_LOGINS['antigravity'].copy is True
    assert PLAN_LOGINS['antigravity'].lease == ('acp_token.json',)
    assert 'at your own risk' in PLAN_LOGINS['antigravity'].hint.lower()
    assert 'AGY_ACP_FORCE_FILE_STORAGE=1' in PLAN_LOGINS['antigravity'].hint
