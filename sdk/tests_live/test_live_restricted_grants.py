# SPDX-License-Identifier: MIT
"""Restricted grants and host CLI placement against the REAL claude adapter.

The claude chat leg's commands levels run against a local Anthropic mock in
``test_live_acp_conformance.py``; these legs put a real model and the
subscription behind the same adapter. The claude presets float
``claude-agent-acp: latest``, which makes adapter drift a live-only risk: a
release that adds a new mode-switching option id would be auto-selected (the
filter is a denylist) and the CLI would stop asking. These legs catch that,
check that ``commands: deny`` gives a real model no shell, and cover the
grant-selected handoff to the machine's own ``claude``.

Gates: the claude legs' own (``COLLIMATE_LIVE_CLAUDE_CODE`` +
``CLAUDE_CODE_OAUTH_TOKEN`` + host node >= 22); the host leg also needs a
usable ``claude`` on this machine. Cost: four short ``haiku`` turns.
"""
from __future__ import annotations
from _agent_shared import acp_policy, host_adapters

import dataclasses
import os
import stat

import pytest

from .conftest import eng, requires_claude_code, run_on_test_loop
from .test_live_acp_conformance import (
    _CLAUDE_MODEL,
    _bind_claude_account,
    requires_host_node,
)

requires_claude_token = pytest.mark.skipif(
    not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
    reason="set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` — the claude "
           "legs run on a setup token in a private config dir, never ~/.claude",
)

pytestmark = [pytest.mark.live, requires_claude_token, requires_claude_code, requires_host_node,
              pytest.mark.usefixtures("one_loop_per_test")]


def test_host_grant_drives_the_machine_s_own_claude(native_bridge, tmp_path):
    """The host grant hands the adapter the probe's ``claude``: a
    wrapper standing in for it records that the adapter really exec'd it (a
    log line alone would only prove we SET the variable)."""
    from collimate import host_harness

    found = host_harness.probe("claude-code")
    if found is None or not found.usable:
        pytest.skip("no usable `claude` on this machine for host placement")
    marker = tmp_path / "exec-count"
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\necho x >> "{marker}"\nexec "{found.path}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)

    _bind_claude_account("claude_code")
    res = run_on_test_loop(eng.run_agent(
        prompt="Reply with exactly: ACP-OK",
        backend="claude-code", auth="claude_code", model=_CLAUDE_MODEL,
        enable_file_tools=False, websearch=False,
        runtime="standard",
        host_harness={"path": str(wrapper), "version": found.version, "binary": "claude"},
    ))
    assert "ACP-OK" in (res.text or ""), res.text
    assert marker.exists(), "the adapter ran its bundled CLI, not the machine's claude"


def test_a_shell_the_grant_withholds_is_refused(native_bridge):
    """``commands: deny`` on claude's chat leg (on this machine): a prompt
    that asks for Bash must not get a shell — neither the profile's removal
    of the tool nor the permission gate's refusal of a command request may
    let ``id`` run."""
    _bind_claude_account("claude_code")
    res = run_on_test_loop(eng.run_agent(
        prompt=("Use your Bash tool to run the command `id` and reply with "
                "its exact output. If you cannot run it, say CANNOT."),
        backend="claude-code", auth="claude_code", model=_CLAUDE_MODEL,
        commands="deny", enable_file_tools=False, websearch=False,
    ))
    outputs = [str(t.get("output", "")) for t in res.trace if t.get("type") == "tool_result"]
    assert not any("uid=" in o for o in outputs), outputs
    assert "uid=" not in (res.text or ""), res.text


# The per-call option ids the auto picker may choose: they answer ONE request
# and leave the adapter session's permission mode alone. The picker takes an
# allow_once option, and an allow_always one only from a prompt that offers no
# allow_once (so claude's allow-with-updates, which persists a rule, is never
# its pick). Any other id it could pick must be caught by the mode-switch
# filter.
_PER_CALL_OPTION_IDS = frozenset({"allow-once", "exit-plan-default"})


def _auto_pick_candidates(options: list) -> list:
    """The allow ids the auto picker could choose from one prompt's
    ``(kind, option_id)`` pairs."""
    once = [oid for kind, oid in options if kind == "allow_once"]
    return once or [oid for kind, oid in options if kind == "allow_always"]


def test_every_allow_option_the_latest_adapter_offers_is_known(native_bridge, monkeypatch):
    """Resolve the adapter the way the presets do (``latest``) and record the
    options of two real prompts: a file write (the plain tool prompt) and an
    ExitPlanMode (the only prompt whose options switch the session's mode —
    the adapter builds every ``exit-plan-*`` id there and nowhere else). Every
    allow-kind id must be a known per-call id or filtered as a mode switch,
    so a new mode-switch id in a floating release fails here instead of being
    auto-selected in users' runs."""
    import gen
    from _agent_shared import acp_client

    monkeypatch.setattr(gen, "packages", lambda: {"claude-agent-acp": "latest"}, raising=False)
    seen: list = []
    original = acp_client._CollimateClient.request_permission

    async def _record(self, session_id, tool_call, options, **kwargs):
        seen.append([(getattr(o, "kind", ""), getattr(o, "option_id", ""))
                     for o in options or []])
        return await original(self, session_id, tool_call, options, **kwargs)

    monkeypatch.setattr(acp_client._CollimateClient, "request_permission", _record)
    _bind_claude_account("claude_code")
    run_on_test_loop(eng.run_agent(
        prompt="Create a file named note.txt containing the word hello. Reply DONE.",
        backend="claude-code", auth="claude_code", model=_CLAUDE_MODEL,
        enable_file_tools=True, websearch=False,
    ))

    # The second turn starts in plan mode, through the same mode select a
    # profile's ``session_mode`` uses, so the model must end it with
    # ExitPlanMode — the prompt that carries the mode-switching options.
    apply_mode = acp_client.AcpSession._apply_session_mode_request

    async def _plan_mode(self):
        self.profile = dataclasses.replace(self.profile, session_mode="plan")
        await apply_mode(self)

    monkeypatch.setattr(acp_client.AcpSession, "_apply_session_mode_request", _plan_mode)
    run_on_test_loop(eng.run_agent(
        prompt=("Plan how you would create a file named plan.txt containing "
                "the word hello (one short step), then present the plan with "
                "your ExitPlanMode tool. Reply DONE."),
        backend="claude-code", auth="claude_code", model=_CLAUDE_MODEL,
        enable_file_tools=True, websearch=False,
    ))

    unknown = [oid for options in seen for oid in _auto_pick_candidates(options)
               if oid not in _PER_CALL_OPTION_IDS
               and not oid.startswith("allow-skill-")
               and not acp_policy._switches_permission_mode(oid)]
    assert not unknown, (f"adapter {host_adapters.resolve_claude_adapter_version()} offers "
                         f"allow options the filter does not know: {unknown}")
    if not any(oid.startswith("exit-plan-") for options in seen for _, oid in options):
        pytest.skip("the adapter offered no ExitPlanMode prompt (no plan mode, or the "
                    "model never left it) — the mode-switch ids went unchecked")
