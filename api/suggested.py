"""The suggested channel setups: where they come from, the catalog the New
channel page shows, and seeding a channel from one.

A suggestion is a ``suggested/<id>.json`` of a content tree (read by
``collimate._host.packaging.suggested``): its cells of the generator and renderer
asset trees, which a channel seeded from it holds on its Capabilities
stack, and the tutorial cells that start on the canvas.

The catalog reads one content tree, from one of three source kinds:

- ``folder``: a local folder the user names (the override, kept in app.db's
  ``ui_state`` under ``suggested_source``); it wins while set.
- ``github``: the latest release's content, fetched only when the catalog
  is refreshed and then read for every channel created after it.
- ``bundled``: the ``content/`` tree installed with the app
  (``genesis.bundled_dir``); the default, read without any network.

Refresh downloads :data:`GITHUB_CONTENT_ZIP`, the ``content.zip`` asset of
the repository's latest GitHub release. Its format: a zip of the
``content/`` tree's three folders, ``generators/``, ``renderers/`` and
``suggested/``, directly at the zip root (no wrapping folder, nothing else
beside them); stored or deflated members only, none absolute, with a ``..``
part, a backslash or a symlink; at most :data:`MAX_MEMBERS` members, each at
most :data:`MAX_MEMBER_BYTES` and together at most
:data:`MAX_EXTRACTED_BYTES` unpacked.

A fetch downloads to ``partial-<uuid>.zip``, extracts into a fresh
``.collimate/suggested/github-<uuid>/`` and then swaps the pointer file
``github.json`` (``{dir, fetched_at, app_version}``, staged as
``partial-<uuid>.json``) in atomically; readers resolve the pointer once and
read only that tree. A pointer another app version fetched is ignored (the
bundled tree is read) until the catalog is refreshed again.
Each fetch first deletes the ``partial-*`` files an interrupted fetch left,
and on success keeps the new and the previous tree and deletes every other
one. A failed fetch keeps serving what was served before it (the fetched
tree, else the bundled one) beside a short error.

A seeded cell records the source it was read from as its provenance: the
source kind and the folder's path, the release URL or the bundled tree's
path.

The runner drafts a suggestion offers are the runner packages inside its
asset cells. They are stripped from the seeded cells; the drafts the user
keeps (each complete) are written back into their generator's cell before
admission, so every cell starts with one version.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

GITHUB_CONTENT_ZIP = "https://github.com/mike-droptables/collimate/releases/latest/download/content.zip"
# The content tree's folders, which the zip root holds and nothing else.
_CONTENT_FOLDERS = frozenset({"generators", "renderers", "suggested"})
SOURCE_KEY = "suggested_source"
CACHE_DIR = os.path.join(".collimate", "suggested")
POINTER = os.path.join(CACHE_DIR, "github.json")
_TREE_PREFIX = "github-"
_PARTIAL_PREFIX = "partial-"

FETCH_TIMEOUT_SECONDS = 30.0
MAX_DOWNLOAD_BYTES = 64 * 1024 ** 2
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 32 * 1024 ** 2
MAX_EXTRACTED_BYTES = 512 * 1024 ** 2
_CHUNK_BYTES = 1024 ** 2

_fetch_lock = threading.Lock()

# What reading a suggested tree raises for a tree that is not a valid one.
_TREE_ERRORS = (OSError, ValueError, yaml.YAMLError)


class SuggestedSelection(BaseModel):
    """The Suggested tab's choice: a suggestion, whether its tutorial cells
    start on the canvas, and the complete runner drafts ``{runner, files}``
    to write into its generator cells."""
    model_config = ConfigDict(extra="forbid")
    id: str
    tutorial: bool = True
    runners: list[dict] = Field(default_factory=list)


@dataclass(frozen=True)
class Source:
    """A resolved suggested source: the tree to read, its kind
    (``bundled``, ``folder`` or ``github``), the path described to the page
    (the directory, or for ``github`` the URL), when a ``github`` tree was
    fetched, and why a refresh failed."""
    root: Path
    kind: str
    path: str
    fetched_at: str | None = None
    error: str | None = None

    def describe(self) -> dict:
        return {"kind": self.kind, "path": self.path, "fetched_at": self.fetched_at}

    def provenance(self):
        """The provenance source a cell admitted from this tree records."""
        from provenance import Source as ProvenanceSource
        return ProvenanceSource(self.kind, self.path)


# ---------------------------------------------------------------------------
# The source: a folder override, the fetched release content, or the bundled tree
# ---------------------------------------------------------------------------

def override() -> str | None:
    """The folder the user chose, or None when there is none."""
    from database import read_global_json
    value = read_global_json(SOURCE_KEY)
    return value.get("path") if isinstance(value, dict) and isinstance(value.get("path"), str) else None


def set_source(path: str | None) -> dict:
    """Use the folder ``path`` (absolute, holding ``suggested/``) as the
    suggested source, or for None drop the folder; answers the new
    catalog. The folder's suggestions are read before anything is saved:
    a folder that is not one, or whose suggestions cannot be read, raises
    ValueError and the previous source stays."""
    from collimate._host.packaging.suggested import SUGGESTED_DIR
    from database import write_global_json
    if path is None:
        write_global_json(SOURCE_KEY, None)
        return catalog(refresh=False)
    if not os.path.isabs(path):
        raise ValueError("The folder must be an absolute path")
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        raise ValueError(f"Not a folder: {path}")
    if not os.path.isdir(os.path.join(path, SUGGESTED_DIR)):
        raise ValueError(f"No {SUGGESTED_DIR}/ folder in {path}")
    answer = _catalog(Source(Path(path), "folder", path))
    if answer["error"] is not None:
        raise ValueError(answer["error"])
    write_global_json(SOURCE_KEY, {"path": path})
    return answer


def _bundled_root() -> Path:
    """The content tree installed with the app."""
    import genesis
    return Path(genesis.bundled_dir("content"))


def _cached() -> tuple[Path, str] | None:
    """The pointer's content tree and when it was fetched, or None without
    one, with one this app version did not fetch, or with a pointer at
    anything but a content tree."""
    from app_version import app_version
    from collimate._host.packaging.suggested import SUGGESTED_DIR
    try:
        with open(POINTER, encoding="utf-8") as handle:
            pointer = json.load(handle)
        tree = (Path(CACHE_DIR) / pointer["dir"]).absolute()
        fetched_at = pointer["fetched_at"]
        fetched_by = pointer["app_version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if fetched_by != app_version():
        return None
    if not isinstance(fetched_at, str) or not (tree / SUGGESTED_DIR).is_dir():
        return None
    return tree, fetched_at


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)


class _ArchiveRefused(ValueError):
    """The fetched archive breaks a cap or the ``content.zip`` format; the
    message is the short reason the page shows."""


def _download(target) -> None:
    received = 0
    with _http_client() as client, client.stream("GET", GITHUB_CONTENT_ZIP) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > MAX_DOWNLOAD_BYTES:
                raise _ArchiveRefused(f"archive larger than {MAX_DOWNLOAD_BYTES} bytes")
            target.write(chunk)


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    """The member's path below the zip root; refuses one that could land
    elsewhere, or that is not a plain stored or deflated file or folder."""
    name = info.filename
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or stat.S_ISLNK(info.external_attr >> 16)):
        raise _ArchiveRefused("archive holds an unsafe member")
    if info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise _ArchiveRefused("archive holds an encrypted or unsupported member")
    return path


def _extract(archive: Path, tree: Path) -> None:
    """Unpack the ``content.zip`` at ``archive`` into ``tree``, checking
    every member before writing any and counting the bytes as they are
    written (a member's declared size is not trusted)."""
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_MEMBERS:
            raise _ArchiveRefused(f"archive holds more than {MAX_MEMBERS} members")
        paths = [(info, _member_path(info)) for info in members]
        if {path.parts[0] for _, path in paths} != _CONTENT_FOLDERS:
            raise _ArchiveRefused("archive root must hold generators/, renderers/ and suggested/ only")
        total = 0
        for info, path in paths:
            target = tree.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with bundle.open(info) as source, open(target, "wb") as out:
                while chunk := source.read(_CHUNK_BYTES):
                    written += len(chunk)
                    total += len(chunk)
                    if written > MAX_MEMBER_BYTES:
                        raise _ArchiveRefused(f"archive member larger than {MAX_MEMBER_BYTES} bytes")
                    if total > MAX_EXTRACTED_BYTES:
                        raise _ArchiveRefused(f"archive expands past {MAX_EXTRACTED_BYTES} bytes")
                    out.write(chunk)


def _sweep_partials() -> None:
    """Delete the download and staged pointer an interrupted fetch left."""
    for entry in Path(CACHE_DIR).iterdir():
        if entry.name.startswith(_PARTIAL_PREFIX):
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


def fetch_github() -> None:
    """Fetch the latest release's ``content.zip`` into a fresh cache tree and
    point at it, recording the app version that fetched it. Raises on any
    failure, leaving the previous tree in place."""
    from app_version import app_version
    os.makedirs(CACHE_DIR, exist_ok=True)
    with _fetch_lock:
        _sweep_partials()
        previous = None
        try:
            with open(POINTER, encoding="utf-8") as handle:
                previous = json.load(handle).get("dir")
        except (OSError, ValueError, AttributeError):
            pass
        token = uuid.uuid4().hex
        name = f"{_TREE_PREFIX}{token}"
        tree = Path(CACHE_DIR) / name
        archive = Path(CACHE_DIR) / f"{_PARTIAL_PREFIX}{token}.zip"
        staged = Path(CACHE_DIR) / f"{_PARTIAL_PREFIX}{token}.json"
        try:
            with open(archive, "wb") as download:
                _download(download)
            tree.mkdir()
            _extract(archive, tree)
            staged.write_text(json.dumps({"dir": name, "fetched_at": datetime.now(timezone.utc).isoformat(),
                                          "app_version": app_version()}), encoding="utf-8")
            os.replace(staged, POINTER)
        except BaseException:
            shutil.rmtree(tree, ignore_errors=True)
            raise
        finally:
            archive.unlink(missing_ok=True)
            staged.unlink(missing_ok=True)
        keep = {name, previous}
        for entry in Path(CACHE_DIR).iterdir():
            if entry.name.startswith(_TREE_PREFIX) and entry.is_dir() and entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)


