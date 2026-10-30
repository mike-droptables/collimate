"""Native account boundary tests. No user's keyring or live service is used."""
from __future__ import annotations

import base64
import json
import os
import multiprocessing
import shutil
import struct
from contextlib import contextmanager
from pathlib import Path

import pytest
from collimate.account_taxonomy import MODEL_HARNESS_RULES, PLAN_LOGINS, PLAN_PROVIDERS, STANDARD_MODEL_ENDPOINTS
from fastapi import FastAPI
from fastapi.testclient import TestClient

import accounts
import account_adapters
import account_catalog
import keystore
from routes.accounts import router


# The real runtime root, for the tests that exercise how it is named.
_REAL_CLI_ROOT = accounts._cli_root


@pytest.fixture
def registry(tmp_path, monkeypatch):
    store = {}
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "user-config" / "accounts.json")
    # Leases stay under this test's own folder, never in the machine's temp
    # directory.
    runtime = tmp_path / "account-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    monkeypatch.setattr(keystore, "_keychain_ok", True)
    import keyring
    monkeypatch.setattr(keyring, "set_password", lambda service, name, value: store.__setitem__((service, name), value))
    monkeypatch.setattr(keyring, "get_password", lambda service, name: store.get((service, name)))
    monkeypatch.setattr(keyring, "delete_password", lambda service, name: store.pop((service, name), None))
    return store


def model(**overrides):
    body = {
        "name": "Work reasoning", "kind": "model", "service": "openai",
        "protocol": "openai", "endpoint": "https://api.openai.com/v1" if overrides.get("auth_type") in {"cli", "subscription"} else "https://models.example/v1",
        "model": "chosen-model", "capabilities": ["text", "reasoning"],
        "auth_type": "api_key", "metadata": {}, "secrets": {"api_key": "private-one"},
        **overrides,
    }
    if body["kind"] != "model":
        body.pop("capabilities")
    return body


def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_global_crud_only_secrets_reach_prefixed_keyring(registry, tmp_path, monkeypatch):
    api = client()
    created = api.post("/api/accounts", json=model()).json()
    assert created["configured"] is True
    assert "secrets" not in created
    assert "private-one" not in accounts.CONFIG_PATH.read_text()
    assert len(registry) == 1
    (service, slot), value = next(iter(registry.items()))
    assert service == "collimate"
    assert slot.startswith(f"collimate_{created['id']}_")
    assert value == "private-one"
    # Build/reset data and channel selection have no bearing on the registry.
    app_data = tmp_path / "app-data"
    (app_data / ".collimate").mkdir(parents=True)
    monkeypatch.chdir(app_data)
    shutil.rmtree(app_data / ".collimate")
    assert api.get("/api/accounts").json()["accounts"][0]["id"] == created["id"]
    api.delete(f"/api/accounts/{created['id']}").raise_for_status()
    assert not registry


@pytest.mark.parametrize("contents", ['{broken', '{"version":1,"accounts":[]}'])
def test_bad_config_notice_and_save_preserve_file(registry, contents):
    accounts.CONFIG_PATH.parent.mkdir(parents=True)
    accounts.CONFIG_PATH.write_text(contents)
    api = client()
    listing = api.get("/api/accounts").json()
    assert listing["accounts"] == []
    assert str(accounts.CONFIG_PATH) in listing["status"]["error"]
    assert api.post("/api/accounts", json=model()).status_code == 409
    assert accounts.CONFIG_PATH.read_text() == contents
    assert not registry
    assert accounts.resolve_accounts({}, {}) == {}


def test_snapshot_is_detached_and_refresh_cannot_overwrite_edit_or_delete(registry):
    saved = accounts.create_account(model())
    identity = saved["id"]
    first = accounts.resolve_accounts({"model": identity}, {"model": {"kind": "model", "capabilities": ["text"]}})
    accounts.update_account(identity, {"name": "Updated", "model": "next-model", "secrets": {"api_key": "private-two"}})
    assert first["model"]["model"] == "chosen-model"
    assert first["model"]["secrets"]["api_key"] == "private-one"
    assert accounts.commit_refreshed_secrets(identity, saved["revision"], {"api_key": "stale"}) is None
    current = accounts.resolve_accounts({"model": identity})["model"]
    assert current["secrets"]["api_key"] == "private-two"
    assert "private-one" not in json.dumps(accounts.public_accounts_snapshot(first))
    accounts.delete_account(identity)
    assert accounts.commit_refreshed_secrets(identity, current["revision"], {"api_key": "stale"}) is None
    assert not registry


def test_refresh_writes_new_revision_and_old_snapshot_stays_private(registry):
    saved = accounts.create_account(model())
    revision = accounts.commit_refreshed_secrets(saved["id"], saved["revision"], {"api_key": "refreshed"})
    assert revision and revision != saved["revision"]
    assert list(registry.values()) == ["refreshed"]
    assert accounts.get_account(saved["id"])["generation"] == saved["generation"]


def test_commit_account_cannot_plant_or_erase_a_secret__F0414(registry):
    # gen.commit_account writes back a rotation: it replaces a credential the
    # account already stores and nothing else. Adding a field or removing one
    # (null) is refused before anything is written.
    saved = accounts.create_account(model())
    for refused in ({"client_secret": "planted"}, {"api_key": None}, {"expires_at": None},
                    {"api_key": "rotated", "client_secret": "planted"},
                    {"api_key": ""}, {"api_key": 5}, {"expires_at": 123}):
        with pytest.raises(accounts.AccountError):
            accounts.commit_refreshed_secrets(saved["id"], saved["revision"], refused)
    # A blank value is refused in the commit's own terms, never by pointing
    # the run at null, which a commit may not carry either.
    with pytest.raises(accounts.AccountError, match="nonempty string") as blank:
        accounts.commit_refreshed_secrets(saved["id"], saved["revision"], {"api_key": ""})
    assert "null" not in str(blank.value)
    assert list(registry.values()) == ["private-one"]
    assert accounts.get_account(saved["id"])["revision"] == saved["revision"]
    assert accounts.get_account(saved["id"])["secret_fields"] == ["api_key"]
    revision = accounts.commit_refreshed_secrets(saved["id"], saved["revision"], {"api_key": "rotated", "expires_at": "123"})
    assert revision and list(registry.values()) == ["rotated"]
    assert accounts.get_account(saved["id"])["metadata"]["auth_state"] == {"expires_at": "123"}


def test_commit_account_on_a_custom_entry_refreshes_only_its_stored_fields__F0414(registry):
    # A custom entry's field names are its own: expires_at is not bookkeeping
    # there, and a secret the schema defines but the entry never stored is new.
    # The schema defines expires_at as a secret, so the entry's own schema
    # would take it: only the commit's rule for custom entries refuses it.
    metadata = {"custom_type": "warehouse", "fields": {}, "field_schema": [
        {"name": "signing_key", "secret": True}, {"name": "spare_key", "secret": True},
        {"name": "expires_at", "secret": True}]}
    saved = accounts.create_account(custom(metadata=metadata))
    assert accounts.get_account(saved["id"])["secret_fields"] == ["signing_key"]
    for refused in ({"spare_key": "planted"}, {"expires_at": "123"}, {"signing_key": None}):
        with pytest.raises(accounts.AccountError, match="already stores|cannot remove"):
            accounts.commit_refreshed_secrets(saved["id"], saved["revision"], refused)
    assert accounts.get_account(saved["id"])["secret_fields"] == ["signing_key"]
    assert list(registry.values()) == ["custom-private"]
    assert accounts.commit_refreshed_secrets(saved["id"], saved["revision"], {"signing_key": "rotated"})
    assert list(registry.values()) == ["rotated"]


def test_commit_account_refusal_reaches_the_run_in_its_ack__F0414(registry):
    # The IPC op answers a refused commit with ok=False and the reason, and
    # neither the registry nor the run's own snapshot takes the write.
    import asyncio
    from collections import deque
    from process_manager import ProcessManager
    saved = accounts.create_account(model())
    manager = ProcessManager.__new__(ProcessManager)
    manager.account_context = accounts.resolve_accounts({"model": saved["id"]})
    manager.account_requirements = {"model": {}}
    manager.run_id = "account-run"
    manager.log_buffer = deque()
    acks = []

    async def capture(body, _request):
        acks.append(body)
    manager._send_ack = capture
    asyncio.run(manager._op_commit_account({"role": "model", "expected_revision": saved["revision"],
                                            "secrets": {"api_key": None}}))
    assert acks[-1]["ok"] is False
    assert "remove" in acks[-1]["error"]
    assert manager.account_context["model"]["secrets"]["api_key"] == "private-one"
    assert list(registry.values()) == ["private-one"]


def test_cli_leases_share_rotation_and_isolate_manager_changes(registry, tmp_path, monkeypatch):
    from pathlib import Path
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    document = json.dumps({"tokens": {"refresh_token": "initial"}})
    saved = accounts.create_account(model(auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"}, secrets={"credential_json": document}))
    initial = accounts.resolve_accounts({"model": saved["id"]})["model"]
    first = accounts.materialize_cli_account(initial, "run-one")
    second = accounts.materialize_cli_account(initial, "run-two")
    assert first["path"] == second["path"]
    shared = Path(first["path"]) / "codex/auth.json"
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    shared.write_text(rotated)
    assert accounts.release_cli_account(first["lease_id"], "run-one")["committed"]
    assert shared.exists()  # The second live run still holds the lease.
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated
    accounts.update_account(saved["id"], {"model": "another-model", "secrets": {"credential_json": document}})
    updated = accounts.resolve_accounts({"model": saved["id"]})["model"]
    third = accounts.materialize_cli_account(updated, "run-three")
    assert third["path"] != first["path"]
    shared.write_text(json.dumps({"tokens": {"refresh_token": "stale-run"}}))
    assert accounts.release_cli_account(second["lease_id"], "run-two")["committed"] is False
    assert not Path(first["path"]).exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    accounts.delete_account(saved["id"])
    assert accounts.release_cli_account(third["lease_id"], "run-three")["committed"] is False
    assert not Path(third["path"]).exists()


def _plan_login_document(harness: str, plan: str, marker: str) -> str:
    """A document that passes ``harness``'s login validator for ``plan``,
    carrying ``marker`` where its refreshable token is."""
    login = PLAN_LOGINS[harness]
    documents = {
        "codex_auth": lambda: {"tokens": {"refresh_token": marker}},
        "oauth_store": lambda: {login.store_keys.get(plan) or plan: {"type": "oauth", "refresh": marker}},
        "cline_providers": lambda: {"providers": {"openai-codex": {"settings": {"auth": {"accessToken": marker}}}}},
        "google_authorized_user": lambda: {"type": "authorized_user", "client_id": "i", "client_secret": "s",
                                           "refresh_token": marker, "token_uri": "u"},
    }
    if login.validator == "github_token":
        return f"github_pat_{marker}"
    document = json.dumps(documents[login.validator]())
    assert login.document_problem(document, plan) is None
    return document


@pytest.mark.parametrize("harness", sorted(PLAN_LOGINS))
def test_cli_lease_places_each_login_where_the_taxonomy_says(registry, tmp_path, monkeypatch, harness):
    # The host writes and collects the document at the SDK's lease path under
    # the account secret the SDK names, the same row the generator reads.
    from pathlib import Path
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    login = PLAN_LOGINS[harness]
    plan = next(auth for auth in MODEL_HARNESS_RULES[harness]["auths"] if auth != "api_key")
    document = _plan_login_document(harness, plan, "initial")
    saved = accounts.create_account(model(service=PLAN_PROVIDERS[plan], protocol="cli", auth_type="cli", endpoint="",
                                          metadata={"harness": harness, "plan": plan}, secrets={login.field: document}))
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "run")
    placed = Path(lease["path"]).joinpath(harness, *login.lease)
    assert placed.read_text() == document
    refreshed = _plan_login_document(harness, plan, "rotated")
    placed.write_text(refreshed)
    assert accounts.release_cli_account(lease["lease_id"], "run")["committed"]
    assert accounts.private_account(saved["id"])["secrets"][login.field] == refreshed


@pytest.mark.parametrize("harness", sorted(PLAN_LOGINS))
def test_a_collected_document_that_fails_its_login_validator_is_never_committed(registry, tmp_path, monkeypatch, harness):
    # The run's harness owns the leased document: the host commits it back
    # only when it still passes the validator a paste passes, for every plan
    # login (the Google sign-in, codex, opencode, pi, cline, copilot).
    from pathlib import Path
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    login = PLAN_LOGINS[harness]
    plan = next(auth for auth in MODEL_HARNESS_RULES[harness]["auths"] if auth != "api_key")
    document = _plan_login_document(harness, plan, "initial")
    saved = accounts.create_account(model(service=PLAN_PROVIDERS[plan], protocol="cli", auth_type="cli", endpoint="",
                                          metadata={"harness": harness, "plan": plan}, secrets={login.field: document}))
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "run")
    placed = Path(lease["path"]).joinpath(harness, *login.lease)
    rewritten = "not a token" if login.field == "credential_file" else json.dumps({"login": {"refresh_token": "planted"}})
    assert login.document_problem(rewritten, plan) is not None
    placed.write_text(rewritten)
    assert accounts.release_cli_account(lease["lease_id"], "run")["committed"] is False
    assert accounts.private_account(saved["id"])["secrets"][login.field] == document


@pytest.mark.parametrize("harness", sorted(PLAN_LOGINS))
def test_a_cli_login_is_configured_only_by_its_apps_own_document_field(registry, harness):
    # The document counts only under the secret the harness's taxonomy row
    # names, the one field a run's lease is written from — except on a plan
    # whose CLI surface signs in on this machine (Antigravity's Google plan),
    # which is complete with nothing pasted.
    from collimate.account_taxonomy import HOST_SIGN_IN_PLANS
    login = PLAN_LOGINS[harness]
    other = "credential_file" if login.field == "credential_json" else "credential_json"
    plan = next(auth for auth in MODEL_HARNESS_RULES[harness]["auths"] if auth != "api_key")

    def entry(field):
        return model(service=PLAN_PROVIDERS[plan], protocol="cli", auth_type="cli", endpoint="",
                     metadata={"harness": harness, "plan": plan}, secrets={field: "document"})
    signs_in_here = (harness, plan) in HOST_SIGN_IN_PLANS
    assert signs_in_here is (harness == "antigravity")
    assert accounts.create_account(entry(other))["configured"] is signs_in_here
    assert accounts.create_account(entry(login.field))["configured"] is True


