"""Public document validation and immutable selected lease consumers."""
import asyncio
import json

import pytest
from collimate.account_taxonomy import PLAN_LOGINS, PLAN_PROVIDERS, PlanLogin
from tests.test_login_store import account_store as account_store
from _agent_shared import cli_container, login_store, secret_hygiene
from _agent_shared.harnesses import pipeline as _pipeline  # noqa: E402
from _agent_shared.harnesses import HARNESS as _HARNESS  # noqa: E402
from _agent_shared.harnesses.antigravity import backend as antigravity_backend
from _agent_shared import login_store as ls

CONTEXT = {'auth': 'chatgpt', 'provider': 'openai', 'model': '', 'commands': 'ask',
           'websearch': True, 'runtime': 'docker', 'host_harness': None,
           'system_prompt': '', 'enable_file_tools': True}


def leased_document(account_store, harness, document):
    snapshot, root, events = account_store
    snapshot['metadata']['harness'] = harness
    path = root.joinpath(harness, *PLAN_LOGINS[harness].lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return snapshot, path, events


def refuse_external_boundary(monkeypatch):
    async def never_start(*args, **kwargs):
        pytest.fail('invalid selected login reached native container boundary')
    async def never_fallback(*args, **kwargs):
        pytest.fail('selected plan must not seek an API key')
    monkeypatch.setattr(cli_container, 'ensure_env', never_start)
    monkeypatch.setattr(_pipeline, 'account_api_key', never_fallback)


@pytest.mark.parametrize('document', [
    {'tokens': 'not-an-object'},
    {'tokens': {'access_token': 'synthetic-expired'}},
    {'auth_mode': 'chatgptAuthTokens', 'tokens': {'refresh_token': 'synthetic-refresh'}},
])
def test_codex_actual_prepare_uses_paste_shape_F1403_F1404_P4F36(account_store, monkeypatch, document):
    _, _, events = leased_document(account_store, 'codex', document)
    assert PLAN_LOGINS['codex'].document_problem(json.dumps(document), 'chatgpt')
    refuse_external_boundary(monkeypatch)
    with pytest.raises(RuntimeError, match='Paste the selected ChatGPT login'):
        asyncio.run(_pipeline.prepare(_HARNESS['codex'], CONTEXT))
    assert events == [('acquire', 'model')]


def test_actual_prepare_dispatches_selected_row_predicate_F1403_F1404_P4F36(account_store, monkeypatch):
    document = {'tokens': {'refresh_token': 'synthetic-refresh'}}
    _, _, events = leased_document(account_store, 'codex', document)
    calls = []
    def refuse(self, content, plan=''):
        calls.append((self.harness, json.loads(content), plan))
        return 'recording selected-row refusal'
    monkeypatch.setattr(PlanLogin, 'document_problem', refuse)
    refuse_external_boundary(monkeypatch)
    with pytest.raises(RuntimeError, match='Paste the selected ChatGPT login'):
        asyncio.run(_pipeline.prepare(_HARNESS['codex'], CONTEXT))
    assert calls == [('codex', document, 'chatgpt')]
    assert events == [('acquire', 'model')]


@pytest.mark.parametrize('harness,document', [
    ('opencode', {'openai': {'type': 'oauth'}, 'xai': {'type': 'oauth'}}),
    ('pi', {'openai-codex': {'type': 'oauth'}, 'xai': {'type': 'oauth'}}),
])
def test_selected_plan_cannot_borrow_other_oauth_row_F1403_F1404_P4F36(account_store, monkeypatch, harness, document):
    snapshot, _, events = leased_document(account_store, harness, document)
    assert snapshot['metadata']['plan'] == 'chatgpt'
    refuse_external_boundary(monkeypatch)
    with pytest.raises(RuntimeError, match='Paste the selected xAI login'):
        asyncio.run(_pipeline.prepare(_HARNESS[harness], {**CONTEXT, 'auth': 'xai', 'provider': 'xai'}))
    assert events == [('acquire', 'model')]


@pytest.mark.parametrize('harness,plan,document,valid', [
    ('opencode', 'unknown', {'openai': {'type': 'oauth'}}, False),
    ('pi', 'unknown', {'openai-codex': {'type': 'oauth'}}, False),
    ('opencode', '', {'openai': {'type': 'oauth'}}, True),
    ('pi', '', {'openai-codex': {'type': 'oauth'}}, True),
])
def test_public_document_owner_scope_and_explicit_selection_F1403_F1404_P4F36(harness, plan, document, valid):
    assert (PLAN_LOGINS[harness].document_problem(json.dumps(document), plan) is None) is valid


@pytest.mark.parametrize('harness,plan,document', [
    ('codex', 'chatgpt', {'tokens': {'refresh_token': 'synthetic-refresh'}}),
    ('opencode', 'github_copilot', {'github-copilot': {'type': 'oauth'}}),
    ('pi', 'xai', {'xai': {'type': 'oauth'}}),
    ('cline', 'chatgpt', {'providers': {'openai-codex': {'settings': {'auth': {'accessToken': 'synthetic-access'}}}}}),
])
def test_shared_json_consumer_dispatches_public_selected_row_F1403_F1404_P4F36(account_store, monkeypatch, harness, plan, document):
    from collimate.account_taxonomy import PLAN_PROVIDERS
    snapshot, root, events = account_store
    snapshot.update(service=PLAN_PROVIDERS[plan])
    snapshot['metadata'] = {'harness': harness, 'plan': plan}
    path = root.joinpath(harness, *PLAN_LOGINS[harness].lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(document, indent=2) + '\n'
    path.write_text(content)
    calls = []
    actual = PlanLogin.document_problem
    def record(self, supplied, selected=''):
        calls.append((self.harness, supplied, selected))
        return actual(self, supplied, selected)
    monkeypatch.setattr(PlanLogin, 'document_problem', record)
    assert login_store.selected_login_ready(harness, plan)
    assert calls == [(harness, content, plan)]
    assert path.read_text() == content
    assert events == [('acquire', 'model')]


_GOOGLE_TOKEN = {'type': 'authorized_user', 'client_id': 'id', 'client_secret': 'secret',
                 'refresh_token': 'refresh', 'token_uri': 'https://oauth2.googleapis.com/token'}


def test_google_login_is_the_servers_json_document_checked_by_its_validator_F1403_F1404_P4F36(account_store, monkeypatch):
    snapshot, root, events = account_store
    snapshot.update(service='gemini')
    snapshot['metadata'] = {'harness': 'antigravity', 'plan': 'google'}
    path = root.joinpath('antigravity', *PLAN_LOGINS['antigravity'].lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(_GOOGLE_TOKEN, indent=2) + '\n'
    path.write_text(content)
    calls = []
    actual = PlanLogin.document_problem
    def record(self, supplied, selected=''):
        calls.append((self.harness, supplied, selected))
        return actual(self, supplied, selected)
    monkeypatch.setattr(PlanLogin, 'document_problem', record)
    assert login_store.selected_login_ready('antigravity', 'google')
    assert calls == [('antigravity', content, 'google')]
    assert path.read_text() == content
    for raw in (b'\xff\x00opaque\n', b' \t\n', b'{"refresh_token": "only"}'):
        path.write_bytes(raw)
        assert not login_store.selected_login_ready('antigravity', 'google')


def select(account_store, harness, plan, document):
    snapshot, root, events = account_store
    snapshot.update(service=PLAN_PROVIDERS[plan])
    snapshot['metadata'] = {'harness': harness, 'plan': plan}
    path = root.joinpath(harness, *PLAN_LOGINS[harness].lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document if isinstance(document, bytes) else json.dumps(document).encode())
    return snapshot, path, events


@pytest.mark.parametrize('harness,plan,document', [
    ('codex', 'chatgpt', {'tokens': {'refresh_token': 'synthetic-refresh', 'access_token': 'expired'}}),
    ('codex', 'chatgpt', {'personal_access_token': 'synthetic-access'}),
    ('codex', 'chatgpt', {'agent_identity': 'synthetic-identity'}),
    ('opencode', 'github_copilot', {'github-copilot': {'type': 'oauth'}}),
    ('pi', 'xai', {'xai': {'type': 'oauth'}}),
    ('cline', 'chatgpt', {'providers': {'openai-codex': {'settings': {'auth': {'accessToken': 'synthetic-access'}}}}}),
])
def test_supported_native_login_modes_and_expired_refresh_remain_ready_F1403_F1404_P4F36(account_store, harness, plan, document):
    _, path, events = select(account_store, harness, plan, document)
    original = path.read_bytes()
    assert ls.selected_login_ready(harness, plan)
    assert path.read_bytes() == original
    assert events == [('acquire', 'model')]


def test_google_document_bytes_remain_identical_through_ready_copy_flush_F1403_F1404_P4F36(account_store):
    raw = json.dumps(_GOOGLE_TOKEN, indent=4).encode() + b'\n\n'
    _, path, events = select(account_store, 'antigravity', 'google', raw)
    assert ls.selected_login_ready('antigravity', 'google')
    assert path.read_bytes() == raw
    antigravity_backend.antigravity_volumes('google')
    assert len(ls._COPIES) == 1
    copy = ls._COPIES['antigravity'][1]
    from pathlib import Path
    assert Path(copy).read_bytes() == raw
    # A refreshed sign-in goes back byte for byte, whatever its layout; one
    # that is not the plan's login is never written back (test_login_store).
    refreshed = json.dumps({**_GOOGLE_TOKEN, 'refresh_token': 'rotated'}, indent=1).encode() + b'\n '
    Path(copy).write_bytes(refreshed)
    ls.close_account_credentials()
    assert path.read_bytes() == refreshed
    assert events[-1] == ('release', 'model', 'lease-one')


def test_selected_snapshot_stays_fixed_after_manager_edit_F1403_F1404_P4F36(account_store):
    snapshot, _, events = select(account_store, 'opencode', 'chatgpt', {'openai': {'type': 'oauth'}, 'xai': {'type': 'oauth'}})
    assert ls.selected_login_ready('opencode', 'chatgpt')
    snapshot['service'] = 'xai'
    snapshot['metadata']['plan'] = 'xai'
    assert ls.selected_login_ready('opencode', 'chatgpt')
    assert not ls.selected_login_ready('opencode', 'xai')
    assert events == [('acquire', 'model')]


@pytest.mark.parametrize('harness,plan', [('codex', ''), ('codex', 'api_key'), ('pi', 'google'), ('opencode', 'unknown'), ('no-such-harness', 'chatgpt')])
def test_unsupported_readiness_never_acquires_lease_F1403_F1404_P4F36(account_store, harness, plan):
    _, _, events = account_store
    assert not ls.selected_login_ready(harness, plan)
    assert events == []


def test_rotated_document_tokens_are_registered_even_when_owner_refuses_F1403_F1404_P4F36(account_store):
    _, _, _ = select(account_store, 'codex', 'chatgpt', {'tokens': {'access_token': 'rotated-synthetic-secret-without-refresh'}})
    assert not ls.selected_login_ready('codex', 'chatgpt')
    assert secret_hygiene.scrub('rotated-synthetic-secret-without-refresh') == '[redacted]'


@pytest.mark.parametrize('harness', ['cline', 'pi', 'codex', 'opencode'])
def test_actual_plan_credential_read_is_off_loop_F1461_P4F36(account_store, monkeypatch, harness):
    import threading
    from pathlib import Path

    snapshot, root, events = account_store
    plan = 'chatgpt'
    provider = 'openai'
    snapshot.update(service=provider)
    snapshot['metadata'] = {'harness': harness, 'plan': plan}
    path = root.joinpath(harness, *PLAN_LOGINS[harness].lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'invalid-selected-document': True}))
    actual_read = Path.read_text
    reader_threads = []
    def read(self, *args, **kwargs):
        if self == path:
            reader_threads.append(threading.get_ident())
        return actual_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    refuse_external_boundary(monkeypatch)
    async def inspect_actual_prepare():
        loop_thread = threading.get_ident()
        with pytest.raises(RuntimeError, match='Paste the selected'):
            await _pipeline.prepare(_HARNESS[harness], {**CONTEXT, 'auth': plan, 'provider': provider})
        assert reader_threads, 'actual leased document was not read'
        assert all(reader != loop_thread for reader in reader_threads), 'selected credential I/O blocks event-loop thread'
    asyncio.run(inspect_actual_prepare())
    assert events == [('acquire', 'model')]
