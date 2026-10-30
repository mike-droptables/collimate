"""Core linting logic for Collimate manifests.

Kept dependency-light: only the stdlib + PyYAML (already a transitive dep
via the SDK). No pydantic models — the manifest surface is small enough that
a handful of typed checks per field are clearer than a big schema.
"""

from __future__ import annotations

from types import SimpleNamespace

import enum
import functools
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover — PyYAML is always installed
    raise ImportError(
        "collimate-lint requires PyYAML. Install it with `pip install pyyaml`."
    ) from e

# Shared per-field-type "empty value" semantics (stdlib-only module;
# pinned cross-language by shared/empty-values-contract.json). The same
# module owns the canonical show_if/required_if/enable_if evaluator, which
# the condition lint below imports so lint and runtime agree on coercion.
from collimate import permissions
from collimate.definitions import discover_definitions
from collimate.lint.conditions import check_set_yaml_conditions, walk_condition_terms


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class Level(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Issue:
    """A single lint finding."""

    level: Level
    code: str
    message: str
    path: Path
    field: str | None = None

    def format(self, level: str | None = None) -> str:
        loc = f"{self.path}" + (f":{self.field}" if self.field else "")
        prefix = level if level is not None else self.level.value
        return f"{prefix}: {self.code}: {loc}: {self.message}"


# ---------------------------------------------------------------------------
# Shared schemas
# ---------------------------------------------------------------------------

_GEN_REQUIRED = ("id", "name", "entrypoint", "summary", "main_config")
GENERATOR_FIELDS = {
    "id", "name", "version", "summary", "entrypoint",
    "chat", "daemon", "prompt_intent",
    "draft_files", "main_config", "configs", "required_files", "account_requirements",
    # Host-side subprocess controls (process_manager/manager.py): rlimits
    # caps committed memory (RLIMIT_DATA), CPU time and open files for
    # native generators; env_passthrough lists env vars that survive the
    # per-subprocess scrub.
    "rlimits", "env_passthrough",
    # ``timeout_seconds``: the per-run watchdog budget in seconds, read by the
    # host process_manager and capped by the host's wall-clock ceiling. Time
    # awaiting input or approval is not charged, and a chat run's budget
    # bounds one turn.
    "timeout_seconds",
    # ``optional_dependencies`` maps a config selection to the extra python
    # requirements that selection needs, so the host can pass them to uv at
    # spawn. Keeps the "which library does backend X need" knowledge in the
    # generator that owns it rather than in the host.
    "optional_dependencies",
    # ``live_files_mode`` opts in to mid-run editing of the cell's files.
    # Read by the host at spawn (api/process_manager/manager.py drives the SDK
    # file sync) and surfaced in the generator catalog (api/routes/generation.py
    # -> frontend GeneratorCard).
    "live_files_mode",
    # ``requires_docker`` (bool, default false): EVERY run of this generator
    # provisions a container, so the runner catalog (api/runners.py) and runner
    # lint reject a runner targeting it without docker: full instead of
    # letting the run fail later. ``requires_docker_if`` is the conditional
    # form: a set_yaml condition over the main config's dropdown and checkbox
    # values (the agent's terminal surface). A need that depends on a value a
    # condition cannot name (an agent backend) is enforced at run time instead.
    "requires_docker", "requires_docker_if",
    # ``adopts_harness_grant`` (bool, default false): an Annotation run of this
    # generator takes the least grant its bound harness needs
    # (:func:`collimate.harness_placement.adopt`) — the host raises the run's
    # docker axis at start and records it. Only a generator that declares it
    # adopts; every other runner's grant is exactly as saved.
    "adopts_harness_grant",
    # ``takes_selected_file`` (bool) / ``takes_selected_file_if`` (condition):
    # a run receives the path of the file the user has selected in the cell
    # (``gen.selected_file``); the host refuses a selected file for a runner
    # whose generator does not take one.
    "takes_selected_file", "takes_selected_file_if",
    # ``opens`` lists the files a run serves as its surfaces, ``[{path, if?}]``:
    # the host creates each one as a starting placeholder when it accepts the
    # run and reveals the first, so the surface shows before the process
    # spawns. ``path`` is a template over ``{run}`` (the run folder) and
    # ``{<main-config key>}``; see _check_opens and collimate.surfaces.
    "opens",
    # ``prompt_required`` gates running on a non-empty prompt; only
    # legal alongside ``prompt_intent``. ``prompt_show_if`` /
    # ``prompt_required_if`` are the conditional forms — set_yaml conditions
    # over the MAIN CONFIG's values, so a generator whose prompt only makes
    # sense under one selection (the agent generator's one-shot annotation
    # session) shows and requires it exactly there.
    "prompt_required", "prompt_show_if", "prompt_required_if",
    # ``config_folders`` declares the named config-folder slots a runner may
    # fill for this generator (skills/mcp/scripts/build context …). Each
    # entry is a mapping ``{name, description, sample?}`` — see
    # _check_config_folders. A run starts from a saved runner; the generator declares
    # the config folders that runner can fill.
    "config_folders",
    # ``runs_on`` declares the generator's execution-locus assumption.
    # Default (omitted / "anywhere"): the generator's world is its
    # workspace + the gen API and it runs identically under any execution
    # backend. "local-machine": the generator legitimately assumes the
    # user's own machine (reads ~/.ssh, talks to a localhost service,
    # touches paths outside the workspace) — the host refuses to place
    # such a run anywhere that assumption breaks (containerized or remote
    # backends). Declare the assumption instead of embodying it silently.
    "runs_on",
    # ``enforces`` lists the grant axes this generator enforces itself — the
    # axes with no host enforcement point (permissions.GENERATOR_AXES). A
    # runner's level on such an axis means something only for a generator
    # that declares it; see _check_runner_generator_axes.
    "enforces",
    # ``ui_files`` lists the filename suffixes of the generator's UI files
    # (``[".chat", ".json"]``) — the only files it may write into its run
    # folder (``gen.update_ui_file``). The host passes the list as
    # COLLIMATE_UI_FILES and refuses any other own-folder file; see
    # _check_ui_files.
    "ui_files",
}
GENERATOR_RUNS_ON = {"anywhere", "local-machine"}
_GEN_RLIMIT_KEYS = {"memory_bytes", "cpu_seconds", "open_files"}
_SECRET_NAME_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)

# Slug id convention shared by generator, renderer, and runner ``id`` fields
# (lowercase, digits, hyphens; the app treats it as a stable handle).
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# ``config_folders`` entry name — the folder slot a runner fills. Allows a
# leading underscore (``_skills``/``_mcp``) plus dots/hyphens; the reserved
# names below are rejected before the pattern is even consulted.
_CONFIG_FOLDER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_RESERVED_FOLDER_NAMES = (".collrun", ".")
CONFIG_FOLDER_FIELDS = {"name", "description", "sample"}
ACCOUNT_KINDS = frozenset({"model", "git", "account", "custom"})
ACCOUNT_REQUIREMENT_LIST_FIELDS = frozenset({"capabilities", "services", "protocols", "auth_types", "harnesses", "custom_types", "kinds"})
ACCOUNT_REQUIREMENT_SCALAR_FIELDS = frozenset({"label", "service", "protocol", "custom_type", "harness", "auth_type"})
ACCOUNT_REQUIREMENT_FIELDS = frozenset({"kind", "required"}) | ACCOUNT_REQUIREMENT_LIST_FIELDS | ACCOUNT_REQUIREMENT_SCALAR_FIELDS
ACCOUNT_PROPERTY_ROOTS = frozenset({"id", "name", "kind", "service", "protocol", "endpoint", "model", "auth_type", "capabilities", "metadata"})
ACCOUNT_PROPERTY_FORBIDDEN = frozenset({"secrets", "__proto__", "prototype", "constructor"})

_REND_REQUIRED = ("id", "name", "entrypoint", "summary")
RENDERER_FIELDS = {
    "id", "name", "version", "summary", "entrypoint",
    "embed", "extensions", "default_for", "supports_diff", "live_refresh",
}
# The ``default_for`` entry for every text file: the renderer is offered under
# "Open with" for any text file, whether or not it claims the extension.
_ANY_TEXT = "*"
_REND_EMBED_MODES = {"native", "iframe"}

