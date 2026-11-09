# SPDX-License-Identifier: MIT
"""Runner configuration, model selection and grant validation.

Extracted from the shared runtime without changing policy or event shapes.
"""
from __future__ import annotations
import gen
from .harnesses import BACKENDS as _BACKENDS, HARNESS, BackendSpec, resolve, terminal_locus
from collimate.account_taxonomy import (
    CLAUDE_MODEL_ALIASES, KEY_DEFAULT_MODELS, PLAN_DEFAULT_MODELS, PLAN_PROVIDERS, default_model_for,
)
from collimate.harness_placement import container_refusal as container_refusal, placement
from collimate.permissions import Grant, effective_commands
from collimate.providers import (
    FAMILY_PREFIXES, PLAN_ONLY_PROVIDERS, PROVIDER_DEFAULT_MODELS, STANDARD_MODEL_ENDPOINTS,
)


# ──────────────────────────────────────────────────────────────────────
# The backend matrix is the harness adapters' (harnesses/<id>/adapter.py):
# the engine validates and dispatches from them, and the agent-config.set_yaml
# options are generated from them (sdk/scripts/sync_harness_catalog.py).
# Canonical names only — no alias tolerance.
# ──────────────────────────────────────────────────────────────────────

# The run's surface is its ``surface`` setting: ``annotation`` (the default)
# reads the user's messages in the run's annotation context and answers in
# place, ``chat`` is a conversation, ``terminal`` (labelled CLI) a harness's
# own TTY, in its container or on this machine (its terminal locus). Only the
# annotation surface takes a ``session`` — a
# one-shot that answers its prompt once, or an interactive run that keeps
# listening. A chat or terminal run is always interactive.
_SESSIONS = ("oneshot", "interactive")
_SURFACES = ("annotation", "chat", "terminal")

# The keys parse_agent_config reads on every surface: the agent-config.set_yaml
# fields every surface uses, the model account's connection
# (accounts.model_config sets backend / provider / model / auth and the
# account itself), and enable_file_tools, which a runner's saved settings may
# carry though no schema field shows it.
_CONFIG_KEYS = frozenset({
    "surface", "backend", "system_prompt", "selected_file_first",
    "provider", "model", "auth", "_account",
    "enable_file_tools",
})
# The keys only some surfaces read. Every one is a field of the one schema,
# so a saved runner lists them all: a surface ignores the keys it does not
# read (the schema hides them). The terminal takes neither ``mcp`` nor
# ``approvals`` (the harness's own TTY answers its permission requests and
# never attaches the cell's MCP servers).
_SURFACE_KEYS = {
    "annotation": frozenset({"session", "mcp", "approvals"}),
    "chat": frozenset({"mcp", "approvals"}),
    "terminal": frozenset(),
}
_KNOWN_KEYS = _CONFIG_KEYS.union(*_SURFACE_KEYS.values())


# Every auth value the roster offers: an API key, or one of the plan
# (subscription) logins. A plan is always a login the USER already pays for,
# pasted once in Key manager and then shared across cells by whatever
# shape that vendor's token rotation allows (see ``login_store``); it never
# bills a Collimate account and never reaches the user's own harness home.
_AUTHS = ("api_key", *PLAN_PROVIDERS)


def backend_spec(backend: str) -> BackendSpec:
    """The :class:`BackendSpec` for a canonical backend name; raises the
    shared dropdown-style ``ValueError`` on an unknown one."""
    bk = (backend or "").strip().lower() or "pydantic-ai"
    spec = _BACKENDS.get(bk)
    if spec is None:
        raise ValueError(
            f"unknown backend {bk!r} — expected one of {', '.join(_BACKENDS)}"
        )
    return spec


# ── Docker placement ──────────────────────────────────────────────────
# Where a harness runs under the run's grant is collimate.harness_placement's
# one answer (placement); this module only routes on it.


def run_needs_container(backend: str, surface: str = "chat", runtime: str = "docker",
                        auth: str = "api_key") -> bool:
    """Whether THIS run (backend × surface × runtime × auth) actually execs
    a CLI in a container — the per-run routing decision.

    The ``terminal`` surface is a live TTY where the harness's terminal
    locus puts it for ``auth``: its own container, or this machine (Claude
    Code; Antigravity on the Google plan). The engine turn (annotation /
    chat) of a containerized backend runs in its image under ``runtime:
    docker`` and from the machine's own CLI under ``runtime: standard``; a
    host-locus harness's engine turn (pydantic-ai, claude-code) is a host
    subprocess either way."""
    spec = backend_spec(backend)
    if (surface or "").strip().lower() == "terminal":
        return "terminal" in spec.uis and terminal_locus(HARNESS[backend], auth) == "container"
    if spec.containerized:
        return (runtime or "docker") != "standard"
    return False


