"""Metadata-only account compatibility shared by Key Manager and Agent.

This module performs no discovery, imports no generator runtime, and never
reads secrets. It is the one home of the harness facts both sides need: which
harnesses a model account serves, each harness's model-id rules and defaults,
and where each plan login lives on a machine and inside a run's lease. The
Agent's backend table consumes the same provider/auth rules; the host serves
the harness and plan lists to the Key manager from here.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field


from collimate.harness_catalog import HARNESSES as _HARNESSES
from collimate.providers import (
    MODEL_LOGIN_PLANS, PROVIDER_DEFAULT_MODELS, PROVIDERS, STANDARD_MODEL_ENDPOINTS,
)

# Every projection below is derived from the generated harness catalog
# (collimate.harness_catalog, written from the harness adapters) and the
# provider registry (collimate.providers); none is a second copy.

MODEL_HARNESS_RULES = {harness: {"providers": row["providers"], "auths": row["auths"]}
                       for harness, row in _HARNESSES.items()}

# Public form vocabulary; consumers obtain these through the accounts API.
DEFAULT_MODEL_HARNESS = "pydantic-ai"
MODEL_HARNESS_LABELS = {harness: row["label"] for harness, row in _HARNESSES.items()}

MODEL_API_PROTOCOLS = {
    "openai": "openai", "openai-compatible": "openai", "openai-chat": "openai",
    "openai-responses": "openai", "anthropic": "anthropic", "gemini": "gemini",
}

# The provider a PLAN pins — a subscription is a credential for exactly one
# vendor, so the plan decides what a turn bills.
PLAN_PROVIDERS = {plan: provider for provider, plan in MODEL_LOGIN_PLANS.items()}

# Per-(harness, plan) default model, where a plan's own catalogue differs from
# the provider's API catalogue ("" = the harness picks its own), and the same
# per-(harness, provider) under an API key. Membership decides, never
# truthiness. Each harness declares its own (adapter Model facet).
PLAN_DEFAULT_MODELS = {(harness, plan): model for harness, row in _HARNESSES.items()
                       for plan, model in row["plan_defaults"].items()}
KEY_DEFAULT_MODELS = {(harness, provider): model for harness, row in _HARNESSES.items()
                      for provider, model in row["key_defaults"].items()}


# The friendly Claude aliases a model entry and the `claude` CLI accept, and the
# full ids they stand for (ANTHROPIC_MODEL takes either): Claude Code's own
# resolution on the Anthropic API (code.claude.com/docs/en/model-config,
# checked 2026-10-25). Shared with the app's Key manager so Verify accepts an
# alias the API's model list spells out.
CLAUDE_MODEL_ALIASES = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}


# The command each harness's CLI answers to on a machine: what the Key manager
# looks for to say whether a "standard" (host) run is possible, and what a
# standard run spawns. antigravity's CLI is `agy` — `antigravity` is the IDE.
HARNESS_HOST_BINARY = {harness: row["host_binary"] for harness, row in _HARNESSES.items()
                       if row["host_binary"]}

# Static placement facts shared by the engine, lint, placement and account
# diagnostics.
HARNESS_CONTAINERIZED = tuple(h for h, row in _HARNESSES.items() if row["locus"] == "container")
HARNESS_HOST_TWINS = tuple(h for h, row in _HARNESSES.items() if row["host_twin"])
HARNESS_TERMINAL = tuple(h for h, row in _HARNESSES.items() if row["terminal"])

# The (harness, plan) pairs whose CLI surface runs the vendor's own CLI on this
# machine (the catalog's ``terminal_locus``): that CLI signs in itself, in a
# per-account home, so an entry on such a plan is complete without the
# credential Collimate holds for the plan's other surfaces (a pasted login
# document, a setup token); only the surfaces that read that credential refuse
# without it.
HOST_SIGN_IN_PLANS = frozenset((harness, auth) for harness, row in _HARNESSES.items()
                               for auth, locus in row["terminal_locus"].items()
                               if auth != "api_key" and locus == "host")
# Harnesses whose installed CLI the host probe can select: the host-locus
# adapter (which can also use its bundled CLI) and the host twins; a host
# harness with no CLI of its own (pydantic-ai) has nothing to probe.
HARNESS_STANDARD_RUNTIME = tuple(h for h, row in _HARNESSES.items()
                                 if row["host_binary"] and (row["locus"] == "host" or row["host_twin"]))

# Harnesses whose blank model is their OWN pick rather than the provider's
# default: the friendly alias claude takes, and agy's "let me choose"
# sentinel.
HARNESS_DEFAULT_MODEL_OVERRIDES = {h: row["default_model"] for h, row in _HARNESSES.items()
                                   if row["default_model"]}

# Harnesses that take their OWN model names rather than the provider's API
# ids. An API id outside the vocabulary is refused by the harness before the
# first turn; the harness's default-model sentinel is always accepted too.
HARNESS_MODEL_VOCABULARY = {h: row["vocabulary"] for h, row in _HARNESSES.items() if row["vocabulary"]}

# What a model id the service cannot list does at run time, per harness. The
# Key manager says it wherever an id goes unchecked, so a pass never reads as
# a promise; a harness without a row fails the first turn.
UNCHECKED_MODEL_CONSEQUENCE = {h: row["unchecked"] for h, row in _HARNESSES.items() if row["unchecked"]}
UNCHECKED_MODEL_FALLBACK = "an ID the plan does not offer fails at the first turn"


@dataclass(frozen=True)
class PlanLogin:
    """A harness whose vendor plan signs in with a pasted login.

    ``command`` signs in on a machine and writes the document to ``relative``
    under HOME, or under the harness's own relocation variable ``env`` (plus
    ``suffix``); ``also`` lists other places an install keeps it; a login with
    no ``relative`` is a token the user creates rather than a file a sign-in
    writes. ``field`` is the account secret the paste is saved as
    (``credential_json``, a JSON store, or ``credential_file``, opaque text),
    and ``lease`` the path it takes inside a run's account lease
    (``<lease>/<harness>/<lease…>``): the host writes and collects it there,
    and the generator's login store hands a harness backend that path
    (``login_store.login_path``) or a folder on it, and no other.
    ``store_keys`` is, for a store that keeps several providers' logins in
    one document (opencode, pi), the entry each plan's login occupies in it.
    ``validator`` names the check in :data:`LOGIN_VALIDATORS`. ``copy`` marks
    a harness that takes a copy of the login into its vendor state and
    returns the refreshed copy at release (antigravity), where every other
    harness writes the leased document in place. ``hint`` is the sentence the
    Key manager shows under the document field, ``{store}`` standing for the
    file found or expected on the machine. Built from each harness adapter's
    ``Login`` (:data:`PLAN_LOGINS`).
    """
    harness: str
    command: str
    relative: str
    field: str
    lease: tuple[str, ...]
    validator: str
    env: "str | None" = None
    suffix: str = ""
    also: tuple[str, ...] = ()
    store_keys: Mapping[str, str] = dataclass_field(default_factory=dict, hash=False)
    copy: bool = False
    hint: str = "The whole contents of {store}, copied from the opening {{ to the closing }}."

    @property
    def opaque(self) -> bool:
        """The paste is opaque text (a token file, a token), not JSON."""
        return self.field == "credential_file"

    def document_problem(self, content: str, plan: str = "") -> str | None:
        """Why a pasted login does NOT hold ``plan``'s login on ``harness``,
        in the user's terms; ``None`` when it does."""
        if not content.strip():
            return "nothing was pasted"
        validator = LOGIN_VALIDATORS[self.validator]
        if self.opaque:
            return validator.problem(self, content.strip(), plan)
        try:
            data = json.loads(content)
        except ValueError:
            if _looks_cut_off(content):
                return ("the pasted text is cut off — its lines end with '>' and the tokens are truncated, which is how a terminal "
                        "shows a wide file with wrapping off. Open the file in an editor (or `cat` it in a wide terminal) and copy "
                        "everything from the opening { to the closing }")
            return "the pasted text is not valid JSON; copy the whole file, from the opening { to the closing }"
        return self._document_problem(data, plan)

    def _document_problem(self, data: object, plan: str = "") -> str | None:
        """Pure parsed-document meaning, shared by paste and selected leases."""
        if not isinstance(data, dict) or not data:
            return "the pasted JSON is not a login file"
        return LOGIN_VALIDATORS[self.validator].problem(self, data, plan)

    def document_note(self, content: str, plan: str = "") -> str:
        """What the pasted login says about its own freshness. An access
        token past its expiry is NOT a failure — every harness refreshes it from
        the refresh token on first use — so this is a note for the Verify line,
        and a warning only where the app itself would stop trusting the login."""
        if self.opaque:
            return ""
        try:
            data = json.loads(content)
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        return LOGIN_VALIDATORS[self.validator].note(self, data, plan)


