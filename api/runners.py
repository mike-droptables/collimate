"""Saved runner definitions and generator-owned configuration templates.

All mutations operate on complete drafts. There are no invocation overlays or
preset links. Generators and runners are the packages of the channel's
Capabilities cells (``cell_placement.capabilities_stack_cells``); a runner's
files travel with its definition inside the cell that holds it.
"""
from __future__ import annotations

from cell_placement import capabilities_stack_cells

import base64
import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from types import SimpleNamespace

import yaml

from collimate.lint import (
    ACCOUNT_KINDS,
    ACCOUNT_PROPERTY_FORBIDDEN,
    ACCOUNT_PROPERTY_ROOTS,
    ACCOUNT_REQUIREMENT_FIELDS,
    ACCOUNT_REQUIREMENT_LIST_FIELDS,
    ACCOUNT_REQUIREMENT_SCALAR_FIELDS,
    RUNNER_FIELDS,
    declared_enforces,
)
from collimate.config_values import resolve_form_values
from collimate.definitions import discover_definitions, package_files
from set_yaml import config_violations
from models import VirtualFile, language_for
from runner_permissions import RunnerPermissions, RunnerPermissionsError

CONFIG_MANIFEST = "manifest.config_yaml"
SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
logger = logging.getLogger(__name__)


