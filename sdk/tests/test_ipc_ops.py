# SPDX-License-Identifier: MIT
"""``collimate._host.ipc.OPS`` is the one list of IPC ops: every op ``_host/bridge.py``
sends is in it and every op in it is sent, a send that waits is a request,
and the forwarding relay's tables are drawn from it."""
from __future__ import annotations

import ast
from pathlib import Path

from collimate._host import ipc
from collimate._host.ipc import OPS
from collimate.testing import forward

HOST = Path(ipc.__file__).with_name("bridge.py")
# The bridge's calls whose first argument is the op they send and wait on.
_REQUESTS = {"_call", "_handler_reply", "_annotations_ack"}
# The connection's token frame, checked before dispatch: not an op.
_PREAMBLE = "__auth__"


def _tree():
    return ast.parse(HOST.read_text(encoding="utf-8"))


def _op_of(node: ast.Dict):
    """The literal ``"op"`` of a dict display, or None."""
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "op" and isinstance(value, ast.Constant):
            return value.value
    return None


def _sent_ops() -> set:
    """Every op literal ``_host/bridge.py`` sends: a dict's ``"op"``, a request
    call's first argument, or a value bound to ``op``."""
    sent = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Dict) and (op := _op_of(node)) is not None:
            sent.add(op)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in _REQUESTS and node.args and isinstance(node.args[0], ast.Constant)):
            sent.add(node.args[0].value)
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and any(isinstance(target, ast.Name) and target.id == "op" for target in node.targets)):
            sent.add(node.value.value)
    return sent - {_PREAMBLE}


def _inline_sends():
    """(op, waits) for each op literal handed straight to a sending call."""
    for node in ast.walk(_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
            continue
        name, first = node.func.attr, node.args[0]
        if name in _REQUESTS and isinstance(first, ast.Constant):
            yield first.value, True
        elif name in ("emit", "emit_and_wait") and isinstance(first, ast.Dict) and _op_of(first):
            yield _op_of(first), name == "emit_and_wait"


def test_the_bridge_sends_exactly_the_ops_in_the_table():
    sent = _sent_ops()
    assert sent - set(OPS) == set(), "ops _host/bridge.py sends that OPS lacks"
    assert set(OPS) - sent == set(), "ops in OPS that _host/bridge.py never sends"


def test_a_send_that_waits_is_a_request_and_one_that_does_not_is_a_notify():
    checked = list(_inline_sends())
    assert len(checked) >= 10
    for op, waits in checked:
        assert (OPS[op].kind == "request") == waits, op


def test_every_spec_is_a_request_or_a_notify_and_gates_on_a_named_capability():
    assert {spec.kind for spec in OPS.values()} == {"request", "notify"}
    assert {spec.capability for spec in OPS.values()} == {None, ipc.CAP_ENV_PLANE}
    assert all(OPS[op].kind == "request" for op in ipc.ANNOTATION_OPS)


def test_the_relay_forwards_and_answers_only_table_ops_with_their_kind():
    assert set(forward.FORWARDED_OPS) | forward.LOCAL_OPS <= set(OPS)
    for op, ack in forward.FORWARDED_OPS.items():
        assert (ack is None) == (OPS[op].kind == "notify"), op
