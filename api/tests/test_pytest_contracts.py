"""The app's copies of the SDK's wire contracts.

- ``frontend/src/generated/channelEvents.ts`` is what the SDK's
  ``sdk/scripts/sync_contracts.py`` generates from ``channel_events.py``,
  byte for byte, so the client's SSE union cannot drift from the records the
  app emits.
- The IPC dispatch table is the SDK's ``OPS``: every op has an ``_op_<name>``
  handler, every handler is an op, and every capability the gate asks for is
  one the grant knows.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from collimate._host.ipc import OPS

from process_manager import ProcessManager
from process_manager.ipc import OP_CAPABILITY, REQUEST_OPS, IPCMixin
from runner_permissions import UNRESTRICTED, RunnerPermissions

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "sdk" / "scripts" / "sync_contracts.py"
GENERATED = ROOT / "frontend" / "src" / "generated" / "channelEvents.ts"


def _sync():
    spec = importlib.util.spec_from_file_location("sync_contracts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_clients_channel_events_are_what_the_sdk_generates():
    sync = _sync()
    assert sync.TS == GENERATED
    assert GENERATED.read_text(encoding="utf-8") == sync.typescript_source(), (
        f"run {sync.COMMAND}")


def test_every_op_has_a_handler_and_every_handler_an_op():
    handlers = {name.removeprefix("_op_") for name in dir(ProcessManager) if name.startswith("_op_")}
    assert set(OPS) - handlers == set(), "ops without an _op_<name> handler"
    assert handlers - set(OPS) == set(), "_op_<name> handlers for no op in OPS"

    class _Mgr(IPCMixin):
        pass
    dispatch = _Mgr()._ipc_handlers
    assert list(dispatch) == list(OPS)
    assert all(handler.__name__ == f"_op_{op}" for op, handler in dispatch.items())


def test_the_request_set_and_the_gate_are_read_from_the_table():
    assert REQUEST_OPS == {op for op, spec in OPS.items() if spec.kind == "request"}
    assert OP_CAPABILITY == {op: spec.capability for op, spec in OPS.items() if spec.capability}
    assert REQUEST_OPS and OP_CAPABILITY and set(OPS) - REQUEST_OPS


def test_every_capability_the_gate_asks_for_is_one_the_grant_knows():
    # ``allows`` raises KeyError for a capability it has no check for.
    for capability in set(OP_CAPABILITY.values()):
        assert UNRESTRICTED.allows(capability) is True
        assert RunnerPermissions.from_definition({}).allows(capability) is False