def _host_commands_level(backend: str, commands: str, host_harness: dict) -> tuple:
    """``(level, warning)``: the strictest level at or below ``commands`` that
    a standard-runtime harness can enforce on this machine (a harness may
    also refuse by raising ``ValueError``) — its ``host_commands_level``
    hook, where it declares one."""
    hook = HARNESS[backend].hooks.host_commands_level
    if hook is None:
        return commands, ""
    return resolve(hook)(commands, (host_harness or {}).get("version", ""))


def _check_model_access(account: dict | None, *, provider: str, auth: str, grant: Grant) -> None:
    """Validate the actual model host before workspace/session side effects.

    Subscription transports are delegated to external harnesses. A catalog's
    public API endpoint does not establish the subscription transport's host.
    """
    from ipaddress import ip_address
    from urllib.parse import urlsplit

    endpoint = str((account or {}).get("endpoint") or "") if auth == "api_key" else ""
    if not endpoint and auth == "api_key":
        service = (account or {}).get("service") or provider
        endpoint = STANDARD_MODEL_ENDPOINTS.get({"google": "gemini"}.get(service, service), "")
    if not endpoint:
        if grant.internet != "full":
            raise ValueError("The model endpoint host cannot be verified for this subscription/connection; use internet: full")
        return
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower().rstrip(".")
        _port = parts.port
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or parts.password is not None or any(c.isspace() for c in host)):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Invalid model endpoint; set an http(s) URL with a valid host and port and no userinfo") from exc
    local = host in {"localhost", "host.docker.internal"}
    try:
        local = local or ip_address(host).is_loopback
    except ValueError:
        pass
    if local or grant.internet == "full":
        return
    if grant.internet == "proxy":
        allowed = (entry.strip().strip(".").strip().lower() for entry in grant.internet_allow)
        if any(entry and (host == entry or host.endswith("." + entry)) for entry in allowed):
            return
    raise ValueError(f"Model endpoint {host!r} is unreachable with internet: {grant.internet}; use internet: full or internet: proxy with {host!r} in internet_allow")








# ──────────────────────────────────────────────────────────────────────
# Config parsing — one place, used by parse_agent_config.
# ──────────────────────────────────────────────────────────────────────


