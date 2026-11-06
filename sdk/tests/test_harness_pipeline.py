# SPDX-License-Identifier: MIT
"""The generic preparation every harness leg goes through."""
from __future__ import annotations

import asyncio
import dataclasses

import pytest


from _agent_shared.acp_client import SpawnProfile  # noqa: E402
from _agent_shared.harnesses import HARNESS, CommandsPolicy, PreparedHarness, pipeline  # noqa: E402


@pytest.fixture
def renderer(monkeypatch):
    """A recording renderer in place of the harness's own."""
    seen = []

    async def render(ctx, *, leg, **kw):
        seen.append((dict(ctx), leg, kw))
        profile = SpawnProfile(locus="container", cwd="/workspace", spawn=lambda: None,
                               commands=ctx["commands"])
        return PreparedHarness(CommandsPolicy(ctx["commands"], {}), profile, None, ("argv",), {}, "")

    real = pipeline.resolve
    monkeypatch.setattr(pipeline, "resolve", lambda target: render if target.endswith(":prepare") else real(target))

    async def key(provider):
        return f"key-for-{provider}"

    monkeypatch.setattr(pipeline, "account_api_key", key)
    monkeypatch.setattr(pipeline, "write_instructions", _written)
    return seen


async def _written(adapter, prompt):
    return bool(prompt.strip())


def test_the_pipeline_hands_the_renderer_its_checked_selection(renderer):
    prepared = asyncio.run(pipeline.prepare(HARNESS["goose"], {"provider": "gemini", "commands": "ask"}))
    ctx, leg, _ = renderer[0]
    assert leg == "acp" and ctx["auth"] == "api_key" and ctx["commands"] == "ask"
    assert ctx["credentials"] == pipeline.Credentials("gemini", "", key="key-for-gemini")
    assert ctx["native_provider"].id == "google" and ctx["native_provider"].key_env == "GOOGLE_API_KEY"
    # goose's static facet: the post-load check rides the profile.
    assert prepared.profile.verify_loaded is not None


@pytest.mark.parametrize("ctx,message", [
    ({"provider": "xai"}, "cannot bill provider 'xai'"),
    ({"auth": "chatgpt"}, "isn't honored by backend 'goose'"),
])
def test_a_selection_the_adapter_does_not_serve_is_refused_before_the_renderer(renderer, ctx, message):
    with pytest.raises(ValueError, match=message):
        asyncio.run(pipeline.prepare(HARNESS["goose"], ctx))
    assert renderer == []


def test_a_plan_leg_carries_its_lease_and_no_key(renderer, monkeypatch):
    monkeypatch.setattr(pipeline, "selected_login_ready", lambda harness, plan: True)
    monkeypatch.setattr(pipeline, "login_path", lambda harness: f"/lease/{harness}")
    asyncio.run(pipeline.prepare(HARNESS["opencode"], {"auth": "github_copilot", "commands": "deny"}))
    ctx = renderer[0][0]
    assert ctx["provider"] == "copilot" and ctx["native_provider"].id == "github-copilot"
    assert ctx["credentials"] == pipeline.Credentials("copilot", "github_copilot", login="/lease/opencode")


def test_a_plan_leg_without_a_login_never_reaches_the_renderer(renderer, monkeypatch):
    monkeypatch.setattr(pipeline, "selected_login_ready", lambda harness, plan: False)
    with pytest.raises(RuntimeError, match="pi auth=xai: .*Key manager"):
        asyncio.run(pipeline.prepare(HARNESS["pi"], {"auth": "xai", "commands": "deny"}))
    assert renderer == []


def test_a_level_the_harness_cannot_hold_resolves_down_with_a_notice(renderer):
    prepared = asyncio.run(pipeline.prepare(HARNESS["hermes"], {"provider": "anthropic", "commands": "ask"}))
    assert renderer[0][0]["commands"] == "deny"
    assert "cannot enforce commands: ask" in prepared.commands.notice
    assert prepared.profile.commands_notice == prepared.commands.notice


@pytest.mark.parametrize("prompt,folds", [("be brief", False), ("", True)])
def test_a_home_file_seam_decides_the_fold(renderer, prompt, folds):
    prepared = asyncio.run(pipeline.prepare(HARNESS["qwen-code"], {
        "provider": "openrouter", "commands": "deny", "system_prompt": prompt}))
    assert prepared.profile.fold_system_prompt is folds
    # qwen's static facets: one session per process, its model matcher.
    assert prepared.profile.one_session_per_process is True
    assert prepared.profile.model_matches("x(openai)", "x")


def test_the_home_file_writer_writes_and_clears_the_seam(tmp_path, monkeypatch):
    from _agent_shared import harness_homes
    monkeypatch.setattr(harness_homes, "harness_home", lambda backend: str(tmp_path))
    adapter = HARNESS["vibe"]
    assert asyncio.run(pipeline.write_instructions(adapter, "  the session context \n")) is True
    assert (tmp_path / "AGENTS.md").read_text() == "the session context\n"
    assert asyncio.run(pipeline.write_instructions(adapter, "")) is False
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_renderer_must_return_a_prepared_harness(monkeypatch):
    async def wrong(ctx, *, leg, **kw):
        return {"argv": ()}

    async def key(provider):
        return "k"

    monkeypatch.setattr(pipeline, "resolve", lambda target: wrong)
    monkeypatch.setattr(pipeline, "account_api_key", key)
    adapter = dataclasses.replace(HARNESS["goose"])
    with pytest.raises(TypeError, match="PreparedHarness"):
        asyncio.run(pipeline.prepare(adapter, {"commands": "deny"}))