def _google(**secrets):
    return model(service="gemini", protocol="cli", auth_type="cli", endpoint="", model="",
                 metadata={"harness": "antigravity", "plan": "google"}, secrets=secrets)


def test_the_google_plan_is_valid_with_and_without_its_pasted_document(registry, monkeypatch):
    """The CLI surface runs this machine's agy, which signs in itself: an
    entry with nothing pasted is complete, and Verify says so. A pasted
    acp_token.json, for the Annotation and Chat surfaces, is still checked."""
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("no request"))
    bare = accounts.create_account(_google())
    assert bare["configured"] is True
    checked = account_adapters.check_draft(_google())
    assert checked["valid"] is True
    assert "the CLI surface runs Antigravity's agy on this machine, which signs in itself" in checked["message"]
    assert "the Annotation and Chat surfaces need the pasted credential (acp_token.json)" in checked["message"]
    token = json.dumps({"type": "authorized_user", "client_id": "id", "client_secret": "secret",
                        "refresh_token": "refresh", "token_uri": "https://oauth2.googleapis.com/token"})
    assert accounts.create_account(_google(credential_json=token))["configured"] is True
    assert account_adapters.check_draft(_google(credential_json=token))["valid"] is True
    bad = account_adapters.check_draft(_google(credential_json='{"refresh_token": "x"}'))
    assert bad["valid"] is False and "not the Antigravity ACP server's sign-in" in bad["message"]


def test_a_google_entry_without_a_document_leases_nothing_and_names_the_paste(registry):
    """The Annotation and Chat surfaces read the pasted acp_token.json: with
    none, the lease is refused naming the paste and the CLI surface; nothing
    is left in the lease store and nothing is collected back."""
    saved = accounts.create_account(_google())
    private = accounts.resolve_accounts({"model": saved["id"]})["model"]
    with pytest.raises(accounts.AccountError) as refused:
        accounts.materialize_cli_account(private, "run")
    message = str(refused.value)
    assert "acp_token.json" in message and "Annotation and Chat" in message
    assert "only the CLI surface runs" in message
    assert not list(accounts._cli_root().glob(f"{saved['credential_account_id']}-*"))
    assert accounts.private_account(saved["id"])["secrets"] == {}


def test_a_claude_subscription_is_complete_without_its_token(registry):
    base = {"name": "Claude plan", "kind": "account", "service": "anthropic", "protocol": "cli",
            "auth_type": "subscription", "metadata": {"harness": "claude-code", "plan": "claude_code"},
            "secrets": {}}
    assert accounts.create_account(base)["configured"] is True