# ---------------------------------------------------------------------------
# Runner manifest (custom.runner) schema — the run *interface* layer.
# A runner lives in a Capabilities cell as
# ``<runner-folder>/<name>.runner`` plus its supplemental files under
# ``<runner-folder>/files/``. It pins one target generator and a complete run
# recipe (settings, config folders, accounts, permission grant, packages);
# tags are search labels only.
# ---------------------------------------------------------------------------
# The permission keys and the membership sets over the ordered ladders in
# ``collimate.permissions`` (the one SDK-wide definition — lint only validates
# membership; order matters to the grant-comparison helpers and the host's
# intersection).
_RUNNER_PERM_KNOWN = set(permissions.AXES)
_RUNNER_DOCKER_LEVELS = set(permissions.DOCKER_LEVELS)
_RUNNER_INTERNET_MODES = set(permissions.INTERNET_MODES)
_RUNNER_COMMAND_LEVELS = set(permissions.COMMAND_LEVELS)
# An ``internet_allow`` entry is a bare host name the proxy matches by DNS
# suffix; one carrying any of these (``*.x``, ``https://x``, ``x/path``,
# ``x:443``, an inner space) can never equal a host, so it matches nothing.
_HOST_ENTRY_NEVER_MATCHES = re.compile(r"[*/:\s]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class GeneratorRef:
    """What a runner's cross-checks need to know about its target generator.

    Build with :meth:`from_manifest` for disk or in-memory linting. ``config_folders`` is the set of declared
    folder-slot names; ``set_yaml_fields`` maps each ``.set_yaml`` draft-file
    path to the set of field keys it declares (or ``None`` when the file's
    contents couldn't be read — key-level checks then skip silently). A path
    present as a key IS a ``.set_yaml`` member of the target's ``draft_files``.
    """
    config_folders: set[str] = field(default_factory=set)
    set_yaml_fields: dict[str, set[str] | None] = field(default_factory=dict)
    #: The target's declared execution-locus assumption (``runs_on``);
    #: "anywhere" when omitted. Lets a runner's ``docker: limited`` grant be
    #: contradicted at AUTHORING time instead of first run.
    runs_on: str = "anywhere"
    main_config: str = ""
    required_files: set[str] = field(default_factory=set)
    account_requirements: dict = field(default_factory=dict)
    requires_docker: bool = False
    #: The target's ``requires_docker_if`` condition over its main config,
    #: or ``None``; see :meth:`docker_required`.
    requires_docker_if: dict | None = None
    #: Whether an Annotation run of the target adopts its harness's least
    #: grant (its manifest's ``adopts_harness_grant``).
    adopts_harness_grant: bool = False
    main_fields: list = field(default_factory=list)
    #: The grant axes the target declares it enforces (its manifest's
    #: ``enforces``, read with :func:`declared_enforces`). Required: a caller
    #: that leaves it out would silently drop the axis checks it gates.
    enforces: frozenset = field(kw_only=True)

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any], *,
                      read_file: Callable[[str], str | None] | None = None) -> GeneratorRef:
        """Read target facts consistently for disk and in-memory callers.

        ``read_file`` reads a path relative to the generator definition. When
        it is absent or a file cannot be parsed, content cross-checks remain
        unavailable (``None``), rather than treating missing contents as an
        empty field schema. Manifest validation is the linter's separate job.
        """
        folders = manifest.get("config_folders")
        folders = folders if isinstance(folders, list) else []
        drafts = _set_yaml_draft_files(manifest)
        fields_by_path = {}
        main_fields = []
        for rel in drafts:
            fields = None
            if read_file is not None:
                try:
                    fields = _parse_set_yaml_fields(read_file(rel))
                except (OSError, UnicodeDecodeError):
                    pass
            fields_by_path[rel] = None if fields is None else {
                f["key"] for f in fields if isinstance(f, dict) and isinstance(f.get("key"), str)}
            if rel == drafts[0] and fields is not None:
                main_fields = fields
        required = manifest.get("required_files")
        runs_on = manifest.get("runs_on")
        return cls(
            config_folders={f["name"] for f in folders if isinstance(f, dict) and isinstance(f.get("name"), str)},
            set_yaml_fields=fields_by_path,
            runs_on=runs_on if isinstance(runs_on, str) and runs_on else "anywhere",
            main_config=str(manifest.get("main_config") or ""),
            required_files={r for r in required if isinstance(r, str)} if isinstance(required, list) else set(),
            account_requirements=dict(manifest["account_requirements"]) if isinstance(manifest.get("account_requirements"), dict) else {},
            requires_docker=bool(manifest.get("requires_docker")),
            requires_docker_if=(dict(manifest["requires_docker_if"])
                                if isinstance(manifest.get("requires_docker_if"), dict)
                                and manifest["requires_docker_if"] else None),
            main_fields=main_fields,
            enforces=declared_enforces(manifest),
            adopts_harness_grant=manifest.get("adopts_harness_grant") is True,
        )

    def docker_required(self, values: Any) -> bool:
        """Whether a run with these main-config ``values`` needs docker:
        full — always under ``requires_docker``, and under
        ``requires_docker_if`` when the condition holds for the values
        resolved over the main config's defaults."""
        if self.requires_docker:
            return True
        if not self.requires_docker_if:
            return False
        from collimate.config_values import compute_visibility, matches, resolve_fields, type_index
        resolved = resolve_fields(self.main_fields, values if isinstance(values, dict) else {})
        if isinstance(values, dict):
            resolved.update(values)
        return matches(self.requires_docker_if, resolved,
                       compute_visibility(self.main_fields, resolved),
                       type_index(self.main_fields))


@dataclass
class _Ctx:
    """Shared lint context.

    ``path`` is informational — for file-based linting it points at the
    manifest on disk; for in-memory linting (used by the
    generator-builders to validate the agent's stage before persisting)
    it's a synthetic Path like ``Path("(stage)/custom.generator")``
    that just makes the Issue rendering readable.

    ``file_exists`` is the predicate the file-existence checks consult.
    For path-based linting it's ``lambda rel: (path.parent / rel).exists()``;
    for in-memory linting it's ``lambda rel: rel in stage_files``.

    ``read_file`` returns a sibling file's text, or None when it can't
    be read. Checks that inspect file *contents* (settings-overlay field
    validation, required-on-checkbox) skip silently when it's None or
    the read fails — content checks are best-effort.

    ``walk`` returns every relative file path available under the manifest
    directory (used by the runner folder-content check, ``files/<folder>/``).
    None when directory listing isn't wired — that check then skips silently.
    """
    path: Path
    file_exists: Callable[[str], bool]
    read_file: Callable[[str], str | None] | None = None
    walk: Callable[[], list[str]] | None = None
    issues: list[Issue] = field(default_factory=list)
    #: Memo for :func:`_read_set_yaml_fields` (rel → parsed fields, or None
    #: when unreadable), so several checks on one manifest parse it once.
    _set_yaml_cache: dict[str, list[dict[str, Any]] | None] = field(default_factory=dict, repr=False)

    def error(self, code: str, message: str, field: str | None = None) -> None:
        self.issues.append(Issue(Level.ERROR, code, message, self.path, field))

    def warn(self, code: str, message: str, field: str | None = None) -> None:
        self.issues.append(Issue(Level.WARNING, code, message, self.path, field))


def _load_yaml(path: Path, ctx: _Ctx) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        ctx.error("io-error", f"cannot read: {e}")
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        ctx.error("yaml-parse", f"invalid YAML: {e}")
        return None
    if data is None:
        ctx.error("empty", "manifest file is empty")
        return None
    if not isinstance(data, dict):
        ctx.error("bad-root", f"manifest root must be a mapping, got {type(data).__name__}")
        return None
    return data


def _check_required(data: dict[str, Any], keys: tuple[str, ...], ctx: _Ctx) -> None:
    for key in keys:
        if key not in data:
            ctx.error("missing-required", f"required field '{key}' is missing", field=key)
        elif not isinstance(data[key], str) or not data[key].strip():
            ctx.error("bad-type", f"'{key}' must be a non-empty string", field=key)


def _check_string(data: dict[str, Any], key: str, ctx: _Ctx) -> None:
    v = data.get(key)
    if v is None:
        return
    if not isinstance(v, str):
        ctx.error("bad-type", f"'{key}' must be a string", field=key)


def _check_bool(data: dict[str, Any], key: str, ctx: _Ctx) -> None:
    if key in data and not isinstance(data[key], bool):
        ctx.error("bad-type", f"'{key}' must be a bool", field=key)


def _check_enum(data: dict[str, Any], key: str, choices: set[str], ctx: _Ctx) -> None:
    if key not in data:
        return
    v = data[key]
    if not isinstance(v, str):
        ctx.error("bad-type", f"'{key}' must be a string", field=key)
        return
    if v not in choices:
        ctx.error(
            "bad-enum",
            f"'{key}' must be one of {sorted(choices)}, got {v!r}",
            field=key,
        )


def _check_unknown(data: dict[str, Any], known: set[str], ctx: _Ctx) -> None:
    for k in data:
        if k not in known:
            ctx.warn("unknown-field", f"unknown field '{k}' — will be ignored", field=k)


def _check_file_exists(data: dict[str, Any], key: str, ctx: _Ctx) -> None:
    v = data.get(key)
    if v is None:
        ctx.error("missing-required", f"required field '{key}' is missing", field=key)
        return
    if not isinstance(v, str):
        ctx.error("bad-type", f"'{key}' must be a string filename", field=key)
        return
    if not ctx.file_exists(v):
        ctx.error(
            "missing-file",
            f"'{key}' references {v!r} but no such file exists",
            field=key,
        )


def _check_file_list(data: dict[str, Any], key: str, ctx: _Ctx) -> None:
    v = data.get(key)
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", f"'{key}' must be a list of filenames", field=key)
        return
    for i, item in enumerate(v):
        if not isinstance(item, str):
            ctx.error(
                "bad-type",
                f"'{key}[{i}]' must be a string filename, got {type(item).__name__}",
                field=key,
            )
            continue
        if not ctx.file_exists(item):
            ctx.warn(
                "missing-file",
                f"'{key}' references {item!r} but no such file exists",
                field=key,
            )


_SENTENCE_END_RE = re.compile(r"[.!?]+\s+\S")


