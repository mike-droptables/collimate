# SPDX-License-Identifier: MIT
"""The Agent's harnesses: one :class:`HarnessAdapter` declaration each.

Every harness lives in one folder, ``harnesses/<package>/``:

* ``adapter.py`` declares ``ADAPTER``, the harness's every static fact —
  where it runs, what it installs, which providers and plans it bills and
  under which native ids, how it takes its instructions, enforces the
  commands level, reports its model and keeps its sessions, its terminal and
  its named quirks (:class:`Hooks`);
* ``backend.py`` holds its renderers: the native files, environment and argv
  the generic preparation (:mod:`.pipeline`) asks it for;
* ``image/`` is its container image build context (Dockerfile and assets),
  for a harness that runs in a container.

The registry (:data:`HARNESS`) is assembled from those declarations. SDK core
never imports this runtime: ``sdk/scripts/sync_harness_catalog.py`` projects the
metadata core needs (account rules, placement facts, plan logins) into
``collimate/harness_catalog.py``, and the Agent's settings schema options
from the same declarations, and a test pins both byte for byte.

Callable facets are lazy ``"module:function"`` strings resolved against the
``_agent_shared`` package (:func:`resolve`), so a declaration imports nothing
heavier than this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from importlib import import_module
from typing import Callable, Mapping

_ROOT = __name__.rsplit(".", 1)[0]

SURFACES = ("annotation", "chat", "terminal")
COMMAND_LEVELS = ("deny", "ask", "allow")


@dataclass(frozen=True)
class Native:
    """A provider as the harness names it: its own provider id, and the
    environment name it reads that provider's key from when that differs from
    :data:`collimate.providers.PROVIDER_KEY_ENV`."""
    id: str
    key_env: str = ""


def natives(*entries: "str | tuple[str, str | Native]") -> dict:
    """``{provider: Native}`` in roster order: a bare id the harness names
    as Collimate does, or ``(provider, native id | Native)`` for a renamed
    one."""
    out = {}
    for entry in entries:
        pid, value = (entry, entry) if isinstance(entry, str) else entry
        out[pid] = value if isinstance(value, Native) else Native(value)
    return out


@dataclass(frozen=True)
class Login:
    """How the harness consumes a plan login the host leases to the run.

    ``command`` is what signs in on a machine, ``relative`` where that writes
    the document under HOME (or under ``env`` + ``suffix``), ``also`` other
    places an install keeps it. ``field`` is the account secret the pasted
    document is saved as: ``credential_json`` (a JSON store), or
    ``credential_file`` / ``credential_token`` (opaque bytes or a token).
    ``lease`` is its path inside the run's lease. ``store_keys`` is, for one
    store holding several providers' logins, the entry each plan occupies.
    ``validator`` names the document check in
    :data:`collimate.account_taxonomy.LOGIN_VALIDATORS`. ``copy`` marks a
    harness that rewrites its vendor state in a way a bind cannot carry: the
    run copies the login in and returns the refreshed copy at release.
    ``hint`` is the Key manager's sentence under the document field. A plan
    whose CLI surface runs on this machine (:func:`terminal_locus`) signs in
    there through the vendor's own CLI, so its entry is complete without the
    document; only the surfaces that read it refuse without it."""
    command: str
    relative: str
    field: str
    lease: tuple[str, ...]
    validator: str
    env: str = ""
    suffix: str = ""
    also: tuple[str, ...] = ()
    store_keys: Mapping[str, str] = field(default_factory=dict)
    copy: bool = False
    hint: str = "The whole contents of {store}, copied from the opening {{ to the closing }}."


@dataclass(frozen=True)
class Auth:
    """What the harness bills. ``providers`` are the key-billed providers in
    the harness's own ids; ``plans`` the subscription plans it signs in to,
    with each plan's native provider id; ``login`` the plan login document
    (``None`` for a plan carried as a pasted token in the environment, or no
    plan)."""
    providers: Mapping[str, Native]
    default_provider: str = "anthropic"
    plans: Mapping[str, str] = field(default_factory=dict)
    login: "Login | None" = None
    # Whether a custom (non-standard) endpoint can be billed (pydantic-ai,
    # whose model connection Collimate builds).
    custom_endpoint: bool = False
    # A plan a new login on that plan starts on in the Key manager.
    plan_default_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class Model:
    """How the harness takes and reports its model."""
    # The harness's own pick for a blank model ("" = the provider default).
    default: str = ""
    # The harness's own model names, where it refuses the providers' ids.
    vocabulary: tuple[str, ...] = ()
    # What a model id the service cannot list does at run time.
    unchecked: str = ""
    # Per plan / per provider defaults where the harness's catalogue differs.
    plan_defaults: Mapping[str, str] = field(default_factory=dict)
    key_defaults: Mapping[str, str] = field(default_factory=dict)
    # ``(reported option value, target) -> bool`` for a harness reporting the
    # model in its own vocabulary.
    matches: "str | None" = None
    # The harness can silently run another model than the one set: check the
    # model in effect on a new session too.
    verify: bool = False


@dataclass(frozen=True)
class Instructions:
    """The seam the session's system prompt rides. ``home_file`` writes
    ``target`` (relative to the per-cell harness home) — the pipeline does it
    — ``meta`` an ACP ``_meta`` option, ``container_file`` / ``config_list``
    a Collimate-owned file in the container, ``fold`` none (the prompt is
    folded ahead of a new session's first prompt)."""
    seam: str
    target: str = ""


@dataclass(frozen=True)
class Tools:
    """What reaches the harness's tools. ``collimate`` is how the bridge's
    tools arrive (``acp`` mcpServers, or ``native`` — the harness's own MCP
    config); ``cell_mcp`` whether the runner's declared MCP servers can be
    attached; ``web`` how web tools are withheld when the grant has no
    internet (``native_off``, ``refusal`` — a permission refusal — or
    ``none``: the harness has none)."""
    collimate: str = "acp"
    cell_mcp: bool = True
    cell_skills: bool = True
    web: str = "native_off"


@dataclass(frozen=True)
class Commands:
    """How the grant's ``commands`` level is enforced. ``levels`` are the
    levels the native mechanism can hold; a level it cannot is resolved down
    to the strictest one below it (with a notice), never up. ``mode_rule``
    is ``(commands, mode value) -> bool``; ``recognisers`` find a shell
    request that does not arrive as kind ``execute``."""
    mechanism: str
    levels: tuple[str, ...] = COMMAND_LEVELS
    mode_rule: str = "acp_policy:_no_modes"
    recognisers: tuple[str, ...] = ()
    persisting_options: frozenset = frozenset()
    mode_switch_options: Mapping[str, str] = field(default_factory=dict)
    # Refuse a permission request of a kind the host gate does not know.
    strict_kinds: bool = False
    # Under docker: full the harness's shell runs in its Docker sandbox
    # (recommended for allow); otherwise on this machine (pydantic-ai).
    sandboxed: bool = False


@dataclass(frozen=True)
class Session:
    """The harness's own session store and its quirks."""
    # Paths in the per-cell harness home that hold sessions.
    paths: tuple[str, ...] = ()
    # File-name patterns under those paths that store "always allow"
    # answers: never carried to a later run, where they would approve what
    # its grant says to ask about.
    standing_grants: tuple[str, ...] = ()
    # The adapter builds one session per process.
    one_per_process: bool = False
    # ``async (session) -> str`` after a session/load ("" = keep it).
    verify_loaded: "str | None" = None
    # A session mode the commands level rests on must hold.
    mode_required: bool = False


#: Where a CLI surface runs: the harness's container, or this machine.
TERMINAL_LOCI = ("container", "host")


@dataclass(frozen=True)
class Terminal:
    """The CLI surface: the harness's own TTY. ``tier`` is how the TTY's
    session relates to the engine's: ``A`` the TTY runs on a session the
    engine minted, ``B`` on one it names itself that the leg finds, ``C`` a
    fresh conversation the transcript cannot read.

    ``locus`` is where the TTY runs, independent of where the harness's
    turns run (:attr:`HarnessAdapter.locus`): ``container`` (the harness's
    own container, under ``docker: full``), ``host`` (the unmodified vendor
    CLI on this machine, under its own sign-in held in a per-account home —
    ``cli_host``), or a ``"module:function"`` target, ``(auth) -> locus``,
    resolved per request for a harness whose locus depends on the selected
    account's auth (:func:`terminal_locus`)."""
    tier: str
    native_context: str
    bridge_enabled: bool
    default_label: str
    tail: "str | None" = None
    harness_session_ids: str = ""
    lifetime_context: "str | None" = None
    prepare_with_request: bool = False
    locus: str = "container"


@dataclass(frozen=True)
class Hooks:
    """Named, harness-scoped quirks the shared modules call through."""
    # ``(normalized update) -> normalized update``: drop adapter noise.
    normalize_update: "str | None" = None
    # ``(session, request _meta) -> bool``: a permission request that runs a
    # tool, not a command (the commands level does not decide it).
    tool_approval: "str | None" = None
    # ``(text, *, runtime, cli_path) -> str | None``: explain a failure.
    explain_failure: "str | None" = None
    # ``(harness, grant level, version) -> (level, notice)`` for a host twin.
    host_commands_level: "str | None" = None
    # ``(commands) -> str``: a warning shown under a level.
    commands_note: "str | None" = None


@dataclass(frozen=True)
class HarnessAdapter:
    """One harness, declared once."""
    id: str
    label: str
    # Where its turn runs: ``host`` (an adapter process on this machine) or
    # ``container`` (its own container).
    locus: str
    # The ACP adapter invocation (the installed binary, never npx).
    acp_argv: tuple[str, ...]
    # The CLI a machine runs it as ("" for none).
    host_binary: str
    # A ``container`` harness whose renderers also serve ``runtime:
    # standard`` (the machine's own CLI, under docker: none).
    host_twin: bool
    auth: Auth
    model: Model
    instructions: Instructions
    tools: Tools
    commands: Commands
    session: Session
    terminal: "Terminal | None"
    hooks: Hooks
    # The renderer the pipeline drives: ``async (ctx, *, leg, **kw) ->
    # PreparedHarness``.
    prepare: str
    # Image build context relative to this package ("" without one), its
    # artifacts ``{name: {source, arg}}``, the ACP adapter artifact (an
    # :data:`adapter_artifacts.ADAPTERS` key) and the ACP registry id.
    image: str = ""
    artifacts: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    adapter_artifact: str = ""
    registry_id: str = ""
    # Why a native ACP harness has no ACP registry row ("" for one that has
    # one, and for Collimate's own shim).
    unregistered: str = ""
    # The preset description, for the Agent's configs/<id> preset.
    summary: str = ""
    # A caveat the settings schema shows under the harness's option.
    note: str = ""
    # The preset's grant: the harness's own minimum for the Annotation
    # surface at commands deny, unless the preset grants more on purpose.
    preset_permissions: Mapping[str, str] = field(default_factory=lambda: {
        "docker": "full", "internet": "full", "commands": "allow"})

    @property
    def surfaces(self) -> tuple[str, ...]:
        return SURFACES if self.terminal else SURFACES[:2]

    @property
    def containerized(self) -> bool:
        return self.locus == "container"

    @property
    def auths(self) -> tuple[str, ...]:
        return (("api_key",) if self.auth.providers else ()) + tuple(self.auth.plans)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self.auth.providers)

    def native_provider(self, provider: str, plan: str = "") -> Native:
        """The harness's own id for ``provider`` (or ``plan``'s provider)."""
        if plan and plan in self.auth.plans:
            return Native(self.auth.plans[plan])
        native = self.auth.providers.get(provider)
        if native is None:
            raise ValueError(f"{self.id} cannot bill provider {provider!r} — it offers "
                             f"{', '.join(self.providers)}")
        return native

    def resolve_commands(self, level: str) -> tuple[str, str]:
        """``(level, notice)``: the strictest level at or below ``level`` the
        native mechanism holds."""
        if level in self.commands.levels:
            return level, ""
        ladder = COMMAND_LEVELS[:COMMAND_LEVELS.index(level) + 1]
        held = next(lv for lv in reversed(ladder) if lv in self.commands.levels)
        return held, (f"{self.label} cannot enforce commands: {level} "
                      f"({self.commands.mechanism}); commands run at {held}")

    @cached_property
    def spec(self) -> "BackendSpec":
        return _spec(self)