@dataclass(frozen=True)
class LoginValidator:
    """A login format's check: ``problem(login, document, plan)`` is why the
    document is not that plan's login (``None`` when it is), ``note`` what
    it says about its freshness. The document is the parsed JSON, or the
    stripped text of an opaque login."""
    problem: object
    note: object = lambda login, data, plan: ""


def _codex_problem(login: PlanLogin, data: dict, plan: str) -> str | None:
    # A browser/device sign-in: tokens with a refresh token (OpenAI's own
    # validity check). A Codex access-token login writes
    # personal_access_token or agent_identity and no tokens at all.
    if any(isinstance(data.get(key), str) and data[key] for key in ("personal_access_token", "agent_identity")):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return ("this is an API-key file, not a ChatGPT sign-in" if data.get("OPENAI_API_KEY")
                else "the file holds no tokens; sign in with `codex login` first")
    if not tokens.get("refresh_token"):
        return "the file's tokens carry no refresh_token; sign in with `codex login` again"
    if data.get("auth_mode") not in (None, "chatgpt"):
        return f"the file's auth_mode is {data.get('auth_mode')!r}, not a ChatGPT sign-in"
    return None


def _codex_note(login: PlanLogin, data: dict, plan: str) -> str:
    if not isinstance(data.get("tokens"), dict):
        return "a Codex access-token login; it expires on the date chosen when the token was created and is never refreshed"
    last = data.get("last_refresh")
    try:
        refreshed = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp() if last else None
    except ValueError:
        refreshed = None
    if refreshed and refreshed <= time.time() - _CODEX_STALE_AFTER:
        return f"codex has not refreshed this login since {_when(refreshed)}; if a run cannot authenticate, sign in again and paste the file"
    exp = _jwt_expiry((data.get("tokens") or {}).get("access_token"))
    if exp:
        return (f"its access token expires {_when(exp)} and codex refreshes it as it runs" if exp > time.time()
                else f"its access token expired {_when(exp)}; codex refreshes it on first use")
    return ""


