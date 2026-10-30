# SPDX-License-Identifier: MIT
"""The model providers a Collimate model account can bill: one row each.

Metadata only, like :mod:`collimate.account_taxonomy`: no discovery, no
secrets, no generator runtime. Every fact a provider has is here once — the
API family its endpoint speaks, the standard endpoint, the environment name a
harness reads its key from, the default model a blank model field means, the
subscription plan the vendor sells, and the model-id prefixes of its own
first-party family. The account taxonomy, the Agent's harness adapters (each
maps these ids to its own native provider ids), the host's service catalog
and its key environment all read this table, so adding a provider is one row
here plus the harnesses that bill it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """One model provider.

    ``wire`` is the API family its standard endpoint speaks (``openai`` for
    every OpenAI-compatible endpoint). ``api_key`` is ``False`` for a
    plan-only family (GitHub Copilot): there is no API key to bill, so it is
    reachable only through its subscription, and the model-family guard does
    not apply to it (one Copilot credential serves several vendors' ids).
    ``family`` lists the model-id prefixes of the vendor's own first-party
    models: an id carrying one of another vendor's prefixes is a mismatch."""

    id: str
    label: str
    wire: str
    endpoint: str
    key_env: str
    default_model: str
    login_plan: str = ""
    api_key: bool = True
    family: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1",
             "ANTHROPIC_API_KEY", "claude-sonnet-5-5", login_plan="claude_code",
             family=("claude", "sonnet", "opus", "haiku")),
    Provider("gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta",
             "GEMINI_API_KEY", "gemini-3.8-flash", login_plan="google", family=("gemini",)),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1",
             "OPENAI_API_KEY", "gpt-6-astra", login_plan="chatgpt",
             family=("gpt", "o1", "o3", "o4", "chatgpt")),
    # Groq serves open-weights lines under their vendor slug.
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1",
             "GROQ_API_KEY", "openai/gpt-oss-120b"),
    # Cerebras rotates its open-weights lineup; gpt-oss-120b is the flagship
    # tier its public catalog serves.
    Provider("cerebras", "Cerebras", "openai", "https://api.cerebras.ai/v1",
             "CEREBRAS_API_KEY", "gpt-oss-120b"),
    # OpenRouter takes ``vendor/model`` slugs (dotted versions); the default is
    # the anthropic-sonnet equivalent of the flagship tier above.
    Provider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1",
             "OPENROUTER_API_KEY", "anthropic/claude-sonnet-5.5"),
    Provider("xai", "xAI", "openai", "https://api.x.ai/v1",
             "XAI_API_KEY", "grok-4.7", login_plan="xai", family=("grok",)),
    Provider("mistral", "Mistral AI", "openai", "https://api.mistral.ai/v1",
             "MISTRAL_API_KEY", "mistral-medium-latest",
             family=("mistral", "magistral", "ministral", "codestral", "devstral", "pixtral")),
    # GitHub Copilot is a PLAN-ONLY family: there is no Copilot API key. The
    # real catalogue is whatever the signed-in account's Copilot policy
    # exposes; the GPT Luna line is the one GitHub names as every plan's
    # small-model replacement.
    Provider("copilot", "GitHub Copilot", "openai", "", "", "gpt-6-luna",
             login_plan="github_copilot", api_key=False),
)}


def provider(provider_id: str) -> Provider:
    """The row for ``provider_id``; ``KeyError`` for an unknown one."""
    return PROVIDERS[provider_id]


#: The environment name each key-billed provider's credential travels in.
PROVIDER_KEY_ENV: dict[str, str] = {p.id: p.key_env for p in PROVIDERS.values() if p.api_key}

#: Each provider's standard API endpoint (key-billed providers only).
STANDARD_MODEL_ENDPOINTS: dict[str, str] = {p.id: p.endpoint for p in PROVIDERS.values() if p.endpoint}

#: What a blank model field means under each provider.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {p.id: p.default_model for p in PROVIDERS.values()}

#: The plan-only families: no key, no endpoint, no family guard.
PLAN_ONLY_PROVIDERS: tuple[str, ...] = tuple(p.id for p in PROVIDERS.values() if not p.api_key)

#: provider → the subscription plan its vendor sells.
MODEL_LOGIN_PLANS: dict[str, str] = {p.id: p.login_plan for p in PROVIDERS.values() if p.login_plan}

#: Model-id prefixes → the provider family they belong to.
FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {p.id: p.family for p in PROVIDERS.values() if p.family}


__all__ = [
    "FAMILY_PREFIXES",
    "MODEL_LOGIN_PLANS",
    "PLAN_ONLY_PROVIDERS",
    "PROVIDERS",
    "PROVIDER_DEFAULT_MODELS",
    "PROVIDER_KEY_ENV",
    "Provider",
    "STANDARD_MODEL_ENDPOINTS",
    "provider",
]
