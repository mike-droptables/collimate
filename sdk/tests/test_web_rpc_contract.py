"""The web-session RPC surface, frozen on both ends of the wire.

A session's daemon drives the client's native guest view over exactly five
verbs (``_web_shared/protocol.py`` ``_RPC_METHODS``), and the webview
renderer's ``answerRpc`` is the switch on the other end. Nothing else pins
that agreement: a sixth verb added to one side would ship as a silent
``rpc_error`` at runtime. These tests make the drift a red test with a
message that names both files. (The shell leg — the IPC channels those verbs
ride in the desktop app — is frozen separately in
collimatedev desktop/tests/bridgeSurface.test.ts.)

This is a protocol-surface test: it imports the ``_web_shared`` shared
library and statically reads the one renderer that answers the wire. It
names no generator.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

from tests._workspace import BUILTINS, GENERATORS


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RPC = ("evaluate", "html", "screenshot", "url", "navigate")

_WEBVIEW_MAIN = os.path.join(BUILTINS, "live", "webview", "main.tsx")


def _protocol_and_capture():
    """``_web_shared`` imported off the generators/ asset root the way a real
    run does (mirrors test_web_generator.py's ``_shared``). Needs aiohttp —
    skipped where the optional dep isn't installed."""
    pytest.importorskip("aiohttp")
    root = str(GENERATORS)
    if root not in sys.path:
        sys.path.insert(0, root)
    from _web_shared import capture, protocol

    return protocol, capture


def _renderer_source() -> str:
    with open(_WEBVIEW_MAIN, encoding="utf-8") as f:
        return f.read()


def _function_body(source: str, header: str) -> str:
    """The renderer slice from ``header`` to the next top-level ``export`` —
    coarse on purpose: a static parse is the only way to freeze a TS switch
    from Python without a node dependency, and the failure mode of a coarse
    slice is a loud test failure, not a silent pass."""
    start = source.index(header)
    end = source.find("\nexport ", start + 1)
    return source[start:end] if end != -1 else source[start:]


def test_rpc_method_set_is_frozen():
    protocol, _ = _protocol_and_capture()
    assert protocol._RPC_METHODS == _RPC, (
        "protocol._RPC_METHODS changed — the webview renderer's answerRpc, "
        "CaptureContext, and the desktop bridge freeze must all move with it"
    )


def test_rpc_request_gates_every_method():
    protocol, _ = _protocol_and_capture()
    for method in _RPC:
        frame = protocol._rpc_request("rpc-1", method)
        assert frame == {"kind": "rpc", "id": "rpc-1", "method": method, "params": {}}
    for bogus in ("click", "cookies", "pdf", ""):
        with pytest.raises(ValueError):
            protocol._rpc_request("rpc-1", bogus)


def test_evaluate_calling_convention_is_one_wrapping():
    protocol, _ = _protocol_and_capture()
    assert protocol._evaluate_expression("() => 1") == "(() => 1)()"
    # The renderer's html-over-evaluate shim must be that same wrapping of
    # the serialised document — byte-identical, or the two ends disagree
    # about what an "expression" is.
    expected = protocol._evaluate_expression("() => document.documentElement.outerHTML")
    assert f"'{expected}'" in _renderer_source(), (
        "HTML_EXPRESSION in builtin/live/webview/main.tsx is no longer "
        f"{expected!r} — the _evaluate_expression convention drifted"
    )


def test_capture_context_exposes_exactly_the_rpc_verbs():
    _, capture = _protocol_and_capture()
    public = {
        name: fn
        for name, fn in inspect.getmembers(capture.CaptureContext, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert set(public) == {"evaluate", "html", "screenshot_png", "current_url", "navigate"}
    # Each verb rides exactly one wire method, and together they cover the
    # tuple — a bijection, no orphan verb on either side.
    wire_calls = {
        name: re.findall(r"self\._rpc\(\s*\"(\w+)\"", inspect.getsource(fn))
        for name, fn in public.items()
    }
    assert all(len(calls) == 1 for calls in wire_calls.values()), wire_calls
    assert sorted(calls[0] for calls in wire_calls.values()) == sorted(_RPC)


def test_renderer_answers_exactly_the_protocol_methods():
    body = _function_body(_renderer_source(), "export async function answerRpc")
    cases = set(re.findall(r"case\s+'([a-z]+)'\s*:", body))
    assert cases == set(_RPC), (
        "protocol.py and the webview renderer disagree about the RPC method "
        f"set: renderer answers {sorted(cases)}, protocol says {sorted(_RPC)}"
    )
    # Unknown methods must answer (rpc_error), never hang the daemon's future.
    assert "default:" in body


def test_rpc_guest_type_lists_exactly_the_five():
    body = _function_body(_renderer_source(), "export type RpcGuest = {")
    methods = set(re.findall(r"^\s{4}(\w+)\(", body, re.MULTILINE))
    assert methods == set(_RPC), (
        f"RpcGuest declares {sorted(methods)}; the wire carries {sorted(_RPC)}"
    )


def test_tab_file_version_and_hello_agree_on_both_ends():
    """A tab file is v4 on both ends, and the hello that attaches a socket to
    a tab names it on both ends: the renderer sends ``tab`` from the file it
    renders, the daemon registers the socket for exactly that tab."""
    protocol, _ = _protocol_and_capture()
    from _web_shared import daemon

    art = protocol._tab_artifact(tab="tab-1", url="", proxy_base="/p/", partition="x",
                                 capture_label=None, device="desktop")
    src = _renderer_source()
    assert art["v"] == 4 and "if (j.v !== 4) return EMPTY_ARTIFACT;" in src, (
        "protocol._tab_artifact and the webview renderer's parseArtifact "
        "disagree about the tab file's version"
    )
    hello = _function_body(src, "export function helloFrame")
    assert "kind: 'hello', tab," in hello
    assert 'payload.get("tab")' in inspect.getsource(daemon._handle_client_message)