def _store_problem(login: PlanLogin, data: dict, plan: str) -> str | None:
    plan = plan.strip().lower()
    key = login.store_keys.get(plan)
    if plan and key is None:
        return "the selected plan is not supported by this login store"
    rows = [data.get(key)] if key else data.values()
    if any(isinstance(row, dict) and row.get("type") == "oauth" for row in rows):
        return None
    return f"the file holds no {plan or 'plan'} OAuth entry" + (f" under {key!r}" if key else "") + "; sign in with the app first"


def _store_note(login: PlanLogin, data: dict, plan: str) -> str:
    key = login.store_keys.get(plan)
    row = data.get(key) if key else None
    expires = row.get("expires") if isinstance(row, dict) else None
    if isinstance(expires, (int, float)) and expires > 0:
        epoch = expires / 1000 if expires > 10 ** 11 else float(expires)
        return (f"its access token expires {_when(epoch)} and the harness refreshes it as it runs" if epoch > time.time()
                else f"its access token expired {_when(epoch)}; the harness refreshes it on first use")
    return ""


# cline keeps a ChatGPT plan login under its oauth-only openai-codex provider.
_CLINE_PLAN_PROVIDER_ID = "openai-codex"


def _cline_problem(login: PlanLogin, data: dict, plan: str) -> str | None:
    auth = data
    for key in ("providers", _CLINE_PLAN_PROVIDER_ID, "settings", "auth"):
        auth = auth.get(key) if isinstance(auth, dict) else None
    token = auth.get("accessToken") if isinstance(auth, dict) else None
    if isinstance(token, str) and token.strip():
        return None
    return "the file holds no openai-codex login; run `cline auth openai-codex` first"


# GitHub token prefixes that can use Copilot: fine-grained personal access
# tokens, OAuth tokens (`copilot login`) and GitHub App user tokens. A classic
# personal access token cannot (docs.github.com, Copilot CLI authentication).
_GITHUB_TOKEN_PREFIXES = ("github_pat_", "gho_", "ghu_")


