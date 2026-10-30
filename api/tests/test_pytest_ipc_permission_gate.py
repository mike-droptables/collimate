"""The host IPC permission chokepoint — ``OP_CAPABILITY`` consulted in
``IPCMixin._handle_ipc`` between op lookup and dispatch.

Every capability-bearing op is gated by the run's ``RunnerPermissions`` grant. A
denial short-circuits BEFORE the handler runs:

  * capability failures NAK ``permission_denied`` (→ SDK PermissionError), naming
    the op + required capability.

These tests drive ``_handle_ipc`` directly with a stub manager whose dispatch
table is replaced by recording stubs, so an ALLOWED op records that it would
have dispatched (no real handler logic runs) and a DENIED op emits the gate's
frame instead. For each gated op: denied one tier below the requirement, allowed
at exactly the requirement, and UNRESTRICTED always passes. A drift guard
checks that every gated op name is a real dispatch op.

The last section drives the real ``account`` op handlers: a run only ever
sees the private account snapshot resolved for it at start, never the live
registry, and refreshes/leases stay bound to that run and role.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from process_manager.ipc import OP_CAPABILITY, REQUEST_OPS, IPCMixin
from runner_permissions import (
    UNRESTRICTED,
    RunnerPermissions,
)
from test_pytest_accounts import registry as registry  # fixture: in-memory keyring


class _FakeWriter:
    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        return None


class _Mgr(IPCMixin):
    pass


def _mk(grant, *, channel="chan"):
    """A stub manager whose dispatch table is recording stubs, so a permitted
    op records (never runs real logic) and a denied op only emits the gate
    frame. ``grant`` is a ``RunnerPermissions`` (or ``UNRESTRICTED``)."""
    m = _Mgr()
    m._ipc_writer = _FakeWriter()
    m.log_buffer = deque()
    m.channel = channel
    m.permissions = grant
    m.dispatched: list[str] = []

    def _recorder(op):
        async def _h(_data):
            m.dispatched.append(op)
        return _h

    # Every gated op gets a recorder; ``_handle_ipc`` reads this cache.
    m._ipc_handlers_cache = {op: _recorder(op) for op in OP_CAPABILITY}
    return m


def _grant(**axes) -> RunnerPermissions:
    return RunnerPermissions.from_definition(dict(axes))


def _run(m, data) -> list[dict]:
    if data.get("op") in REQUEST_OPS:
        data = {**data, "req_id": "test-request"}
    asyncio.run(m._handle_ipc(json.dumps(data)))
    return [json.loads(line.decode()) for line in m._ipc_writer.lines]


def _is_perm_nak(frames) -> bool:
    return any(
        f.get("op") == "nak" and f.get("reason") == "permission_denied"
        for f in frames
    )


def _assert_denied(op, data, grant):
    m = _mk(grant)
    frames = _run(m, {"op": op, **data})
    assert _is_perm_nak(frames), f"{op}: expected permission_denied NAK, got {frames}"
    assert m.dispatched == [], f"{op}: handler ran despite denial"
    # The NAK names the op + a capability.
    nak = next(f for f in frames if f.get("op") == "nak")
    assert nak["for_op"] == op
    assert nak.get("capability")


def _assert_allowed(op, data, grant):
    m = _mk(grant)
    frames = _run(m, {"op": op, **data})
    assert not _is_perm_nak(frames), f"{op}: unexpected permission NAK: {frames}"
    assert m.dispatched == [op], f"{op}: handler did not dispatch under an allowing grant"


# ---------------------------------------------------------------------------
# Drift guard — every gated op is a real dispatch op.
# ---------------------------------------------------------------------------

def test_every_gated_op_exists_in_dispatch_table():
    handlers = set(_Mgr()._ipc_handlers.keys())
    missing = [op for op in OP_CAPABILITY if op not in handlers]
    assert missing == [], f"gated ops absent from the dispatch table: {missing}"


# ---------------------------------------------------------------------------
# Static / leveled ops — denied one tier below, allowed at the requirement.
# ---------------------------------------------------------------------------

# (op, data, deny_grant_axes, allow_grant_axes)
_LEVELED_CASES = [
    (op, {}, {"docker": "limited"}, {"docker": "full"})
    for op in ("env_place", "env_exec", "env_expose_port", "env_close",
               "create_terminal_session")
]
_DEDICATED_GATE_TESTS = frozenset()
# Both cell-read operations derive the cell from the manager, never the
# payload; the annotation ops address only the run's own context. The host
# terminal op gates itself on the grant and the run's placement record
# (test_pytest_terminal_host.py).
_REVIEWED_UNGATED = frozenset({
    "account", "account_materialize", "account_release", "chat_poll",
    "annotations_list", "annotations_poll", "annotations_post", "annotations_pull",
    "client_caps", "commit_account", "create_host_terminal_session", "destroy_terminal_session",
    "finish", "phase", "poll_renderer_action", "port_bound", "progress", "read_cell_files",
    "read_cell_hashes", "list_handlers", "handler_skills", "validate_file", "reveal_file", "stream_event", "surface_register",
    "terminal_status", "update_cell",
})


def test_every_dispatch_op_is_gated_or_reviewed_ungated():
    ungated = set(_Mgr()._ipc_handlers) - set(OP_CAPABILITY)
    assert ungated == _REVIEWED_UNGATED, (
        f"new ungated ops: {sorted(ungated - _REVIEWED_UNGATED)}; "
        f"stale entries: {sorted(_REVIEWED_UNGATED - ungated)}"
    )


def test_every_gated_op_has_a_gate_test():
    leveled = {case[0] for case in _LEVELED_CASES}
    assert set(OP_CAPABILITY) - leveled - _DEDICATED_GATE_TESTS == set()


@pytest.mark.parametrize("op", ["env_place", "create_terminal_session"])
def test_host_docker_ops_denied_without_docker(op):
    _assert_denied(op, {}, _grant())
    _assert_denied(op, {}, _grant(docker="none"))


@pytest.mark.parametrize("op,data,deny,allow", _LEVELED_CASES,
                         ids=[c[0] for c in _LEVELED_CASES])
def test_leveled_op_denied_below_and_allowed_at_tier(op, data, deny, allow):
    _assert_denied(op, data, _grant(**deny))
    _assert_allowed(op, data, _grant(**allow))
    # UNRESTRICTED always passes.
    _assert_allowed(op, data, UNRESTRICTED)


# ---------------------------------------------------------------------------
# Accounts are granted only by explicit role bindings, independently of tiers.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grant", [_grant(), UNRESTRICTED])
def test_account_access_uses_saved_role_bindings(grant):
    m = _mk(grant)
    m._ipc_handlers_cache = None
    m.account_context = {"model": {"id": "chosen", "secrets": {"api_key": "private"}}}
    m.account_requirements = {"model": {}, "git": {"required": False}}
    frames = _run(m, {"req_id": "test-request", "op": "account", "role": "model"})
    assert frames[-1]["ok"] is True
    assert frames[-1]["account"]["id"] == "chosen"
    frames = _run(m, {"req_id": "test-request", "op": "account", "role": "unbound"})
    assert frames[-1]["ok"] is False
    assert frames[-1]["reason"] == "permission_denied"
    frames = _run(m, {"req_id": "test-request", "op": "account", "role": "git"})
    assert frames[-1]["ok"] is False
    assert frames[-1]["reason"] == "not_found"


# ---------------------------------------------------------------------------
# port_bound — the reverse proxy forwards to what it records, so the host
# records only a port the run serves.
# ---------------------------------------------------------------------------

def test_port_bound_registers_only_a_port_the_run_serves(tmp_path):
    from process_manager.backends import LocalDockerBackend
    from test_pytest_environments import refusals, run_with_environment

    run, _own = run_with_environment(tmp_path, ports={"3000/tcp": ("127.0.0.1", 43001)})
    # A docker:limited run's own container publishes nothing on host loopback.
    run.backend = LocalDockerBackend()

    def claim(**frame):
        run._ipc_writer.frames.clear()
        asyncio.run(run._handle_ipc(json.dumps({"req_id": "test-request", "op": "port_bound", "name": "App", **frame})))
        return run._ipc_writer.frames

    # A host service the run never exposed: refused, logged, nothing kept.
    assert claim(port=5432) == [
        {"op": "nak", "for_op": "port_bound", "reason": "port_not_exposed: 5432", "req_id": "test-request"}]
    assert run.bound_ports == set()
    assert not any(row["type"] == "port_bound" for row in run.log_buffer)
    assert len(refusals(run)) == 1
    assert refusals(run)[0].startswith("run run-own: port_bound refused")

    # The port the run's environment published.
    assert claim(port=43001) == [{"op": "ack_port_bound", "ok": True, "req_id": "test-request"}]
    assert run.bound_ports == {43001}
    assert any(row["type"] == "port_bound" for row in run.log_buffer)

    # Closing the environment frees its port, so the proxy stops reaching it.
    run.environment_service.close("env-1")
    assert run.bound_ports == set()


# ---------------------------------------------------------------------------
# Account ops — a run works from its private snapshot, never the registry.
# ---------------------------------------------------------------------------

def _account_manager(context):
    import copy
    from process_manager import ProcessManager
    instance = ProcessManager.__new__(ProcessManager)
    instance.account_context = copy.deepcopy(context)
    instance.account_requirements = {role: {} for role in context}
    instance.run_id = "account-run"
    instance.workspace_dir = "/temporary/workspace"
    instance.log_buffer = deque()
    instance.acks = []

    async def capture(body, _request):
        instance.acks.append(body)
    instance._send_ack = capture
    return instance


def _frozen_account():
    return {"id": "choice", "revision": "choice-revision", "credential_account_id": "owner",
            "credential_revision": "owner-revision", "credential_generation": "generation",
            "model": "frozen-model", "endpoint": "https://frozen.example",
            "secrets": {"access_token": "old-token"}, "metadata": {}}


def test_account_ipc_returns_private_detached_snapshot_without_global_lookup(monkeypatch):
    import accounts
    monkeypatch.setattr(accounts, "resolve_accounts", lambda *a, **k: pytest.fail("run reread registry"))
    manager = _account_manager({"model": _frozen_account()})
    asyncio.run(manager._op_account({"role": "model"}))
    assert manager.acks[-1]["account"]["secrets"]["access_token"] == "old-token"
    manager.acks[-1]["account"]["model"] = "caller-modified"
    assert manager.account_context["model"]["model"] == "frozen-model"
    assert not manager.log_buffer


def test_unknown_account_role_is_denied():
    manager = _account_manager({"model": _frozen_account()})
    asyncio.run(manager._op_account({"role": "unselected"}))
    assert manager.acks[-1]["ok"] is False
    assert manager.acks[-1]["reason"] == "permission_denied"


def test_refresh_uses_credential_owner_and_keeps_frozen_selection(monkeypatch):
    import accounts
    calls = []
    monkeypatch.setattr(accounts, "commit_refreshed_secrets", lambda *args: calls.append(args) or "next-revision")
    manager = _account_manager({"model": _frozen_account()})
    asyncio.run(manager._op_commit_account({"role": "model", "expected_revision": "owner-revision", "secrets": {"access_token": "new-token"}}))
    assert calls == [("owner", "owner-revision", {"access_token": "new-token"})]
    assert manager.acks[-1]["committed"] is True
    assert manager.account_context["model"]["secrets"]["access_token"] == "new-token"
    assert manager.account_context["model"]["model"] == "frozen-model"
    assert manager.account_context["model"]["revision"] == "choice-revision"


def test_stale_manager_edit_declines_persistence_but_refresh_stays_in_run(monkeypatch):
    import accounts
    monkeypatch.setattr(accounts, "commit_refreshed_secrets", lambda *args: None)
    manager = _account_manager({"model": _frozen_account()})
    asyncio.run(manager._op_commit_account({"role": "model", "expected_revision": "owner-revision", "secrets": {"access_token": "new-token"}}))
    assert manager.acks[-1]["ok"] is True
    assert manager.acks[-1]["committed"] is False
    assert manager.account_context["model"]["secrets"]["access_token"] == "new-token"
    assert manager.account_context["model"]["model"] == "frozen-model"


def test_refresh_cannot_address_unselected_account_or_revision(monkeypatch):
    import accounts
    monkeypatch.setattr(accounts, "commit_refreshed_secrets", lambda *args: pytest.fail("unexpected keyring write"))
    manager = _account_manager({"model": _frozen_account()})
    for data in ({"role": "another", "expected_revision": "owner-revision"}, {"role": "model", "expected_revision": "foreign-revision"}):
        asyncio.run(manager._op_commit_account({**data, "secrets": {"access_token": "bad"}}))
        assert manager.acks[-1]["ok"] is False


def test_cli_leases_are_bound_to_run_and_role__D05(monkeypatch):
    import accounts
    calls = []
    monkeypatch.setattr(accounts, "materialize_cli_account", lambda account, owner: {"path": "/temporary/selected-account", "lease_id": "lease"})
    monkeypatch.setattr(accounts, "release_cli_account", lambda lease, owner: calls.append((lease, owner)) or {"committed": True, "revision": "next"})
    manager = _account_manager({"model": _frozen_account()})
    asyncio.run(manager._op_account_materialize({"role": "model"}))
    assert manager.acks[-1]["lease_id"] == "lease"
    assert manager.host_bindings == {"lease": {"path": "/temporary/selected-account", "mode": "rw"}}
    asyncio.run(manager._op_account_release({"role": "another", "lease_id": "lease"}))
    assert manager.acks[-1]["ok"] is False
    assert not calls
    assert "lease" in manager.host_bindings
    asyncio.run(manager._op_account_release({"role": "model", "lease_id": "lease"}))
    assert manager.acks[-1]["committed"] is True
    assert calls == [("lease", "account-run")]
    assert manager.host_bindings == {}


@pytest.mark.parametrize("manager_change", ["edit", "delete"])
def test_account_use_answers_the_frozen_snapshot_without_rebinding(registry, monkeypatch, manager_change):
    import accounts
    import account_adapters
    saved = accounts.create_account({"name": "Synthetic", "kind": "account", "service": "openai",
        "metadata": {"harness": "codex"}, "secrets": {"api_key": "captured-key"}})
    manager = _account_manager(accounts.resolve_accounts({"model": saved["id"]}))
    if manager_change == "edit":
        accounts.update_account(saved["id"], {"metadata": {"harness": "opencode"}, "secrets": {"api_key": "manager-key"}})
    else:
        accounts.delete_account(saved["id"])
    monkeypatch.setattr(account_adapters, "check_account", lambda *a: pytest.fail("no credential checks"))
    monkeypatch.setattr(account_adapters, "discover_models", lambda *a: pytest.fail("no model discovery"))

    async def use_twice():
        await asyncio.gather(manager._op_account({"role": "model"}), manager._op_account({"role": "model"}))
    asyncio.run(use_twice())
    for ack in manager.acks:
        assert ack["ok"] is True
        assert ack["account"]["metadata"]["harness"] == "codex"
        assert ack["account"]["secrets"]["api_key"] == "captured-key"
    if manager_change == "edit":
        assert accounts.private_account(saved["id"])["secrets"]["api_key"] == "manager-key"
    else:
        assert accounts.list_accounts()["accounts"] == []