_FETCH_ERRORS = (httpx.HTTPError, zipfile.BadZipFile, zlib.error, EOFError, OSError, ValueError)


def _fetch_error(exc: BaseException) -> str:
    """The short line the page shows for a failed fetch (the raw error is
    logged, not shown)."""
    if isinstance(exc, httpx.HTTPStatusError):
        reason = f"HTTP {exc.response.status_code}"
    elif isinstance(exc, httpx.TimeoutException):
        reason = "timed out"
    elif isinstance(exc, httpx.HTTPError):
        reason = "connection failed"
    elif isinstance(exc, _ArchiveRefused):
        reason = str(exc)
    elif isinstance(exc, OSError):
        reason = "cannot write the local cache"
    else:
        reason = "the archive could not be unpacked"
    return f"Cannot fetch the latest release's content from GitHub ({reason})"


def resolve(*, refresh: bool) -> Source:
    """The tree suggestions are read from: the folder override, else the
    fetched release content, else the bundled tree. ``refresh`` fetches the
    release content first (not for a folder); only a refresh touches the
    network."""
    folder = override()
    if folder is not None:
        return Source(Path(folder), "folder", folder)
    error = None
    if refresh:
        try:
            fetch_github()
        except _FETCH_ERRORS as exc:
            logger.warning("suggested: cannot fetch %s: %s", GITHUB_CONTENT_ZIP, exc)
            error = _fetch_error(exc)
    cached = _cached()
    if cached is None:
        root = _bundled_root()
        return Source(root, "bundled", str(root), error=error)
    return Source(cached[0], "github", GITHUB_CONTENT_ZIP, cached[1], error)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