def _github_token_problem(login: PlanLogin, token: str, plan: str) -> str | None:
    if any(c.isspace() for c in token):
        return "a token is one line with no spaces; paste only the token"
    if token.startswith("ghp_"):
        return ("this is a classic personal access token, which Copilot does not accept; create a fine-grained token "
                "with the Copilot Requests permission")
    if not token.startswith(_GITHUB_TOKEN_PREFIXES):
        return "this is not a GitHub token (a fine-grained token starts with github_pat_)"
    return None


# The Google authorized-user document the Antigravity ACP server keeps as its
# sign-in (``antigravity-acp/acp_token.json``): what it needs to refresh an
# access token. Checked by key presence only; no value is ever echoed.
_GOOGLE_AUTHORIZED_USER_KEYS = ("refresh_token", "client_id", "client_secret", "token_uri")


def _google_authorized_user_problem(login: PlanLogin, data: dict, plan: str) -> str | None:
    missing = [key for key in _GOOGLE_AUTHORIZED_USER_KEYS
               if not (isinstance(data.get(key), str) and data[key].strip())]
    if missing:
        return ("the file is not the Antigravity ACP server's sign-in: it lacks "
                + ", ".join(missing) + "; sign in through the official server first")
    return None


#: Every login format a harness adapter's ``Login.validator`` can name.
LOGIN_VALIDATORS = {
    "codex_auth": LoginValidator(_codex_problem, _codex_note),
    "oauth_store": LoginValidator(_store_problem, _store_note),
    "cline_providers": LoginValidator(_cline_problem),
    "opaque": LoginValidator(lambda login, content, plan: None),
    "github_token": LoginValidator(_github_token_problem),
    "google_authorized_user": LoginValidator(_google_authorized_user_problem),
}


def _plan_login(harness: str, row: dict) -> PlanLogin:
    login = row["login"]
    return PlanLogin(harness, login["command"], login["relative"], login["field"], tuple(login["lease"]),
                     login["validator"], login["env"] or None, login["suffix"], tuple(login["also"]),
                     dict(login["store_keys"]), login["copy"], login["hint"])


# Every harness whose plan login is pasted as a document or token file.
# Claude's plan is a pasted token carried in the environment, so claude-code
# has no row.
PLAN_LOGINS = {harness: _plan_login(harness, row) for harness, row in _HARNESSES.items() if row["login"]}

# The harness a new login under each plan starts on in the Key manager: the
# vendor's own CLI, and opencode for xAI, which has none.
PLAN_DEFAULT_HARNESS = {plan: harness for harness, row in _HARNESSES.items()
                        for plan in row["plan_default_for"]}


def default_model_for(account: Mapping) -> "str | None":
    """What a BLANK model on this entry means: the id the harness will use
    (``""`` when the harness keeps its own default, as cline does under a
    plan and codex does on openai either way), or ``None`` when
    nothing can stand in — a custom endpoint (or a blank one on a service
    with no standard endpoint) under an API key has no default, so a model
    must be entered."""
    metadata = account.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return None
    try:
        provider = model_account_provider(account)
    except (ValueError, TypeError):
        return None
    login = is_cli_model_login(account)
    harness = str(metadata.get("harness") or ("" if login else DEFAULT_MODEL_HARNESS))
    if login:
        plan = str(metadata.get("plan") or MODEL_LOGIN_PLANS.get(provider) or "")
        if (harness, plan) in PLAN_DEFAULT_MODELS:
            return PLAN_DEFAULT_MODELS[(harness, plan)]
    else:
        # A blank endpoint means the SERVICE's standard one (as in
        # supported_model_harnesses); only the wire provider's own standard
        # endpoint has a default model.
        service = str(account.get("service") or "")
        endpoint = str(account.get("endpoint") or "").rstrip("/") or STANDARD_MODEL_ENDPOINTS.get(service, "")
        if not endpoint or endpoint != STANDARD_MODEL_ENDPOINTS.get(provider):
            return None
        if (harness, provider) in KEY_DEFAULT_MODELS:
            return KEY_DEFAULT_MODELS[(harness, provider)]
    return HARNESS_DEFAULT_MODEL_OVERRIDES.get(harness) or PROVIDER_DEFAULT_MODELS.get(provider) or None


def is_cli_model_login(account: Mapping) -> bool:
    metadata = account.get("metadata") or {}
    return (account.get("auth_type") in {"cli", "subscription"}
            or account.get("protocol") == "cli"
            or bool(metadata.get("plan")))


