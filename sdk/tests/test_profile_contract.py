"""Provider tables flow from their owning backend and taxonomy into profiles."""
import ast
import dataclasses
import json
from pathlib import Path

import pytest
from tests.test_login_store import account_store as account_store
from tests._workspace import GENERATORS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name, provider, expected', [('cline', 'openai', 'openai'), ('pi', 'gemini', 'google'),
                                                     ('opencode', 'gemini', 'google')])
def test_provider_ids_are_the_adapters_declaration(name, provider, expected):
    """A harness's own provider ids are its adapter's ``Auth.providers`` — the
    one table — and no backend keeps a copy or reaches orchestration for one."""
    from _agent_shared import acp_run
    from _agent_shared.harnesses import HARNESS
    assert HARNESS[name].native_provider(provider).id == expected
    package = name.replace('-', '_')
    source = ast.parse((GENERATORS / '_agent_shared/harnesses' / package / 'backend.py').read_text())
    assert not [node for node in ast.walk(source)
                if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith('acp_run')], (
                    'a backend must not import its provider vocabulary from orchestration')
    assert not [node for node in ast.walk(source) if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id.endswith('PROVIDER_IDS') for t in node.targets)]
    assert not hasattr(acp_run, f'_{name.upper()}_PROVIDER_IDS')


@pytest.mark.parametrize('harness,module_name', [('pi', 'harnesses.pi.backend'), ('opencode', 'harnesses.opencode.backend')])
def test_readiness_uses_taxonomy_store_keys__F1537(account_store, monkeypatch, harness, module_name):
    from collimate.account_taxonomy import PLAN_LOGINS
    from _agent_shared import login_store as ls
    snapshot, root, events = account_store
    snapshot['metadata']['harness'] = harness
    login = dataclasses.replace(PLAN_LOGINS[harness], store_keys={'chatgpt': 'new-store-key'})
    monkeypatch.setitem(PLAN_LOGINS, harness, login)
    path = root.joinpath(harness, *login.lease)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'new-store-key': {'type': 'oauth', 'access': 'token'}}))
    assert ls.selected_login_ready(harness, 'chatgpt')
    path.write_text(json.dumps({'openai': {'type': 'oauth'}}))
    assert not ls.selected_login_ready(harness, 'chatgpt')
    assert events == [('acquire', 'model')]