def _check_summary_length(data: dict[str, Any], ctx: _Ctx) -> None:
    """Warn (don't error) when ``summary`` is more than 2 sentences.

    Heuristic: count ``[.!?]+`` followed by whitespace + a non-whitespace
    glyph. A trailing period doesn't count (no following non-whitespace).
    Two such transitions ⇒ three sentences ⇒ too long.
    """
    summary = data.get("summary")
    if not isinstance(summary, str):
        return
    transitions = len(_SENTENCE_END_RE.findall(summary))
    if transitions >= 2:
        ctx.warn(
            "summary-too-long",
            "'summary' should be 1–2 sentences — keep it tight enough to read at a glance",
            field="summary",
        )


def _check_extensions(data: dict[str, Any], ctx: _Ctx) -> None:
    v = data.get("extensions")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", "'extensions' must be a list", field="extensions")
        return
    for i, ext in enumerate(v):
        if not isinstance(ext, str):
            ctx.error(
                "bad-type",
                f"'extensions[{i}]' must be a string, got {type(ext).__name__}",
                field="extensions",
            )
            continue
        if not ext.startswith("."):
            ctx.error(
                "ext-no-dot",
                f"extension {ext!r} must start with a dot (e.g. '.md')",
                field="extensions",
            )


def _check_default_for(data: dict[str, Any], ctx: _Ctx) -> None:
    """``default_for``: the files this renderer is a default for. Each entry
    is one of its own ``extensions`` (it opens files of that extension by
    default) or ``*`` (it is offered under "Open with" for every text file)."""
    v = data.get("default_for")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", "'default_for' must be a list", field="default_for")
        return
    claimed = data.get("extensions")
    claimed = {e.casefold() for e in claimed if isinstance(e, str)} if isinstance(claimed, list) else set()
    seen: set[str] = set()
    for i, entry in enumerate(v):
        if not isinstance(entry, str):
            ctx.error("bad-type", f"'default_for[{i}]' must be a string, got {type(entry).__name__}",
                      field="default_for")
            continue
        if entry.casefold() in seen:
            ctx.error("default-for-duplicate", f"default_for lists {entry!r} twice", field="default_for")
            continue
        seen.add(entry.casefold())
        if entry != _ANY_TEXT and entry.casefold() not in claimed:
            ctx.error(
                "default-for-unclaimed",
                f"default_for {entry!r} is not one of this renderer's extensions — a renderer is "
                f"the default only for an extension it claims, or {_ANY_TEXT!r} for "
                "every text file",
                field="default_for",
            )