def model_account_auth(account: Mapping) -> str:
    """What a model account bills under: ``api_key``, or the plan its login
    signs in to (its ``metadata.plan``, else its provider's plan)."""
    if not is_cli_model_login(account):
        return "api_key"
    metadata = account.get("metadata") or {}
    plan = metadata.get("plan") if isinstance(metadata, Mapping) else None
    return str(plan or MODEL_LOGIN_PLANS.get(model_account_provider(account)) or "")


def signs_in_on_host(account: Mapping) -> bool:
    """Whether ``account``'s harness (its plan's default one when it names
    none) and plan run its CLI surface on this machine, signing in there
    (:data:`HOST_SIGN_IN_PLANS`)."""
    metadata = account.get("metadata") or {}
    harness = metadata.get("harness") if isinstance(metadata, Mapping) else None
    try:
        auth = model_account_auth(account)
    except (ValueError, TypeError):
        return False
    return (str(harness or PLAN_DEFAULT_HARNESS.get(auth) or ""), auth) in HOST_SIGN_IN_PLANS


def model_account_provider(account: Mapping) -> str:
    service = str(account.get("service") or "")
    protocol = str(account.get("protocol") or "")
    provider = service
    if is_cli_model_login(account):
        if provider not in PROVIDERS:
            raise ValueError(f"Unsupported CLI model account service {service!r}")
        return provider
    wire_provider = MODEL_API_PROTOCOLS.get(protocol)
    if wire_provider is None:
        raise ValueError(f"Unsupported model account protocol {protocol!r}")
    if wire_provider == "openai" and PROVIDERS.get(provider) and PROVIDERS[provider].wire == "openai":
        return provider
    return wire_provider


def supported_model_harnesses(account: Mapping) -> list[str]:
    """Usable built-in harnesses for a public credential account/model entry.

    Custom endpoints require the direct API harness. CLI logins are owned by
    their selected harness; an account still being configured (no owner yet)
    exposes only harnesses supporting its service's login plan.
    """
    if not isinstance(account, Mapping):
        return []
    kind = account.get("kind", "account")
    if not isinstance(kind, str) or kind not in {"account", "model"}:
        return []
    metadata = account.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return []
    auth_type = account.get("auth_type") or "api_key"
    if not isinstance(auth_type, str) or auth_type not in {"api_key", "none", "cli", "subscription"}:
        return []
    try:
        provider = model_account_provider(account)
    except (ValueError, TypeError):
        return []
    endpoint = str(account.get("endpoint") or "").rstrip("/")
    login = is_cli_model_login(account)
    if login:
        if auth_type not in {"cli", "subscription"}:
            return []
        auth = metadata.get("plan") or MODEL_LOGIN_PLANS.get(provider)
        if auth != MODEL_LOGIN_PLANS.get(provider):
            return []
    else:
        auth = "api_key"
        service = str(account.get("service") or "")
        if not endpoint and (service not in STANDARD_MODEL_ENDPOINTS or auth_type == "none"):
            return []
        endpoint = endpoint or STANDARD_MODEL_ENDPOINTS[service]
    result = []
    for harness, row in _HARNESSES.items():
        if auth not in row["auths"]:
            continue
        # A harness that bills custom endpoints (pydantic-ai, whose model
        # connection Collimate builds) takes any provider's wire, any endpoint
        # and an unauthenticated one.
        custom = row["custom_endpoint"]
        if login:
            if metadata.get("harness") and metadata["harness"] != harness:
                continue
        elif provider not in row["providers"] and not custom:
            continue
        if endpoint and not custom and endpoint != STANDARD_MODEL_ENDPOINTS.get(provider):
            continue
        if auth_type == "none" and not custom:
            continue
        result.append(harness)
    return result


def _looks_cut_off(content: str) -> bool:
    """A file shown in a terminal with wrapping off (or a narrow pager) is
    copied with every long line ending in ``>`` or ``…`` and its tokens
    truncated — the most common way a paste stops being JSON."""
    lines = [line.rstrip() for line in content.strip().splitlines()]
    return sum(line.endswith((">", "…")) for line in lines) >= 2

_CODEX_STALE_AFTER = 8 * 24 * 3600

def _jwt_expiry(token) -> float | None:
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, TypeError):
        return None
    return float(claims["exp"]) if isinstance(claims.get("exp"), (int, float)) else None

def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
