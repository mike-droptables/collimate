"""Deterministic account-lease coordination; all provider/keyring I/O is fake."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import accounts
from test_pytest_accounts import registry as registry, model


def cli_login(name='synthetic'):
    """A pasted plan login: the one credential a run leases as a store."""
    return model(auth_type='cli', metadata={'harness': 'codex'},
                 secrets={'credential_json': json.dumps({'tokens': {'refresh_token': name}})})


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir()
    monkeypatch.setattr(accounts, '_cli_root', lambda: root)
    yield root


def test_failed_run_retention_unwinds_all_acquired_credentials(registry, runtime_root, monkeypatch):
    first = accounts.create_account(cli_login('first'))
    second = accounts.create_account(cli_login('second'))
    context = accounts.resolve_accounts({'first': first['id'], 'second': second['id']})
    original = accounts.materialize_cli_account

    def materialize(account, owner):
        if account['credential_account_id'] == second['id']:
            raise accounts.KeyringUnavailable('synthetic keyring refusal')
        return original(account, owner)

    monkeypatch.setattr(accounts, 'materialize_cli_account', materialize)
    with pytest.raises(accounts.KeyringUnavailable):
        accounts.retain_account_context(context, 'failed-start')
    assert not accounts._OWNED_CLI_LEASES
    assert not list(runtime_root.glob('*/leases.json'))


def test_shared_choice_inherits_owner_connection_but_keeps_model_configuration(registry):
    owner = accounts.create_account(model(kind='account', model='', auth_type='cli', protocol='cli', endpoint='',
                                          metadata={'harness': 'codex', 'plan': 'chatgpt'},
                                          secrets={'credential_json': '{"tokens":{"refresh_token":"owner-private"}}'}))
    choice = accounts.create_account(model(credential_account_id=owner['id'], auth_type='cli', protocol='cli', model='different-model',
        endpoint='https://choice.example/v1', metadata={'plan': 'xai', 'temperature': 0.2}, secrets={}))
    snapshot = accounts.resolve_accounts({'model': choice['id']})['model']
    assert snapshot['metadata']['harness'] == 'codex' and snapshot['metadata']['plan'] == 'chatgpt'
    assert snapshot['metadata']['temperature'] == 0.2
    assert snapshot['model'] == 'different-model'
    assert snapshot['endpoint'] == owner['endpoint']
    assert 'owner-private' in snapshot['secrets']['credential_json']


def test_cli_generation_is_retained_before_its_first_materialization(registry, runtime_root):
    saved = accounts.create_account(model(auth_type='cli', metadata={'harness': 'codex'},
        secrets={'credential_json': '{"tokens":{"refresh_token":"old-chain"}}'}))
    context = accounts.resolve_accounts({'model': saved['id']})
    accounts.retain_account_context(context, 'first-run')
    accounts.retain_account_context(context, 'idle-run')
    first = accounts.materialize_cli_account(context['model'], 'first-sdk')
    path = Path(first['path']) / 'codex/auth.json'
    path.write_text('{"tokens":{"refresh_token":"rotated-old-chain"}}')
    accounts.update_account(saved['id'], {'secrets': {'credential_json': '{"tokens":{"refresh_token":"new-manager-login"}}'}})
    accounts.release_cli_account(first['lease_id'], 'first-sdk')
    accounts.release_run_account_leases('first-run')
    delayed = accounts.materialize_cli_account(context['model'], 'delayed-sdk')
    assert (Path(delayed['path']) / 'codex/auth.json').read_text() == path.read_text()
    assert 'rotated-old-chain' in path.read_text()
    accounts.release_cli_account(delayed['lease_id'], 'delayed-sdk')
    accounts.release_run_account_leases('idle-run')
    assert not path.exists()
    assert 'new-manager-login' in accounts.private_account(saved['id'])['secrets']['credential_json']


def test_a_start_cancelled_as_the_loop_closes_leaves_no_lease_once_the_retain_ends__F0397(registry, runtime_root, monkeypatch):
    # Closing the event loop cancels the start and the retain's task together:
    # the start unwinds (and releases) while the retain thread is still
    # materializing a login store. Once that thread ends, no lease of the
    # cancelled run remains.
    import asyncio
    import threading
    from process_manager.manager import retain_account_leases
    saved = accounts.create_account(cli_login())
    context = accounts.resolve_accounts({'model': saved['id']})
    writing, proceed = threading.Event(), threading.Event()
    original = accounts.materialize_cli_account

    def slow_materialize(account, owner):
        writing.set()
        proceed.wait(5)
        return original(account, owner)

    monkeypatch.setattr(accounts, 'materialize_cli_account', slow_materialize)

    async def start():
        try:
            await retain_account_leases(context, 'cancelled-run')
        except asyncio.CancelledError:
            # The run start's unwind (routes/runs/create.py).
            await asyncio.to_thread(accounts.release_run_account_leases, 'cancelled-run')
            raise

    async def serve():
        asyncio.create_task(start())
        assert await asyncio.to_thread(writing.wait, 5)
        threading.Timer(0.3, proceed.set).start()

    asyncio.run(serve())
    assert not [lease for lease, (_, owner) in accounts._OWNED_CLI_LEASES.items() if owner == 'cancelled-run']
    assert not any(json.loads(manifest.read_text())['leases'] for manifest in runtime_root.glob('*/leases.json'))


def test_abandoned_start_and_unwind_release_each_lease_once__P2F01(registry, runtime_root, monkeypatch):
    """Both cancellation owners call the real aggregate on real synthetic leases."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    saved = accounts.create_account(cli_login())
    context = accounts.resolve_accounts({'model': saved['id']})
    accounts.retain_account_context(context, 'abandoned-start')
    original = accounts.release_cli_account
    first_entered, second_entered, proceed, first_done = (threading.Event() for _ in range(4))
    calls = []

    def release(lease, owner):
        calls.append((lease, owner))
        if len(calls) == 1:
            first_entered.set()
            assert proceed.wait(5)
            try:
                return original(lease, owner)
            finally:
                first_done.set()
        second_entered.set()
        assert first_done.wait(5)
        return original(lease, owner)

    monkeypatch.setattr(accounts, 'release_cli_account', release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(accounts.release_run_account_leases, 'abandoned-start')
        assert first_entered.wait(5)
        second = pool.submit(accounts.release_run_account_leases, 'abandoned-start')
        # The old aggregate enters the low-level release twice from two stale
        # snapshots. A serialized second caller waits before taking a snapshot.
        second_entered.wait(.2)
        proceed.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert len(calls) == 1
    assert not [entry for entry in accounts._OWNED_CLI_LEASES.values() if entry[1] == 'abandoned-start']
    # Idempotent aggregate release does not weaken direct owner authorization.
    with pytest.raises(accounts.AccountError):
        original(calls[0][0], 'unrelated-run')


def test_one_run_release_does_not_block_another_run__P2F01(registry, runtime_root, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    saved = accounts.create_account(cli_login())
    context = accounts.resolve_accounts({'model': saved['id']})
    accounts.retain_account_context(context, 'slow-run')
    accounts.retain_account_context(context, 'other-run')
    original = accounts.release_cli_account
    entered, proceed = threading.Event(), threading.Event()

    def release(lease, owner):
        if owner == 'slow-run':
            entered.set()
            assert proceed.wait(5)
        return original(lease, owner)

    monkeypatch.setattr(accounts, 'release_cli_account', release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(accounts.release_run_account_leases, 'slow-run')
        assert entered.wait(5)
        try:
            pool.submit(accounts.release_run_account_leases, 'other-run').result(timeout=2)
        finally:
            proceed.set()
        slow.result(timeout=5)
    assert not accounts._OWNED_CLI_LEASES


def test_release_failure_still_tries_other_leases_and_allows_retry__P2F01(registry, runtime_root, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    saved = [accounts.create_account(cli_login(f'login-{i}')) for i in range(2)]
    context = accounts.resolve_accounts({str(i): item['id'] for i, item in enumerate(saved)})
    leases = accounts.retain_account_context(context, 'retry-run')
    original = accounts.release_cli_account

    def release(lease, owner):
        if lease == leases[0]:
            raise RuntimeError('synthetic collection failure')
        return original(lease, owner)

    monkeypatch.setattr(accounts, 'release_cli_account', release)
    with pytest.raises(RuntimeError, match='synthetic collection failure'):
        accounts.release_run_account_leases('retry-run')
    assert leases[0] in accounts._OWNED_CLI_LEASES
    assert leases[1] not in accounts._OWNED_CLI_LEASES
    monkeypatch.setattr(accounts, 'release_cli_account', original)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(accounts.release_run_account_leases, 'retry-run').result(timeout=2)
    assert leases[0] not in accounts._OWNED_CLI_LEASES
