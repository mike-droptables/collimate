# SPDX-License-Identifier: MIT
"""The provider registry: one row per provider, every projection derived."""
from __future__ import annotations

from collimate import providers


def test_every_projection_is_derived_from_the_rows():
    rows = providers.PROVIDERS
    assert providers.PROVIDER_KEY_ENV == {p.id: p.key_env for p in rows.values() if p.api_key}
    assert providers.STANDARD_MODEL_ENDPOINTS == {p.id: p.endpoint for p in rows.values() if p.endpoint}
    assert providers.PROVIDER_DEFAULT_MODELS == {p.id: p.default_model for p in rows.values()}
    assert providers.PLAN_ONLY_PROVIDERS == ("copilot",)
    assert providers.MODEL_LOGIN_PLANS == {
        "anthropic": "claude_code", "gemini": "google", "openai": "chatgpt",
        "xai": "xai", "copilot": "github_copilot"}


def test_key_billed_providers_have_an_endpoint_and_a_conventional_key_env():
    for row in providers.PROVIDERS.values():
        if not row.api_key:
            assert not row.key_env and not row.endpoint, row.id
            continue
        assert row.endpoint.startswith("https://"), row.id
        assert row.key_env.isupper() and row.key_env.endswith("_API_KEY"), row.id
        assert row.wire in ("openai", "anthropic", "gemini"), row.id
        assert row.default_model, row.id


def test_mistral_is_one_row():
    mistral = providers.provider("mistral")
    assert (mistral.wire, mistral.endpoint, mistral.key_env) == (
        "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY")
    assert "mistral" in providers.FAMILY_PREFIXES
