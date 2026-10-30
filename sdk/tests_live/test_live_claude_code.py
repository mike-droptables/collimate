# SPDX-License-Identifier: MIT
"""LIVE claude-code backend — the ACP HOST locus on subscription auth.

``auth="claude_code"`` bills nothing to an API key: the engine spawns
``claude-agent-acp`` as a host subprocess (installed + preflighted into the
Collimate cache on first use), and the CLI it drives authenticates from the
logged-in ``~/.claude`` OAuth credentials. That is precisely why it needs a
live test — the branch has no key to stub and no wire format to fake.

The attachment test exercises the ACP ``ImageContentBlock`` inline path (the
prompt capability every other media type falls back from into staged files).

Requires: ``COLLIMATE_LIVE_CLAUDE_CODE=1``, host node >= 22 (the adapter's
floor), and a logged-in ``claude`` (``claude login``). No API key is used or
needed. The deeper ACP conformance assertions (session record, MCP tool
visibility, cancel, cross-turn history) live in
``test_live_acp_conformance.py``; this file keeps the product-level checks.

Cost: four ``haiku`` turns against the subscription, a handful of seconds
each. Nothing here builds or runs a container — the ``claude`` TTY on this
machine is ``test_live_host_terminal.py``.
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from .conftest import (
    RED,
    agent_config,
    assert_ran,
    assistant_replies,
    bind_model_account,
    eng,
    model_account,
    png_bytes,
    requires_claude_code,
    run_chat_generator,
)

pytestmark = [
    pytest.mark.live,
    requires_claude_code,
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="no `claude` CLI on PATH — the ACP adapter drives it",
    ),
    # Subscription auth is a `claude setup-token` token — the agent never
    # reads ~/.claude. It reaches the run on the bound model account, which
    # this suite builds from this variable (conftest.PLAN_TOKEN_ENV), so
    # without it there is no credential at all: skip with the fix named.
    pytest.mark.skipif(
        not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
        reason="set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`",
    ),
]

# Cheapest model the CLI accepts by alias; the spawn profile maps it to the
# full id on ANTHROPIC_MODEL (and set_config_option applies it when listed).
_MODEL = "haiku"


def _claude_turn(**overrides):
    """One engine-level claude-code turn on subscription auth.

    The subscription token reaches the run off the bound ``model`` account
    (``gen.account("model")`` inside ``claude_subscription_token``), so the
    turn binds the same claude-code plan entry a runner would before it
    starts — an engine-level turn has no host to resolve roles for it."""
    bind_model_account(model_account(
        "anthropic", model=_MODEL, harness="claude-code",
        auth_type="subscription", plan="claude_code"))
    kwargs = dict(
        prompt="Reply with exactly: LIVE-OK",
        backend="claude-code", auth="claude_code", provider="anthropic",
        model=_MODEL, websearch=False,
    )
    kwargs.update(overrides)

    async def turn():
        return await eng.run_agent(**kwargs)

    return asyncio.run(turn())


@pytest.mark.usefixtures("native_bridge")
def test_oneshot_text_turn_on_subscription_auth():
    """The plain path: no key in play, the reply comes back, and the trace
    carries it."""
    result = _claude_turn()
    assert "LIVE-OK" in (result.text or ""), f"reply was {result.text!r}"
    assert result.backend == "claude-code"
    assert any(ev.get("type") == "text" for ev in result.trace), result.trace


@pytest.mark.usefixtures("native_bridge")
def test_oneshot_attachment_uses_streaming_input():
    """A red PNG through the ACP prompt's inline ``ImageContentBlock`` path.

    A reply that names the colour proves the capability gate read
    ``promptCapabilities.image``, the base64 image block is well-formed on
    the wire, and the text block still arrives alongside it."""
    result = _claude_turn(
        prompt="What colour is the attached image? Reply with one word: the colour.",
        attachments=[{"data": png_bytes(RED), "media_type": "image/png", "name": "square.png"}],
    )
    assert "red" in (result.text or "").lower(), f"reply was {result.text!r}"


def test_chat_turn_through_the_generator():
    """The assistant-ui surface end to end on this backend — the generator
    resolves the harness, the model and the subscription auth from the bound
    ``model`` account rather than the test handing them to ``run_agent``. The
    account is a claude-code login entry, which is exactly what a runner binds
    for this harness."""
    res = run_chat_generator(
        prompt="",
        messages=["Reply with exactly: LIVE-CHAT-OK"],
        account=model_account("anthropic", model=_MODEL, harness="claude-code",
                              auth_type="subscription", plan="claude_code"),
        config=agent_config(surface="chat"),
    )
    assert_ran(res, "claude-code")
    replies = assistant_replies(res)
    assert len(replies) == 1, replies
    assert not replies[0].startswith("_(error:"), replies[0]
    assert "LIVE-CHAT-OK" in replies[0], f"reply was {replies[0]!r}"