def _check_rlimits(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate the optional ``rlimits`` block.

    Shape: a mapping with any subset of ``{memory_bytes, cpu_seconds,
    open_files}`` as keys, each mapped to a positive integer. The host's
    subprocess setup (process_manager/manager.py:_build_preexec_fn) reads
    exactly these three keys; anything else is silently ignored, so call
    it out at lint time.
    """
    v = data.get("rlimits")
    if v is None:
        return
    if not isinstance(v, dict):
        ctx.error("bad-type", "'rlimits' must be a mapping", field="rlimits")
        return
    for key, val in v.items():
        if key not in _GEN_RLIMIT_KEYS:
            ctx.error(
                "bad-enum",
                f"'rlimits.{key}' is unknown — must be one of {sorted(_GEN_RLIMIT_KEYS)}",
                field=f"rlimits.{key}",
            )
            continue
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            ctx.error(
                "bad-type",
                f"'rlimits.{key}' must be a positive integer, got {val!r}",
                field=f"rlimits.{key}",
            )


def _check_timeout_seconds(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate the optional ``timeout_seconds``: a positive number. The host
    (process_manager/manager.py) ignores anything else, and would read a
    boolean ``true`` as a one-second budget."""
    if "timeout_seconds" not in data:
        return
    v = data["timeout_seconds"]
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
        ctx.error("bad-type", "'timeout_seconds' must be a positive number",
                  field="timeout_seconds")




def _check_enforces(data: dict[str, Any], ctx: _Ctx) -> None:
    """``enforces`` is a list of distinct grant axes the generator enforces
    itself, each one the host cannot enforce (``permissions.GENERATOR_AXES``)."""
    if "enforces" not in data:
        return
    value = data["enforces"]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        ctx.error("enforces", "'enforces' must be a list of grant axis names", "enforces")
        return
    if len(set(value)) != len(value):
        ctx.error("enforces", "'enforces' lists an axis twice", "enforces")
    for axis in value:
        if axis not in permissions.AXES:
            ctx.error(
                "enforces-axis",
                f"'enforces' names {axis!r}, which is not a grant axis "
                f"(axes: {', '.join(permissions.AXES)})",
                "enforces",
            )
        elif axis not in permissions.GENERATOR_AXES:
            ctx.error(
                "enforces-host-axis",
                f"'enforces' names {axis!r}, which the host enforces for every "
                "generator — a generator declares only the axes the host cannot "
                f"enforce ({', '.join(permissions.GENERATOR_AXES)})",
                "enforces",
            )


def declared_enforces(manifest: dict[str, Any]) -> frozenset:
    """The grant axes a generator manifest declares it enforces itself
    (``enforces``), keeping only real generator axes — what a host building a
    :class:`GeneratorRef`, or telling its runner editor which axes a
    generator honours, reads."""
    value = manifest.get("enforces")
    if not isinstance(value, list):
        return frozenset()
    return frozenset(x for x in value if x in permissions.GENERATOR_AXES)


def _check_env_passthrough(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate the optional ``env_passthrough`` list.

    Shape: a list of non-empty strings naming env vars that survive the
    per-subprocess scrub in process_manager/manager.py. Warn (don't
    error) when a name looks like a secret — secrets belong in the
    keystore, not in inherited env vars.
    """
    v = data.get("env_passthrough")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", "'env_passthrough' must be a list of env var names", field="env_passthrough")
        return
    for i, name in enumerate(v):
        if not isinstance(name, str) or not name.strip():
            ctx.error(
                "bad-type",
                f"'env_passthrough[{i}]' must be a non-empty string, got {name!r}",
                field="env_passthrough",
            )
            continue
        if _SECRET_NAME_RE.search(name):
            ctx.warn(
                "env-passthrough-looks-secret",
                f"'env_passthrough' entry {name!r} looks like a secret — prefer the keystore (gen.account)",
                field="env_passthrough",
            )


def _check_ui_files(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate the optional ``ui_files`` list: filename suffixes, each a
    non-empty string starting with a dot (``.chat``, ``.website``).
    ``.run`` is the host's run card, never a generator's UI file."""
    v = data.get("ui_files")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", "'ui_files' must be a list of filename suffixes",
                  field="ui_files")
        return
    for i, suffix in enumerate(v):
        if not isinstance(suffix, str) or len(suffix) < 2 or not suffix.startswith("."):
            ctx.error(
                "ui-files-suffix",
                f"'ui_files[{i}]' must be a filename suffix starting with a dot "
                f"(e.g. '.chat'), got {suffix!r}",
                field="ui_files",
            )
        elif "/" in suffix or "\\" in suffix:
            ctx.error("ui-files-suffix",
                      f"'ui_files[{i}]' {suffix!r} must be a filename suffix, not a path",
                      field="ui_files")
        elif suffix == ".run":
            ctx.error("ui-files-run-card",
                      "'.run' is the host's run card, never a generator's UI file",
                      field="ui_files")


def _has_prompt_intent(data: dict[str, Any]) -> bool:
    v = data.get("prompt_intent")
    return isinstance(v, str) and bool(v.strip())


def _set_yaml_draft_files(data: dict[str, Any]) -> list[str]:
    if isinstance(data.get("main_config"), str) and data["main_config"]:
        return [data["main_config"]]
    raw = data.get("draft_files")
    if not isinstance(raw, list):
        return []
    return [f for f in raw if isinstance(f, str) and f.endswith(".set_yaml")]


def _read_set_yaml_fields(rel: str, ctx: _Ctx) -> list[dict[str, Any]] | None:
    """Parse ``rel`` via ``ctx.read_file`` and return its ``fields:`` list.

    Returns None when the file can't be read or parsed (or no
    ``read_file`` callback was wired) — content-level checks skip
    silently in that case.
    """
    if ctx.read_file is None:
        return None
    if rel not in ctx._set_yaml_cache:
        ctx._set_yaml_cache[rel] = _parse_set_yaml_fields(ctx.read_file(rel))
    cached = ctx._set_yaml_cache[rel]
    return None if cached is None else list(cached)


def _parse_set_yaml_fields(raw: str | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    fields = parsed.get("fields")
    if not isinstance(fields, list):
        return None
    return [f for f in fields if isinstance(f, dict)]


def _check_prompt_required(data: dict[str, Any], ctx: _Ctx) -> None:
    _check_bool(data, "prompt_required", ctx)
    if data.get("prompt_required") is True and not _has_prompt_intent(data):
        ctx.error(
            "prompt-required-without-intent",
            "'prompt_required: true' needs a 'prompt_intent' — without one "
            "the generator declares no prompt box, so the run could never start",
            field="prompt_required",
        )




def _check_prompt_conditions(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate ``prompt_show_if`` / ``prompt_required_if``.

    Each is a set_yaml condition over the MAIN CONFIG's values: it decides
    whether the runner editor shows the prompt box at all, and whether a run needs
    it filled. A condition gates the box ``prompt_intent`` declares, so one
    without an intent has nothing to gate; every referent must be a field of
    the main config (content-gated — an unreadable main config skips that
    half, like the other set_yaml checks).
    """
    conditions = [
        (key, data[key]) for key in ("prompt_show_if", "prompt_required_if")
        if key in data
    ]
    if not conditions:
        return
    main = data.get("main_config")
    fields = (_read_set_yaml_fields(main, ctx)
              if isinstance(main, str) and main else None)
    declared = (
        {f["key"] for f in fields if isinstance(f.get("key"), str)}
        if fields is not None else None
    )
    for key, cond in conditions:
        if not isinstance(cond, dict) or not cond:
            ctx.error(
                "bad-type",
                f"'{key}' must be a non-empty {{field: value}} condition map, "
                f"got {cond!r}",
                field=key,
            )
            continue
        if not _has_prompt_intent(data):
            ctx.error(
                "prompt-condition-without-intent",
                f"'{key}' needs a 'prompt_intent' — without one the generator "
                "declares no prompt box, so the condition gates nothing",
                field=key,
            )
        # Structure errors are not content-gated: they fire even when the
        # main config is unreadable.
        refs = [
            r for r, _ in walk_condition_terms(
                None, None, key, cond, ctx, allow_not=True,
                subject=f"'{key}'", issue_field=key,
            )
            if isinstance(r, str)
        ]
        if declared is None:
            continue
        for ref in refs:
            if ref not in declared:
                ctx.error(
                    "prompt-condition-unknown-field",
                    f"'{key}' references {ref!r}, which is not a field of the "
                    f"main config ({main})",
                    field=key,
                )


# The main-config field types a manifest condition (``takes_selected_file_if``,
# ``requires_docker_if``, an ``opens`` entry's ``if``) may name, and the types
# an ``opens`` path template may substitute (``text`` is the untyped default).
_FLAG_CONDITION_TYPES = ("dropdown", "checkbox")
_OPENS_TEMPLATE_TYPES = ("dropdown", "text")
_OPENS_TOKEN_RE = re.compile(r"\{([^{}]*)\}")
# The run folder a template's ``{run}`` stands for while lint checks its shape.
_LINT_RUN_FOLDER = "_runs/lint-00000000"


def _main_fields_by_key(data: dict[str, Any], ctx: _Ctx) -> dict[str, dict[str, Any]] | None:
    """The main config's fields by key, or None when it cannot be read (the
    content-gated checks then skip, like the other set_yaml checks)."""
    main = data.get("main_config")
    fields = (_read_set_yaml_fields(main, ctx)
              if isinstance(main, str) and main else None)
    if fields is None:
        return None
    return {f["key"]: f for f in fields if isinstance(f.get("key"), str)}


def _check_manifest_condition(key: str, cond: Any, declared: dict[str, dict[str, Any]] | None,
                              ctx: _Ctx, *, subject: str | None = None) -> None:
    """One manifest condition over the main config's dropdown and checkbox
    values: a non-empty condition map (the set_yaml grammar, ``not``
    allowed) whose every referent is such a field and whose dropdown values
    are among its options."""
    from collimate.config_values import UNKNOWN, canon
    from collimate.lint.conditions import _dropdown_option_canons
    subject = subject or f"'{key}'"
    if not isinstance(cond, dict) or not cond:
        ctx.error("bad-type", f"{subject} must be a non-empty {{field: value}} condition map, "
                  f"got {cond!r}", field=key)
        return
    for ref, acceptable in walk_condition_terms(None, None, key, cond, ctx, allow_not=True,
                                                subject=subject, issue_field=key):
        if declared is None or not isinstance(ref, str):
            continue
        spec = declared.get(ref)
        if spec is None:
            ctx.error("manifest-condition-unknown-field",
                      f"{subject} references {ref!r}, which is not a field of the main config",
                      field=key)
            continue
        kind = spec.get("type", "text")
        if kind not in _FLAG_CONDITION_TYPES:
            ctx.error("manifest-condition-referent-type",
                      f"{subject} references {ref!r}, a {kind!r} field; a manifest "
                      "condition names only dropdown and checkbox fields",
                      field=key)
            continue
        values = acceptable if isinstance(acceptable, list) else [acceptable]
        options = _dropdown_option_canons(spec) if kind == "dropdown" else None
        for value in values:
            c = canon(value, kind)
            if c is UNKNOWN or (options is not None and options and c not in options):
                ctx.error("manifest-condition-value",
                          f"{subject} compares {ref!r} with {value!r}, which is not "
                          f"one of its {'options' if kind == 'dropdown' else 'values'}",
                          field=key)


def _check_flag_conditions(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate ``takes_selected_file_if`` / ``requires_docker_if``: each is a
    condition over the main config's dropdown and checkbox values, and is
    redundant beside its unconditional ``true`` form."""
    declared = None
    for key, flag in (("takes_selected_file_if", "takes_selected_file"),
                      ("requires_docker_if", "requires_docker")):
        if key not in data:
            continue
        if declared is None:
            declared = _main_fields_by_key(data, ctx)
        _check_manifest_condition(key, data[key], declared, ctx)
        if data.get(flag) is True:
            ctx.error("manifest-condition-redundant",
                      f"'{key}' gates nothing beside '{flag}: true' — declare one of them",
                      field=key)


def _opens_ui_suffix_ok(path: str, ui_files: list[str]) -> bool:
    return any(isinstance(s, str) and path.endswith(s) for s in ui_files)


def _check_opens(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate ``opens``: a list of ``{path, if?}`` mappings.

    ``path`` is a template. ``{run}``, only as its first segment, stands for
    the run folder (``_runs/<label>``); any other ``{<key>}`` is the resolved
    value of a main-config dropdown or text field. A path in the run folder
    must end in a declared ``ui_files`` suffix (a path ending in a template
    is checked against that field's options or default); any other path is a
    cell-scoped file. ``if`` is a manifest condition (see
    :func:`_check_manifest_condition`).
    """
    from collimate.surfaces import surface_path
    v = data.get("opens")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error("bad-type", "'opens' must be a list of {path, if?} mappings", field="opens")
        return
    declared = _main_fields_by_key(data, ctx)
    ui_files = data.get("ui_files") if isinstance(data.get("ui_files"), list) else []
    for i, entry in enumerate(v):
        where = f"opens[{i}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not entry["path"] or set(entry) - {"path", "if"}:
            ctx.error("opens-entry", f"'{where}' must be a mapping with a 'path' template "
                      "and an optional 'if' condition", field="opens")
            continue
        template = entry["path"]
        if "if" in entry:
            _check_manifest_condition("opens", entry["if"], declared, ctx,
                                      subject=f"'{where}.if'")
        tokens = _OPENS_TOKEN_RE.findall(template)
        literal = _OPENS_TOKEN_RE.sub("", template)
        if "{" in literal or "}" in literal:
            ctx.error("opens-path", f"'{where}.path' {template!r} has an unbalanced brace",
                      field="opens")
            continue
        in_run = template.startswith("{run}/")
        if tokens.count("run") > (1 if in_run else 0):
            ctx.error("opens-path", f"'{where}.path' {template!r} may use {{run}} only as "
                      "its first segment ('{run}/...')", field="opens")
            continue
        bad = False
        for name in tokens:
            if name == "run":
                continue
            spec = declared.get(name) if declared is not None else None
            if declared is not None and spec is None:
                ctx.error("opens-path", f"'{where}.path' names {{{name}}}, which is not a "
                          "field of the main config", field="opens")
                bad = True
            elif spec is not None and spec.get("type", "text") not in _OPENS_TEMPLATE_TYPES:
                ctx.error("opens-path", f"'{where}.path' names {{{name}}}, a "
                          f"{spec.get('type')!r} field; a path substitutes only dropdown "
                          "and text fields", field="opens")
                bad = True
        if bad:
            continue
        sample = _OPENS_TOKEN_RE.sub(
            lambda m: _LINT_RUN_FOLDER if m.group(1) == "run" else "x", template)
        try:
            surface_path(sample, folder=_LINT_RUN_FOLDER)
        except ValueError as exc:
            ctx.error("opens-path", f"'{where}.path' {template!r}: {exc}", field="opens")
            continue
        if not in_run:
            continue
        if template.endswith("}"):
            name = tokens[-1]
            spec = declared.get(name) if declared is not None else None
            if spec is None:
                continue
            if spec.get("type", "text") == "dropdown":
                endings = [str(o.get("value") if isinstance(o, dict) else o)
                           for o in spec.get("options") or []]
            else:
                default = spec.get("default")
                endings = [default] if isinstance(default, str) and default else []
            for ending in endings:
                if not _opens_ui_suffix_ok(template[:-len(name) - 2] + ending, ui_files):
                    ctx.error("opens-ui-files",
                              f"'{where}.path' {template!r} resolves to a run-folder file "
                              f"ending {ending!r}, not a declared ui_files suffix",
                              field="opens")
                    break
        elif not _opens_ui_suffix_ok(template, ui_files):
            ctx.error("opens-ui-files",
                      f"'{where}.path' {template!r} is in the run folder but does not end "
                      "in a declared ui_files suffix", field="opens")


def _check_config_folders(data: dict[str, Any], ctx: _Ctx) -> None:
    """Validate the optional ``config_folders`` block.

    Shape: a list of ``{name, description, sample?}`` mappings. ``name`` is
    a folder-slot handle a runner may fill (skills/mcp/scripts/build context);
    it must match :data:`_CONFIG_FOLDER_NAME_RE`, must not be reserved
    (``.collrun``, ``.``, or anything starting with ``..``), and must be
    unique across entries. ``description`` is a required non-empty string.
    ``sample`` is an optional docs-only starter path that must exist and live
    under ``samples/`` (never staged into a run — it just shows the shape).
    """
    v = data.get("config_folders")
    if v is None:
        return
    if not isinstance(v, list):
        ctx.error(
            "bad-type",
            "'config_folders' must be a list of {name, description, sample?} "
            "mappings",
            field="config_folders",
        )
        return
    seen: set[str] = set()
    for i, entry in enumerate(v):
        if not isinstance(entry, dict):
            ctx.error(
                "bad-type",
                f"'config_folders[{i}]' must be a mapping with 'name' and "
                "'description'",
                field="config_folders",
            )
            continue
        for k in entry:
            if k not in CONFIG_FOLDER_FIELDS:
                ctx.warn(
                    "unknown-field",
                    f"'config_folders[{i}].{k}' is unknown — only "
                    f"{sorted(CONFIG_FOLDER_FIELDS)} are allowed",
                    field=f"config_folders[{i}].{k}",
                )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            ctx.error(
                "missing-required",
                f"'config_folders[{i}]' is missing a non-empty 'name'",
                field="config_folders",
            )
        elif name in _RESERVED_FOLDER_NAMES or name.startswith(".."):
            ctx.error(
                "reserved-config-folder-name",
                f"config folder name {name!r} is reserved — pick another",
                field="config_folders",
            )
        elif not _CONFIG_FOLDER_NAME_RE.match(name):
            ctx.error(
                "bad-config-folder-name",
                f"config folder name {name!r} is invalid — must match "
                f"{_CONFIG_FOLDER_NAME_RE.pattern}",
                field="config_folders",
            )
        elif name in seen:
            ctx.error(
                "duplicate-config-folder",
                f"config folder name {name!r} is declared more than once",
                field="config_folders",
            )
        else:
            seen.add(name)

        desc = entry.get("description")
        if not isinstance(desc, str) or not desc.strip():
            ctx.error(
                "missing-required",
                f"config folder {name!r} needs a non-empty 'description'",
                field="config_folders",
            )

        sample = entry.get("sample")
        if sample is None:
            continue
        if not isinstance(sample, str):
            ctx.error(
                "bad-type",
                f"config folder {name!r} 'sample' must be a string path",
                field="config_folders",
            )
            continue
        if sample != "samples" and not sample.startswith("samples/"):
            ctx.error(
                "sample-not-under-samples",
                f"config folder {name!r} sample {sample!r} must live under "
                "'samples/' (docs-only starter, never staged)",
                field="config_folders",
            )
            continue
        if not ctx.file_exists(sample):
            ctx.error(
                "missing-file",
                f"config folder {name!r} sample {sample!r} references a path "
                "that does not exist",
                field="config_folders",
            )


# ---------------------------------------------------------------------------
# Runner manifest checks (custom.runner)
# ---------------------------------------------------------------------------

# Cross-checks resolve the target generator via ``resolve``; a runner whose
# target does not resolve is an ERROR (runner-generator).


def _runner_perm_enum(
    perms: dict[str, Any], key: str, choices: set[str], default: str,
    ctx: _Ctx,
) -> str:
    """Resolve one permission axis to a legal enum value (error + fall back to
    ``default`` on a bad value)."""
    if key not in perms:
        return default
    val = perms[key]
    if not isinstance(val, str) or val not in choices:
        ctx.error(
            "runner-permission-bad-enum",
            f"'runner.permissions.{key}' must be one of {sorted(choices)}, "
            f"got {val!r}",
            field=f"runner.permissions.{key}",
        )
        return default
    return val


def _check_runner_permissions(perms: Any, ctx: _Ctx) -> None:
    """Validate a runner's (or config recipe's) ``permissions`` grant.

    Every axis is optional; omission means the safe reproduction of a plain
    host subprocess (``docker: none``, ``internet: full``,
    ``update_packages: true``, ``commands: deny``, everything else
    off/empty). Enforces the cross-invariant that any restricted internet mode
    (``none``/``proxy``) needs ``docker: limited`` — only that container gets the
    egress proxy; ``full`` and ``none`` run as host subprocesses, which have
    no honest network limiter, so the catalog would reject it.
    """
    if perms is None:
        return
    if not isinstance(perms, dict):
        ctx.error(
            "bad-type",
            "'runner.permissions' must be a mapping of axis → value",
            field="runner.permissions",
        )
        return
    removed = sorted(set(perms) & {"side_cells", "authoring", "renderers"})
    if removed:
        ctx.error("runner-permission-removed-axis",
                  "Delete removed permission axes: " + ", ".join(removed),
                  field="runner.permissions")
    for k in perms:
        if k in removed:
            continue
        if k not in _RUNNER_PERM_KNOWN:
            ctx.error(
                "runner-permission-unknown-key",
                f"'runner.permissions.{k}' is unknown — axes are "
                f"{sorted(_RUNNER_PERM_KNOWN)}",
                field=f"runner.permissions.{k}",
            )
    for _bool_axis in ("update_packages",):
        if _bool_axis in perms and not isinstance(perms[_bool_axis], bool):
            ctx.error(
                "bad-type",
                f"'runner.permissions.{_bool_axis}' must be a bool",
                field=f"runner.permissions.{_bool_axis}",
            )
    docker = _runner_perm_enum(
        perms, "docker", _RUNNER_DOCKER_LEVELS, "none", ctx)
    internet = _runner_perm_enum(
        perms, "internet", _RUNNER_INTERNET_MODES, "full", ctx)
    _runner_perm_enum(
        perms, "commands", _RUNNER_COMMAND_LEVELS, "deny", ctx)

    ia = perms.get("internet_allow")
    if ia is not None:
        if not isinstance(ia, list) or not all(isinstance(x, str) for x in ia):
            ctx.error(
                "bad-type",
                "'runner.permissions.internet_allow' must be a list of "
                "host strings",
                field="runner.permissions.internet_allow",
            )
        elif ia and internet != "proxy":
            ctx.warn(
                "runner-internet-allow-unused",
                f"'runner.permissions.internet_allow' is set but "
                f"internet={internet!r} — the allowlist only applies in "
                "'proxy' mode",
                field="runner.permissions.internet_allow",
            )
        else:
            never = [x for x in ia if _HOST_ENTRY_NEVER_MATCHES.search(x.strip())]
            if never:
                ctx.warn(
                    "runner-internet-allow-entry",
                    f"'runner.permissions.internet_allow' entries {never!r} "
                    "never match: an entry is a host name, which also covers "
                    "its subdomains (example.com covers api.example.com); "
                    "wildcards, schemes, paths, ports and spaces are not part "
                    "of it",
                    field="runner.permissions.internet_allow",
                )

    if internet != "full" and docker != "limited":
        ctx.error(
            "runner-internet-needs-docker",
            f"'runner.permissions': internet={internet!r} requires "
            "docker == limited — a restricted network is only enforceable when "
            "the main loop itself is containerized (docker: none is a bare "
            "host subprocess; docker: full keeps a native loop with socket "
            "access, and neither can have its egress bounded)",
            field="runner.permissions",
        )


_PACKAGE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~-]*$")


def _check_runner_packages(
    packages: Any, perms: Any, ctx: _Ctx,
) -> None:
    """``packages`` maps an artifact name to ``latest`` or a concrete version.

    Names are not validated against the target — a generator's artifact set is
    runtime knowledge (it depends on which build folder the runner supplies), and
    the generator already reports an unknown name at run time. What IS worth
    catching here: a malformed shape, and the contradiction of asking for
    ``latest`` under a grant that forbids reaching upstream, which would
    silently fall back to a pin and quietly not do what the runner says."""
    if packages is None:
        return
    if not isinstance(packages, dict):
        ctx.error(
            "bad-type",
            "'runner.packages' must be a mapping of artifact name → version",
            field="runner.packages",
        )
        return
    wants_latest = False
    for name, version in packages.items():
        field = f"runner.packages.{name}"
        if not isinstance(name, str) or not name.strip():
            ctx.error("bad-type", "'runner.packages' has a non-string name",
                      field="runner.packages")
            continue
        if not isinstance(version, str) or not version.strip():
            ctx.error(
                "runner-package-bad-version",
                f"'{field}' must be 'latest' or a version string",
                field=field,
            )
            continue
        if version.strip().lower() == "latest":
            wants_latest = True
        elif not _PACKAGE_VERSION_RE.match(version.strip()):
            ctx.error(
                "runner-package-bad-version",
                f"'{field}={version!r}' is not a usable version",
                field=field,
            )
    if wants_latest and isinstance(perms, dict) and perms.get("update_packages") is False:
        ctx.error(
            "runner-package-latest-needs-update",
            "'runner.packages' asks for 'latest' but "
            "'runner.permissions.update_packages' is false — resolving "
            "'latest' reaches a registry, so this would silently keep the "
            "pinned default",
            field="runner.packages",
        )


def _check_runner_runs_on(
    perms: Any, target: GeneratorRef | None, ctx: _Ctx,
) -> None:
    """A ``docker: limited`` grant contradicts a target that declares
    ``runs_on: local-machine`` — the host would refuse the run at spawn
    (process_manager/backends.enforce_runs_on), so say it at authoring
    time, where the fix is choosing a different grant or generator."""
    if target is None or target.runs_on != "local-machine":
        return
    if not isinstance(perms, dict) or perms.get("docker") != "limited":
        return
    ctx.error(
        "runner-runs-on-conflict",
        "'runner.permissions.docker' is 'limited' (the generator main loop "
        "runs in a container), but the target generator declares "
        "'runs_on: local-machine' — it assumes the user's own machine, and "
        "the host will refuse this pairing at run start. Drop the docker "
        "containment or target a generator without the constraint.",
        field="runner.permissions.docker",
    )




# ---------------------------------------------------------------------------
# Conditional-field adapter: loading and manifest orchestration stay here;
# analysis is owned by lint.conditions and evaluation by config_values.
# ---------------------------------------------------------------------------


def _lint_set_yaml_conditions(data: dict[str, Any], ctx: _Ctx) -> None:
    """Run :func:`check_set_yaml_conditions` on each draft ``.set_yaml``.

    Content-gated: silently skips files whose contents aren't available
    (``read_file`` unwired or the read failed), exactly like the other
    set_yaml content checks.
    """
    for rel in _set_yaml_draft_files(data):
        fields = _read_set_yaml_fields(rel, ctx)
        if fields is None:
            continue
        check_set_yaml_conditions(rel, fields, ctx)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Shared ``_*`` helper libraries: a cell names them under ``shared:`` in its
# ``cell.yaml``, and they are resolved at admission from the installed helper
# set, which copies each into the cell beside its packages. A package must
# NOT ship its own copy — a hand-rolled one collides with (or shadows) the
# installed helper and usually carries incomplete logic. Builder agents
# commonly fabricate ``_agent_shared.py`` because the docs say
# ``from _agent_shared import …``; this rule catches it before install.
_RESERVED_SHARED_CELLS = ("_agent_shared", "_env_shared", "_web_shared")


def _check_no_reserved_shared_cells(ctx: _Ctx) -> None:
    for name in _RESERVED_SHARED_CELLS:
        for rel in (f"{name}.py", f"{name}/__init__.py"):
            if ctx.file_exists(rel):
                ctx.error(
                    "reserved-shared-cell",
                    f"ships {rel!r}, but {name!r} is a shared helper resolved at "
                    f"admission from the installed helper set — remove it, name "
                    f"{name!r} under `shared:` in the cell's cell.yaml and just "
                    f"`from {name} import …` (a hand-rolled copy collides with "
                    f"the installed helper).",
                )
                break


def _lint_generator_body(data: dict[str, Any], ctx: _Ctx) -> None:
    """Run the generator-manifest checks against an already-loaded dict."""
    _check_required(data, _GEN_REQUIRED, ctx)
    _check_string(data, "version", ctx)
    _check_summary_length(data, ctx)
    _check_string(data, "prompt_intent", ctx)

    _check_bool(data, "chat", ctx)
    _check_bool(data, "daemon", ctx)

    _check_file_exists(data, "entrypoint", ctx)
    _check_file_list(data, "draft_files", ctx)

    _check_generator_configs(data, ctx)
    _check_prompt_required(data, ctx)
    _check_prompt_conditions(data, ctx)
    _check_bool(data, "takes_selected_file", ctx)
    _check_bool(data, "requires_docker", ctx)
    _check_flag_conditions(data, ctx)
    _check_opens(data, ctx)
    _check_config_folders(data, ctx)
    _lint_set_yaml_conditions(data, ctx)
    _check_enum(data, "runs_on", GENERATOR_RUNS_ON, ctx)
    _check_rlimits(data, ctx)
    _check_timeout_seconds(data, ctx)
    _check_env_passthrough(data, ctx)
    _check_enforces(data, ctx)
    _check_ui_files(data, ctx)
    _check_no_reserved_shared_cells(ctx)
    _check_unknown(data, GENERATOR_FIELDS, ctx)


def _lint_renderer_body(data: dict[str, Any], ctx: _Ctx) -> None:
    """Run the renderer-manifest checks against an already-loaded dict."""
    _check_required(data, _REND_REQUIRED, ctx)
    _check_string(data, "version", ctx)
    _check_summary_length(data, ctx)

    _check_enum(data, "embed", _REND_EMBED_MODES, ctx)
    _check_bool(data, "supports_diff", ctx)
    _check_bool(data, "live_refresh", ctx)

    _check_file_exists(data, "entrypoint", ctx)
    _check_extensions(data, ctx)
    _check_default_for(data, ctx)

    _check_no_reserved_shared_cells(ctx)
    _check_unknown(data, RENDERER_FIELDS, ctx)


def lint_generator_manifest(path: str | os.PathLike[str]) -> list[Issue]:
    """Lint a single ``custom.generator`` file on disk."""
    p = Path(path)

    def _read_rel(rel: str) -> str | None:
        try:
            return (p.parent / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    ctx = _Ctx(
        path=p,
        file_exists=lambda rel: (p.parent / rel).exists(),
        read_file=_read_rel,
        walk=functools.cache(
            lambda: [f.relative_to(p.parent).as_posix() for f in p.parent.rglob("*") if f.is_file()]),
    )
    data = _load_yaml(p, ctx)
    if data is None:
        return ctx.issues
    _lint_generator_body(data, ctx)
    return ctx.issues


def lint_renderer_manifest(path: str | os.PathLike[str]) -> list[Issue]:
    """Lint a single ``custom.renderer`` file on disk."""
    p = Path(path)
    ctx = _Ctx(path=p, file_exists=lambda rel: (p.parent / rel).exists())
    data = _load_yaml(p, ctx)
    if data is None:
        return ctx.issues
    _lint_renderer_body(data, ctx)
    return ctx.issues


def lint_generator_manifest_data(
    data: dict[str, Any],
    *,
    files_present: set[str],
    pseudo_path: str = "(stage)/custom.generator",
    file_contents: dict[str, str] | None = None,
) -> list[Issue]:
    """Lint a generator manifest already parsed into a dict.

    ``files_present`` is the set of available relative paths. Entrypoint
    and declared draft files are checked against it rather than disk, for
    editors and other in-memory validation callers.

    ``file_contents`` optionally maps staged paths to their text so
    content-level checks (settings-condition field validation,
    required-on-checkbox) can run; files absent from the mapping — or
    the mapping itself when omitted — are skipped silently.
    """
    ctx = _Ctx(
        path=Path(pseudo_path),
        file_exists=lambda rel: rel in files_present,
        walk=functools.cache(lambda: list(files_present)),
        read_file=(
            None if file_contents is None
            else lambda rel: file_contents.get(rel)
        ),
    )
    if not isinstance(data, dict):
        ctx.error(
            "bad-root",
            f"manifest must be a mapping, got {type(data).__name__}",
        )
        return ctx.issues
    _lint_generator_body(data, ctx)
    return ctx.issues


def lint_renderer_manifest_data(
    data: dict[str, Any],
    *,
    files_present: set[str],
    pseudo_path: str = "(stage)/custom.renderer",
) -> list[Issue]:
    """Lint a renderer manifest already parsed into a dict.

    Used by the renderer-builders, mirror of
    :func:`lint_generator_manifest_data`.
    """
    ctx = _Ctx(path=Path(pseudo_path), file_exists=lambda rel: rel in files_present)
    if not isinstance(data, dict):
        ctx.error(
            "bad-root",
            f"manifest must be a mapping, got {type(data).__name__}",
        )
        return ctx.issues
    _lint_renderer_body(data, ctx)
    return ctx.issues


RUNNER_FIELDS = {"id", "name", "description", "tags", "generator", "prompt",
                 "settings", "folders", "accounts", "permissions", "packages"}
_CONFIG_KNOWN = RUNNER_FIELDS - {"id", "generator"}


def _safe_relative(value) -> bool:
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and not re.match(r"^[A-Za-z]:", value)
            and "\\" not in value and all(p not in ("", ".", "..") for p in value.split("/")))


def _lint_runner_body(data, ctx, *, resolve=None, target=None, config=False):
    known = _CONFIG_KNOWN if config else RUNNER_FIELDS
    for key in data:
        if key not in known:
            ctx.error("unknown-field", f"Unknown {'config' if config else 'runner'} field {key!r}", key)
    if not config:
        _check_required(data, ("id", "name", "generator"), ctx)
        if isinstance(data.get("id"), str) and not _SLUG_RE.fullmatch(data["id"]):
            ctx.error("runner-id", "Runner id must be a lowercase slug", "id")
        if resolve is not None:
            target = resolve(data.get("generator", ""))
            if target is None:
                ctx.error("runner-generator", "Target generator does not exist", "generator")
    for key in ("name", "description", "prompt"):
        if key in data and not isinstance(data[key], str):
            ctx.error("bad-type", f"{key} must be a string", key)
    for key in ("tags", "folders"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
            ctx.error("bad-type", f"{key} must be a list of nonempty strings", key)
    settings = data.get("settings")
    if not isinstance(settings, dict):
        ctx.error("runner-settings", "settings must contain the complete main config", "settings")
    elif target is not None:
        expected_file = target.main_config or next(iter(target.set_yaml_fields), "")
        if set(settings) != {expected_file}:
            ctx.error("runner-main-config", f"settings must contain only {expected_file!r}", "settings")
        values = settings.get(expected_file)
        if not isinstance(values, dict):
            ctx.error("runner-settings", "Main config values must be a mapping", "settings")
        else:
            keys = target.set_yaml_fields.get(expected_file)
            if keys is not None and set(values) != keys:
                ctx.error("runner-settings-complete", f"Main config fields differ: missing {sorted(keys-set(values))}; unknown {sorted(set(values)-keys)}", "settings")
            for field in target.main_fields:
                if not isinstance(field, dict):
                    continue
                key, kind = field.get("key"), field.get("type", "text")
                value = values.get(key)
                if value is None:
                    continue  # allowed to save an unready runner
                valid = (isinstance(value, str) if kind in {"text", "textarea", "dropdown"} else
                         isinstance(value, bool) if kind == "checkbox" else
                         isinstance(value, (int, float)) and not isinstance(value, bool) if kind == "number" else
                         isinstance(value, list) if kind == "list" else
                         isinstance(value, dict) if kind == "object" else True)
                if not valid:
                    ctx.error("runner-value-type", f"Field {key!r} requires type {kind!r}", "settings")
    accounts = data.get("accounts", {})
    if not isinstance(accounts, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k,v in accounts.items()):
        ctx.error("runner-accounts", "accounts must map roles to local option IDs", "accounts")
    elif target:
        for role in accounts:
            if role not in target.account_requirements:
                ctx.error("runner-account-role", f"Unknown account role {role!r}", "accounts")
    folders = data.get("folders", [])
    if isinstance(folders, list):
        present = None
        for folder in folders:
            if not isinstance(folder, str) or not _safe_relative(folder) or "/" in folder:
                ctx.error("runner-folder", "Folder slot must be one safe path component", "folders")
            elif target and folder not in target.config_folders:
                ctx.error("runner-folder", f"Unknown folder slot {folder!r}", "folders")
            elif ctx.walk is not None:
                if present is None:
                    present = ctx.walk()
                if not any(f.startswith(f"files/{folder}/") for f in present):
                    ctx.error("runner-folder-empty", f"No files provided for {folder!r}", "folders")
    if target:
        for rel in target.required_files:
            if not ctx.file_exists(f"files/{rel}"):
                ctx.error("runner-required-file", f"Missing required file {rel!r}", "files")
        if ctx.file_exists(f"files/{target.main_config}"):
            ctx.error("runner-main-file", "The main config cannot appear in supplemental files", "files")
        perms = data.get("permissions")
        main_values = settings.get(target.main_config) if isinstance(settings, dict) else None
        if (target.docker_required(main_values)
                and (not isinstance(perms, dict) or perms.get("docker") != "full")):
            ctx.error("runner-docker-required",
                      "This generator requires docker: full" + (
                          "" if target.requires_docker else " with these settings"),
                      "permissions")
    _check_runner_permissions(data.get("permissions"), ctx)
    _check_runner_generator_axes(data.get("permissions"), settings, target, ctx)
    _check_runner_packages(data.get("packages"), data.get("permissions"), ctx)
    _check_runner_runs_on(data.get("permissions"), target, ctx)


def _check_runner_generator_axes(perms: Any, settings: Any, target: GeneratorRef | None,
                                 ctx: _Ctx) -> None:
    """A level on an axis only the generator can enforce means something only
    when the target declares it (``enforces``): check it there, and warn that
    it has no effect anywhere else. An unresolved target is left alone."""
    if target is None or not isinstance(perms, dict):
        return
    for axis in permissions.GENERATOR_AXES:
        if axis in target.enforces:
            if axis == "commands":
                _check_runner_commands(perms, settings, ctx, adopts=target.adopts_harness_grant)
        elif axis in perms:
            ctx.warn(
                "runner-axis-not-enforced",
                f"'runner.permissions.{axis}' is set, but it is not enforced by this "
                f"generator (its manifest does not declare `enforces: [{axis}]`), so "
                "the level has no effect",
                field=f"runner.permissions.{axis}",
            )


def _check_runner_commands(perms: Any, settings: Any, ctx: _Ctx, *, adopts: bool = False) -> None:
    """Warn about statically impossible grants and unattended host commands.

    Where a harness runs under a grant is :func:`collimate.harness_placement.placement`'s
    one answer, asked here without probing the machine: a host twin's CLI may
    be installed at run time. An Annotation run of a generator that adopts
    (``adopts_harness_grant``) takes its harness's minimum grant at start, so
    only a grant no adoption reaches is reported for it, and every other
    check reads the grant the run actually gets. A CLI surface runs where
    its terminal locus puts it for the account's auth, which a runner does
    not name, so it is checked for every auth the harness offers and a
    refusal names the auths it holds for. Installation and account/model
    endpoint checks stay at the generator's startup boundary.
    """
    from collimate.account_taxonomy import HARNESS_CONTAINERIZED, HARNESS_HOST_TWINS
    from collimate.harness_catalog import HARNESSES
    from collimate.harness_placement import adopt, placement
    from collimate.permissions import Grant
    if not isinstance(perms, dict) or not isinstance(settings, dict):
        return
    commands = perms.get("commands", "deny")
    if commands not in _RUNNER_COMMAND_LEVELS:
        return
    docker = perms.get("docker", "none")
    try:
        grant = Grant(docker=docker, internet=perms.get("internet", "full"), commands=commands)
    except ValueError:
        return
    twin_found = lambda harness: SimpleNamespace(  # noqa: E731 — a probe that finds every twin
        path="", version="", binary="", blocker="")
    for values in settings.values():
        if not isinstance(values, dict):
            continue
        backend = values.get("backend")
        if not isinstance(backend, str) or backend not in HARNESSES:
            continue
        reasons = []
        surface = str(values.get("surface") or "annotation")
        loci = HARNESSES[backend]["terminal_locus"] if surface == "terminal" else {}
        auths = tuple(loci) or ("",)
        placements = [(auth, (adopt if adopts else placement)(backend, grant, surface, auth=auth,
                                                             probe=twin_found)) for auth in auths]
        refused = [(auth, placed) for auth, placed in placements if not placed.ok]
        for auth, placed in refused:
            on = f" (on {auth})" if len(auths) > 1 else ""
            reasons.append(placed.refusal + on)
        placed = placements[0][1]
        # The grant the run gets: the adopted one where it was raised.
        run_docker = placed.grant.docker if placed.ok else docker
        if values.get("mcp") == "http_stdio" and (run_docker != "full" or backend not in HARNESS_CONTAINERIZED):
            reasons.append("mcp: http_stdio needs docker: full with a containerized harness")
        if reasons:
            ctx.warn("runner-grant-cannot-serve", "; ".join(reasons), field="runner.permissions")
        # A sandboxed harness's commands run in its container under docker: full.
        sandboxed = HARNESSES[backend].get("commands_sandboxed") and run_docker == "full"
        host_cli = "host" in loci.values()
        if commands == "allow" and not sandboxed and (
            HARNESSES[backend]["locus"] == "host" or host_cli
            or (run_docker == "none" and backend in HARNESS_HOST_TWINS)
        ):
            ctx.warn(
                "runner-commands-unattended-host",
                f"'runner.permissions.commands' is 'allow' with backend {backend} and docker: {run_docker} — "
                "the host harness runs commands on this machine unattended (use 'ask' to approve each one)",
                field="runner.permissions.commands",
            )


def lint_runner_manifest(path, *, resolve=None):
    p = Path(path)
    ctx = _Ctx(path=p, file_exists=lambda rel: (p.parent / rel).is_file(),
               walk=functools.cache(
                   lambda: [f.relative_to(p.parent).as_posix() for f in p.parent.rglob("*") if f.is_file()]))
    data = _load_yaml(p, ctx)
    if data is not None:
        _lint_runner_body(data, ctx, resolve=resolve)
    return ctx.issues


def lint_runner_manifest_data(data, *, files_present=frozenset(), resolve=None,
                              pseudo_path="(stage)/custom.runner"):
    ctx = _Ctx(path=Path(pseudo_path), file_exists=lambda rel: rel in files_present,
               walk=functools.cache(lambda: sorted(files_present)))
    if not isinstance(data, dict):
        ctx.error("bad-root", "Runner manifest must be a mapping")
    else:
        _lint_runner_body(data, ctx, resolve=resolve)
    return ctx.issues


def _check_generator_configs(data, ctx):
    main = data.get("main_config")
    if main is None:
        return  # the required-field check reports the missing declaration
    if not _safe_relative(main) or not main.endswith(".set_yaml"):
        ctx.error("main-config", "main_config must name a safe .set_yaml path", "main_config")
        return
    if not ctx.file_exists(main):
        ctx.error("main-config-missing", f"Main config {main!r} is missing", "main_config")
    required = data.get("required_files", [])
    if not isinstance(required, list) or not all(_safe_relative(r) and r != main for r in required):
        ctx.error("required-files", "required_files must contain safe supplemental paths", "required_files")
        required = []
    requirements = data.get("account_requirements", {})
    if not isinstance(requirements, dict):
        ctx.error("account-requirements", "account_requirements must map roles to requirements", "account_requirements")
        requirements = {}
    for role, req in requirements.items():
        if not isinstance(role, str) or not role or not isinstance(req, dict):
            ctx.error("account-requirements", "Each account role requires a mapping",
                      f"account_requirements.{role}" if isinstance(role, str) and role else "account_requirements")
            continue
        if ("kind" in req and (not isinstance(req["kind"], str) or req["kind"] not in ACCOUNT_KINDS)) or not ("kind" in req or "kinds" in req):
            ctx.error("account-kind", f"Invalid account kind for {role!r}", f"account_requirements.{role}.kind")
        for key in ACCOUNT_REQUIREMENT_LIST_FIELDS:
            if key in req and (not isinstance(req[key], list) or not all(isinstance(x,str) and x.strip() for x in req[key])):
                ctx.error("account-requirements", f"{role}.{key} must be a list of nonempty strings", f"account_requirements.{role}.{key}")
        if isinstance(req.get("kinds"), list) and any(not isinstance(kind, str) or kind not in ACCOUNT_KINDS for kind in req["kinds"]):
            ctx.error("account-kind", f"Invalid account kinds for {role!r}", f"account_requirements.{role}.kinds")
        for key in ACCOUNT_REQUIREMENT_SCALAR_FIELDS:
            if key in req and (not isinstance(req[key], str) or not req[key].strip()):
                ctx.error("account-requirements", f"{role}.{key} must be a nonempty string", f"account_requirements.{role}.{key}")
        if "required" in req and not isinstance(req["required"], bool):
            ctx.error("account-requirements", f"{role}.required must be a bool", f"account_requirements.{role}.required")
        for key in set(req) - ACCOUNT_REQUIREMENT_FIELDS:
            ctx.error("account-requirements", f"Unknown requirement {role}.{key}", f"account_requirements.{role}.{key}")
    fields = _read_set_yaml_fields(main, ctx)
    if fields is not None:
        seen_fields = set()
        for f in fields:
            if not isinstance(f, dict) or not isinstance(f.get("key"), str):
                ctx.error("config-field", "Main config fields require string keys", main)
                continue
            if f["key"] in seen_fields:
                ctx.error("config-field-duplicate", f"Duplicate field {f['key']!r}", f"{main}:{f['key']}")
            seen_fields.add(f["key"])
            if "advanced" in f and not isinstance(f["advanced"], bool):
                ctx.error("config-field-advanced", "advanced must be a boolean", f"{main}:{f['key']}")
            if f.get("secret"):
                ctx.error("config-field-secret", "Sensitive values belong to account roles, not main config", f"{main}:{f['key']}")
            if "account_role" in f or "account_property" in f:
                role = f.get("account_role")
                prop = f.get("account_property")
                if not isinstance(role, str) or role not in requirements:
                    ctx.error("config-field-account", "account_role must name a declared account requirement", f"{main}:{f['key']}")
                if (not isinstance(prop, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*", prop)
                        or prop.split(".")[0] not in ACCOUNT_PROPERTY_ROOTS
                        or any(part in ACCOUNT_PROPERTY_FORBIDDEN for part in prop.split("."))):
                    ctx.error("config-field-account", "account_property must be a safe public account property path", f"{main}:{f['key']}")
    target = GeneratorRef.from_manifest(data, read_file=ctx.read_file)
    configs = data.get("configs", [])
    if not isinstance(configs, list):
        ctx.error("configs", "configs must be a list of {id, name, path}", "configs")
        return
    seen = set()
    for i, entry in enumerate(configs):
        if not isinstance(entry, dict) or set(entry) != {"id", "name", "path"}:
            ctx.error("config-entry", "Each config must have id, name and path", f"configs[{i}]")
            continue
        cid, rel = entry.get("id"), entry.get("path")
        if not isinstance(cid,str) or not _SLUG_RE.fullmatch(cid) or cid in seen:
            ctx.error("config-id", "Config ids must be unique lowercase slugs", f"configs[{i}]")
        seen.add(cid if isinstance(cid,str) else "")
        if not isinstance(entry.get("name"),str) or not entry["name"].strip():
            ctx.error("config-name", "Config name must be nonempty", f"configs[{i}]")
        if not _safe_relative(rel) or not rel.endswith("manifest.config_yaml") or not ctx.file_exists(rel):
            ctx.error("config-path", f"Missing or invalid config path {rel!r}", f"configs[{i}]")
            continue
        if ctx.read_file is None:
            continue
        try:
            recipe = yaml.safe_load(ctx.read_file(rel) or "")
        except yaml.YAMLError:
            ctx.error("config-yaml", f"Config {rel!r} is not parsable", f"configs[{i}]")
            continue
        if not isinstance(recipe, dict):
            ctx.error("config-root", f"Config {rel!r} must be a mapping", f"configs[{i}]")
            continue
        parent = Path(rel).parent
        base = "" if parent == Path(".") else parent.as_posix() + "/"
        child = _Ctx(path=ctx.path.parent / rel,
            file_exists=lambda r, base=base: ctx.file_exists(base + r),
            read_file=None,
            walk=(None if ctx.walk is None else functools.cache(
                  lambda base=base: [r[len(base):] for r in ctx.walk() if r.startswith(base)])))
        _lint_runner_body(recipe, child, target=target, config=True)
        ctx.issues.extend(child.issues)
    if "default" not in seen:
        for rel in required:
            if not ctx.file_exists(f"configs/default/files/{rel}"):
                ctx.error("default-required-file", f"Default must provide {rel!r}", "required_files")


def _read_gen_ref(path: Path) -> tuple[str, GeneratorRef] | None:
    """Load a ``custom.generator`` into an ``(id, GeneratorRef)`` pair for the
    runner resolution index. Returns None for an unreadable/idless manifest."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    gid = data.get("id")
    if not isinstance(gid, str) or not gid.strip():
        return None
    return gid, GeneratorRef.from_manifest(
        data, read_file=lambda rel: (path.parent / rel).read_text(encoding="utf-8"))


def _build_gen_index(manifests: list[Path]) -> dict[str, GeneratorRef]:
    """Build the runner resolution index from every generator manifest found in
    a directory walk (id → GeneratorRef)."""
    index: dict[str, GeneratorRef] = {}
    for m in manifests:
        if _detect_kind(m) == "generator":
            got = _read_gen_ref(m)
            if got is not None:
                index[got[0]] = got[1]
    return index


def _detect_kind(path: Path) -> str | None:
    """Detect a definition kind from a nonempty filename stem."""
    return next((kind for kind in ("generator", "renderer", "runner", "handler")
                 if path.name.endswith("." + kind) and path.name != "." + kind), None)


def lint_path(path: str | os.PathLike[str]) -> list[Issue]:
    """Lint an explicit definition file or selected package directory.

    A directory with direct definitions is a selected package, evaluated relative
    to its parent. Other directory targets discover shallow package boundaries;
    nested definitions are package content. All four kinds establish boundaries,
    including handlers, whose runtime validation belongs to the handler system.
    Generator IDs index runner targets within a parent-tree traversal. Selected
    packages and files omit target checks when external generators are unavailable.
    """
    p = Path(path)
    if p.is_file():
        found = discover_definitions("package/" + child.name for child in p.parent.iterdir()
                                     if child.is_file())
        if found.problems:
            return [Issue(Level.ERROR, problem.code, problem.message,
                          path=p.parent if problem.path == "package" else
                          p.parent / problem.path.removeprefix("package/"))
                    for problem in found.problems]
        selected_package = next((package for package in found.packages
                                 if package.definition_path == "package/" + p.name), None)
        kind = selected_package.kind if selected_package else None
        if kind == "generator":
            return lint_generator_manifest(p)
        if kind == "renderer":
            return lint_renderer_manifest(p)
        if kind == "runner":
            return lint_runner_manifest(p)
        if kind == "handler":
            return []
        return [Issue(Level.ERROR, "unknown-kind",
                      "cannot auto-detect definition type — use .generator, .renderer, .runner, or .handler",
                      path=p)]
    if not p.is_dir():
        return [Issue(Level.ERROR, "not-found", f"path does not exist: {p}", path=p)]

    selected = any(child.is_file() and _detect_kind(child) for child in p.iterdir())
    base = p.parent if selected else p
    paths = [child.relative_to(base).as_posix() for child in p.rglob("*") if child.is_file()]
    found = discover_definitions(paths)
    manifests = [base / package.definition_path for package in found.packages]
    gen_index = _build_gen_index(manifests)
    issues = [Issue(Level.ERROR, problem.code, problem.message, path=base / problem.path)
              for problem in found.problems]
    for sub in manifests:
        kind = _detect_kind(sub)
        if kind == "generator":
            issues.extend(lint_generator_manifest(sub))
        elif kind == "renderer":
            issues.extend(lint_renderer_manifest(sub))
        elif kind == "runner":
            issues.extend(lint_runner_manifest(sub, resolve=None if selected else gen_index.get))
    return issues