@pytest.mark.parametrize("plant", ["link", "fifo"])
def test_cli_lease_release_never_follows_a_planted_link(registry, tmp_path, monkeypatch, plant):
    # A harness container mounts the lease read-write and can replace the
    # login with a link to any host file (or a FIFO). Release must not read
    # through it into the keyring, and the next lease must refuse it.
    from pathlib import Path
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    document = json.dumps({"tokens": {"refresh_token": "initial"}})
    saved = accounts.create_account(model(auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": document}))
    snapshot = accounts.resolve_accounts({"model": saved["id"]})["model"]
    first = accounts.materialize_cli_account(snapshot, "run-one")
    second = accounts.materialize_cli_account(snapshot, "run-two")
    host_file = tmp_path / "host-only.json"
    host_file.write_text(json.dumps({"tokens": {"refresh_token": "HOST-ONLY"}}))
    shared = Path(first["path"]) / "codex/auth.json"
    shared.unlink()
    if plant == "link":
        shared.symlink_to(host_file)
    else:
        os.mkfifo(shared)
    assert accounts.release_cli_account(first["lease_id"], "run-one")["committed"] is False
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    with pytest.raises(accounts.AccountError, match="not a regular file"):
        accounts.materialize_cli_account(snapshot, "run-three")
    accounts.release_cli_account(second["lease_id"], "run-two")
    assert host_file.exists()


def test_cli_run_cleanup_releases_only_its_own_lease(registry, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    saved = accounts.create_account(model(auth_type="cli", metadata={"harness": "codex"}, secrets={"credential_json": '{"tokens":{"refresh_token":"r"}}'}))
    snapshot = accounts.private_account(saved["id"])
    lease = accounts.materialize_cli_account(snapshot, "owner")
    with pytest.raises(accounts.AccountError, match="does not belong"):
        accounts.release_cli_account(lease["lease_id"], "other")
    accounts.release_run_account_leases("owner")
    assert not (runtime / (saved["id"] + "-" + saved["generation"])).exists()


def _cli_login(registry, tmp_path, monkeypatch, harness="codex", **overrides):
    """A saved plan login of ``harness`` and a runtime root of its own."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(accounts, "_cli_root", lambda: runtime)
    login = PLAN_LOGINS[harness]
    plan = next(auth for auth in MODEL_HARNESS_RULES[harness]["auths"] if auth != "api_key")
    document = "opaque-token" if login.field == "credential_file" else json.dumps({"tokens": {"refresh_token": "initial"}})
    saved = accounts.create_account(model(service=PLAN_PROVIDERS[plan], protocol="cli", auth_type="cli", endpoint="",
                                          metadata={"harness": harness, "plan": plan}, secrets={login.field: document},
                                          **overrides))
    return runtime, saved, document


def _orphan(lease, pid):
    """What a server that died holding ``lease`` leaves: the directory and
    its manifest, with nothing in this process holding the lease."""
    from pathlib import Path
    directory, _ = accounts._OWNED_CLI_LEASES.pop(lease["lease_id"])
    state = accounts._lease_manifest(directory)
    state["leases"][lease["lease_id"]]["pid"] = pid
    accounts._save_leases(directory, state)
    return Path(lease["path"])


@pytest.mark.parametrize("pid", [999999999, os.getpid()], ids=["dead-pid", "reused-own-pid"])
def test_sweep_collects_a_dead_servers_cli_lease_back_and_removes_it__F0457(registry, tmp_path, monkeypatch, pid):
    # A server that crashed never released its CLI lease: the plaintext login
    # stays in temp, and a rotation the run made is never saved. The sweep
    # commits the document back exactly as a release would, then deletes the
    # directory. A lease under this process's own pid that it does not hold
    # was left by an earlier server with the same pid (a container restart).
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (_orphan(lease, pid) / "codex/auth.json").write_text(rotated)
    accounts.sweep_expired_account_leases()
    assert not any(runtime.iterdir())
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated
    assert accounts.get_account(saved["id"])["generation"] == saved["generation"]


@pytest.mark.parametrize("damage", ["not-a-login", "manager-edit"])
def test_sweep_deletes_a_dead_lease_it_must_not_collect__F0457(registry, tmp_path, monkeypatch, damage):
    # A document that parses but is not a login object holds no login, and a
    # lease of a generation the Key manager has since replaced cannot revive
    # it: neither is committed, and the plaintext still goes.
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    placed = _orphan(lease, 999999999) / "codex/auth.json"
    expected = document
    if damage == "not-a-login":
        placed.write_text("[]")
    else:
        placed.write_text(json.dumps({"tokens": {"refresh_token": "stale-run"}}))
        expected = json.dumps({"tokens": {"refresh_token": "new-login"}})
        accounts.update_account(saved["id"], {"secrets": {"credential_json": expected}})
    accounts.sweep_expired_account_leases()
    assert not any(runtime.iterdir())
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == expected


def test_sweep_keeps_a_lease_a_live_run_still_holds__F0457(registry, tmp_path, monkeypatch):
    # One dead lease beside a live one: the dead entry leaves the manifest,
    # the shared document stays for the live run, and nothing is collected
    # until that run releases it.
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    snapshot = accounts.resolve_accounts({"model": saved["id"]})["model"]
    dead = accounts.materialize_cli_account(snapshot, "crashed-run")
    live = accounts.materialize_cli_account(snapshot, "live-run")
    directory = _orphan(dead, 999999999)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (directory / "codex/auth.json").write_text(rotated)
    accounts.sweep_expired_account_leases()
    assert set(accounts._lease_manifest(directory)["leases"]) == {live["lease_id"]}
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    assert accounts.release_cli_account(live["lease_id"], "live-run")["committed"]
    assert not directory.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


def test_sweep_removes_a_login_whose_lease_manifest_never_landed__F0457(registry, tmp_path, monkeypatch):
    # A server that died between writing the document and saving the lease
    # handed that document to no run: there is nothing to collect.
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    directory = _orphan(lease, 999999999)
    (directory / "leases.json").unlink()
    accounts.sweep_expired_account_leases()
    assert not any(runtime.iterdir())
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document


def test_sweep_keeps_a_dead_lease_whose_rotation_cannot_be_saved_yet__F0457(registry, tmp_path, monkeypatch):
    # A keyring that refuses the write must not cost the rotated login: the
    # directory stays for the next sweep, which collects it.
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    directory = _orphan(lease, 999999999)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (directory / "codex/auth.json").write_text(rotated)
    original = keystore.set_key
    monkeypatch.setattr(keystore, "set_key", lambda slot, value: False)
    accounts.sweep_expired_account_leases()
    assert (directory / "codex/auth.json").read_text() == rotated
    monkeypatch.setattr(keystore, "set_key", original)
    accounts.sweep_expired_account_leases()
    assert not directory.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


# Login documents a contained run can leave that the server cannot read, each
# with the harness whose store holds it: nested past the parser's recursion
# limit, not UTF-8 (antigravity's token file too, which is never parsed as
# JSON), cut off mid-write, and larger than _MAX_LEASE_BYTES. The oversized
# ones still read as a login in the _MAX_LEASE_BYTES + 1 bytes the server
# reads of them (a JSON login padded with whitespace; a token file one byte
# over the cap), so only the size check turns them away.
_UNREADABLE_LOGINS = {
    "nested-200k": ("codex", b"[" * 200_000),
    "not-utf-8": ("codex", b'{"tokens": {"refresh_token": "\xff"}}'),
    "not-utf-8-token-file": ("antigravity", b"token-\xff-bytes"),
    "half-written": ("codex", b'{"tokens": {"refresh_to'),
    "oversized-padded": ("codex", json.dumps({"tokens": {"refresh_token": "rotated"}}).encode()
                         + b" " * accounts._MAX_LEASE_BYTES),
    "oversized-token-file": ("antigravity", b"A" * (accounts._MAX_LEASE_BYTES + 1)),
}


@pytest.mark.parametrize("unreadable", sorted(_UNREADABLE_LOGINS))
def test_sweep_leaves_a_login_it_cannot_read_and_sweeps_the_rest__F0457(registry, tmp_path, monkeypatch, caplog, unreadable):
    # The harness owns its login document read-write. One it left that the
    # server cannot read is neither committed nor taken for no login: its
    # store stays as it is and is named, and every other store in the same
    # sweep is still collected. Once a Key manager edit replaces the
    # account's generation, the store has nothing left to collect and goes
    # without its document being read.
    import logging
    harness, body = _UNREADABLE_LOGINS[unreadable]
    login = PLAN_LOGINS[harness]
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch, harness=harness)
    other = accounts.create_account(model(name="Other", auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "other"}})}))
    stores = {}
    for account in (saved, other):
        lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": account["id"]})["model"], "crashed-run")
        stores[account["id"]] = _orphan(lease, 999999999)
    hostile = stores[saved["id"]].joinpath(harness, *login.lease)
    hostile.write_bytes(body)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (stores[other["id"]] / "codex/auth.json").write_text(rotated)
    with caplog.at_level(logging.WARNING, logger="accounts"):
        accounts.sweep_expired_account_leases()
    assert hostile.read_bytes() == body
    assert f"{stores[saved['id']].name} kept for the next sweep: The login document" in caplog.text
    assert accounts.private_account(saved["id"])["secrets"][login.field] == document
    assert not stores[other["id"]].exists()
    assert accounts.private_account(other["id"])["secrets"]["credential_json"] == rotated
    pasted = json.dumps({"tokens": {"refresh_token": "pasted-again"}})
    accounts.update_account(saved["id"], {"secrets": {login.field: pasted}})
    accounts.sweep_expired_account_leases()
    assert not any(runtime.iterdir())
    assert accounts.private_account(saved["id"])["secrets"][login.field] == pasted


@pytest.mark.parametrize("unreadable", sorted(_UNREADABLE_LOGINS))
def test_a_login_the_server_cannot_read_never_fails_a_later_run_start__F0457(registry, tmp_path, monkeypatch, unreadable):
    # The run's own release cannot read the document either. It keeps the
    # store with no lease and says so with an account error, never the
    # parser's, and neither it nor any later sweep commits the document or
    # fails the run start of another account.
    from pathlib import Path
    harness, body = _UNREADABLE_LOGINS[unreadable]
    login = PLAN_LOGINS[harness]
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch, harness=harness)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "hostile-run")
    directory = Path(lease["path"])
    directory.joinpath(harness, *login.lease).write_bytes(body)
    with pytest.raises(accounts.AccountError, match="cannot be read"):
        accounts.release_cli_account(lease["lease_id"], "hostile-run")
    assert accounts._lease_manifest(directory)["leases"] == {}
    other = accounts.create_account(model(name="Other", auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "other"}})}))
    for start in ("first-run", "second-run"):
        assert accounts.retain_account_context(accounts.resolve_accounts({"model": other["id"]}), start)
        accounts.release_run_account_leases(start)
    assert directory.joinpath(harness, *login.lease).read_bytes() == body
    assert accounts.private_account(saved["id"])["secrets"][login.field] == document


@pytest.mark.parametrize("unreadable", ["half-written", "oversized-token-file"])
@pytest.mark.parametrize("kept_by", ["its-release", "a-sweep"])
def test_a_login_the_server_cannot_read_is_never_leased_to_the_accounts_next_run__F0457(registry, tmp_path, monkeypatch,
                                                                                         kept_by, unreadable):
    # A store kept because the server cannot read its login, by the run's own
    # release or by a sweep after its server died, is held by no run and is
    # never leased again: the account's next run start is refused with what
    # to do, and the document stays as it is. Pasting the login in Key
    # manager again makes a new generation, whose run gets that login in a
    # store of its own, and the kept store is swept.
    from pathlib import Path
    harness, body = _UNREADABLE_LOGINS[unreadable]
    login = PLAN_LOGINS[harness]
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch, harness=harness)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "hostile-run")
    kept = Path(lease["path"])
    kept.joinpath(harness, *login.lease).write_bytes(body)
    if kept_by == "its-release":
        with pytest.raises(accounts.AccountError, match="cannot be read"):
            accounts.release_cli_account(lease["lease_id"], "hostile-run")
    else:
        _orphan(lease, 999999999)
    with pytest.raises(accounts.AccountError, match="paste the login document in Key manager again"):
        accounts.retain_account_context(accounts.resolve_accounts({"model": saved["id"]}), "next-run")
    assert kept.joinpath(harness, *login.lease).read_bytes() == body
    assert all(held["owner"] != "next-run" for held in accounts._lease_manifest(kept)["leases"].values())
    assert all(owner != "next-run" for _, owner in accounts._OWNED_CLI_LEASES.values())
    assert accounts.private_account(saved["id"])["secrets"][login.field] == document
    pasted = json.dumps({"tokens": {"refresh_token": "pasted-again"}})
    accounts.update_account(saved["id"], {"secrets": {login.field: pasted}})
    assert accounts.retain_account_context(accounts.resolve_accounts({"model": saved["id"]}), "next-run")
    [store] = runtime.iterdir()
    assert store != kept
    assert store.joinpath(harness, *login.lease).read_text() == pasted
    accounts.release_run_account_leases("next-run")
    assert not any(runtime.iterdir())


@contextmanager
def _recording_reads():
    """While in effect, each file os.fdopen opens records the size every
    read() of it asks for and how many bytes those reads return."""
    opened = []
    real_fdopen = os.fdopen

    class Recorded:
        def __init__(self, handle):
            self.handle, self.asked, self.returned = handle, [], 0

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self.handle.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            data = self.handle.read(size)
            self.asked.append(size)
            self.returned += len(data)
            return data

    def fdopen(*args, **kwargs):
        opened.append(Recorded(real_fdopen(*args, **kwargs)))
        return opened[-1]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "fdopen", fdopen)
        yield opened


@pytest.mark.parametrize("judge", ["sweep", "release"])
def test_the_server_reads_no_more_of_a_login_than_its_cap__F0457(registry, tmp_path, monkeypatch, judge):
    # A harness can grow its login document without bound, and every sweep
    # looks again at a store it kept. However large the document, the sweep
    # and the release ask for at most _MAX_LEASE_BYTES + 1 bytes of it, just
    # enough to know it is too large.
    from pathlib import Path
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "run")
    oversized = json.dumps({"tokens": {"refresh_token": "rotated"}}).encode() + b" " * (4 * accounts._MAX_LEASE_BYTES)
    placed = Path(lease["path"]) / "codex/auth.json"
    placed.write_bytes(oversized)
    if judge == "sweep":
        _orphan(lease, 999999999)
    with _recording_reads() as opened:
        if judge == "sweep":
            accounts.sweep_expired_account_leases()
        else:
            with pytest.raises(accounts.AccountError, match="cannot be read"):
                accounts.release_cli_account(lease["lease_id"], "run")
    reads = [handle for handle in opened if handle.asked]
    assert reads
    for handle in reads:
        assert all(type(size) is int and 0 <= size <= accounts._MAX_LEASE_BYTES + 1 for size in handle.asked), handle.asked
        assert handle.returned <= accounts._MAX_LEASE_BYTES + 1
    assert placed.read_bytes() == oversized
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document


def test_a_release_leaves_a_login_another_run_is_rewriting_to_that_run__F0457(registry, tmp_path, monkeypatch, caplog):
    # Runs of one account share its store and one login document, which a
    # harness rewrites in place as its tokens rotate. A release that finds
    # the document mid-rewrite while another run still holds the store has
    # nothing to collect yet: it neither fails its own run nor commits the
    # half, and says so. A run that starts meanwhile shares the store as it
    # is, never refused for a document a live run may be rewriting, and the
    # last run out collects the finished document.
    import logging
    from pathlib import Path
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    snapshot = accounts.resolve_accounts({"model": saved["id"]})["model"]
    rewriting = accounts.materialize_cli_account(snapshot, "rewriting-run")
    finished = accounts.materialize_cli_account(snapshot, "finished-run")
    store = Path(rewriting["path"])
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (store / "codex/auth.json").write_text(rotated[:20])
    with caplog.at_level(logging.WARNING, logger="accounts"):
        assert accounts.release_cli_account(finished["lease_id"], "finished-run") == {"committed": False}
    assert f"{store.name}: its login document cannot be read yet" in caplog.text
    assert set(accounts._lease_manifest(store)["leases"]) == {rewriting["lease_id"]}
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    joining = accounts.materialize_cli_account(snapshot, "joining-run")
    assert joining["path"] == rewriting["path"]
    (store / "codex/auth.json").write_text(rotated)
    assert accounts.release_cli_account(rewriting["lease_id"], "rewriting-run")["committed"]
    assert accounts.release_cli_account(joining["lease_id"], "joining-run")["committed"]
    assert not store.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


def test_a_release_still_raises_a_rotation_it_could_not_save_beside_another_run__F0457(registry, tmp_path, monkeypatch):
    # Only a document mid-rewrite is excused while another run holds the
    # store. A rotation the keyring refuses is an error the releasing run
    # hears about; the store and the other run's lease stay, and the last
    # run out saves the rotation once the keyring takes it.
    from pathlib import Path
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    snapshot = accounts.resolve_accounts({"model": saved["id"]})["model"]
    staying = accounts.materialize_cli_account(snapshot, "staying-run")
    leaving = accounts.materialize_cli_account(snapshot, "leaving-run")
    store = Path(staying["path"])
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (store / "codex/auth.json").write_text(rotated)
    original = keystore.set_key
    monkeypatch.setattr(keystore, "set_key", lambda slot, value: False)
    with pytest.raises(accounts.KeyringUnavailable):
        accounts.release_cli_account(leaving["lease_id"], "leaving-run")
    assert set(accounts._lease_manifest(store)["leases"]) == {staying["lease_id"]}
    monkeypatch.setattr(keystore, "set_key", original)
    assert accounts.release_cli_account(staying["lease_id"], "staying-run")["committed"]
    assert not store.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


def test_a_run_start_that_fails_releases_every_lease_it_took__F0457(registry, tmp_path, monkeypatch, caplog):
    # A run start that fails part-way releases each lease it took on its
    # own: a release that fails is named, the leases after it are still
    # released, and the caller sees what stopped the start, not the
    # release's error.
    import logging
    runtime, first, _ = _cli_login(registry, tmp_path, monkeypatch)
    second, third = (accounts.create_account(model(name=name, auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                                   secrets={"credential_json": json.dumps({"tokens": {"refresh_token": name}})}))
                     for name in ("Second", "Third"))
    context = accounts.resolve_accounts({"first": first["id"], "second": second["id"], "third": third["id"]})
    real_materialize, real_collect = accounts.materialize_cli_account, accounts._collect_cli_login

    def materialize(account, owner):
        if account["credential_account_id"] == third["id"]:
            raise accounts.AccountError("the third account cannot start")
        return real_materialize(account, owner)

    def collect(directory, state):
        if state["identity"] == first["id"]:
            raise TypeError("a surprise")
        return real_collect(directory, state)
    monkeypatch.setattr(accounts, "materialize_cli_account", materialize)
    monkeypatch.setattr(accounts, "_collect_cli_login", collect)
    with caplog.at_level(logging.WARNING, logger="accounts"):
        with pytest.raises(accounts.AccountError, match="the third account cannot start"):
            accounts.retain_account_context(context, "failed-start")
    assert all(owner != "failed-start" for _, owner in accounts._OWNED_CLI_LEASES.values())
    assert not (runtime / f"{second['id']}-{second['generation']}").exists()
    [record] = [record for record in caplog.records if "whose start failed" in record.getMessage()]
    assert "failed-start" in record.getMessage() and isinstance(record.exc_info[1], TypeError)


@pytest.mark.parametrize("phase", ["scan", "collect"])
def test_sweep_goes_on_past_a_store_it_cannot_judge__F0457(registry, tmp_path, monkeypatch, caplog, phase):
    # Whatever error judging one store raises, in the unlocked scan or while
    # collecting it under the lock, that store is kept and named, with the
    # traceback of an error no rule anticipates, and the sweep still
    # collects every other store.
    import logging
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    other = accounts.create_account(model(name="Other", auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "other"}})}))
    stores = {}
    for account in (saved, other):
        lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": account["id"]})["model"], "crashed-run")
        stores[account["id"]] = _orphan(lease, 999999999)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    for directory in stores.values():
        (directory / "codex/auth.json").write_text(rotated)
    broken = stores[saved["id"]]
    target = "_cli_lease_state" if phase == "scan" else "_collect_cli_login"
    real = getattr(accounts, target)

    def surprise(directory, *args):
        if directory == broken:
            raise TypeError("a surprise")
        return real(directory, *args)
    monkeypatch.setattr(accounts, target, surprise)
    with caplog.at_level(logging.WARNING, logger="accounts"):
        accounts.sweep_expired_account_leases()
    assert (broken / "codex/auth.json").read_text() == rotated
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    assert not stores[other["id"]].exists()
    assert accounts.private_account(other["id"])["secrets"]["credential_json"] == rotated
    [record] = [record for record in caplog.records if broken.name in record.getMessage()]
    assert "a surprise" in record.getMessage() and record.exc_info


@pytest.mark.parametrize("pid", [2**70, -1, 0, True, "4242"], ids=["out-of-range", "negative", "zero", "bool", "text"])
def test_sweep_treats_a_lease_naming_no_process_as_dead__F0457(registry, tmp_path, monkeypatch, pid):
    # A manifest pid that os.kill cannot take, or one naming a process group
    # rather than a server, is held by no live server. The store is collected
    # and removed like any dead lease, instead of failing every run start or
    # staying forever.
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (_orphan(lease, pid) / "codex/auth.json").write_text(rotated)
    accounts.sweep_expired_account_leases()
    assert not any(runtime.iterdir())
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


@pytest.mark.parametrize("damage", ["unparseable", "nested-too-deep", "identity-missing", "identity-not-a-string",
                                    "leases-a-list", "names-another-account", "harness-missing", "harness-unknown",
                                    "harness-a-list", "harness-an-object"])
def test_sweep_leaves_a_store_whose_manifest_is_unusable__F0457(registry, tmp_path, monkeypatch, caplog, damage):
    # A manifest the sweep cannot use cannot say whether a live run holds the
    # store, so the store stays, and the sweep names it rather than passing it
    # by or failing the run start that swept. A manifest that parses is no
    # better when it lacks an identity or a known harness, types a field
    # wrongly (a harness that is not a string cannot even be looked up) or
    # names an account and generation other than its folder's: the document
    # in it is never committed into that other account. The manifest check
    # itself turns each away, not the sweep's last-resort catch.
    import logging
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    victim_document = json.dumps({"tokens": {"refresh_token": "victim"}})
    victim = accounts.create_account(model(name="Victim", auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                           secrets={"credential_json": victim_document}))
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    directory = _orphan(lease, 999999999)
    (directory / "codex/auth.json").write_text(json.dumps({"tokens": {"refresh_token": "planted"}}))
    state = accounts._lease_manifest(directory)
    if damage == "unparseable":
        (directory / "leases.json").write_text("{broken")
    elif damage == "nested-too-deep":
        (directory / "leases.json").write_text("[" * 200_000)
    else:
        if damage == "identity-missing":
            del state["identity"]
        elif damage == "identity-not-a-string":
            state["identity"] = 5
        elif damage == "leases-a-list":
            state["leases"] = list(state["leases"].values())
        elif damage == "names-another-account":
            state.update(identity=victim["id"], generation=victim["generation"])
        elif damage == "harness-missing":
            del state["harness"]
        elif damage == "harness-unknown":
            state["harness"] = "bogus"
        elif damage == "harness-a-list":
            state["harness"] = ["codex"]
        else:
            state["harness"] = {"codex": True}
        (directory / "leases.json").write_text(json.dumps(state))
    with caplog.at_level(logging.WARNING, logger="accounts"):
        accounts.sweep_expired_account_leases()
    assert (directory / "codex/auth.json").is_file()
    assert f"{directory.name} has an unusable lease manifest" in caplog.text
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == document
    assert accounts.private_account(victim["id"])["secrets"]["credential_json"] == victim_document


def test_sweep_rejudges_a_store_materialized_while_it_scanned__F0457(registry, tmp_path, monkeypatch):
    # The scan takes no lock. A store another server is materializing reads
    # as one whose manifest never landed (so dead) when scanned, and carries
    # that server's live lease once the registry lock is taken: judged again
    # under the lock, it stays. The other server is a child of this test, a
    # live pid wherever pytest runs (as PID 1 in a container, its parent
    # would be pid 0, which names no process).
    import subprocess
    import sys
    runtime, saved, document = _cli_login(registry, tmp_path, monkeypatch)
    directory = runtime / f"{saved['id']}-{saved['generation']}"
    (directory / "codex").mkdir(parents=True, mode=0o700)
    (directory / "codex/auth.json").write_text(document)
    other_server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        leases = {"other-server": {"pid": other_server.pid, "owner": "other-run"}}
        manifest = {"identity": saved["id"], "generation": saved["generation"], "harness": "codex", "leases": leases}
        real_locked = accounts._locked

        @contextmanager
        def materialized_meanwhile():
            accounts._save_leases(directory, manifest)
            with real_locked():
                yield
        monkeypatch.setattr(accounts, "_locked", materialized_meanwhile)
        accounts.sweep_expired_account_leases()
        assert (directory / "codex/auth.json").read_text() == document
        assert accounts._lease_manifest(directory)["leases"] == leases
    finally:
        other_server.kill()
        other_server.wait()


def test_release_keeps_a_login_it_could_not_save_for_the_sweep__F0457(registry, tmp_path, monkeypatch):
    # The last run out collects the login before the store goes. When the
    # keyring refuses that write, the store stays with no lease instead of
    # taking the rotation with it, and the next sweep collects it.
    from pathlib import Path
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "run")
    directory = Path(lease["path"])
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (directory / "codex/auth.json").write_text(rotated)
    original = keystore.set_key
    monkeypatch.setattr(keystore, "set_key", lambda slot, value: False)
    with pytest.raises(accounts.KeyringUnavailable):
        accounts.release_cli_account(lease["lease_id"], "run")
    assert lease["lease_id"] not in accounts._OWNED_CLI_LEASES
    assert accounts._lease_manifest(directory)["leases"] == {}
    assert (directory / "codex/auth.json").read_text() == rotated
    monkeypatch.setattr(keystore, "set_key", original)
    accounts.sweep_expired_account_leases()
    assert not directory.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


def test_sweep_leaves_another_registrys_cli_store__F0457(registry, tmp_path, monkeypatch):
    # A second profile or a test server shares HOME and temp with the user's
    # server but has its own registry and registry lock. Each registry keeps
    # its stores under a runtime root of its own, so a sweep made on another
    # registry, which holds none of this store's accounts, neither deletes it
    # nor loses the rotation in it; this registry's own sweep collects it.
    import tempfile
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(accounts, "_cli_root", _REAL_CLI_ROOT)
    saved = accounts.create_account(model(auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "initial"}})}))
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    directory = _orphan(lease, 999999999)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (directory / "codex/auth.json").write_text(rotated)
    own_registry = accounts.CONFIG_PATH
    monkeypatch.setattr(accounts, "CONFIG_PATH", tmp_path / "other-profile" / "accounts.json")
    assert accounts._cli_root() != directory.parent
    accounts.sweep_expired_account_leases()
    assert (directory / "codex/auth.json").read_text() == rotated
    monkeypatch.setattr(accounts, "CONFIG_PATH", own_registry)
    accounts.sweep_expired_account_leases()
    assert not directory.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated


def test_run_start_sweeps_dead_cli_leases_before_leasing__F0457(registry, tmp_path, monkeypatch):
    # The retain sweep runs for a CLI login too, not only for OAuth chains: a
    # crashed server's lease of any account is collected when a run starts.
    runtime, saved, _ = _cli_login(registry, tmp_path, monkeypatch)
    lease = accounts.materialize_cli_account(accounts.resolve_accounts({"model": saved["id"]})["model"], "crashed-run")
    directory = _orphan(lease, 999999999)
    rotated = json.dumps({"tokens": {"refresh_token": "rotated"}})
    (directory / "codex/auth.json").write_text(rotated)
    other = accounts.create_account(model(name="Other", auth_type="cli", metadata={"harness": "codex", "plan": "chatgpt"},
                                          secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "other"}})}))
    leases = accounts.retain_account_context(accounts.resolve_accounts({"model": other["id"]}), "next-run")
    assert not directory.exists()
    assert accounts.private_account(saved["id"])["secrets"]["credential_json"] == rotated
    accounts.release_run_account_leases("next-run")
    assert leases and not any(runtime.iterdir())


def test_server_startup_sweeps_account_leases__F0457(api_app, monkeypatch):
    # No run start may come for days after a crash; the server sweeps what a
    # previous process left as it starts, in the background after its orphan
    # container reaps, so boot never waits on the registry or the keyring.
    import threading
    import docker
    import sandbox
    from fastapi.testclient import TestClient

    def no_daemon(*args, **kwargs):
        raise docker.errors.DockerException("no docker daemon in this test")
    # The reaps ahead of the sweep find nothing to list or remove here, so
    # the wait times the sweep's place in the chain, never a docker call.
    monkeypatch.setattr(docker, "from_env", no_daemon)
    monkeypatch.setattr(sandbox, "reap_orphan_containers", lambda: None)
    swept = threading.Event()
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", swept.set)
    with TestClient(api_app):
        assert swept.wait(30)


def test_failed_metadata_write_rolls_back_new_secrets(registry, monkeypatch):
    saved = accounts.create_account(model())
    original = accounts.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(accounts, "_write", lambda _: (_ for _ in ()).throw(OSError("full disk")))
    with pytest.raises(OSError):
        accounts.update_account(saved["id"], {"secrets": {"api_key": "replacement"}})
    assert accounts.CONFIG_PATH.read_bytes() == original
    assert list(registry.values()) == ["private-one"]


def test_missing_keyring_never_falls_back_to_plaintext(registry, monkeypatch):
    monkeypatch.setattr(keystore, "_keychain_ok", False)
    response = client().post("/api/accounts", json=model())
    assert response.status_code == 503
    assert not accounts.CONFIG_PATH.exists()
    assert not registry


def test_capabilities_and_multiple_roles_are_local_and_scoped(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("unexpected live request"))
    first = accounts.create_account(model())
    second = accounts.create_account(model(name="Personal", secrets={"api_key": "private-two"}))
    context = accounts.resolve_accounts({"think": first["id"], "write": second["id"]}, {
        "think": {"protocols": ["openai"], "capabilities": ["reasoning"]},
        "write": {"services": ["openai"], "capabilities": ["text"]},
    })
    assert context["think"]["secrets"]["api_key"] == "private-one"
    assert context["write"]["secrets"]["api_key"] == "private-two"
    assert "OPENAI_API_KEY" not in accounts.account_environment(context)
    assert accounts.account_environment({"think": context["think"]})["OPENAI_API_KEY"] == "private-one"
    with pytest.raises(accounts.AccountError, match="lacks capabilities"):
        accounts.resolve_accounts({"vision": first["id"]}, {"vision": {"capabilities": ["vision"]}})


def test_shared_credential_model_choices_and_unresolved_delete(registry):
    owner = accounts.create_account(model(kind="account", model=""))
    choice = accounts.create_account(model(secrets={}, credential_account_id=owner["id"]))
    assert len(registry) == 1
    snapshot = accounts.resolve_accounts({"model": choice["id"]})["model"]
    assert snapshot["credential_account_id"] == owner["id"]
    assert snapshot["secrets"]["api_key"] == "private-one"
    accounts.delete_account(owner["id"])
    assert accounts.get_account(choice["id"])["configured"] is False
    with pytest.raises(accounts.AccountNotFound):
        accounts.resolve_accounts({"model": choice["id"]})


def test_explicit_models_and_check_are_only_network_callers(registry, monkeypatch):
    calls = []
    def response(method, url, **kwargs):
        calls.append((method, url))
        assert kwargs["headers"]["Authorization"] == "Bearer private-one"
        return {"data": [{"id": "model-a", "capabilities": {"text": True, "vision": False}}]}
    monkeypatch.setattr(account_adapters, "request_service", response)
    api = client()
    saved = api.post("/api/accounts", json=model()).json()
    api.get("/api/accounts").raise_for_status()
    accounts.resolve_accounts({"model": saved["id"]})
    assert not calls
    listed = api.post(f"/api/accounts/{saved['id']}/models").json()
    assert listed["models"][0]["capabilities"] == ["text"]
    checked = api.post("/api/accounts/check", json=model(id=saved["id"], model="model-a")).json()
    assert checked["valid"] is True
    assert len(calls) == 2


def test_capabilities_belong_to_model_entries_and_derive_from_the_service(registry):
    api = client()
    rejected = {"name": "Work OpenAI", "kind": "account", "service": "openai", "capabilities": ["text"]}
    with pytest.raises(accounts.AccountError, match="belong to model entries"):
        accounts.create_account(rejected)
    owner = accounts.create_account(model(kind="account", model=""))
    assert owner["capabilities"] == ["text"]
    github = accounts.create_account(git_entry())
    assert github["capabilities"] == ["git"]
    with pytest.raises(accounts.AccountError, match="belong to model entries"):
        accounts.update_account(github["id"], {"capabilities": []})
    chosen = accounts.create_account({"name": "Chosen", "kind": "model", "model": "chosen-model",
        "credential_account_id": owner["id"], "capabilities": ["text", "vision"]})
    assert chosen["capabilities"] == ["text", "vision"]
    assert accounts.update_account(chosen["id"], {"name": "Renamed"})["capabilities"] == ["text", "vision"]
    inherited = accounts.create_account({"name": "Inherited", "kind": "model", "model": "other-model",
                                         "credential_account_id": owner["id"]})
    assert inherited["capabilities"] == ["text"]
    assert accounts.create_account(custom())["capabilities"] == []
    assert api.post("/api/accounts", json=rejected).status_code == 400


def test_draft_account_previews_a_save_without_writing_anything(registry):
    saved = accounts.create_account(model(kind="account", model=""))
    before = accounts.CONFIG_PATH.read_bytes()
    fresh = accounts.draft_account({"name": "New", "kind": "account", "service": "anthropic",
                                    "secrets": {"api_key": "draft-private"}})
    assert (fresh["protocol"], fresh["auth_type"], fresh["capabilities"]) == ("anthropic", "api_key", ["text"])
    assert fresh["secrets"] == {"api_key": "draft-private"}
    assert fresh["configured"] is True
    edited = accounts.draft_account({"id": saved["id"], "name": "Edited", "kind": "account",
        "service": "openai", "endpoint": "https://next.example/v1", "revision": "ignored", "secrets": {}})
    assert edited["endpoint"] == "https://next.example/v1"
    assert edited["secrets"] == {"api_key": "private-one"}
    typed = accounts.draft_account({"id": saved["id"], "secrets": {"api_key": "typed-in"}})
    assert typed["secrets"] == {"api_key": "typed-in"}
    cleared = accounts.draft_account({"id": saved["id"], "secrets": {"api_key": None}})
    assert cleared["secrets"] == {} and cleared["configured"] is False
    child = accounts.draft_account({"name": "Model", "kind": "model", "model": "chosen-model",
                                    "credential_account_id": saved["id"]})
    assert child["secrets"] == {"api_key": "private-one"}
    assert (child["endpoint"], child["capabilities"]) == (saved["endpoint"], ["text"])
    assert accounts.draft_account(custom())["secrets"] == {"signing_key": "custom-private"}
    with pytest.raises(accounts.AccountError, match="field definition marked secret"):
        accounts.draft_account(custom(secrets={"api_key": "stray"}))
    with pytest.raises(accounts.AccountError, match="Unknown account fields"):
        accounts.draft_account({"name": "Bad", "kind": "account", "service": "openai", "nope": 1})
    assert accounts.CONFIG_PATH.read_bytes() == before
    assert list(registry.values()) == ["private-one"]


def test_check_route_verifies_the_open_editor_and_never_saves(registry, monkeypatch):
    api = client()
    saved = api.post("/api/accounts", json=model()).json()
    seen = {}
    monkeypatch.setattr(account_adapters, "check_account",
                        lambda account: seen.update(account) or {"valid": True, "checked_at": "now"})
    result = api.post("/api/accounts/check", json=model(id=saved["id"], model="typed-model",
                                                       secrets={"api_key": "typed-key"})).json()
    assert result == {"valid": True, "checked_at": "now"}
    assert seen["model"] == "typed-model"
    assert seen["secrets"] == {"api_key": "typed-key"}
    assert accounts.get_account(saved["id"])["model"] == "chosen-model"
    assert list(registry.values()) == ["private-one"]
    monkeypatch.setattr(account_adapters, "check_account",
                        lambda account: {"valid": False, "message": "Service returned HTTP 401"})
    failed = api.post("/api/accounts/check", json=model(service="anthropic", protocol="anthropic")).json()
    assert failed["message"] == "Service returned HTTP 401"
    assert api.post(f"/api/accounts/{saved['id']}/check").status_code == 404


def test_check_reports_an_unreadable_keyring_rather_than_missing_setup(registry, monkeypatch):
    """A locked or absent keyring is a keyring failure, not an account waiting
    to be set up — the draft says so instead of offering sign-in advice for a
    credential that is already saved."""
    api = client()
    saved = api.post("/api/accounts", json=model()).json()
    monkeypatch.setattr(keystore, "get_key", lambda slot: None)
    blocked = api.post("/api/accounts/check", json=model(id=saved["id"], secrets={})).json()
    assert blocked["valid"] is False
    assert "keyring could not read" in blocked["message"]
    # A retyped secret needs nothing from the keyring, so it still verifies.
    monkeypatch.setattr(account_adapters, "check_account", lambda account: {"valid": True})
    typed = api.post("/api/accounts/check",
                     json=model(id=saved["id"], secrets={"api_key": "typed"})).json()
    assert typed["valid"] is True


def test_check_that_does_not_apply_carries_no_authentication_help(registry):
    """A custom entry has no endpoint to call and no authentication method on
    screen — an unapplicable check must not caption itself with one."""
    api = client()
    result = api.post("/api/accounts/check", json=custom()).json()
    assert result["valid"] is None
    assert "no credential check" in result["message"]
    assert "help" not in result


def test_check_reports_sign_in_and_model_membership_with_help(registry, monkeypatch):
    api = client()
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "model-a"}]})
    pending = api.post("/api/accounts/check", json={"name": "Copilot", "kind": "account",
        "service": "copilot", "auth_type": "cli", "metadata": {"harness": "opencode", "plan": "github_copilot"}, "secrets": {}}).json()
    assert pending["valid"] is False
    assert "Paste the app's login document" in pending["message"]
    missing = api.post("/api/accounts/check", json=model()).json()
    assert missing["valid"] is False
    assert "is not in the service's current model list" in missing["message"]
    assert api.post("/api/accounts/check", json=model(model="model-a")).json()["valid"] is True


def test_service_catalog_explains_every_offered_authentication(registry):
    for service in account_catalog.service_catalog():
        assert set(service["help"]) == set(service["auth_types"])
        assert all(isinstance(text, str) and text.strip() for text in service["help"].values())


def test_service_catalog_model_endpoints_match_sdk_taxonomy():
    # Harness support compares a record's endpoint with the SDK's standard
    # endpoints, so the catalog a new record copies from must agree with them.
    rows = [service for service in account_catalog.service_catalog() if service["id"] in STANDARD_MODEL_ENDPOINTS]
    assert rows
    for service in rows:
        assert service["endpoint"] == STANDARD_MODEL_ENDPOINTS[service["id"]]


def test_reject_secrets_in_metadata_and_stale_editor(registry):
    with pytest.raises(accounts.AccountError, match="stored as a secret"):
        accounts.create_account(model(metadata={"nested": {"access_token": "secret"}}))
    saved = accounts.create_account(model())
    accounts.update_account(saved["id"], {"name": "New"})
    with pytest.raises(accounts.AccountConflict):
        accounts.update_account(saved["id"], {"revision": saved["revision"], "name": "Stale"})
    with pytest.raises(ValueError, match="collimate_"):
        keystore.set_key("OPENAI_API_KEY", "must-not-save")


def test_account_defaults_and_model_subitems_inherit_credential_setup(registry, monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("unexpected discovery"))
    owner = accounts.create_account({
        "name": "Work OpenAI", "kind": "account", "service": "openai",
        "secrets": {"api_key": "work-secret"},
    })
    assert owner["endpoint"] == "https://api.openai.com/v1"
    assert owner["protocol"] == "openai"
    assert owner["capabilities"] == ["text"]
    assert "codex" in owner["supported_harnesses"]
    choice = accounts.create_account({
        "name": "Coding", "kind": "model", "model": "chosen-model",
        "credential_account_id": owner["id"], "metadata": {"harness": "codex"},
    })
    selected = accounts.resolve_accounts({"coding": choice["id"]}, {
        "coding": {"kind": "model", "harnesses": ["codex"], "auth_types": ["api_key"]},
    })["coding"]
    assert selected["metadata"]["harness"] == "codex"
    assert selected["endpoint"] == owner["endpoint"]
    assert selected["secrets"] == {"api_key": "work-secret"}
    assert len(registry) == 1
    with pytest.raises(accounts.AccountError, match="incompatible harness"):
        accounts.resolve_accounts({"coding": choice["id"]}, {"coding": {"harnesses": ["opencode"]}})


def test_existing_api_model_exposes_default_harness_without_rewriting_record(registry):
    saved = accounts.create_account(model())
    before = accounts.CONFIG_PATH.read_bytes()
    assert saved["metadata"]["harness"] == "pydantic-ai"
    selected = accounts.resolve_accounts({"model": saved["id"]}, {
        "model": {"harnesses": ["pydantic-ai"]},
    })["model"]
    assert selected["metadata"]["harness"] == "pydantic-ai"
    assert accounts.CONFIG_PATH.read_bytes() == before
    assert "harness" not in json.loads(before)["accounts"][saved["id"]]["metadata"]


def test_children_follow_account_connection_edits_but_active_snapshots_stay_frozen(registry):
    owner = accounts.create_account(model(kind="account", model=""))
    choice = accounts.create_account({"name": "Model", "kind": "model", "model": "test-model",
                                      "credential_account_id": owner["id"]})
    before = accounts.resolve_accounts({"model": choice["id"]})["model"]
    accounts.update_account(owner["id"], {"endpoint": "https://next.example/v1", "protocol": "openai-responses"})
    after = accounts.resolve_accounts({"model": choice["id"]}, {
        "model": {"protocols": ["openai-responses"]},
    })["model"]
    public = accounts.get_account(choice["id"])
    assert after["endpoint"] == public["endpoint"] == "https://next.example/v1"
    assert after["protocol"] == public["protocol"] == "openai-responses"
    assert before["endpoint"] == "https://models.example/v1"
    assert before["protocol"] == "openai"


def test_cli_child_exposes_inherited_harness_and_rejects_other_harness(registry):
    owner = accounts.create_account(model(kind="account", model="", auth_type="cli",
                                          metadata={"harness": "codex"},
                                          secrets={"credential_json": "private-cli"}))
    choice = accounts.create_account({"name": "Coding", "kind": "model", "model": "test-model",
                                      "credential_account_id": owner["id"]})
    assert choice["metadata"]["harness"] == "codex"
    assert choice["supported_harnesses"] == ["codex"]
    assert "model_configuration_error" not in choice
    assert accounts.resolve_accounts({"coding": choice["id"]}, {
        "coding": {"harness": "codex"},
    })["coding"]["metadata"]["harness"] == "codex"
    with pytest.raises(accounts.AccountError, match="signed-in harness"):
        accounts.update_account(choice["id"], {"metadata": {"harness": "opencode"}})


def test_plan_children_keep_the_account_harness(registry):
    owner = accounts.create_account({
        "name": "Copilot", "kind": "account", "service": "copilot", "auth_type": "cli",
        "metadata": {"harness": "opencode", "plan": "github_copilot"},
        "secrets": {"credential_json": "private-cli"},
    })
    choice = accounts.create_account({"name": "Coding", "kind": "model", "model": "test-model",
                                      "credential_account_id": owner["id"]})
    selected = accounts.resolve_accounts({"model": choice["id"]})["model"]
    assert selected["metadata"]["harness"] == "opencode"
    assert selected["supported_harnesses"] == ["opencode"]
    with pytest.raises(accounts.AccountError, match="signed-in harness"):
        accounts.update_account(choice["id"], {"metadata": {"harness": "pi"}})


def test_parent_harness_changes_allow_current_child_edits_and_discovery(registry):
    auth_type = "cli"
    owner = accounts.create_account(model(kind="account", model="", auth_type=auth_type, protocol="cli",
        endpoint="https://api.openai.com/v1", metadata={"harness": "codex"},
        secrets={"credential_json": "private-cli"}))
    children = [accounts.create_account({"name": name, "kind": "model", "model": "chosen-model",
        "credential_account_id": owner["id"], "metadata": {"harness": "codex"}})
        for name in ("Editable", "Checkable")]
    before = accounts.resolve_accounts({"model": children[0]["id"]})["model"]
    accounts.update_account(owner["id"], {"metadata": {"harness": "opencode"}})
    renamed = accounts.update_account(children[0]["id"], {"name": "Renamed"})
    assert renamed["metadata"]["harness"] == "opencode"
    current = accounts.get_account(children[1]["id"])
    discovered = {"models": [{"id": "cli-model"}], "supported": True}
    cached = accounts.cache_manager_result(current["id"], current["revision"], "model_discovery", discovered)
    assert cached["metadata"]["harness"] == "opencode"
    assert cached["metadata"]["model_discovery"] == discovered
    assert before["metadata"]["harness"] == "codex"
    with pytest.raises(accounts.AccountConflict):
        accounts.cache_manager_result(children[0]["id"], children[0]["revision"], "model_discovery",
                                      discovered, expected_generation=children[0]["generation"])


@pytest.mark.parametrize("change", [
    {"endpoint": "https://updated.example/v1"}, {"protocol": "openai-responses"},
    {"secrets": {"api_key": "replacement-private"}}, {"metadata": {"username": "another-user"}},
])
def test_connection_edits_invalidate_discovery_for_account_and_children(registry, change):
    owner = accounts.create_account(model(kind="account", model=""))
    child = accounts.create_account({"name": "Model", "kind": "model", "model": "chosen-model",
                                    "credential_account_id": owner["id"]})
    for saved in (owner, child):
        accounts.cache_manager_result(saved["id"], saved["revision"], "model_discovery", {
            "models": [{"id": "old-provider-model"}], "supported": True,
        })
    renamed = accounts.update_account(owner["id"], {"name": "Account display name"})
    assert "model_discovery" in renamed["metadata"]
    before = accounts.get_account(child["id"])
    accounts.update_account(owner["id"], change)
    for identity in (owner["id"], child["id"]):
        assert "model_discovery" not in accounts.get_account(identity)["metadata"]
    after = accounts.get_account(child["id"])
    assert before["generation"] != after["generation"]
    with pytest.raises(accounts.AccountConflict):
        accounts.update_account(child["id"], {"revision": before["revision"], "name": "Stale editor"})


@pytest.mark.parametrize("overrides", [
    {"service": "anthropic", "protocol": "anthropic", "endpoint": "https://api.anthropic.com/v1"},
    {"endpoint": "https://custom.example/v1"},
])
def test_incompatible_model_harness_can_be_saved_but_not_selected(registry, monkeypatch, overrides):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("unexpected discovery"))
    saved = accounts.create_account(model(metadata={"harness": "codex"}, **overrides))
    assert saved["configured"] is True  # Authentication remains separate from model compatibility.
    assert "codex" not in saved["supported_harnesses"]
    assert "pydantic-ai" in saved["supported_harnesses"]
    assert "Key manager" in saved["model_configuration_error"]
    with pytest.raises(accounts.AccountError, match="incompatible"):
        accounts.resolve_accounts({"model": saved["id"]})
    fixed = accounts.update_account(saved["id"], {"metadata": {"harness": "pydantic-ai"}})
    assert "model_configuration_error" not in fixed
    assert accounts.resolve_accounts({"model": saved["id"]})["model"]["model"] == "chosen-model"


def test_custom_and_unauthenticated_model_connections_use_explicit_api_endpoint(registry):
    saved = accounts.create_account(model(endpoint="https://custom.example/v1"))
    assert saved["supported_harnesses"] == ["pydantic-ai"]
    local = accounts.create_account(model(service="custom", protocol="openai-compatible", auth_type="none",
                                         endpoint="", secrets={}))
    assert local["configured"] is True
    assert local["supported_harnesses"] == []
    with pytest.raises(accounts.AccountError, match="incompatible"):
        accounts.resolve_accounts({"model": local["id"]})
    updated = accounts.update_account(local["id"], {"endpoint": "http://localhost:8000/v1"})
    assert updated["supported_harnesses"] == ["pydantic-ai"]
    assert accounts.resolve_accounts({"model": local["id"]})["model"]["secrets"] == {}


AUTHOR = {"author_name": "Octo", "author_email": "octo@example.com"}


def _load_ssh_keys():
    """sdk/tests/_ssh_keys.py, loaded by path: the throwaway SSH keys
    ssh-keygen makes once per process (no private key is committed)."""
    import importlib.util
    import sys
    path = Path(__file__).resolve().parents[2] / "sdk" / "tests" / "_ssh_keys.py"
    spec = importlib.util.spec_from_file_location("collimate_test_ssh_keys", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ssh_keys = _load_ssh_keys().throwaway_keys


def known_host():
    return "box.example.org " + ssh_keys().public()


def git_entry(**overrides):
    """A kind-git entry: a GitHub token with its commit author, unless overridden."""
    return {"name": "GitHub", "kind": "git", "service": "github", "metadata": dict(AUTHOR),
            "secrets": {"api_key": "github-private"}, **overrides}


def ssh_key(name="ed25519"):
    return ssh_keys()[name]


def _ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def synthetic_key(keytype: str) -> str:
    """An unencrypted ``openssh-key-v1`` key whose public blob names ``keytype``
    (ssh-keygen 10 makes neither DSA nor, without hardware, security keys)."""
    public = _ssh_string(keytype.encode()) + _ssh_string(b"\x01" * 32)
    private = struct.pack(">II", 7, 7) + _ssh_string(keytype.encode()) + b"\x00" * 40
    data = (b"openssh-key-v1\0" + _ssh_string(b"none") + _ssh_string(b"none") + _ssh_string(b"")
            + struct.pack(">I", 1) + _ssh_string(public) + _ssh_string(private))
    body = base64.b64encode(data).decode()
    lines = [body[i:i + 70] for i in range(0, len(body), 70)]
    return "\n".join(["-----BEGIN OPENSSH PRIVATE KEY-----", *lines, "-----END OPENSSH PRIVATE KEY-----"]) + "\n"


def test_git_services_offer_token_or_key(registry):
    with pytest.raises(accounts.AccountError, match="Unsupported authentication type 'basic' for service 'github'"):
        accounts.create_account(git_entry(auth_type="basic", secrets={"password": "github-private"}))
    saved = accounts.create_account(git_entry())
    assert (saved["auth_type"], saved["protocol"], saved["endpoint"]) == ("api_key", "github", "https://github.com")
    keyed = accounts.create_account(git_entry(service="gitlab", auth_type="ssh_key", secrets={"private_key": ssh_key()}))
    assert (keyed["protocol"], keyed["endpoint"], keyed["configured"]) == ("gitlab", "https://gitlab.com", True)
    assert keyed["secret_fields"] == ["private_key"]
    public = accounts.create_account(git_entry(service="git", auth_type="none", secrets={}))
    assert (public["endpoint"], public["configured"]) == ("", True)


def test_kind_must_fit_its_service(registry):
    for kind, service, expected in (("account", "github", "git"), ("git", "openai", "account or model"),
                                    ("model", "tavily", "account")):
        with pytest.raises(accounts.AccountError) as refused:
            accounts.create_account({"name": "Misfit", "kind": kind, "service": service, "metadata": dict(AUTHOR),
                                     "model": "chosen-model" if kind == "model" else ""})
        assert str(refused.value) == f"Account kind {kind!r} does not fit service {service!r} (expected {expected})"
    assert not accounts.CONFIG_PATH.exists() or not json.loads(accounts.CONFIG_PATH.read_text())["accounts"]


def test_git_entry_requires_identity_on_save_and_for_configured(registry):
    for metadata in ({}, {"author_name": "Octo"}, {"author_name": "  ", "author_email": "octo@example.com"}):
        with pytest.raises(accounts.AccountError, match="^Enter the commit author name and email: pushes are committed as them$"):
            accounts.create_account(git_entry(metadata=metadata))
    with pytest.raises(accounts.AccountError, match="^Commit author name must be one line without < or >$"):
        accounts.create_account(git_entry(metadata={**AUTHOR, "author_name": "Octo <octo@example.com>"}))
    with pytest.raises(accounts.AccountError, match="^Commit author name must be one line without < or >$"):
        accounts.create_account(git_entry(metadata={**AUTHOR, "author_name": "Octo\nCat"}))
    for email in ("octo", "octo@", "o cto@example.com", "a@b@c", "x" * 250 + "@example.com"):
        with pytest.raises(accounts.AccountError, match="^Commit author email must look like name@host$"):
            accounts.create_account(git_entry(metadata={**AUTHOR, "author_email": email}))
    saved = accounts.create_account(git_entry())
    assert saved["configured"] is True and saved["metadata"] == AUTHOR
    # A hand-edited registry that lost the author stays readable; the entry is
    # simply not configured, and Verify names what is missing.
    doc = json.loads(accounts.CONFIG_PATH.read_text())
    doc["accounts"][saved["id"]]["metadata"] = {}
    accounts.CONFIG_PATH.write_text(json.dumps(doc))
    assert accounts.get_account(saved["id"])["configured"] is False


def test_git_endpoint_rules_per_service(registry):
    from collimate import git_hosts
    refused = {
        ("github", "api_key", "http://github.com"): git_hosts._E_A,
        ("github", "api_key", "https://user:pw@github.com"): git_hosts._E_B,
        ("github", "api_key", "https://ghe.example.com/?q=1"): git_hosts._E_B,
        ("github", "api_key", "https://api.github.com"): git_hosts._E_C,
        ("github", "api_key", "https://ghe.example.com/api/v3"): git_hosts._E_C,
        ("gitlab", "api_key", "https://example.com/gitlab/api/v4"): git_hosts._E_D,
        ("git", "ssh_key", "https://git.example.org"): git_hosts._E_E,
        ("git", "none", "https://git.example.org"): git_hosts._E_E,
        ("git", "ssh_key", "http://x"): git_hosts._E_E,
        ("git", "none", "https://user:pw@git.example.org"): git_hosts._E_E,
    }
    for (service, auth, endpoint), message in refused.items():
        with pytest.raises(accounts.AccountError) as exc:
            accounts.create_account(git_entry(service=service, auth_type=auth, endpoint=endpoint, secrets={}))
        assert str(exc.value) == message, (service, auth, endpoint)
    assert git_hosts._E_C.startswith("A GitHub account's address is where its repositories open in a browser")
    assert accounts.create_account(git_entry(endpoint="https://GHE.example.com/"))["endpoint"] == "https://ghe.example.com"
    assert accounts.create_account(git_entry(endpoint="https://ghe.example.com:443/x/"))["endpoint"] == "https://ghe.example.com/x"
    assert accounts.create_account(git_entry(service="gitlab", endpoint="https://example.com:8443/gitlab"))["endpoint"] \
        == "https://example.com:8443/gitlab"
    basic = accounts.create_account(git_entry(service="git", auth_type="basic", endpoint="https://git.example.org/",
                                              metadata={**AUTHOR, "username": "ada"}, secrets={"password": "pw"}))
    assert (basic["endpoint"], basic["configured"]) == ("https://git.example.org", True)
    no_server = accounts.create_account(git_entry(service="git", auth_type="basic",
                                                  metadata={**AUTHOR, "username": "ada"}, secrets={"password": "pw"}))
    assert (no_server["endpoint"], no_server["configured"]) == ("", False)


def test_git_protocol_is_the_services(registry):
    with pytest.raises(accounts.AccountError, match="^A git account's protocol is fixed by its service$"):
        accounts.create_account(git_entry(protocol="openai-compatible"))
    saved = accounts.create_account(git_entry(protocol="github"))
    assert saved["protocol"] == "github"
    moved = accounts.update_account(saved["id"], {"service": "gitlab", "endpoint": ""})
    assert (moved["protocol"], moved["endpoint"]) == ("gitlab", "https://gitlab.com")


def test_ghe_verify_uses_api_v3_and_ghe_com_uses_api_subdomain(registry, monkeypatch):
    calls = []
    monkeypatch.setattr(account_adapters, "request_service",
                        lambda method, url, **kwargs: calls.append((method, url, kwargs["headers"])) or {"login": "octo"})
    for endpoint in ("https://ghe.example.com/", "https://acme.ghe.com"):
        result = account_adapters.check_draft(git_entry(endpoint=endpoint))
        assert (result["valid"], result["message"]) == (True, "Signed in as octo")
    assert [url for _, url, _ in calls] == ["https://ghe.example.com/api/v3/user", "https://api.acme.ghe.com/user"]
    assert calls[0][2] == {"Authorization": "Bearer github-private", "Accept": "application/vnd.github+json"}


def test_gitlab_verify_uses_private_token_never_a_query_and_names_the_username(registry, monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"username": "ada", "login": "not-this"}

    monkeypatch.setattr(account_adapters, "request_service", request)
    result = account_adapters.check_draft(git_entry(service="gitlab", endpoint="https://example.com/gitlab",
                                                    secrets={"api_key": "glpat-private"}))
    assert (result["valid"], result["identity"], result["message"]) == (True, "ada", "Signed in as ada")
    assert calls == [("GET", "https://example.com/gitlab/api/v4/user", {"headers": {"PRIVATE-TOKEN": "glpat-private"}})]
    assert "glpat-private" not in calls[0][1]


def test_gitlab_403_names_read_user(registry, monkeypatch):
    def forbidden(method, url, **kwargs):
        raise accounts.AccountError("Service returned HTTP 403")

    monkeypatch.setattr(account_adapters, "request_service", forbidden)
    result = account_adapters.check_draft(git_entry(service="gitlab", secrets={"api_key": "glpat-private"}))
    assert result["valid"] is False
    assert result["message"].startswith("GitLab accepted the token but not for its API: add the read_user scope")


def test_ssh_key_verify_is_offline_and_returns_fingerprint_and_public_key(registry, monkeypatch):
    import subprocess
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("Verify contacted a host"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Verify started a process"))
    fingerprints = ssh_keys().fingerprints
    result = account_adapters.check_draft(git_entry(auth_type="ssh_key", secrets={"private_key": ssh_key()}))
    assert result["valid"] is True
    assert result["message"] == f"Key accepted (ED25519 {fingerprints['ed25519']['fingerprint']}); the host is not contacted"
    assert result["public_key"] == ssh_keys().public()
    off_pinned = account_adapters.check_draft(git_entry(service="git", auth_type="ssh_key", endpoint="",
        metadata={**AUTHOR, "known_hosts": known_host()}, secrets={"private_key": ssh_key("rsa")}))
    assert off_pinned["valid"] is True and off_pinned["message"].startswith("Key accepted (RSA SHA256:")


def test_unconfigured_git_entry_names_what_is_missing(registry):
    def needed(**body):
        result = account_adapters.check_draft(git_entry(**body))
        assert result["valid"] is False
        return result["message"].removesuffix(" before this account can be verified")

    assert needed(metadata={}) == "Enter the commit author name and email"
    assert needed(service="git", auth_type="basic", metadata={**AUTHOR, "username": "ada"},
                  secrets={"password": "pw"}) == "Enter the server URL"
    assert needed(secrets={}) == "Enter the personal access token"
    assert needed(service="git", auth_type="basic", endpoint="https://git.example.org",
                  secrets={"password": "pw"}) == "Enter the username and token"
    assert needed(auth_type="ssh_key", secrets={}) == "Paste the SSH private key"
    assert needed(service="git", auth_type="ssh_key", endpoint="",
                  secrets={"private_key": ssh_key()}) == "Paste the server's known_hosts line"
    assert needed(service="github", auth_type="ssh_key", endpoint="https://ghe.example.com",
                  secrets={"private_key": ssh_key()}) == "Paste the server's known_hosts line"


def test_basic_username_refuses_colon(registry):
    for username in ("ada:x", "ada\n"):
        with pytest.raises(accounts.AccountError, match="^The username cannot contain ':' or a line break$"):
            accounts.create_account(git_entry(service="git", auth_type="basic", endpoint="https://git.example.org",
                                              metadata={**AUTHOR, "username": username}, secrets={"password": "pw"}))


def test_token_refuses_whitespace_and_strips_surrounding_space(registry):
    for token in ("ghp_one two", "ghp_one\ntwo", "ghp_\x01", "g" * 4097):
        with pytest.raises(accounts.AccountError, match="^The token cannot contain spaces or line breaks$"):
            accounts.create_account(git_entry(secrets={"api_key": token}))
    saved = accounts.create_account(git_entry(secrets={"api_key": "  ghp_padded\n"}))
    assert list(registry.values()) == ["ghp_padded"]
    with pytest.raises(accounts.AccountError, match="^The token cannot contain spaces or line breaks$"):
        accounts.update_account(saved["id"], {"service": "git", "auth_type": "basic", "endpoint": "https://git.example.org",
                                              "metadata": {**AUTHOR, "username": "ada"}, "secrets": {"password": "p w"}})
    assert account_adapters.check_draft(git_entry(secrets={"api_key": "a b"}))["message"] \
        == "The token cannot contain spaces or line breaks"


def test_private_key_refusals_name_the_fix(registry):
    from collimate import git_hosts
    cases = {
        ssh_key("ed25519.pub"): git_hosts._PUBLIC_KEY,
        ssh_key("ed25519_putty.txt"): git_hosts._PUTTY,
        ssh_key("rsa_pem"): git_hosts._OLD,
        ssh_key("ed25519_encrypted"): git_hosts._PASSPHRASE,
        synthetic_key("ssh-dss"): git_hosts._UNKNOWN_TYPE,
        synthetic_key("sk-ssh-ed25519@openssh.com"): git_hosts._SECURITY_KEY,
        "not a key at all": git_hosts._UNREADABLE,
    }
    assert "ssh-keygen -p -N" in git_hosts._PASSPHRASE
    for key, message in cases.items():
        with pytest.raises(accounts.AccountError) as refused:
            accounts.create_account(git_entry(auth_type="ssh_key", secrets={"private_key": key}))
        assert str(refused.value) == message
        verified = account_adapters.check_draft(git_entry(auth_type="ssh_key", secrets={"private_key": key}))
        assert verified == {"valid": False, "message": message}
    assert registry == {}
    saved = accounts.create_account(git_entry(auth_type="ssh_key", secrets={"private_key": ssh_key().replace("\n", "\r\n") + "\n\n"}))
    assert list(registry.values()) == [ssh_key()]
    assert saved["configured"] is True


def test_known_hosts_refusals(registry):
    blob = known_host().split()[2]
    cases = {
        f"|1|abc=|def= ssh-ed25519 {blob}": "Known hosts line 1 is hashed; paste the unhashed line (ssh-keyscan without -H)",
        f"*.example.org ssh-ed25519 {blob}": "Known hosts line 1 uses a wildcard; pin each host by name",
        f"@cert-authority box.example.org ssh-ed25519 {blob}": "Known hosts line 1 uses @cert-authority, which runs do not support",
        "box.example.org ssh-ed25519 AAAAnotakey": "Known hosts line 1 has a key that does not decode as ssh-ed25519",
        f"# comment\nbox.example.org ssh-dss {blob}": "Known hosts line 2 has an unsupported key type ssh-dss",
        "\n".join([known_host()] * 65): "Known hosts holds more than 64 lines or 16 KiB",
    }
    for text, message in cases.items():
        with pytest.raises(accounts.AccountError) as refused:
            accounts.create_account(git_entry(service="git", auth_type="ssh_key", endpoint="",
                                              metadata={**AUTHOR, "known_hosts": text}, secrets={"private_key": ssh_key()}))
        assert str(refused.value) == message
    assert registry == {}


def test_known_hosts_required_off_pinned_hosts(registry):
    def configured(**body):
        return accounts.create_account(git_entry(auth_type="ssh_key", secrets={"private_key": ssh_key()}, **body))["configured"]

    assert configured() is True
    assert configured(service="gitlab", endpoint="") is True
    assert configured(endpoint="https://ghe.example.com") is False
    assert configured(service="gitlab", endpoint="https://example.com/gitlab") is False
    assert configured(service="git", endpoint="") is False
    assert configured(service="git", endpoint="", metadata={**AUTHOR, "known_hosts": known_host()}) is True
    assert configured(endpoint="https://ghe.example.com",
                      metadata={**AUTHOR, "known_hosts": known_host().replace("box.example.org", "ghe.example.com")}) is True


def test_private_key_in_any_metadata_refused(registry):
    key = ssh_key()
    for body, path in ((git_entry(metadata={**AUTHOR, "known_hosts": key}), "metadata.known_hosts"),
                       (git_entry(metadata={**AUTHOR, "author_name": "-----BEGIN RSA PRIVATE KEY-----"}), "metadata.author_name"),
                       (model(metadata={"notes": {"pasted": [key]}}), "metadata.notes.pasted"),
                       (custom(metadata={**custom()["metadata"], "fields": {"region": key}}), "metadata.fields.region")):
        with pytest.raises(accounts.AccountError) as refused:
            accounts.create_account(body)
        assert str(refused.value) == f"{path} holds a private key; paste it into SSH private key, which the keyring keeps"
    assert registry == {}


def test_switching_auth_retires_other_secret_slots(registry):
    saved = accounts.create_account(git_entry())
    token_slot = accounts._read()["accounts"][saved["id"]]["secret_refs"]["api_key"]
    switched = accounts.update_account(saved["id"], {"auth_type": "ssh_key", "secrets": {"private_key": ssh_key()}})
    assert switched["secret_fields"] == ["private_key"]
    record = accounts._read()["accounts"][saved["id"]]
    assert set(record["secret_refs"]) == {"private_key"}
    assert token_slot not in str(record["secret_refs"])
    assert "github-private" not in registry.values()
    public = accounts.update_account(saved["id"], {"service": "git", "endpoint": "", "auth_type": "none", "secrets": {}})
    assert public["secret_fields"] == [] and registry == {}


def test_switching_auth_journals_a_slot_the_keyring_cannot_delete(registry, monkeypatch):
    saved = accounts.create_account(git_entry())
    token_slot = accounts._read()["accounts"][saved["id"]]["secret_refs"]["api_key"]
    monkeypatch.setattr(keystore, "delete_key", lambda slot: False)
    accounts.update_account(saved["id"], {"auth_type": "ssh_key", "secrets": {"private_key": ssh_key()}})
    assert accounts._read()["pending_delete"] == [token_slot]


def test_git_entry_stores_only_its_methods_secret(registry):
    refusals = {
        ("github", "api_key", "password"): "A Git account signing in with Personal access token stores only its token",
        ("github", "ssh_key", "api_key"): "A Git account signing in with SSH key stores only its SSH private key",
        ("git", "basic", "private_key"): "A Git account signing in with Username and token stores only its token or password",
        ("git", "none", "password"): "A public Git account stores no secret",
    }
    for (service, auth, field), message in refusals.items():
        body = git_entry(service=service, auth_type=auth, secrets={field: "x"})
        if service == "git":
            body.update(endpoint="https://git.example.org" if auth == "basic" else "")
        with pytest.raises(accounts.AccountError) as refused:
            accounts.create_account(body)
        assert str(refused.value) == message
        assert account_adapters.check_draft(body) == {"valid": False, "message": message}
    with pytest.raises(accounts.AccountError, match="stores only its token"):
        accounts.create_account(git_entry(secrets={"api_key": "ghp_x", "expires_at": "soon"}))
    assert registry == {}


def test_verify_ignores_check_path_for_git(registry, monkeypatch):
    calls = []
    monkeypatch.setattr(account_adapters, "request_service", lambda method, url, **kwargs: calls.append(url) or {})
    result = account_adapters.check_draft(git_entry(service="git", auth_type="basic", endpoint="https://git.example.org",
        metadata={**AUTHOR, "username": "ada", "check_path": "/api/v1/user"}, secrets={"password": "pw"}))
    assert result == {"valid": None, "supported": False,
                      "message": "This account type has no credential check; its saved settings remain available"}
    assert account_adapters.check_draft(git_entry(service="git", auth_type="none", endpoint="", secrets={},
                                                  metadata={**AUTHOR, "check_path": "/x"}))["valid"] is None
    assert calls == []


@pytest.mark.parametrize("kind", ["git", "account"])
def test_only_model_entries_draw_on_a_credential_account(registry, kind):
    service = "github" if kind == "git" else "openai"
    metadata = dict(AUTHOR) if kind == "git" else {}
    owner = accounts.create_account({"name": "Owner", "kind": kind, "service": service, "metadata": metadata,
                                     "secrets": {"api_key": "owner-private"}})
    before = accounts.CONFIG_PATH.read_bytes()
    with pytest.raises(accounts.AccountError, match="only model entries can draw on a credential account"):
        accounts.create_account({"name": "Child", "kind": kind, "service": service, "metadata": metadata,
                                 "credential_account_id": owner["id"]})
    assert accounts.CONFIG_PATH.read_bytes() == before


def custom(**overrides):
    return {
        "name": "Analytics database", "kind": "custom", "metadata": {
            "custom_type": "warehouse", "field_schema": [
                {"name": "region", "secret": False}, {"name": "signing_key", "secret": True},
            ], "fields": {"region": "us-west"},
        }, "secrets": {"signing_key": "custom-private"}, **overrides,
    }


def test_custom_schema_separates_public_fields_and_credentials(registry):
    api = client()
    response = api.post("/api/accounts", json=custom())
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["configured"] is True
    assert saved["auth_type"] == "none"
    assert saved["secret_fields"] == ["signing_key"]
    assert "custom-private" not in response.text
    assert "custom-private" not in accounts.CONFIG_PATH.read_text()
    selected = accounts.resolve_accounts({"data": saved["id"]}, {
        "data": {"kind": "custom", "custom_types": ["warehouse"]},
    })
    assert selected["data"]["metadata"]["fields"] == {"region": "us-west"}
    assert selected["data"]["secrets"] == {"signing_key": "custom-private"}
    assert "custom-private" not in json.dumps(accounts.public_accounts_snapshot(selected))
    with pytest.raises(accounts.AccountError, match="incompatible custom_type"):
        accounts.resolve_accounts({"data": saved["id"]}, {"data": {"custom_type": "other"}})
    accounts.update_account(saved["id"], {"secrets": {"signing_key": None}})
    assert accounts.get_account(saved["id"])["configured"] is False
    with pytest.raises(accounts.AccountError, match="Complete authentication"):
        accounts.resolve_accounts({"data": saved["id"]})


def test_custom_schema_changes_retire_removed_and_newly_public_credentials(registry):
    saved = accounts.create_account(custom())
    metadata = saved["metadata"]
    metadata["field_schema"][1]["secret"] = False
    metadata["fields"]["signing_key"] = "public-reference"
    updated = accounts.update_account(saved["id"], {"metadata": metadata})
    assert updated["secret_fields"] == []
    assert not registry
    assert "custom-private" not in accounts.CONFIG_PATH.read_text()
    metadata["field_schema"][1]["secret"] = True
    del metadata["fields"]["signing_key"]
    accounts.update_account(saved["id"], {"metadata": metadata, "secrets": {"signing_key": "replacement-private"}})
    metadata["field_schema"].pop()
    accounts.update_account(saved["id"], {"metadata": metadata})
    assert not registry


def test_custom_auth_named_fields_remain_secret_through_crud_and_runtime(registry, monkeypatch):
    values = {"expires_at": "private-expiry", "token_type": "private-type",
              "refresh_token": "private-refresh", "credential_json": "private-cli"}
    metadata = {"custom_type": "integration", "harness": "codex",
                "field_schema": [{"name": name, "secret": True} for name in values], "fields": {}}
    saved = accounts.create_account(custom(metadata=metadata, secrets=values))
    assert saved["configured"] is True
    assert "auth_state" not in saved["metadata"]
    assert set(saved["secret_fields"]) == set(values)
    context = accounts.resolve_accounts({"integration": saved["id"]})
    assert context["integration"]["secrets"] == values
    assert accounts.private_account(saved["id"])["secrets"] == values
    for value in values.values():
        assert value not in accounts.CONFIG_PATH.read_text()
        assert value not in json.dumps(saved)
        assert value not in json.dumps(accounts.public_accounts_snapshot(context))

    def unexpected(*args, **kwargs):
        pytest.fail("custom fields must not trigger native auth storage or refresh")
    monkeypatch.setattr(accounts, "materialize_cli_account", unexpected)
    monkeypatch.setattr(accounts, "sweep_expired_account_leases", unexpected)
    monkeypatch.setattr(account_adapters, "request_service", unexpected)
    assert accounts.retain_account_context(context, "custom-run") == []
    assert set(registry.values()) == set(values.values())

    values["expires_at"] = "private-updated-expiry"
    updated = accounts.update_account(saved["id"], {"name": "Renamed integration",
        "endpoint": "https://custom.example", "secrets": {"expires_at": values["expires_at"]}})
    assert "auth_state" not in updated["metadata"]
    assert accounts.resolve_accounts({"integration": saved["id"]})["integration"]["secrets"] == values
    for value in values.values():
        assert value not in accounts.CONFIG_PATH.read_text()
        assert value not in json.dumps(updated)
    metadata["field_schema"] = [field for field in metadata["field_schema"] if field["name"] not in {"expires_at", "token_type"}]
    accounts.update_account(saved["id"], {"metadata": metadata, "secrets": {"expires_at": None, "token_type": None}})
    assert set(registry.values()) == {values["refresh_token"], values["credential_json"]}


def test_failed_custom_schema_save_keeps_original_secret_and_schema(registry, monkeypatch):
    saved = accounts.create_account(custom())
    original = accounts.CONFIG_PATH.read_bytes()
    metadata = saved["metadata"]
    metadata["field_schema"][1]["secret"] = False
    metadata["fields"]["signing_key"] = "public-reference"
    monkeypatch.setattr(accounts, "_write", lambda _: (_ for _ in ()).throw(OSError("full disk")))
    with pytest.raises(OSError):
        accounts.update_account(saved["id"], {"metadata": metadata})
    assert accounts.CONFIG_PATH.read_bytes() == original
    assert list(registry.values()) == ["custom-private"]


@pytest.mark.parametrize("metadata,secrets,error", [
    ({"custom_type": "", "field_schema": []}, {}, "custom type"),
    ({"custom_type": "type", "field_schema": [{"name": "bad name", "secret": True}]}, {}, "custom fields require"),
    ({"custom_type": "type", "field_schema": [{"name": "key", "secret": True}], "fields": {"key": "never-public"}}, {}, "stored as a secret"),
    ({"custom_type": "type", "field_schema": []}, {"unknown": "never-public"}, "marked secret"),
    ({"custom_type": "type", "field_schema": [{"name": "region", "secret": False}], "fields": {"unknown": "value"}}, {}, "matching field"),
    ({"custom_type": "type", "field_schema": [{"name": "region", "secret": False}] * 2}, {}, "unique"),
])
def test_custom_schema_rejects_invalid_and_misclassified_fields(registry, metadata, secrets, error):
    with pytest.raises(accounts.AccountError, match=error):
        accounts.create_account(custom(metadata=metadata, secrets=secrets))
    assert not registry
    assert not accounts.CONFIG_PATH.exists()


def test_discovery_uses_advertised_capabilities_and_preserves_manual_fallback(monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"models": [{
        "name": "models/text-example", "supportedGenerationMethods": ["generateContent"],
    }, {"name": "models/embed-example", "supportedGenerationMethods": ["embedContent"]}]})
    models = account_adapters.discover_models(model(service="gemini", protocol="gemini"))["models"]
    assert models[0]["capabilities"] == ["text"]
    assert models[1]["capabilities"] == ["embeddings"]
    assert models[0]["capabilities_source"] == "service"
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{
        "id": "model-vision-reasoning", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": ["tools"],
    }]})
    models = account_adapters.discover_models(model())["models"]
    assert models[0]["capabilities"] == ["text", "tools"]
    assert "vision" not in models[0]["capabilities"]  # Names do not declare capabilities.
    assert account_adapters.discover_models(model(protocol="custom"))["supported"] is False


def test_plan_model_entries_verify_the_id_against_the_plan_list_or_say_it_is_unchecked(monkeypatch):
    import plan_logins
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("unexpected provider request"))
    monkeypatch.setattr(plan_logins, "check_plan_login", lambda account: {"valid": True, "message": "opencode login present"})
    monkeypatch.setattr(plan_logins, "plan_models", lambda account: {"models": [{"id": "cli-model"}], "supported": True})
    listed = model(service="copilot", protocol="cli", auth_type="cli", endpoint="", model="cli-model", metadata={"harness": "opencode", "plan": "github_copilot"})
    result = account_adapters.check_account(listed)
    assert result["valid"] is True and "'cli-model' is in the service's model list" in result["message"] and "opencode can run it" in result["message"]
    assert account_adapters.discover_models(listed)["models"] == [{"id": "cli-model"}]
    unlisted = model(service="copilot", protocol="cli", auth_type="cli", endpoint="", model="other-model", metadata={"harness": "opencode", "plan": "github_copilot"})
    assert account_adapters.check_account(unlisted)["valid"] is False
    monkeypatch.setattr(plan_logins, "plan_models", lambda account: {"models": [], "supported": False, "message": "opencode exposes no model list to query"})
    result = account_adapters.check_account(unlisted)
    assert result["valid"] is True and "the model ID is not checked: opencode exposes no model list" in result["message"]
    assert "opencode login present" in result["message"]


def test_model_entry_verify_covers_harness_fit_and_the_text_capability(monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "chosen-model"}]})
    assert account_adapters.check_account(model())["valid"] is True
    wrong_harness = model(metadata={"harness": "codex"})   # codex cannot take a custom endpoint
    result = account_adapters.check_account(wrong_harness)
    assert result["valid"] is False and "'codex'" in result["message"] and "pydantic-ai" in result["message"]
    no_text = model(capabilities=["embeddings"])
    result = account_adapters.check_account(no_text)
    assert result["valid"] is False and "must include text" in result["message"]
    blank = model(model="")
    assert account_adapters.check_account(blank)["valid"] is False


def test_model_entry_on_an_endpoint_without_a_list_answers_a_one_token_request(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/models"):
            raise accounts.AccountError("Service returned HTTP 404")
        assert kwargs["json"]["model"] == "chosen-model" and kwargs["json"]["max_tokens"] == 1
        return {"choices": [{"message": {"content": "pong"}}]}
    monkeypatch.setattr(account_adapters, "request_service", request)
    result = account_adapters.check_account(model(protocol="openai-compatible"))
    assert result["valid"] is True and "answered a one-token request" in result["message"]
    assert calls == [("GET", "https://models.example/v1/models"), ("POST", "https://models.example/v1/chat/completions")]
    def refuse(method, url, **kwargs):
        raise accounts.AccountError("Service returned HTTP 404")
    monkeypatch.setattr(account_adapters, "request_service", refuse)
    with pytest.raises(accounts.AccountError, match="HTTP 404"):
        account_adapters.check_account(model(protocol="openai-compatible"))


def test_antigravity_entries_verify_against_the_gemini_list(monkeypatch):
    # Google's antigravity-acp lists Gemini API ids literally, so an entry is
    # checked against the service's list; ``default`` lets the server choose.
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]})
    def entry(model_id):
        return model(service="gemini", protocol="gemini", endpoint="https://generativelanguage.googleapis.com/v1beta", model=model_id, metadata={"harness": "antigravity"})
    assert account_adapters.check_account(entry("gemini-2.5-flash"))["valid"] is True
    chosen = account_adapters.check_account(entry("default"))
    assert chosen["valid"] is True and "lets antigravity choose its own" in chosen["message"]
    blank = account_adapters.check_account(entry(""))
    assert blank["valid"] is True and "no model chosen, so antigravity uses 'default'" in blank["message"]
    unknown = account_adapters.check_account(entry("gemini-9-unknown"))
    assert unknown["valid"] is False and "not in the service's current model list" in unknown["message"]


def test_verify_checks_the_default_a_blank_model_runs(monkeypatch):
    # A blank model is filled with the taxonomy's default_model_for, the rule
    # the agent generator applies, so Verify checks the id the run will use.
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "gpt-6-astra"}]})
    blank = model(model="", endpoint="https://api.openai.com/v1", metadata={"harness": "opencode"})
    result = account_adapters.check_account(blank)
    assert result["valid"] is True
    assert "no model chosen, so opencode uses 'gpt-6-astra'" in result["message"] and "model 'gpt-6-astra' is in the service's model list" in result["message"]
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "gpt-4.1"}]})
    result = account_adapters.check_account(blank)
    assert result["valid"] is False and "'gpt-6-astra'" in result["message"] and "default for a blank model" in result["message"]
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "claude-sonnet-5-5"}]})
    claude = model(service="anthropic", protocol="anthropic", endpoint="https://api.anthropic.com/v1", model="", metadata={"harness": "claude-code"})
    result = account_adapters.check_account(claude)
    assert result["valid"] is True and "uses 'sonnet'" in result["message"] and "(as claude-sonnet-5-5)" in result["message"]
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "anthropic/claude-sonnet-5.5"}]})
    openrouter = model(service="openrouter", protocol="openai-compatible", endpoint="https://openrouter.ai/api/v1", model="", metadata={"harness": "codex"})
    result = account_adapters.check_account(openrouter)
    assert result["valid"] is True and "uses 'anthropic/claude-sonnet-5.5'" in result["message"]


def test_a_model_of_only_spaces_is_saved_blank_and_verified_as_blank(registry, monkeypatch):
    # The registry strips the model once, so Verify, the registry and a run
    # all read the same id: blank here, which runs the harness default.
    body = model(model="  ", endpoint="https://api.openai.com/v1", metadata={"harness": "codex"})
    saved = accounts.create_account(body)
    assert saved["model"] == "" and accounts.private_account(saved["id"])["model"] == ""
    assert accounts.update_account(saved["id"], {"model": " gpt-5 "})["model"] == "gpt-5"
    draft = accounts.draft_account(body)
    assert draft["model"] == ""
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "gpt-5"}]})
    assert "with no model chosen, codex picks its own default model" in account_adapters.check_account(draft)["message"]


def test_every_model_capable_entry_is_served_what_its_blank_model_runs(registry):
    # The form asks for a model where the served default is None, instead of
    # re-deriving the endpoint rule.
    standard = accounts.create_account(model(kind="account", name="OpenAI", endpoint="https://api.openai.com/v1"))
    proxy = accounts.create_account(model(kind="account", name="Proxy", endpoint="https://proxy.example/v1"))
    claude = accounts.create_account(model(service="anthropic", protocol="anthropic", endpoint="https://api.anthropic.com/v1",
                                           model="", metadata={"harness": "claude-code"}))
    plan = accounts.create_account(model(protocol="cli", auth_type="cli", endpoint="", model="",
                                         metadata={"harness": "opencode", "plan": "chatgpt"}, secrets={"credential_json": "{}"}))
    rows = {row["id"]: row for row in accounts.list_accounts()["accounts"]}
    assert rows[standard["id"]]["default_model"] == "gpt-6-astra"
    assert rows[proxy["id"]]["default_model"] is None
    assert rows[claude["id"]]["default_model"] == "sonnet"
    assert rows[plan["id"]]["default_model"] == "gpt-5.5"


def test_a_listed_model_that_is_not_a_text_model_fails_verify(monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"models": [{"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]}]})
    entry = model(service="gemini", protocol="gemini", endpoint="https://generativelanguage.googleapis.com/v1beta", model="gemini-embedding-001")
    result = account_adapters.check_account(entry)
    assert result["valid"] is False and "not a text model" in result["message"] and "embeddings" in result["message"]


def test_a_blank_codex_model_is_codexs_own_pick_on_both_openai_legs(monkeypatch):
    # The service refuses the API default gpt-5 for a ChatGPT account and
    # codex's own catalogue dropped it: a blank model sends none, and Verify
    # and the plan's model list say so in the same words.
    import plan_logins
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "gpt-5"}]})
    note = "with no model chosen, codex picks its own default model"
    plan = model(service="openai", protocol="cli", auth_type="cli", endpoint="", model="",
                 metadata={"harness": "codex", "plan": "chatgpt"},
                 secrets={"credential_json": json.dumps({"tokens": {"refresh_token": "r"}})})
    result = account_adapters.check_account(plan)
    assert result["valid"] is True and note in result["message"] and "gpt-5" not in result["message"]
    listed = plan_logins.plan_models(plan)
    assert listed["models"] == [] and note in listed["message"] and "gpt-5" not in listed["message"]
    key = model(model="", endpoint="https://api.openai.com/v1", metadata={"harness": "codex"})
    result = account_adapters.check_account(key)
    assert result["valid"] is True and note in result["message"] and "gpt-5" not in result["message"]


def test_unchecked_plan_ids_name_the_harness_consequence(monkeypatch):
    import plan_logins
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: pytest.fail("no request"))
    monkeypatch.setattr(plan_logins, "check_plan_login", lambda account: {"valid": True, "message": "pi login present"})
    monkeypatch.setattr(plan_logins, "plan_models", lambda account: {"models": [], "supported": False, "message": "pi exposes no model list to query"})
    entry = model(service="openai", protocol="cli", auth_type="cli", endpoint="", model="gpt-5.5", metadata={"harness": "pi", "plan": "chatgpt"})
    result = account_adapters.check_account(entry)
    assert result["valid"] is True and "pi refuses to open a session" in result["message"]


def test_openai_native_probe_uses_max_completion_tokens(monkeypatch):
    bodies = []
    def request(method, url, **kwargs):
        if url.endswith("/models"):
            raise accounts.AccountError("Service returned HTTP 404")
        bodies.append(kwargs["json"])
        return {"choices": []}
    monkeypatch.setattr(account_adapters, "request_service", request)
    assert account_adapters.check_account(model(protocol="openai"))["valid"] is True
    assert bodies[-1] == {"model": "chosen-model", "messages": [{"role": "user", "content": "ping"}], "max_completion_tokens": 1}
    def refuse(method, url, **kwargs):
        raise accounts.AccountError("Service returned HTTP 404")
    monkeypatch.setattr(account_adapters, "request_service", refuse)
    with pytest.raises(accounts.AccountError, match="Model 'chosen-model' did not answer a one-token request: Service returned HTTP 404"):
        account_adapters.check_account(model(protocol="openai"))


def test_claude_aliases_pass_the_model_list_check(monkeypatch):
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "claude-sonnet-5-5"}]})
    alias = model(service="anthropic", protocol="anthropic", endpoint="https://api.anthropic.com/v1", model="sonnet", metadata={"harness": "claude-code"})
    result = account_adapters.check_account(alias)
    assert result["valid"] is True and "(as claude-sonnet-5-5)" in result["message"]
    assert account_adapters.check_account({**alias, "model": "haiku"})["valid"] is False


def test_openai_chat_protocol_uses_saved_connection_for_discovery_and_check(monkeypatch):
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Authorization"] == "Bearer private-one"
        return {"data": [{"id": "chosen-model"}]}
    monkeypatch.setattr(account_adapters, "request_service", request)
    account = model(protocol="openai-chat")
    assert account_adapters.discover_models(account)["models"][0]["id"] == "chosen-model"
    assert account_adapters.check_account(account)["valid"] is True
    assert calls == ["https://models.example/v1/models"] * 2
    assert account_adapters.secret_environment(account)["OPENAI_BASE_URL"] == account["endpoint"]


def _create_process(path, number):
    from pathlib import Path
    import accounts as child_accounts
    child_accounts.CONFIG_PATH = Path(path)
    for index in range(6):
        child_accounts.create_account({"name": f"{number}/{index}", "kind": "account", "service": "custom", "auth_type": "none"})


def test_process_lock_prevents_lost_registry_updates(registry):
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_create_process, args=(str(accounts.CONFIG_PATH), number)) for number in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert len(accounts.list_accounts()["accounts"]) == 12


def test_check_names_the_missing_credential_for_typed_secret_methods(registry):
    """An unsaved API-key account with no key yet is not a sign-in problem —
    the message says what to type."""
    api = client()
    result = api.post("/api/accounts/check", json={"name": "OpenAI", "kind": "account",
        "service": "openai", "auth_type": "api_key", "secrets": {}}).json()
    assert result["valid"] is False
    assert result["message"] == "Enter the API key before this account can be verified"


def test_github_verify_names_the_login(registry, monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        return {"login": "octo"}

    monkeypatch.setattr(account_adapters, "request_service", request)
    api = client()
    result = api.post("/api/accounts/check", json=git_entry(auth_type="api_key")).json()
    assert result["valid"] is True
    assert result["identity"] == "octo"
    assert result["message"] == "Signed in as octo"
    assert calls == [("GET", "https://api.github.com/user")]


def test_a_blank_model_is_the_harness_default_except_on_a_custom_endpoint(registry, monkeypatch):
    """No model chosen = the harness picks (a complete setup); only a custom
    endpoint, which has no default, still demands one."""
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "gpt-5"}]})
    owner = accounts.create_account(model(kind="account", model="", endpoint="https://api.openai.com/v1"))
    blank = accounts.create_account({"name": "Codex default", "kind": "model", "model": "",
                                     "credential_account_id": owner["id"], "metadata": {"harness": "codex"}})
    assert "model_configuration_error" not in blank
    selected = accounts.resolve_accounts({"model": blank["id"]}, {"model": {"kind": "model", "capabilities": ["text"]}})["model"]
    assert selected["model"] == "" and selected["metadata"]["harness"] == "codex"
    result = account_adapters.check_account(accounts.private_account(blank["id"]))
    assert result["valid"] is True and "with no model chosen, codex picks its own default model" in result["message"]
    plan = accounts.create_account(model(kind="account", model="", auth_type="cli", protocol="cli", endpoint="",
                                         metadata={"harness": "opencode", "plan": "xai"}, service="xai",
                                         secrets={"credential_json": '{"xai":{"type":"oauth","refresh":"private"}}'}))
    own = accounts.create_account({"name": "xAI plan default", "kind": "model", "model": "", "credential_account_id": plan["id"],
                                   "metadata": {"harness": "opencode"}})
    assert "model_configuration_error" not in own
    custom = accounts.create_account(model(kind="account", model="", service="custom", protocol="openai-compatible", endpoint="https://models.example/v1"))
    required = accounts.create_account({"name": "Custom", "kind": "model", "model": "", "credential_account_id": custom["id"]})
    assert "no default model" in required["model_configuration_error"]
    with pytest.raises(accounts.AccountError, match="no default model"):
        accounts.resolve_accounts({"model": required["id"]}, {"model": {"kind": "model"}})
    result = account_adapters.check_draft({"name": "Custom", "kind": "model", "model": "", "credential_account_id": custom["id"], "secrets": {}})
    assert result["valid"] is False and "no default model" in result["message"]


def test_verify_says_whether_the_harness_is_on_this_machine(monkeypatch):
    from collimate import host_harness
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "chosen-model"}]})
    entry = model(service="openai", protocol="openai", endpoint="https://api.openai.com/v1", metadata={"harness": "codex"})
    found = host_harness.HostHarness("codex", "codex", "/usr/local/bin/codex", "0.156.0", False, ("/usr/local/bin",))
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: found)
    result = account_adapters.check_account(entry)
    assert result["valid"] is True
    assert "codex can run it: docker: none uses this machine's codex 0.156.0 at /usr/local/bin/codex; docker: full uses its container" in result["message"]
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: None)
    missing = account_adapters.check_account(entry)["message"]
    assert "codex can run it: codex was not found on this machine — install codex outside Snap to run it under docker: none" in missing
    # Without the machine's CLI, an Annotation runner adopts the container.
    assert "an Annotation runner on docker: none or limited adopts docker: full" in missing
    stale = host_harness.HostHarness("codex", "codex", "/usr/local/bin/codex", "", False, ("/usr/local/bin",))
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: stale)
    assert "did not answer --version as codex — install codex outside Snap to run it under docker: none" in account_adapters.check_account(entry)["message"]
    snap = host_harness.HostHarness("codex", "codex", "/snap/bin/codex", "0.114.0", True, ("/snap/bin",))
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: snap)
    assert "strictly confined Snap" in account_adapters.check_account(entry)["message"]
    not_yet = host_harness.HostHarness("pi", "pi", "/usr/local/bin/pi", "0.9.0", False, ("/usr/local/bin",))
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: not_yet)
    pi = model(service="openai", protocol="openai", endpoint="https://api.openai.com/v1", metadata={"harness": "pi"})
    assert ("pi can run it: docker: full uses its container; an Annotation runner on docker: none or limited adopts "
            "docker: full for it") in account_adapters.check_account(pi)["message"]
    assert "no supported host twin" in account_adapters.check_account(pi)["message"]
    direct = account_adapters.check_account(model())
    # A host harness is never raised from limited: none already places it.
    assert ("pydantic-ai can run it: docker: none or full runs the host adapter; docker: limited cannot run it; "
            "docker: full sandboxes the commands its grant allows, recommended for commands: allow") in direct["message"]
    assert "adopts" not in direct["message"].split("pydantic-ai can run it:")[1]
    claude = model(service="anthropic", protocol="anthropic", endpoint="https://api.anthropic.com/v1", model="chosen-model", metadata={"harness": "claude-code"})
    # The Claude host adapter drives the machine's own `claude` when there is a
    # usable one; otherwise the Claude Code bundled in Collimate's adapter.
    mine = host_harness.HostHarness("claude-code", "claude", "/home/u/.local/bin/claude", "2.1.281", False, ("/home/u/.local/bin",))
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: mine)
    assert "uses this machine's claude 2.1.281 at /home/u/.local/bin/claude" in account_adapters.check_account(claude)["message"]
    monkeypatch.setattr(host_harness, "probe", lambda harness, **kw: None)
    message = account_adapters.check_account(claude)["message"]
    assert "claude was not found on this machine; running the Claude Code bundled in Collimate's adapter instead" in message
    assert "its CLI runs on this machine under docker: none or full" in message


def test_verify_forgets_the_cached_harness_probe__F1204(monkeypatch):
    from collimate import host_harness
    cached = {"present": True}
    def forget():
        cached.clear()
    def probe(harness):
        assert not cached, "Verify reused a cached installation"
        return None
    monkeypatch.setattr(host_harness, "forget", forget)
    monkeypatch.setattr(host_harness, "probe", probe)
    assert "not found on this machine" in account_adapters._harness_availability("codex")


def test_unavailable_keyring_explains_unlock_then_restart__F0418(registry, monkeypatch):
    monkeypatch.setattr(keystore, "available", lambda: False)
    with pytest.raises(accounts.KeyringUnavailable, match="[Uu]nlock.*restart"):
        accounts.create_account(model())


def test_a_key_check_never_depends_on_the_machine_s_own_clis(monkeypatch):
    # The suite's autouse probe stub: with no probe of its own, a check must
    # read as "not on this machine" wherever it runs — never the developer's
    # installed codex/claude (which the real probe would also execute).
    monkeypatch.setattr(account_adapters, "request_service", lambda *a, **k: {"data": [{"id": "chosen-model"}]})
    codex = model(service="openai", protocol="openai", endpoint="https://api.openai.com/v1", metadata={"harness": "codex"})
    assert "codex can run it: codex was not found on this machine" in account_adapters.check_account(codex)["message"]
    claude = model(service="anthropic", protocol="anthropic", endpoint="https://api.anthropic.com/v1", model="chosen-model", metadata={"harness": "claude-code"})
    assert "claude was not found on this machine" in account_adapters.check_account(claude)["message"]


def test_serves_default_harness_and_labels__F0951(registry):
    from collimate import account_taxonomy
    payload = accounts.list_accounts()
    assert payload['default_model_harness'] == account_taxonomy.DEFAULT_MODEL_HARNESS
    assert payload['harness_labels'] == account_taxonomy.MODEL_HARNESS_LABELS
    assert set(payload['harness_labels']) == set(payload['model_harnesses'])


def test_a_saved_record_of_a_retired_service_is_dropped_on_read_TR02(registry):
    """A Google Drive record saved before the service was retired would make
    the whole registry unreadable; reading drops it instead, queues its
    keyring slots for deletion (and deletes them) and forgets the Drive
    selection, while every other account reads as saved."""
    kept = accounts.create_account(model())
    doc = json.loads(accounts.CONFIG_PATH.read_text())
    slot = "collimate_drive-1_refresh_token"
    keystore.set_key(slot, "retired-private")
    doc["accounts"]["drive-1"] = {
        "id": "drive-1", "name": "Drive", "kind": "account", "service": "google_drive",
        "protocol": "google-drive", "endpoint": "", "model": "", "capabilities": ["files"],
        "auth_type": "oauth", "metadata": {"client_id": "public-client"},
        "revision": "r1", "generation": "g1", "secret_refs": {"refresh_token": slot},
    }
    doc["defaults"]["drive"] = "drive-1"
    accounts.CONFIG_PATH.write_text(json.dumps(doc))
    listing = accounts.list_accounts()
    assert [account["id"] for account in listing["accounts"]] == [kept["id"]]
    saved = json.loads(accounts.CONFIG_PATH.read_text())
    assert set(saved["accounts"]) == {kept["id"]}
    assert "drive" not in saved["defaults"]
    assert saved["pending_delete"] == []
    assert keystore.get_key(slot) is None
    assert accounts.private_account(kept["id"])["secrets"]["api_key"] == "private-one"