@dataclass(frozen=True)
class BackendSpec:
    """The engine's flat view of one adapter (what config parsing, dispatch
    and the gates read)."""
    acp_locus: str
    acp_argv: tuple
    acp_adapter_artifact: str
    acp_registry_id: str
    terminal_session_tier: str
    uis: tuple
    providers: tuple
    default_provider: str
    auths: tuple
    default_model_override: str
    containerized: bool
    websearch: bool
    cell_skills: bool
    cell_mcp: bool
    harness_session_ids: str


def _spec(row: HarnessAdapter) -> BackendSpec:
    return BackendSpec(
        acp_locus=row.locus, acp_argv=row.acp_argv,
        acp_adapter_artifact=row.adapter_artifact, acp_registry_id=row.registry_id,
        terminal_session_tier=row.terminal.tier if row.terminal else "none",
        uis=("assistant-ui", "terminal") if row.terminal else ("assistant-ui",),
        providers=row.providers, default_provider=row.auth.default_provider,
        auths=row.auths, default_model_override=row.model.default,
        containerized=row.containerized,
        websearch=row.tools.web != "none", cell_skills=row.tools.cell_skills,
        cell_mcp=row.tools.cell_mcp,
        harness_session_ids=row.terminal.harness_session_ids if row.terminal else "",
    )