def _asset_specs(root: Path, suggestion):
    """The suggestion's cells of both asset trees, generators first."""
    from collimate._host.packaging.suggested import read_suggested_cell
    return [read_suggested_cell(root, ref, kind)
            for kind, refs in (("generator", suggestion.generators), ("renderer", suggestion.renderers))
            for ref in refs]


def _entry(root: Path, suggestion) -> dict:
    import runners

    def refs(items):
        return [{"name": ref.name, "folder": ref.folder} for ref in items]

    specs = _asset_specs(root, suggestion)
    index = runners.generator_index(specs)
    packaged = [draft for spec in specs for draft in runners.packaged_runners(spec)]
    by_id = {}
    for draft in packaged:
        runner = draft.get("runner", {})
        runner_id = runner.get("id") if isinstance(runner, dict) else None
        if not isinstance(runner_id, str) or not runner_id.strip():
            raise ValueError("A packaged runner needs a nonempty id")
        if runner_id in by_id:
            raise ValueError(f"Packaged runner id {runner_id!r} appears twice")
        by_id[runner_id] = draft
    for runner_id, refs_for_runner in suggestion.quick_fields.items():
        draft = by_id.get(runner_id)
        if draft is None:
            raise ValueError(f"quick_fields names unknown packaged runner {runner_id!r}")
        definition = draft["runner"]
        generator_id = definition.get("generator")
        if not isinstance(generator_id, str):
            raise ValueError(f"quick_fields runner {runner_id!r} needs a generator id")
        gi = index.get(generator_id)
        if gi is None:
            raise ValueError(f"quick_fields runner {runner_id!r} has unknown generator {generator_id!r}")
        fields = {field["key"]: field for field in gi["fields"]}
        roles = gi["manifest"].get("account_requirements", {})
        for ref in refs_for_runner:
            if "account" in ref and ref["account"] not in roles:
                raise ValueError(f"quick_fields runner {runner_id!r} names unknown account role {ref['account']!r}")
            if "setting" in ref:
                key = ref["setting"]
                field = fields.get(key)
                if field is None:
                    raise ValueError(f"quick_fields runner {runner_id!r} names unknown main-config field {key!r}")
                if field.get("account_role"):
                    raise ValueError(f"quick_fields runner {runner_id!r} field {key!r} comes from an account; use its role")
                if field.get("top_level") or field.get("type") not in {"text", "textarea", "number", "checkbox", "dropdown"}:
                    raise ValueError(f"quick_fields runner {runner_id!r} field {key!r} is not a scalar main-config control")
            if "prompt" in ref and not isinstance(definition.get("prompt"), str):
                raise ValueError(f"quick_fields runner {runner_id!r} has no prompt text")
    return {
        "id": suggestion.id, "name": suggestion.name, "description": suggestion.description,
        "generators": refs(suggestion.generators), "renderers": refs(suggestion.renderers),
        "tutorial": refs(suggestion.tutorial),
        "runner_generators": [runners.generator_payload(gi) for gi in index.values()],
        "runners": [{"draft": draft, "quick_fields": suggestion.quick_fields.get(draft["runner"]["id"], [])}
                    for draft in packaged],
    }