class RunnerError(ValueError):
    def __init__(self, status: int, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


def safe_path(path: str) -> str:
    if (not isinstance(path, str) or not path or path.startswith("/") or "\\" in path
            or re.match(r"^[A-Za-z]:", path)
            or "\x00" in path or any(p in ("", ".", "..") for p in path.split("/"))
            or path.split("/")[0] == ".collrun"):
        raise RunnerError(400, f"Invalid file path: {path!r}")
    return path


def without_runners(files):
    """Strip discovered runner packages from a source-cell file collection."""
    files = list(files)
    prefixes = [p.prefix + "/" for p in discover_definitions(f.name for f in files).packages
                if p.kind == "runner"]
    return [f for f in files if not any(f.name.startswith(prefix) for prefix in prefixes)]


def definition_files(cell, kind):
    """Visible definitions of one kind after all-kind boundary analysis."""
    by_name = {f.name: f for f in cell.files}
    discovery = discover_definitions(by_name)
    for problem in discovery.problems:
        logger.warning("%s: %s", problem.path, problem.message)
    return [(package, by_name[package.definition_path]) for package in discovery.packages
            if package.kind == kind]


def generator_source_files(cell, prefix):
    """Selected package plus cell-top-level shared folders, minus runners/runs."""
    from run_folders import is_run_path
    allowed = set()
    discovery = discover_definitions(f.name for f in cell.files)
    invalid = {problem.path for problem in discovery.problems if problem.code == "definition-path-invalid"}
    for package in discovery.packages:
        if package.kind == "generator" and package.prefix + "/" == prefix:
            allowed.update(package_files(package, (f.name for f in cell.files)))
    return [f for f in without_runners(cell.files) if f.name not in invalid and not is_run_path(f.name)
            and (f.name in allowed or ("/" in f.name and f.name.split("/")[0].startswith("_")))]


def _yaml(text, label):
    try:
        value = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RunnerError(400, f"Cannot parse {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError(400, f"{label} must contain a mapping")
    return value


def _validate_generator_metadata(manifest, label):
    """Validate structures the editor reads from hand-edited/imported source."""
    configs = manifest.get("configs", [])
    if not isinstance(configs, list):
        raise RunnerError(400, f"{label}: configs must be a list")
    seen = set()
    for entry in configs:
        if not isinstance(entry, dict) or set(entry) != {"id", "name", "path"}:
            raise RunnerError(400, f"{label}: each config needs id, name and path")
        identity = entry["id"]
        if not isinstance(identity, str) or not SLUG.fullmatch(identity) or identity in seen:
            raise RunnerError(400, f"{label}: config ids must be unique lowercase slugs")
        if not isinstance(entry["name"], str) or not entry["name"].strip():
            raise RunnerError(400, f"{label}: config names must be nonempty text")
        safe_path(entry["path"])
        seen.add(identity)
    folders = manifest.get("config_folders", [])
    if not isinstance(folders, list):
        raise RunnerError(400, f"{label}: config_folders must be a list")
    seen = set()
    for folder in folders:
        if not isinstance(folder, dict) or not isinstance(folder.get("name"), str):
            raise RunnerError(400, f"{label}: each config folder needs a name")
        name = safe_path(folder["name"])
        if name in seen:
            raise RunnerError(400, f"{label}: duplicate config folder {name!r}")
        seen.add(name)
    required = manifest.get("required_files", [])
    if not isinstance(required, list):
        raise RunnerError(400, f"{label}: required_files must be a list")
    for path in required:
        safe_path(path)
    for key in ("prompt_show_if", "prompt_required_if", "takes_selected_file_if", "requires_docker_if"):
        if key in manifest and (not isinstance(manifest[key], dict) or not manifest[key]):
            raise RunnerError(400, f"{label}: {key} must be a nonempty condition mapping")
    if "takes_selected_file" in manifest and not isinstance(manifest["takes_selected_file"], bool):
        raise RunnerError(400, f"{label}: takes_selected_file must be a boolean")
    ui_files = manifest.get("ui_files", [])
    if not isinstance(ui_files, list) or any(
            not isinstance(suffix, str) or len(suffix) < 2 or not suffix.startswith(".")
            or "/" in suffix or "\\" in suffix or suffix == ".run"
            for suffix in ui_files):
        raise RunnerError(400, f"{label}: ui_files must be a list of filename suffixes "
                               "starting with a dot (never the host's '.run')")
    _validate_opens(manifest.get("opens", []), ui_files, label)
    requirements = manifest.get("account_requirements", {})
    if not isinstance(requirements, dict):
        raise RunnerError(400, f"{label}: account_requirements must be a mapping")
    for role, requirement in requirements.items():
        if not isinstance(role, str) or not role or not isinstance(requirement, dict):
            raise RunnerError(400, f"{label}: each account role needs a requirement mapping")
        if set(requirement) - ACCOUNT_REQUIREMENT_FIELDS:
            raise RunnerError(400, f"{label}: {role} contains an unknown account requirement")
        for key in ACCOUNT_REQUIREMENT_SCALAR_FIELDS | {"kind"}:
            if key in requirement and (not isinstance(requirement[key], str) or not requirement[key].strip()):
                raise RunnerError(400, f"{label}: {role}.{key} must be nonempty text")
        for key in ACCOUNT_REQUIREMENT_LIST_FIELDS:
            if key in requirement and (not isinstance(requirement[key], list)
                    or any(not isinstance(value, str) or not value.strip() for value in requirement[key])):
                raise RunnerError(400, f"{label}: {role}.{key} must be a list of strings")
        if "kind" not in requirement and "kinds" not in requirement:
            raise RunnerError(400, f"{label}: {role} must declare an account kind")
        kinds = requirement.get("kinds", [requirement["kind"]] if "kind" in requirement else [])
        if set(kinds) - ACCOUNT_KINDS:
            raise RunnerError(400, f"{label}: {role} contains an unknown account kind")
        if "required" in requirement and not isinstance(requirement["required"], bool):
            raise RunnerError(400, f"{label}: {role}.required must be a boolean")


_OPENS_TOKEN = re.compile(r"\{([^{}]*)\}")
_OPENS_SAMPLE_FOLDER = "_runs/opens-00000000"


def _validate_opens(opens, ui_files, label):
    """``opens: [{path, if?}]``: ``path`` is a template whose ``{run}`` (its
    first segment only) is the run folder and whose ``{<key>}`` is a
    main-config value; a run-folder path that ends in literal text must end
    in a declared ``ui_files`` suffix (one ending in a ``{<key>}`` is
    checked when the run resolves it)."""
    from collimate.surfaces import surface_path
    if not isinstance(opens, list):
        raise RunnerError(400, f"{label}: opens must be a list of {{path, if?}} mappings")
    for entry in opens:
        if (not isinstance(entry, dict) or set(entry) - {"path", "if"}
                or not isinstance(entry.get("path"), str) or not entry["path"]):
            raise RunnerError(400, f"{label}: each opens entry needs a path and an optional if")
        if "if" in entry and (not isinstance(entry["if"], dict) or not entry["if"]):
            raise RunnerError(400, f"{label}: an opens entry's if must be a nonempty condition mapping")
        template = entry["path"]
        tokens = _OPENS_TOKEN.findall(template)
        literal = _OPENS_TOKEN.sub("", template)
        in_run = template.startswith("{run}/")
        if "{" in literal or "}" in literal or tokens.count("run") != (1 if in_run else 0) or "" in tokens:
            raise RunnerError(400, f"{label}: opens path {template!r} must use {{run}} only as its "
                                   "first segment and name main-config keys in braces")
        sample = _OPENS_TOKEN.sub(
            lambda m: _OPENS_SAMPLE_FOLDER if m.group(1) == "run" else "x", template)
        try:
            surface_path(sample, folder=_OPENS_SAMPLE_FOLDER)
        except ValueError as exc:
            raise RunnerError(400, f"{label}: opens path {template!r}: {exc}") from exc
        if in_run and not template.endswith("}") and not any(
                template.endswith(suffix) for suffix in ui_files):
            raise RunnerError(400, f"{label}: opens path {template!r} is in the run folder but does "
                                   "not end in a declared ui_files suffix")


def generator_index(cells, problems: list | None = None):
    """Validated generator metadata indexed by YAML-declared identity.

    A generator package that cannot serve is left out, so one broken package
    never hides the others; with ``problems``, each one is appended as
    ``{cell_id, path, error}``. A duplicate id keeps the first definition,
    marked with a ``conflict`` that refuses its runners."""
    out = {}

    def report(cell, path, error):
        if problems is not None:
            problems.append({"cell_id": str(cell.id), "path": path, "error": error})

    for cell in cells:
        for package, file in definition_files(cell, "generator"):
            try:
                manifest = _yaml(file.content, file.name)
                gid = manifest["id"]
                prefix = package.prefix + "/"
                if not isinstance(gid, str) or not gid:
                    report(cell, file.name, "Generator id must be a nonempty string")
                    continue
                main = manifest.get("main_config")
                if not isinstance(main, str):
                    report(cell, file.name, "Generator declares no main_config")
                    continue
                safe_path(main)
                _validate_generator_metadata(manifest, file.name)
                members = set(package_files(package, (f.name for f in cell.files)))
                by_name = {f.name[len(prefix):]: f for f in cell.files if f.name in members}
                if main not in by_name:
                    report(cell, file.name, f"Main config {main!r} is missing from the package")
                    continue
                raw = by_name[main].content
                schema = _yaml(raw, main)
                if not isinstance(schema.get("fields"), list):
                    raise RunnerError(400, f"Main config {main!r} must declare fields")
                if any(not isinstance(field, dict) or not isinstance(field.get("key"), str)
                       or not field["key"] for field in schema["fields"]):
                    raise RunnerError(400, f"Main config {main!r} fields need nonempty string keys")
                for field in schema["fields"]:
                    if "advanced" in field and not isinstance(field["advanced"], bool):
                        raise RunnerError(400, f"Main config {main!r}: advanced must be a boolean")
                    if "account_role" in field or "account_property" in field:
                        role, prop = field.get("account_role"), field.get("account_property")
                        if not isinstance(role, str) or role not in manifest.get("account_requirements", {}):
                            raise RunnerError(400, f"Main config {main!r}: account_role must name a declared account role")
                        if (not isinstance(prop, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)*", prop)
                                or prop.split(".")[0] not in ACCOUNT_PROPERTY_ROOTS
                                or any(part in ACCOUNT_PROPERTY_FORBIDDEN for part in prop.split("."))):
                            raise RunnerError(400, f"Main config {main!r}: account_property must name public account information")
                    if field.get("secret"):
                        raise RunnerError(400, "Sensitive fields belong to Key manager account roles")
                fields, defaults = resolve_form_values(schema)
                entry = {"id": gid, "name": manifest.get("name", gid), "cell": cell,
                         "prefix": prefix, "definition_path": file.name, "manifest": manifest, "main_config": main,
                         "fields": fields, "defaults": defaults, "settings_files": {main: raw},
                         "files_by_name": by_name}
                if len(fields) != len({f["key"] for f in fields}):
                    raise RunnerError(400, f"Main config {main!r} has duplicate field names")
                if gid in out:
                    reason = f"Duplicate generator id {gid!r}"
                    out[gid]["conflict"] = reason
                    logger.warning("%s; keeping the first definition and refusing its runners", reason)
                    report(cell, file.name, reason)
                    continue
                out[gid] = entry
            except RunnerError as exc:
                report(cell, file.name, exc.reason)
            except KeyError as exc:
                report(cell, file.name, f"Generator is missing {exc.args[0]!r}")
            except TypeError as exc:
                report(cell, file.name, f"Generator is malformed: {exc}")
    return out


def _file_payload(file, name=None):
    row = {"name": name or file.name, "content": file.content or "", "language": file.language}
    if getattr(file, "blob_hash", None):
        row["blob_hash"] = file.blob_hash
    if getattr(file, "data", None) is not None:
        row["content"] = base64.b64encode(file.data).decode("ascii")
        row["encoding"] = "base64"
        row.pop("blob_hash", None)
    return row


def config_template(gi, config_id):
    main = gi["main_config"]
    default = {"name": gi["name"], "description": gi["manifest"].get("summary", ""),
               "tags": [], "prompt": "", "settings": {main: copy.deepcopy(gi["defaults"])},
               "folders": [], "accounts": {}, "permissions": {}, "packages": {}}
    files = []
    entries = gi["manifest"].get("configs") or []
    chosen = next((x for x in entries if x.get("id") == config_id), None)
    if chosen is None:
        if config_id != "default":
            raise RunnerError(404, f"Config {config_id!r} not found")
        prefix = "configs/default/files/"
        files = [_file_payload(f, n[len(prefix):]) for n, f in gi["files_by_name"].items()
                 if n.startswith(prefix)]
        default["folders"] = [folder["name"] for folder in gi["manifest"].get("config_folders", [])
                              if any(f["name"].startswith(folder["name"] + "/") for f in files)]
    else:
        rel = safe_path(chosen["path"])
        source = gi["files_by_name"].get(rel)
        if source is None:
            raise RunnerError(400, f"Config file {rel!r} is missing")
        content = _yaml(source.content, rel)
        # Config identity is the manifest index entry, not runner identity.
        default.update({k: copy.deepcopy(v) for k, v in content.items() if k in RUNNER_FIELDS - {"id", "generator"}})
        parent = PurePosixPath(rel).parent
        fp = ("" if parent == PurePosixPath(".") else str(parent) + "/") + "files/"
        files = [_file_payload(f, n[len(fp):]) for n, f in gi["files_by_name"].items() if n.startswith(fp)]
    # The main config is the authority for Load Default. Its companion
    # template supplies metadata and files, while other named configs retain
    # their own complete settings snapshot.
    if config_id == "default":
        default["settings"] = {main: copy.deepcopy(gi["defaults"])}
    validate_definition(gi, {**default, "id": "config-template", "generator": gi["id"]}, files)
    display_name = chosen["name"] if chosen else "Default"
    return {"runner": default, "files": files,
            "revision": revision({"config_id": config_id, "name": display_name, "runner": default}, files)}


def generator_payload(gi):
    entries = [{"id": x["id"], "name": x.get("name", x["id"])}
               for x in gi["manifest"].get("configs", [])]
    if not any(x["id"] == "default" for x in entries):
        entries.insert(0, {"id": "default", "name": "Default"})
    templates = {entry["id"]: config_template(gi, entry["id"]) for entry in entries}
    for entry in entries:
        entry["revision"] = templates[entry["id"]]["revision"]
    conditions = {key: gi["manifest"][key] for key in ("prompt_show_if", "prompt_required_if",
                                                       "takes_selected_file_if", "requires_docker_if")
                  if gi["manifest"].get(key)}
    return {"id": gi["id"], "name": gi["name"], "cell_id": str(gi["cell"].id),
            "main_config": gi["main_config"], "fields": gi["fields"],
            "settings_files": gi["settings_files"],
            "account_requirements": gi["manifest"].get("account_requirements", {}),
            "config_folders": gi["manifest"].get("config_folders", []),
            "prompt_intent": gi["manifest"].get("prompt_intent"),
            "prompt_required": gi["manifest"].get("prompt_required", False),
            "takes_selected_file": gi["manifest"].get("takes_selected_file") is True,
            # The grant axes this generator enforces itself (its manifest's
            # ``enforces``): the runner editor offers such an axis only here.
            "enforces": sorted(declared_enforces(gi["manifest"])),
            "configs": entries, "default": templates["default"], **conditions}


def packaged_runners(cell):
    """The runner packages ``cell`` ships, as runner drafts ``{runner, files}``
    (files relative to the package's ``files/``), in discovery order."""
    drafts = []
    for package, file in definition_files(cell, "runner"):
        prefix = package.prefix + "/files/"
        drafts.append({"runner": _yaml(file.content, file.name),
                       "files": [_file_payload(f, f.name[len(prefix):]) for f in cell.files
                                 if f.name.startswith(prefix)]})
    return drafts


def home_cell(channel, cell_id):
    """The Capabilities cell ``cell_id`` names — where a runner created from
    that cell lives — or a 404 for any other id (a built-in card included)."""
    for cell in capabilities_stack_cells(channel):
        if str(cell.id) == cell_id:
            return cell
    raise RunnerError(404, "Runners live in a Capabilities cell")


def find_runner(channel, runner_id):
    matches = []
    for cell in capabilities_stack_cells(channel):
        for _, file in definition_files(cell, "runner"):
            data = _yaml(file.content, file.name)
            if data.get("id") == runner_id:
                matches.append((cell, file, data))
    if len(matches) > 1:
        raise RunnerError(409, f"Duplicate runner id {runner_id!r}")
    if not matches:
        raise RunnerError(404, f"Runner {runner_id!r} not found")
    return matches[0]


def runner_files(cell, runner_id):
    matches = [(package, file) for package, file in definition_files(cell, "runner")
               if _yaml(file.content, file.name).get("id") == runner_id]
    if len(matches) != 1:
        raise RunnerError(409 if matches else 404, f"Runner {runner_id!r} is not unique in its cell")
    prefix = matches[0][0].prefix + "/files/"
    return [_file_payload(f, f.name[len(prefix):]) for f in cell.files if f.name.startswith(prefix)]


def revision(runner, files):
    return hashlib.sha256(json.dumps({"runner": runner, "files": sorted(files, key=lambda f:f["name"])},
                                    sort_keys=True).encode()).hexdigest()


def prompt_gate(manifest, fields, values):
    """``(shown, required)`` for a generator's prompt against main-config values.

    ``prompt_show_if`` hides the prompt entirely; ``prompt_required`` and a
    matching ``prompt_required_if`` each make a shown prompt mandatory.
    """
    from collimate.config_values import compute_visibility, matches, type_index
    show_if = manifest.get("prompt_show_if")
    required_if = manifest.get("prompt_required_if")
    if not show_if and not required_if:
        return True, bool(manifest.get("prompt_required"))
    visible = compute_visibility(fields, values)
    types = type_index(fields)
    if show_if and not matches(show_if, values, visible, types):
        return False, False
    return True, bool(manifest.get("prompt_required")) or bool(
        required_if and matches(required_if, values, visible, types))


def selected_file_gate(manifest, fields, values) -> bool:
    """Whether a run with these main-config values takes the selected file:
    always under ``takes_selected_file: true``, else when its
    ``takes_selected_file_if`` condition holds."""
    if manifest.get("takes_selected_file") is True:
        return True
    condition = manifest.get("takes_selected_file_if")
    if not condition:
        return False
    from collimate.config_values import compute_visibility, matches, type_index
    return bool(matches(condition, values, compute_visibility(fields, values), type_index(fields)))


def validate_definition(gi, runner, files, *, runnable=False):
    if not isinstance(runner, dict) or set(runner) - RUNNER_FIELDS:
        raise RunnerError(400, "Unknown runner fields")
    rid = runner.get("id", "")
    if not isinstance(rid, str) or not SLUG.fullmatch(rid):
        raise RunnerError(400, "Runner id must be a lowercase slug")
    if runner.get("generator") != gi["id"]:
        raise RunnerError(400, "Runner must target its selected generator")
    for key in ("name", "description", "prompt"):
        if not isinstance(runner.get(key, ""), str):
            raise RunnerError(400, f"{key} must be text")
    for key in ("tags", "folders"):
        if not isinstance(runner.get(key, []), list) or any(not isinstance(x, str) for x in runner.get(key, [])):
            raise RunnerError(400, f"{key} must be a list of strings")
    for key in ("settings", "accounts", "permissions", "packages"):
        if not isinstance(runner.get(key, {}), dict):
            raise RunnerError(400, f"{key} must be a mapping")
    if any(not isinstance(v, str) for v in runner.get("accounts", {}).values()):
        raise RunnerError(400, "Account bindings must be option IDs")
    main = gi["main_config"]
    settings = runner.get("settings", {})
    if set(settings) != {main} or not isinstance(settings[main], dict):
        raise RunnerError(400, "Runner settings must include the complete main config")
    known = {f["key"] for f in gi["fields"]}
    if set(settings[main]) != known:
        raise RunnerError(400, "Runner settings must contain every main-config field exactly once")
    slots = {x["name"] for x in gi["manifest"].get("config_folders", [])}
    if set(runner.get("folders", [])) - slots:
        raise RunnerError(400, "Runner has an undeclared config folder")
    required = set(gi["manifest"].get("required_files", []))
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
        raise RunnerError(400, "Files must be a list of file records")
    seen = set()
    for f in files:
        name = safe_path(f.get("name", ""))
        if set(f) - {"name", "content", "language", "blob_hash", "encoding"}:
            raise RunnerError(400, f"Unknown fields for file {name!r}")
        if not isinstance(f.get("content", ""), str) or f.get("encoding") not in (None, "base64"):
            raise RunnerError(400, f"Invalid content encoding for {name!r}")
        if "language" in f and not isinstance(f["language"], str):
            raise RunnerError(400, f"Invalid file language for {name!r}")
        blob = f.get("blob_hash")
        if blob is not None and (not isinstance(blob, str) or not re.fullmatch(r"[a-f0-9]{64}", blob)):
            raise RunnerError(400, f"Invalid blob hash for {name!r}")
        if name == main or name in (CONFIG_MANIFEST, "prompt.md"):
            raise RunnerError(400, f"{name} is edited through the runner settings")
        if name in seen:
            raise RunnerError(400, f"Duplicate file {name!r}")
        seen.add(name)
    if required - seen:
        raise RunnerError(400, f"Missing required files: {sorted(required - seen)}")
    from collimate.lint import GeneratorRef, Level, lint_runner_manifest_data
    def read_file(path):
        source = gi["files_by_name"].get(path)
        return source.content if source is not None else None

    target = GeneratorRef.from_manifest(gi["manifest"], read_file=read_file)
    issues = lint_runner_manifest_data(runner, files_present=frozenset("files/" + n for n in seen),
        resolve=lambda gid: target if gid == gi["id"] else None)
    errors = [i.message for i in issues if i.level == Level.ERROR]
    if errors:
        raise RunnerError(400, "; ".join(errors))
    try:
        perms = RunnerPermissions.from_definition(runner.get("permissions"))
    except RunnerPermissionsError as exc:
        raise RunnerError(400, str(exc)) from exc
    missing, invalid = config_violations(main, gi["fields"], settings[main])
    if prompt_gate(gi["manifest"], gi["fields"], settings[main])[1] and not runner.get("prompt", "").strip():
        missing.append({"file": "prompt.md", "key": "prompt", "label": "Prompt"})
    requirements = gi["manifest"].get("account_requirements", {})
    if set(runner.get("accounts", {})) - set(requirements):
        raise RunnerError(400, "Runner binds an undeclared account role")
    for role, requirement in requirements.items():
        if requirement.get("required", True) and not runner.get("accounts", {}).get(role):
            missing.append({"file": "accounts", "key": role, "label": role})
    if runnable and (missing or invalid):
        raise RunnerError(400, "Runner is incomplete: " + ", ".join(str(x.get("label", x.get("key"))) for x in missing + invalid))
    return missing, invalid, perms


def build_editor(channel, runner_id):
    cell, mf, data = find_runner(channel, runner_id)
    gi = generator_index(capabilities_stack_cells(channel)).get(data.get("generator"))
    if gi is None:
        raise RunnerError(400, "Runner target generator is unavailable")
    files = runner_files(cell, runner_id)
    validate_definition(gi, data, files)
    return {"runner": data, "files": files, "revision": revision(data, files),
            "cell_id": str(cell.id), "generator": generator_payload(gi)}


def build_catalog(channel):
    result = []
    cells = capabilities_stack_cells(channel)
    index = generator_index(cells)
    declarations = []
    counts = {}
    for cell in cells:
        for _, file in definition_files(cell, "runner"):
            try:
                data = _yaml(file.content, file.name)
            except RunnerError:
                continue
            rid = data.get("id")
            if not isinstance(rid, str) or not SLUG.fullmatch(rid):
                continue
            declarations.append((cell, file, data, rid))
            counts[rid] = counts.get(rid, 0) + 1
    for cell, file, data, rid in declarations:
        entry = {"id": rid, "name": rid, "description": "", "tags": [], "cell_id": str(cell.id),
                 "icon": getattr(cell, "icon", None), "takes_selected_file": False}
        try:
            data = _yaml(file.content, file.name)
            if counts[rid] > 1:
                raise RunnerError(400, "Duplicate runner identity")
            entry.update(data)
            gi = index.get(data.get("generator"))
            if gi is None:
                raise RunnerError(400, "Target generator is unavailable")
            if gi.get("conflict"):
                raise RunnerError(409, gi["conflict"])
            files = runner_files(cell, rid)
            missing, invalid, _ = validate_definition(gi, data, files)
            settings = data["settings"][gi["main_config"]]
            if gi["manifest"].get("account_requirements") or data.get("accounts"):
                from accounts import AccountError, resolve_public_accounts
                try:
                    public = resolve_public_accounts(data.get("accounts", {}), gi["manifest"].get("account_requirements", {}))
                except AccountError as exc:
                    raise RunnerError(400, str(exc)) from exc
                fields, defaults = gi["fields"], gi["defaults"]
                effective = {**data, "settings": _account_settings(gi["main_config"], fields, defaults, data["settings"], public)}
                missing, invalid, _ = validate_definition(gi, effective, files)
                settings = effective["settings"][gi["main_config"]]
            entry.update(missing_required=missing, invalid_options=invalid, revision=revision(data, files),
                         takes_selected_file=selected_file_gate(gi["manifest"], gi["fields"], settings))
            if missing or invalid:
                entry["invalid"] = {"reason": "Runner has incomplete or invalid settings"}
        except RunnerError as exc:
            entry["invalid"] = {"reason": exc.reason}
        # A malformed source definition remains discoverable, but cannot
        # leak wrong types into the picker's search/render contract.
        for key, fallback in (("name", rid), ("description", "")):
            if not isinstance(entry.get(key), str):
                entry[key] = fallback
        tags = entry.get("tags", [])
        entry["tags"] = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
        result.append(entry)
    return result


def _vfiles(prefix, files):
    from pathlib import Path
    from cas import blob_path
    from cas import store_bytes
    out = []
    for row in files:
        name = prefix + safe_path(row["name"])
        content = row.get("content", "")
        blob = row.get("blob_hash")
        if row.get("encoding") == "base64":
            try:
                blob = store_bytes(base64.b64decode(content, validate=True))
            except (ValueError, TypeError) as exc:
                raise RunnerError(400, "Invalid binary file encoding") from exc
            content = ""
        if blob:
            if not re.fullmatch(r"[a-f0-9]{64}", blob):
                raise RunnerError(400, "Invalid blob hash")
            if not Path(blob_path(blob)).is_file():
                raise RunnerError(400, f"Missing file blob for {row['name']}")
        out.append(VirtualFile(name=name, content=content, language=row.get("language", "text" if blob else language_for(name)), blob_hash=blob))
    return out


def commit_cell_files(channel, cell, files, label="Runner edit", *, persist=True):
    """Commit a complete runner-source draft using the core source writer."""
    from cell_commits import commit_source_cell
    commit_source_cell(channel, cell, files, label=label, generator_name="runner-editor", persist=persist)


def runner_vfiles(definition, files, *, occupied_files, definition_path=None):
    """Prepare a runner package without mutating occupied source files.

    New packages use runner-<id> (then -2, -3); edits retain the exact path.
    """
    rid = definition.get("id")
    if not isinstance(rid, str) or not SLUG.fullmatch(rid):
        raise RunnerError(400, "Runner id must be a lowercase slug")
    if definition_path is not None:
        safe_path(definition_path)
        discovery = discover_definitions([definition_path])
        if len(discovery.packages) != 1 or discovery.packages[0].kind != "runner":
            raise RunnerError(400, "Runner definition must be a non-root .runner path")
        prefix = discovery.packages[0].prefix + "/"
    else:
        names = [f.name for f in occupied_files]
        folder = f"runner-{rid}"
        number = 1
        while any(name == folder or name.startswith(folder + "/")
                  or folder.startswith(name + "/") for name in names):
            number += 1
            folder = f"runner-{rid}-{number}"
        prefix = folder + "/"
        definition_path = prefix + rid + ".runner"
    prepared = _vfiles(prefix + "files/", files)
    manifest = VirtualFile(name=definition_path, content=yaml.safe_dump(definition, sort_keys=False), language="yaml")
    return [manifest] + prepared


def new_runner_cell_name(channel, runner) -> str:
    """The name of the new Capabilities cell a runner is created into: the
    runner's name, numbered past every Capabilities cell's name (compared
    casefolded)."""
    from cell_placement import capabilities_stack
    base = str(runner.get("name") or runner["id"]).strip() or runner["id"]
    taken = {cell.name.casefold() for cell in capabilities_stack(channel).cells}
    name, number = base, 1
    while name.casefold() in taken:
        number += 1
        name = f"{base} {number}"
    return name


def save_runner(channel, runner, files, *, expected_revision=None, create=False, cell_id=None, new_cell=False):
    """Commit a complete runner draft.

    A runner lives in a Capabilities cell. On a create, ``cell_id`` names
    the Capabilities cell it is saved into, which need not hold its generator,
    and defaults to its generator's cell; ``new_cell`` instead saves it into
    a new standard cell at the end of Capabilities, named after the runner
    (:func:`new_runner_cell_name`). Afterwards the runner is edited where it
    lives: an update finds it by id in whatever cell holds it, and a
    ``cell_id`` naming any other cell, or ``new_cell``, is refused."""
    from models import Cell
    from cell_placement import capabilities_stack
    from state import state_lock

    if new_cell and cell_id:
        raise RunnerError(400, "Name a cell or ask for a new one, not both")
    gi = generator_index(capabilities_stack_cells(channel)).get(runner.get("generator"))
    if gi is None:
        raise RunnerError(404, "Generator not found")
    validate_definition(gi, runner, files)
    rid = runner["id"]
    created = None
    if create:
        try:
            find_runner(channel, rid)
        except RunnerError as exc:
            if exc.status != 404:
                raise
        else:
            raise RunnerError(409, "Runner already exists")
        if new_cell:
            cell = created = Cell(name=new_runner_cell_name(channel, runner))
        else:
            cell = home_cell(channel, cell_id) if cell_id else gi["cell"]
        definition_path = None
        prefix = None
    else:
        if new_cell:
            raise RunnerError(400, "A runner is edited in the cell it lives in")
        cell, manifest_file, current = find_runner(channel, rid)
        definition_path = manifest_file.name
        prefix = str(PurePosixPath(definition_path).parent) + "/"
        if cell_id and cell_id != str(cell.id):
            raise RunnerError(400, "A runner is edited in the cell it lives in")
        if current.get("generator") != runner.get("generator"):
            raise RunnerError(400, "An existing runner's generator cannot change")
        if not expected_revision or revision(current, runner_files(cell, rid)) != expected_revision:
            raise RunnerError(409, "Runner changed since this editor was opened")
    new_files = runner_vfiles(runner, files, occupied_files=cell.files, definition_path=definition_path)
    if created is not None:
        with state_lock:
            capabilities_stack(channel).cells.append(created)
    try:
        commit_cell_files(channel, cell, [f for f in cell.files if prefix is None or not f.name.startswith(prefix)] + new_files)
    except BaseException:
        if created is not None:
            with state_lock:
                capabilities_stack(channel).cells.remove(created)
        raise
    return build_editor(channel, rid)


def delete_runner(channel, runner_id):
    cell, manifest_file, _ = find_runner(channel, runner_id)
    prefix = str(PurePosixPath(manifest_file.name).parent) + "/"
    commit_cell_files(channel, cell, [f for f in cell.files if not f.name.startswith(prefix)], "Delete runner")


@dataclass
class ResolvedRunner:
    runner_id: str
    runner_name: str
    runner_cell_id: str
    runner_dir: str
    generator_id: str
    settings_overlay: dict
    folders: list
    permissions: RunnerPermissions
    packages: dict
    prompt: str
    accounts: dict
    account_requirements: dict
    revision: str
    files: list
    generator_revision: str
    main_config: str
    main_config_content: str
    fields: list[dict]
    defaults: dict
    generator_manifest: dict
    definition: dict
    generator_cell_id: str = ""


def resolve_runner_run(channel, runner_id, expected_revision=None):
    from state import state_lock
    with state_lock:
        return _resolve_runner_run(channel, runner_id, expected_revision)


def _resolve_runner_run(channel, runner_id, expected_revision=None):
    cell, manifest_file, data = find_runner(channel, runner_id)
    gi = generator_index(capabilities_stack_cells(channel)).get(data.get("generator"))
    if gi is None:
        raise RunnerError(404, "Target generator not found")
    if gi.get("conflict"):
        raise RunnerError(409, gi["conflict"])
    files = runner_files(cell, runner_id)
    # Account-derived fields and their conditional dependents can only be
    # checked against the account snapshot captured by startup below.
    has_account_fields = any(field.get("account_role") for field in gi["fields"])
    _, _, perms = validate_definition(gi, data, files, runnable=not has_account_fields)
    rev = revision(data, files)
    if expected_revision and expected_revision != rev:
        raise RunnerError(409, "Runner changed before it could start")
    return ResolvedRunner(
        runner_id=runner_id,
        runner_name=data.get("name", runner_id),
        runner_cell_id=str(cell.id),
        runner_dir=str(PurePosixPath(manifest_file.name).parent) + "/",
        generator_id=data["generator"],
        settings_overlay=copy.deepcopy(data["settings"]),
        folders=list(data.get("folders", [])),
        permissions=perms,
        packages=dict(data.get("packages", {})),
        prompt=data.get("prompt", ""),
        accounts=dict(data.get("accounts", {})),
        account_requirements=gi["manifest"].get("account_requirements", {}),
        revision=rev,
        files=files,
        # The generator's own files: another runner saved into the same
        # cell must not read as a generator change (verify_runner_start).
        generator_revision=revision({}, [_file_payload(f) for f in generator_source_files(gi["cell"], gi["prefix"]) ]),
        main_config=gi["main_config"],
        main_config_content=gi["settings_files"][gi["main_config"]],
        fields=copy.deepcopy(gi["fields"]),
        defaults=copy.deepcopy(gi["defaults"]),
        generator_manifest=copy.deepcopy(gi["manifest"]),
        definition=copy.deepcopy(data),
        generator_cell_id=str(gi["cell"].id))


def _account_settings(main, fields, defaults, saved_settings, public):
    settings = copy.deepcopy(saved_settings)
    for field in fields:
        role = field.get("account_role")
        if not role:
            continue
        value = public.get(role)
        for part in field["account_property"].split("."):
            value = value.get(part) if isinstance(value, dict) else None
        settings[main][field["key"]] = copy.deepcopy(
            value if value is not None else field.get("default", defaults[field["key"]]))
    return settings


def apply_account_settings(resolved: ResolvedRunner, account_context: dict) -> None:
    """Derive and validate execution settings from the captured public account
    snapshot. The saved runner, its revision, and the registry remain intact.
    Startup uses this before staging or recording a run.
    """
    fields, defaults = resolved.fields, resolved.defaults
    if not any(field.get("account_role") for field in fields):
        return
    from accounts import public_accounts_snapshot
    settings = _account_settings(resolved.main_config, fields, defaults, resolved.settings_overlay,
                                 public_accounts_snapshot(account_context))
    definition = {**resolved.definition, "settings": settings}
    validate_definition({"id": resolved.generator_id, "main_config": resolved.main_config,
                         "manifest": resolved.generator_manifest, "fields": fields,
                         "files_by_name": {resolved.main_config: SimpleNamespace(content=resolved.main_config_content)}},
                        definition, resolved.files, runnable=True)
    resolved.settings_overlay = settings


def adopt_harness_grant(resolved: ResolvedRunner, account_context: dict | None = None) -> dict | None:
    """Raise a harness-bound run's grant to its harness's minimum (decision
    8): an Annotation run whose model's harness needs ``docker: full`` (a
    container harness, a command sandbox) runs under the runner's grant with
    ``docker`` lifted to ``full`` instead of refusing, and the run's records
    say so. Every other axis, and a runner that already grants enough, is
    kept as it is; a chat or CLI runner's grant is the user's explicit
    choice and is never raised. Call after :func:`apply_account_settings`
    (the harness is the model account's; ``account_context``'s model account
    decides its auth, which places a CLI surface). The answer is the SDK's one
    placement function, :func:`collimate.harness_placement.adopt`; a grant
    it cannot place is left for the run's own config check to refuse, so the
    refusal reads the same wherever the run starts.

    Returns the run's placement record (``None`` for a run no harness binds,
    or whose generator does not declare ``adopts_harness_grant: true``):
    ``harness``, ``surface``, ``runtime`` (on the terminal surface, where the
    CLI runs: ``host``, this machine, or ``docker``, its container),
    ``adopted``, ``raised`` (``[{axis, from, to}]``), ``runner_grant`` and
    ``effective_grant`` (the grant the run runs under, which
    ``resolved.permissions`` now is). The host terminal op reads it."""
    from collimate.account_taxonomy import model_account_auth
    from collimate.harness_catalog import HARNESSES
    from collimate.harness_placement import adopt
    from collimate.permissions import Grant
    # Only a generator whose manifest opts in (the built-in Agent's
    # ``adopts_harness_grant: true``) is ever raised: any other generator's
    # runner keeps its own grant, whatever fields it binds.
    if (resolved.generator_manifest or {}).get("adopts_harness_grant") is not True:
        return None
    settings = resolved.settings_overlay.get(resolved.main_config) or {}
    key = next((field["key"] for field in resolved.fields
                if field.get("account_role") and field.get("account_property") == "metadata.harness"), None)
    harness = settings.get(key) if key else None
    if not isinstance(harness, str) or harness not in HARNESSES:
        return None
    surface = settings.get("surface") or "annotation"
    runner = resolved.permissions
    model = (account_context or {}).get("model")
    try:
        auth = model_account_auth(model) if isinstance(model, dict) else ""
    except ValueError:
        auth = ""
    if auth not in HARNESSES[harness]["auths"]:
        auth = ""
    placed = adopt(harness, Grant(docker=runner.docker, internet=runner.internet, commands=runner.commands),
                   str(surface), auth=auth)
    if placed.adopted:
        resolved.permissions = replace(runner, docker=placed.grant.docker)
    return {
        "harness": harness,
        "surface": placed.surface,
        "runtime": placed.runtime,
        "adopted": placed.adopted,
        "raised": [{"axis": axis, "from": before, "to": after} for axis, before, after in placed.raised],
        "runner_grant": runner.to_dict(),
        "effective_grant": resolved.permissions.to_dict(),
    }


def verify_runner_start(channel, resolved):
    """Call under state_lock immediately before freezing source versions."""
    current = resolve_runner_run(channel, resolved.runner_id, resolved.revision)
    if current.generator_revision != resolved.generator_revision:
        raise RunnerError(409, "Generator changed before the runner could start")