def to_bool(raw, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        # A BLANK string is "nobody answered", not "off" — runner settings and
        # YAML fields both arrive as "" when left empty, and the whole config
        # layer's policy is that a blank takes the default. Without this, leaving
        # ``enable_file_tools:`` empty would silently disable file tools. An explicit
        # "0"/"false"/"no"/"off" IS an answer and stays False.
        if not s:
            return default
        return s in ("1", "true", "yes", "on")
    if raw is None:
        return default
    return bool(raw)


# Per-provider default models — used when the model field is blank, and as the
# substitute when a configured model contradicts the selected backend/provider
# (see :func:`resolve_model`).
# The provider and plan model tables are the taxonomy's (shared with the
# app's Key manager, which offers a plan's default when the plan exposes no
# model list): PROVIDER_DEFAULT_MODELS, PLAN_PROVIDERS, PLAN_DEFAULT_MODELS,
# KEY_DEFAULT_MODELS.

# Provider families that exist ONLY under a plan. Three consequences, all of
# them here rather than scattered: the family is never offered in the provider
# dropdown (it is absent from every spec's ``providers``, which is what the
# dropdown is derived from), it has no key env (there is no such API key to
# ask for), and :func:`resolve_model` SKIPS its family guard — Copilot serves
# claude-*, gpt-*, gemini-* and grok-* ids side by side through one
# credential, so "the model's family must match the provider" is simply false
# for it.
_PLAN_ONLY_PROVIDERS = PLAN_ONLY_PROVIDERS

# Model-id prefixes → the provider family they belong to. Used to detect a
# model/backend mismatch — the classic trap being the model field still holding
# ``claude-sonnet-4-6`` after the backend dropdown was switched to
# antigravity (which bills gemini alone). Ids
# with an explicit provider prefix (``openai/gpt-oss-120b`` on groq) or an
# unrecognised family pass through untouched; only a KNOWN-contradicting id is
# swapped for the target's default.
_FAMILY_PREFIXES = FAMILY_PREFIXES

# Open-weights lines whose id LOOKS first-party but isn't served by that
# vendor's API: gpt-oss-* is OpenAI's open-weights family, hosted by the
# aggregators (groq / cerebras / openrouter) — claiming it for "openai"
# would make the family guard reject it on exactly the providers that
# serve it.
_OPEN_WEIGHT_PREFIXES = ("gpt-oss",)


def model_family(model: str) -> str:
    """The provider family a model id belongs to (``anthropic`` / ``gemini`` /
    ``openai``), or ``""`` when unknown, explicitly provider-prefixed, or an
    open-weights line the first-party API doesn't serve."""
    m = (model or "").strip().lower()
    if not m or "/" in m:
        return ""
    if any(m == p or m.startswith(p) for p in _OPEN_WEIGHT_PREFIXES):
        return ""
    for family, prefixes in _FAMILY_PREFIXES.items():
        if any(m == p or m.startswith(p) for p in prefixes):
            return family
    return ""


def resolve_model(backend: str, provider: str, model,
                  *, auth: str = "api_key") -> str:
    """The effective model for a backend/provider/auth selection.

    Blank model → the target's default, which under a PLAN login is the
    per-(backend, plan) entry in :data:`PLAN_DEFAULT_MODELS` when there is one
    (a subscription's catalogue is not the API's — see that table), under a
    key the per-(backend, provider) entry in :data:`KEY_DEFAULT_MODELS` when
    there is one, and the provider default otherwise. A NON-blank model whose family contradicts what
    the selection actually talks to (claude-* on the antigravity backend,
    gemini-* on provider openai, …) can't be honored as set and raises
    ``ValueError`` — it is NOT silently swapped for the default. A model of
    unknown family, or one carrying an explicit provider prefix
    (``openai/gpt-oss-120b``), passes through untouched (best effort).

    ``auth`` defaults to ``api_key`` so every existing caller keeps its
    meaning; ``parse_agent_config`` passes the parsed auth.
    """
    bk = (backend or "").strip().lower() or "pydantic-ai"
    spec = backend_spec(bk)
    prov = (provider or spec.default_provider).strip().lower()
    auth = (auth or "api_key").strip().lower()
    m = str(model or "").strip()

    # Which provider family this selection actually talks to: a single-provider
    # backend pins its family (claude-code → anthropic, antigravity →
    # gemini); the
    # provider dropdown picks for everyone else.
    if len(spec.providers) == 1 and prov not in _PLAN_ONLY_PROVIDERS:
        target = spec.providers[0]
        fallback = spec.default_model_override or PROVIDER_DEFAULT_MODELS[target]
    else:
        target, fallback = prov, PROVIDER_DEFAULT_MODELS.get(prov, "")

    # What a blank model really yields for THIS selection, computed once so
    # the family-mismatch hint below can never drift from the behaviour: a
    # plan's own default beats the provider's where the two differ (the
    # OAuth-eligible catalogue is a subset, and sometimes a disjoint set, of
    # what the API key can reach). An entry of "" is a REAL answer — "let the
    # harness resolve its own default" — so membership decides, never
    # truthiness.
    # Under a key, a harness whose own catalogue no longer carries the
    # provider's default keeps its own pick (KEY_DEFAULT_MODELS).
    if auth != "api_key":
        own = PLAN_DEFAULT_MODELS.get((bk, auth))
    else:
        own = KEY_DEFAULT_MODELS.get((bk, target))
    default = fallback if own is None else own
    if not m:
        return default
    fam = model_family(m)
    # A plan-only family serves several vendors' ids through one credential
    # (Copilot: claude-*, gpt-*, gemini-*, grok-*), so the family guard — "the
    # id must match what this provider serves" — has nothing true to say about
    # it and would reject every valid pick but one.
    if fam and fam != target and target not in _PLAN_ONLY_PROVIDERS:
        if default:
            hint = (f"set the Model field to a {target} model or leave it "
                    f"blank for the default ({default!r})")
        elif own is not None:
            # A "" entry is a real answer: the harness picks its own.
            hint = (f"set the Model field to a {target} model or leave it "
                    f"blank and {bk} picks its own default")
        else:
            hint = f"provider {prov!r} cannot serve a {fam} model"
        raise ValueError(
            f"model {m!r} is a {fam} model, but the selected "
            f"backend/provider needs a {target} one — {hint}."
        )
    return m


def is_claude_model(model: str) -> bool:
    return model_family(model) == "anthropic"


def full_claude_model_id(short: str) -> str:
    """Map a friendly alias (a Key Manager model entry may name one) to its
    full model id for the ANTHROPIC_MODEL env var the `claude` CLI reads."""
    return CLAUDE_MODEL_ALIASES.get((short or "").strip().lower(), short)




def _raise_on_config_violations(cfg: dict, fields: list) -> None:
    """Evaluate the agent ``set_yaml`` ``fields`` against ``cfg`` via
    ``config_values`` and raise when the config can't be honored as set — a
    visible required field is empty, or a chosen dropdown option is disabled
    by its ``enable_if`` (auth × engine and the like)."""
    from collimate.config_values import resolve_fields, validate_config

    report = validate_config(fields, resolve_fields(fields, cfg))
    invalid = report["invalid_options"]
    missing = report["missing_required"]
    if not invalid and not missing:
        return
    parts = [
        f"{v.get('label') or v.get('key')}={v.get('value')!r} isn't available "
        "with the current selection"
        for v in invalid
    ]
    parts += [f"{m.get('label') or m.get('key')} is required" for m in missing]
    raise ValueError("agent config can't be honored as set: " + "; ".join(parts))


def parse_agent_config(
    cfg: dict, *, fields: "list | None" = None,
    grant: "Grant | None" = None, can_ask: "bool | None" = None,
) -> dict:
    """Normalise an ``agent-config.set_yaml`` mapping into the kwargs the agent
    machinery expects. The single source of truth for defaults + coercion.

    ``cfg["surface"]`` is the run's surface — ``annotation`` (the default),
    ``chat`` or ``terminal`` — and decides which keys the run reads
    (:data:`_SURFACE_KEYS`); a key of another surface is ignored. ``grant``
    controls runtime, network and
    commands. ``can_ask`` records whether anyone can answer an approval
    card. ``None`` for either reads this run's own (``gen.grant()`` /
    ``gen.can_ask()``).

    Validation policy: a **blank/absent** field takes a sensible default; a field
    **explicitly set to a value that can't be honored as set** is a hard
    ``ValueError``, never a silent reinterpretation. Two kinds:

    * Per-field / matrix (always enforced, from the ``_BACKENDS`` spec table):
      a key no surface reads, an unknown ``surface`` / ``session`` / ``backend`` /
      ``auth`` / ``provider`` (outside its dropdown's options), an
      auth/provider the selected backend can't serve, a terminal on a
      backend with no TTY, and a ``model`` whose family contradicts the
      selection (see :func:`resolve_model`).
    * **Cross-field**: encoded in the ``agent-config.set_yaml`` itself as
      ``show_if`` / option ``enable_if`` conditions and enforced by evaluating
      THAT schema through ``collimate.config_values`` — the same evaluator the
      UI and the server gate use, so there is one source of truth and no
      re-encoding. The Agent generator passes its parsed ``fields``;
      trusted callers pass a constructed config
      without ``fields`` and are validated by the matrix + coercion +
      :func:`resolve_model` alone (they legitimately carry non-UI overrides).

    A grant the selected backend cannot serve refuses at start with an
    actionable fix. The returned ``"warnings"`` list carries informational
    backend limitations and the selected Claude CLI.
    """
    cfg = cfg or {}
    warnings: list = []
    unknown = sorted(str(k) for k in cfg if k not in _KNOWN_KEYS)
    if unknown:
        raise ValueError(
            f"unknown config key{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(k) for k in unknown)} — expected keys of "
            f"{', '.join(sorted(k for k in _KNOWN_KEYS if not k.startswith('_')))}"
        )
    surface = str(cfg.get("surface") or "").strip().lower() or "annotation"
    if surface not in _SURFACES:
        raise ValueError(
            f"unknown surface {surface!r} — expected one of "
            f"{', '.join(_SURFACES)}"
        )
    # The schema validates the mapping as saved (hidden fields contribute
    # nothing); every rule below reads only the keys this surface reads.
    schema_cfg = cfg
    reads = _CONFIG_KEYS | _SURFACE_KEYS[surface]
    cfg = {k: v for k, v in cfg.items() if k in reads}
    # Every dropdown: blank/whitespace → the field default; anything else must
    # be a known option, else the config can't be honored as set → hard error.
    backend = (cfg.get("backend") or "").strip().lower() or "pydantic-ai"
    spec = backend_spec(backend)   # unknown/removed backend → hard error
    # An interactive session is the default; a one-shot is always an
    # annotation run. The terminal needs a TTY the backend actually has (spec.uis) —
    # the schema disables pydantic-ai there — and a grant that can place it
    # where its terminal locus puts it (placement, below).
    session = (cfg.get("session") or "").strip().lower() or "interactive"
    if session not in _SESSIONS:
        raise ValueError(
            f"unknown session {session!r} — expected one of "
            f"{', '.join(_SESSIONS)}"
        )
    if surface == "terminal" and "terminal" not in spec.uis:
        raise ValueError(
            f"backend {backend!r} has no TTY; the terminal surface runs a "
            "harness's own TTY"
        )
    auth = (cfg.get("auth") or "").strip().lower() or "api_key"
    if auth not in _AUTHS:
        raise ValueError(
            f"unknown auth {auth!r} — expected one of {', '.join(_AUTHS)}"
        )
    if auth not in spec.auths:
        raise ValueError(
            f"auth {auth!r} isn't honored by backend {backend!r} — "
            f"it offers {', '.join(spec.auths)}"
        )
    # Provider: blank → the backend's default. An explicit provider must be a
    # known key AND one the backend can bill — codex speaks only the
    # Responses-wire providers. Single-provider backends (claude-code /
    # antigravity) pin their family and IGNORE the dropdown, so any known value passes through there
    # (the schema hides the field for them). EVERY PLAN pins its provider
    # (PLAN_PROVIDERS): a subscription is a credential for exactly one
    # vendor, so the dropdown has nothing to decide — the schema hides it
    # under any plan, and a residual value from an earlier api_key selection
    # is pinned, not rejected (the evaluator's hidden-fields-contribute-
    # nothing semantics, engine-side).
    provider = (cfg.get("provider") or "").strip().lower()
    pinned = PLAN_PROVIDERS.get(auth) if auth != "api_key" else None
    if pinned:
        provider = pinned
    provider = provider or spec.default_provider
    if provider not in PROVIDER_DEFAULT_MODELS:
        raise ValueError(
            f"unknown provider {provider!r} — expected one of "
            f"{', '.join(PROVIDER_DEFAULT_MODELS)}"
        )
    # The "can this backend bill that provider" check judges the DROPDOWN, so
    # it applies to key auth alone: a pinned plan provider is not a choice the
    # config made, and two of them (copilot, and xai on opencode/pi) are
    # deliberately outside the backend's key-billable set — those families are
    # reachable only through the subscription, which is exactly the point.
    if not cfg.get("_account") and not pinned and len(spec.providers) > 1 and provider not in spec.providers:
        raise ValueError(
            f"provider {provider!r} isn't supported by backend {backend!r} — "
            f"it offers {', '.join(spec.providers)}"
        )
    # Blank model → the target's default (per-plan where the plan's catalogue
    # differs); a contradicting explicit model raises.
    if cfg.get("_account"):
        if provider != cfg["provider"]:
            raise ValueError("The selected account service does not match its login plan")
        api_wire = (cfg["_account"].get("protocol") or "") in {"openai", "openai-compatible", "openai-chat", "openai-responses"}
        if not pinned and provider not in spec.providers and not (backend == "pydantic-ai" and api_wire):
            raise ValueError(f"Account provider {provider!r} is not supported by {backend!r}")
        # A model-list choice is authoritative, including custom endpoint IDs.
        # Family-name heuristics must not replace or reject it. A blank model
        # is the account's default, by the rule Key Manager shows it with
        # (account_taxonomy.default_model_for), for the backend this run
        # resolved (a runner's settings pick it for an account that names no
        # harness): "" where the harness keeps its own.
        account = cfg["_account"]
        metadata = account.get("metadata") or {}
        if isinstance(metadata, dict):
            account = {**account, "metadata": {**metadata, "harness": backend}}
        model = str(cfg.get("model") or "") or (default_model_for(account) or "")
    else:
        model = resolve_model(backend, provider, cfg.get("model"), auth=auth)
    # How the run answers the harness's permission requests: "auto" answers
    # from the grant (allow what it already permits, refuse the rest);
    # "ask" surfaces each grant-permitted request as a card the human answers
    # over the surface WebSocket, bounded by a timeout that refuses
    # (shared/docs/agent-runtime.md, grants and tools — the grant gate runs first either way).
    approvals = (cfg.get("approvals") or "").strip().lower() or "auto"
    if approvals not in ("auto", "ask"):
        raise ValueError(
            f"unknown approvals {approvals!r} — expected auto / ask"
        )
    if grant is None:
        grant = gen.grant()
    if can_ask is None:
        can_ask = gen.can_ask()
    containers_ok = grant.at_least("docker", "full")
    if not can_ask and (approvals == "ask" or grant.commands == "ask"):
        requested = "approvals: ask" if approvals == "ask" else "commands: ask"
        raise ValueError(f"{requested} needs an answerable live surface; use approvals: auto and commands: deny, or run with a live surface")
    # The grant chooses where the harness runs; no fallback may silently
    # weaken it. The host already raised an Annotation run's grant to its
    # harness's minimum (harness_placement.adopt), so the grant read here is
    # the effective one; a grant that still cannot place the harness refuses.
    placed = placement(backend, grant, surface, auth=auth)
    if not placed.ok:
        raise ValueError(placed.refusal)
    _check_model_access(cfg.get("_account"), provider=provider, auth=auth, grant=grant)
    warnings.extend(placed.notes)
    host_harness = placed.host
    runtime = "standard" if host_harness else "docker"
    # ── Commands — the grant's `commands` axis (deny < ask < allow) ───────
    # Whether the agent may run shell commands IT chooses. The host has no NAK
    # point for these (they run inside the harness), so the engine enforces
    # the level on every path — the ACP client gate and each harness's native
    # settings — and the manifest declares it (`enforces: [commands]`). `ask` needs someone to answer the
    # card; an unanswerable ask already refused above at startup.
    commands, note = effective_commands(grant, can_ask)
    if note:
        warnings.append(note)
    if runtime == "standard":
        commands, note = _host_commands_level(backend, commands, host_harness)
        if note:
            warnings.append(note)
    note_hook = HARNESS[backend].hooks.commands_note
    note = resolve(note_hook)(commands, runtime) if note_hook else ""
    if note:
        warnings.append(note)

    # ── Cell-carried capabilities (_skills/ + _mcp/) ──────────────────────
    # Skills are inert runner-provided instructions, delivered wherever the
    # harness can receive them (spec.cell_skills, read by run_agent). `mcp`
    # is per-run consent, default OFF — cell files arming network/process
    # capabilities is opt-in.
    mcp = (cfg.get("mcp") or "").strip().lower() or "off"
    if mcp not in ("off", "http", "http_stdio"):
        raise ValueError(
            f"unknown mcp {mcp!r} — expected off / http / http_stdio"
        )
    if mcp != "off":
        if not spec.cell_mcp:
            raise ValueError(
                f"mcp isn't available on backend {backend!r} — it has no "
                "MCP client to attach the cell's servers to"
            )
        # Only a containerized harness's OWN container hosts a command
        # server: a host-locus harness would spawn it on this machine.
        if mcp == "http_stdio" and (not containers_ok or not spec.containerized or runtime != "docker"):
            raise ValueError(
                "mcp 'http_stdio' runs command servers, which need a "
                "docker: full and a containerized harness's own container — they never run on "
                f"the host; use mcp 'http' on backend {backend!r}"
            )

    # ── Cross-field validity (single source: the agent-config.set_yaml) ────
    # The remaining cross-field rules (auth × backend option gating and the
    # like) are encoded as set_yaml conditions; evaluate that schema (when the
    # generator supplies it) so the engine, the UI, and the server gate
    # share one source of truth.
    if fields:
        _raise_on_config_violations(schema_cfg, fields)

    out = {
        "session": session,
        "surface": surface,
        "runtime": runtime,
        "host_harness": host_harness,
        "backend": backend,
        "_account": cfg.get("_account"),
        "auth": auth,
        "provider": provider,
        "model": model,
        # The per-run consent for runner-provided MCP servers (off / http /
        # http_stdio).
        "mcp": mcp,
        "system_prompt": (cfg.get("system_prompt") or "").strip(),
        # File access is the norm — every agent works on the cell's files
        # and live-syncs them; a runner's saved settings may turn it off for
        # a text-only agent.
        "enable_file_tools": to_bool(cfg.get("enable_file_tools"), True),
        # The effective commands level (deny | ask | allow) every path
        # enforces; a startup ask always has an answerable surface.
        "commands": commands,
        "websearch": grant.internet != "none",
        # Whether the file selected when the run started is part of its
        # first turn (``gen.selected_file``; the host passes one only when
        # this is on).
        "selected_file_first": to_bool(cfg.get("selected_file_first"), False),
        # Permission-request policy (auto | ask — §2.1).
        "approvals": approvals,
        "warnings": warnings,
    }
    return out