@dataclass(frozen=True)
class CommandsPolicy:
    level: str
    native: dict
    notice: str = ""


@dataclass(frozen=True)
class PreparedHarness:
    commands: CommandsPolicy
    profile: object | None
    env_obj: object | None
    argv: tuple[str, ...]
    env: dict
    model: str


@dataclass(frozen=True)
class Image:
    """A harness's container image: its build context (relative to
    ``_agent_shared``), its tag prefix and its artifacts."""
    folder: str
    kind: str
    name_prefix: str
    artifacts: Mapping[str, Mapping[str, str]]


def image(row: HarnessAdapter) -> "Image | None":
    if not row.image:
        return None
    return Image(f"harnesses/{_package(row.id)}/{row.image}", row.id,
                 f"collimate-{row.id}", row.artifacts)


def resolve(target: str) -> Callable:
    """The callable a ``"module:function"`` facet names, the module relative
    to ``_agent_shared``."""
    module, name = target.split(":", 1)
    owner = import_module(_ROOT if module == "__init__" else f"{_ROOT}.{module}")
    return getattr(owner, name)


def terminal_locus(row: "HarnessAdapter", auth: str) -> str:
    """Where ``row``'s CLI surface runs for an account billed under ``auth``
    (``api_key`` or a plan): ``host`` or ``container``."""
    if row.terminal is None:
        raise ValueError(f"harness {row.id!r} has no CLI surface")
    locus = row.terminal.locus
    if locus not in TERMINAL_LOCI:
        locus = resolve(locus)(auth)
    if locus not in TERMINAL_LOCI:
        raise ValueError(f"harness {row.id!r}: terminal locus {locus!r} for {auth!r} is not "
                         f"one of {', '.join(TERMINAL_LOCI)}")
    return locus


