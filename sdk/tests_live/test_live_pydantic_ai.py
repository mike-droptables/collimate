# SPDX-License-Identifier: MIT
"""LIVE pydantic-ai backend — the multi-provider default, end to end.

This is the file the five deferrals to "the end-to-end smoke" were pointing at.
It runs the REAL ``agent`` generator (and, where the assertion is about engine
behaviour rather than generator wiring, ``eng.run_agent`` directly) against a
real provider.

Requires: any of ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GROQ_API_KEY`` /
``CEREBRAS_API_KEY`` / ``OPENROUTER_API_KEY`` / ``GEMINI_API_KEY``. Each
provider's parametrization skips independently when its key is absent.

Cost: about four short turns per keyed provider (one oneshot, two chat, one
tool turn) — prompts are one line and replies are one word, so a few
thousand tokens each, seconds apiece. No test here touches Docker.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from .conftest import (
    agent_config,
    assert_ran,
    assistant_replies,
    bind_model_account,
    eng,
    model_account,
    model_for,
    provider_params,
    run_agent_generator,
    run_chat_generator,
)

pytestmark = pytest.mark.live

_TRACE = "_agent/trace.json"


# ══════════════════════════════════════════════════════════════════════
# (a) oneshot — the composition default, through the real generator
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", provider_params())
def test_oneshot_text_turn(provider):
    """A one-line prompt in, the literal reply out: the one-shot's prompt is
    its first annotation message (seeded by the harness as the host does),
    and its answer is posted as a reply to it. The trace is asserted
    separately because the engine writes it unconditionally (it costs no
    model call) while the reply is the model's — a run that produced one but
    not the other is a real regression, not a flake. The trace is one of the
    session's live files, which the run end logs and clears (§9.1.8)."""
    res = run_agent_generator(
        prompt="Reply with exactly: LIVE-OK",
        account=model_account(provider),
        config=agent_config(),
    )
    assert_ran(res, provider)
    prompt = next(m for m in res.annotations if m.kind == "prompt")
    replies = [m for m in res.annotations if m.author == "run"]
    assert [m.reply_to for m in replies] == [prompt.id], f"{provider}: {res.annotations}"
    assert "LIVE-OK" in replies[0].text, f"{provider}: reply was {replies[0].text!r}"
    raw = res.log_files.get(_TRACE)
    assert raw, f"{provider}: no trace logged; log files: {sorted(res.log_files)}"
    trace = json.loads(raw)
    assert any(ev.get("type") == "text" for ev in trace), f"{provider}: {trace}"


# ══════════════════════════════════════════════════════════════════════
# (b) interactive assistant-ui — scripted two-turn conversation
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", provider_params())
def test_chat_carries_prior_turns_into_the_next_prompt(provider):
    """Two scripted turns where the second is answerable ONLY from the first.

    In-process chat continuity is now native ``message_history`` keyed per
    chat thread (``session_key``) — the v1 ``_compose_prompt`` re-serialization
    is gone. A model that answers the second question therefore proves the
    thread history carries, live, which no synthetic test can.

    ``prompt=""`` matters: ``serve_chat``'s ``initial_message`` would otherwise
    seed a THIRD turn ahead of the scripted two."""
    res = run_chat_generator(
        prompt="",
        messages=[
            "Remember this word: ZEPHYR. Reply with just: OK",
            "What word did I just teach you? Reply with only that word.",
        ],
        account=model_account(provider),
        config=agent_config(surface="chat"),
    )
    assert_ran(res, provider)
    replies = assistant_replies(res)
    assert len(replies) == 2, f"{provider}: expected two assistant turns, got {replies}"
    # A failed turn is recorded as `_(error: …)_` instead of raising, so the run
    # would be "ok" with nothing having happened — check before the content.
    assert not any(r.startswith("_(error:") for r in replies), f"{provider}: {replies}"
    assert "ZEPHYR" in replies[1].upper(), f"{provider}: second reply was {replies[1]!r}"


# ══════════════════════════════════════════════════════════════════════
# (c) file tools — a real write reaching the real workspace
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("provider", provider_params())
def test_file_tool_writes_into_the_workspace(provider, native_bridge):
    """Engine-level: the model must choose ``write_file`` and the bytes must
    land in the bridge's workspace. Asserting on the file (not on the reply)
    is the point — a model that merely *says* it wrote the file fails here."""
    bind_model_account(model_account(provider))

    async def turn():
        return await eng.run_agent(
            prompt=(
                "Use the write_file tool to create a file named live.txt whose "
                "entire content is exactly LIVE-FILE-OK. Then reply DONE."
            ),
            backend="pydantic-ai", provider=provider, model=model_for(provider),
            enable_file_tools=True, websearch=False,
        )

    result = asyncio.run(turn())
    files = native_bridge.fs_list()
    assert "live.txt" in files, f"{provider}: workspace holds {sorted(files)}; reply={result.text!r}"
    body = native_bridge.fs_read_any("live.txt")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    assert "LIVE-FILE-OK" in body, f"{provider}: live.txt is {body!r}"
    assert any(ev.get("type") == "tool_use" for ev in result.trace), result.trace