def catalog(*, refresh: bool) -> dict:
    """``GET /api/suggested``: the source, its suggestions with their runner
    generators and drafts, and any error."""
    return _catalog(resolve(refresh=refresh))


def _catalog(source: Source) -> dict:
    """The catalog of ``source``; a tree that cannot be read answers its
    error and no suggestions."""
    from collimate._host.packaging.suggested import read_suggestions
    suggestions: list[dict] = []
    error = source.error
    try:
        suggestions = [_entry(source.root, suggestion) for suggestion in read_suggestions(source.root)]
    except _TREE_ERRORS as exc:
        suggestions = []
        error = f"Cannot read the suggestions in {source.root}: {exc}"
    return {"source": source.describe(), "suggestions": suggestions, "error": error}


def _suggestion(root: Path, suggestion_id: str):
    from collimate._host.packaging.suggested import read_suggestions
    found = next((s for s in read_suggestions(root) if s.id == suggestion_id), None)
    if found is None:
        raise ValueError(f"Unknown suggestion {suggestion_id!r}")
    return found


def current_root() -> Path:
    """The tree to read for a request that names a suggestion (the source
    the catalog answers from, never fetched)."""
    return resolve(refresh=False).root


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _with_drafts(cells, drafts: list[dict]) -> None:
    """Replace every cell's runner packages with the complete ``drafts``,
    each written into the cell of its generator (in place)."""
    import runners
    index = runners.generator_index(cells)
    prepared = {str(cell.id): runners.without_runners(cell.files) for cell in cells}
    ids = []
    for draft in drafts:
        if not isinstance(draft, dict) or set(draft) - {"runner", "files", "revision"}:
            raise ValueError("Invalid runner draft")
        definition, files = draft.get("runner", {}), draft.get("files", [])
        if not isinstance(definition, dict) or not isinstance(definition.get("generator"), str):
            raise ValueError("Runner draft needs a definition and generator id")
        gi = index.get(definition["generator"])
        if gi is None:
            raise ValueError("Runner target generator is not included in the suggestion")
        # Resolve selected accounts before the runnable check: their public
        # values can change derived settings and conditional requirements.
        runners.validate_definition(gi, definition, files)
        from accounts import resolve_public_accounts
        public = resolve_public_accounts(definition.get("accounts", {}),
                                         gi["manifest"].get("account_requirements", {}))
        effective = {**definition, "settings": runners._account_settings(
            gi["main_config"], gi["fields"], gi["defaults"], definition["settings"], public)}
        runners.validate_definition(gi, effective, files, runnable=True)
        occupied = prepared[str(gi["cell"].id)]
        occupied.extend(runners.runner_vfiles(definition, files, occupied_files=occupied))
        ids.append(definition["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("Runner ids must be unique")
    for cell in cells:
        cell.files = prepared[str(cell.id)]


def seed_suggested(ch, selection: SuggestedSelection) -> list[dict]:
    """Seed the unregistered channel ``ch`` from a suggestion; returns the
    held packages. The cells of both asset trees pass package admission
    into the Capabilities stack together, with the drafts already in their
    generator cells; tutorial cells become one main stack each. Everything
    publishes through one ImportPlan."""
    from collimate._host.packaging.suggested import read_suggested_cell, read_tutorial_cell
    from channel_sources import admission_errors, admit_packages, specs_to_cells
    from genesis import package_cell_vfiles
    from import_plan import ImportPlan
    from models import Cell, Stack

    source = resolve(refresh=False)
    root = source.root
    with admission_errors():
        try:
            suggestion = _suggestion(root, selection.id)
            incoming = specs_to_cells([
                read_suggested_cell(root, ref, kind)
                for kind, refs in (("generator", suggestion.generators), ("renderer", suggestion.renderers))
                for ref in refs])
            tutorial_specs = [read_tutorial_cell(root, ref) for ref in suggestion.tutorial] if selection.tutorial else []
        except yaml.YAMLError as exc:
            raise ValueError(f"Cannot read the suggestion in {root}: {exc}") from exc
        _with_drafts(incoming, selection.runners)
        tutorial = []
        for spec in tutorial_specs:
            files = package_cell_vfiles(spec)
            tutorial.append(Stack(name=spec.name, cells=[
                Cell(name=spec.name, icon=spec.icon or None, files=files,
                     pinned=files[0].name if len(files) == 1 else None)]))
        plan = ImportPlan(ch, new_channel=True)
        plan.candidate.main_stacks.extend(tutorial)
        skipped = admit_packages(ch, plan, incoming, source.provenance())
        plan.publish()
    return skipped