def _package(harness_id: str) -> str:
    return harness_id.replace("-", "_")


# The roster, in the order every list shows it (the built-in harness first).
ROSTER = ("pydantic-ai", "claude-code", "antigravity", "codex", "goose", "opencode",
          "qwen-code", "cline", "pi", "hermes", "vibe", "copilot")


def _load() -> dict:
    rows = {}
    for harness_id in ROSTER:
        adapter = import_module(f"{__name__}.{_package(harness_id)}.adapter").ADAPTER
        rows[adapter.id] = adapter
    return rows


def validate_registry(rows: Mapping[str, HarnessAdapter]) -> None:
    for name, row in rows.items():
        if row.id != name:
            raise ValueError(f"harness key {name!r} disagrees with its id")
        if row.locus not in ("host", "container"):
            raise ValueError(f"harness {name}: unknown locus {row.locus!r}")
        if not row.acp_argv:
            raise ValueError(f"harness {name}: an ACP harness needs its adapter argv")
        if row.containerized and not row.image:
            raise ValueError(f"harness {name}: a container harness needs its image")
        if row.host_twin and not (row.containerized and row.host_binary):
            raise ValueError(f"harness {name}: a host twin is a container harness with a host CLI")
        if not (row.auth.providers or row.auth.plans):
            raise ValueError(f"harness {name}: bills nothing")
        if row.auth.providers and row.auth.default_provider not in row.auth.providers:
            raise ValueError(f"harness {name}: default provider is not billed")
        if bool(row.auth.login) and not row.auth.plans:
            raise ValueError(f"harness {name}: a login document needs a plan")
        if row.terminal and row.terminal.tier not in ("A", "B", "C"):
            raise ValueError(f"harness {name}: terminal tier must be A, B or C")
        if row.terminal and row.terminal.tier == "C" and row.terminal.tail:
            raise ValueError(f"harness {name}: a tier-C terminal has no tail")
        if row.terminal and row.terminal.locus not in TERMINAL_LOCI and ":" not in row.terminal.locus:
            raise ValueError(f"harness {name}: terminal locus must be host, container or a target")
        if row.terminal and not row.host_binary and any(
                terminal_locus(row, auth) == "host" for auth in row.auths):
            raise ValueError(f"harness {name}: a terminal on this machine needs the harness's CLI")
        if not set(row.commands.levels) <= set(COMMAND_LEVELS) or "deny" not in row.commands.levels:
            raise ValueError(f"harness {name}: commands levels must include deny")
        if row.instructions.seam not in ("meta", "home_file", "container_file", "config_list", "fold"):
            raise ValueError(f"harness {name}: unknown instruction seam")


HARNESS: dict[str, HarnessAdapter] = _load()
validate_registry(HARNESS)
BACKENDS: dict[str, BackendSpec] = {name: row.spec for name, row in HARNESS.items()}
