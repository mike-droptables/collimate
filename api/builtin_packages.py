"""The app's built-in packages: the handlers and renderers the app cannot
function without, read from the bundled built-ins tree (``genesis.BUILTIN_DIR``:
the ``collimate`` wheel's ``collimate_builtins/``, or a checkout's
``builtins/``) and served for every channel.

The app names its built-ins in one committed list, ``api/builtins.json``
(:data:`LIST_PATH`): each package's kind, id and content hash
(``collimate._host.package_hash.content_hash`` of its files as the build reads
them). The build is that list's packages and nothing else: a tree that holds
a package the list does not name, lacks one it names, or holds one whose bytes
hash otherwise refuses to load (:func:`build`), so every privilege below
(the reserved handler types, a handler's self-accepted ``derived:`` hash, the
locked surfaces, the admission holds and the read-only cards) belongs to a
listed entry. An intentional edit of the tree is one step: edit, run
``build/sync_builtins.py``, commit both.

The tree is laid out like ``renderers/`` (parent folders are cells), and each
of its cells is a CARD at the head of every channel's Capabilities stack: a
real layout cell (``Cell.builtin``, a deterministic id per channel, inserted
by ``genesis.ensure_capabilities_stack``) whose package bytes come from the
build, never from the channel database. A card is never removed, moved, renamed,
duplicated, exported, copied, suggested or reset, and its package bytes are
never written.

The one channel-owned part is the OVERLAY of a card that holds handlers (the
Definitions card), kept as that card's own channel files (:func:`overlay_path`
is the rule every writer consults):

- ``<handler>/examples/<folder>/**``: examples the channel added to a
  built-in handler;
- ``<handler>/derived/skills/**``: its regenerated skills, a complete tree
  of the non-pinned files (a regenerate work stack starts from the effective
  skills, so a deletion is real), offered from that work stack;
- ``<handler>/additions.yaml``: ``{examples: [{folder, description, expect,
  feedback}], skills: <accepted skills hash>}``, written by the host only.

Validation is always the build's: the ``.handler`` manifest, ``verify.ts``,
the types and the pinned skill files (:func:`pinned`).

A card's file listing and bodies are a read-side projection
(:func:`projection`) of the vendored bytes under its overlay rows; vendored
entries never enter ``Cell.files`` or the channel database.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from cell_placement import BUILTIN_REFUSAL

# The host writer of a card's overlay and the writer its vendored entries
# carry on the wire.
WRITER = "host:builtin"
ADDITIONS = "additions.yaml"

# The app's list of its built-ins and the name a mismatch reads under.
LIST_PATH = Path(__file__).with_name("builtins.json")
LIST_NAME = "api/builtins.json"

# Extensions the app's own surfaces own: host protocols (chat, terminal,
# website, webview), the run card and the renderer preview. Each is locked to
# its built-in renderer: a user renderer never claims one and a preference
# never moves one.
SURFACE_EXTENSIONS = frozenset({".run", ".chat", ".terminal", ".webview", ".website",
                                ".renderer_example"})

# The ``handler`` built-in's skills that are app machinery (the briefs of
# every Build, Regenerate skills and Create work stack), pinned like asset
# folders.
_MACHINERY_SKILLS = {"handler": frozenset({"build", "regenerate", "create"})}


@dataclass(frozen=True)
class BuiltinCell:
    """One card: the build's cell (``spec_id`` from ``read_asset_cells``),
    its display name and icon and its files (cell-relative path → bytes)."""
    spec_id: str
    name: str
    icon: str | None
    files: Mapping[str, bytes]
    languages: Mapping[str, str]
    binary: frozenset[str]
    digests: Mapping[str, str]


@dataclass(frozen=True)
class BuiltinHandler:
    """A built-in handler: its validated manifest (examples as the build
    ships them), the card holding it and its package files (relative to the
    package folder, which is named by the id). ``skills_hash`` is the
    :func:`owned_hash` of the build's skills: the hash a channel that never
    regenerated them reports, and the one a regeneration that changes
    nothing reproduces."""
    manifest: dict
    cell: str
    files: Mapping[str, bytes]
    asset_skills: frozenset[str]
    skills_hash: str

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def definition_path(self) -> str:
        """The ``.handler`` file, relative to its card."""
        return f"{self.id}/{self.id}.handler"

    @property
    def verify_ts(self) -> bytes:
        return self.files["derived/verify.ts"]

    @property
    def example_folders(self) -> frozenset[str]:
        """The first segments of the build's example folders."""
        return frozenset({path.split("/")[1] for path in self.files
                          if path.startswith("examples/") and path.count("/") >= 2}
                         | {example["folder"].split("/")[0] for example in self.manifest["examples"]})

    def skills(self) -> dict[str, bytes]:
        """The build's skills, keyed relative to ``derived/`` (``skills/…``)."""
        return {path[len("derived/"):]: body for path, body in self.files.items()
                if path.startswith("derived/skills/")}


@dataclass(frozen=True)
class BuiltinRenderer:
    """A built-in renderer and its bundle, compiled as admission compiles a
    renderer: ``source_hash`` is its identity under the compiler contract,
    the one the source route serves (:func:`publish_bundles`)."""
    manifest: dict
    cell: str
    prefix: str
    entrypoint: str
    files: Mapping[str, bytes]
    source_hash: str
    bundle: bytes

    @property
    def id(self) -> str:
        return self.manifest["id"]


@dataclass(frozen=True)
class Build:
    cells: tuple[BuiltinCell, ...]
    handlers: Mapping[str, BuiltinHandler]
    renderers: Mapping[str, BuiltinRenderer]
    bodies: Mapping[str, bytes] = field(default_factory=dict)


@lru_cache(maxsize=4)
def _read_list(path: str) -> Mapping[tuple[str, str], str]:
    """The list at ``path`` (the app's is :data:`LIST_PATH`): each
    built-in's kind and id to the content hash of its files."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{LIST_NAME} must be a list of built-ins")
    pins = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"kind", "id", "content_hash"}
                or entry["kind"] not in ("handler", "renderer") or not isinstance(entry["id"], str)
                or not isinstance(entry["content_hash"], str)
                or re.fullmatch(r"[0-9a-f]{64}", entry["content_hash"]) is None):
            raise ValueError(f"{LIST_NAME} entries are {{kind: handler or renderer, id, content_hash}}: {entry!r}")
        if (entry["kind"], entry["id"]) in pins:
            raise ValueError(f"{LIST_NAME} names the {entry['kind']} {entry['id']!r} twice")
        pins[entry["kind"], entry["id"]] = entry["content_hash"]
    return MappingProxyType(pins)


@dataclass(frozen=True)
class _Package:
    """One package of the tree as the build reads it: its kind, manifest id
    and parsed definition, the card holding it (``spec_id``), its folder,
    its definition file (card-relative) and its files (folder-relative)."""
    kind: str
    id: str
    manifest: dict
    cell: str
    prefix: str
    definition: str
    files: Mapping[str, bytes]


def _package_files(kind: str, files: Mapping[str, bytes], prefix: str) -> dict[str, bytes]:
    """A package's files as the build serves them, relative to its folder: a
    renderer's are its compilation's sources (no run folders or compiled
    Python)."""
    from run_folders import is_run_path
    own = {path[len(prefix) + 1:]: body for path, body in files.items()
           if path.startswith(prefix + "/") and not is_run_path(path)}
    if kind == "renderer":
        own = {path: body for path, body in own.items()
               if "_runs" not in path.split("/") and not path.endswith((".pyc", ".pyo"))}
    return own


def _read_tree(root: str) -> tuple[tuple[BuiltinCell, ...], tuple[_Package, ...], dict[str, bytes]]:
    """The built-in tree at ``root`` as the build reads it: its cards, its
    packages in tree order and every file's bytes by digest. Refuses a
    symlink, a definition problem, a definition that is not a handler or a
    renderer or has no id, one kind and id twice, and a card file that is
    neither its ``cell.yaml`` nor one of its packages' files: a card holds
    its packages and nothing else."""
    from collimate.definitions import discover_definitions
    from collimate._host.packaging import CELL_MANIFEST
    from collimate._host.packaging.asset_cells import read_asset_cells

    base = Path(root)
    if base.is_symlink() or any(path.is_symlink() for path in base.rglob("*")):
        raise ValueError(f"The built-in packages in {root} must not hold symlinks")
    cells, packages, bodies, seen = [], [], {}, set()
    for spec in read_asset_cells(base, "renderer"):
        files, languages, binary, digests = {}, {}, set(), {}
        for file in spec.files:
            if any(part in ("", ".", "..") for part in file.name.split("/")):
                raise ValueError(f"Invalid built-in file path: {file.name}")
            body = file.data if file.data is not None else file.content.encode("utf-8")
            files[file.name] = body
            languages[file.name] = "binary" if file.data is not None else _language(file.name)
            if file.data is not None:
                binary.add(file.name)
            digests[file.name] = hashlib.sha256(body).hexdigest()
            bodies[digests[file.name]] = body
        cells.append(BuiltinCell(spec.id, spec.name, spec.icon or None, MappingProxyType(files),
                                 MappingProxyType(languages), frozenset(binary), MappingProxyType(digests)))
        discovered = discover_definitions(files)
        if discovered.problems:
            raise ValueError("; ".join(problem.message for problem in discovered.problems))
        held = {CELL_MANIFEST}
        for package in discovered.packages:
            if package.kind not in ("handler", "renderer"):
                raise ValueError(f"The built-in tree holds handlers and renderers only: {package.definition_path}")
            own = _package_files(package.kind, files, package.prefix)
            manifest = yaml.safe_load(own[package.definition_path[len(package.prefix) + 1:]])
            if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str):
                raise ValueError(f"Invalid built-in definition {package.definition_path}")
            if (package.kind, manifest["id"]) in seen:
                raise ValueError(f"The built-in tree holds the {package.kind} {manifest['id']!r} twice")
            seen.add((package.kind, manifest["id"]))
            packages.append(_Package(package.kind, manifest["id"], manifest, spec.id, package.prefix,
                                     package.definition_path, MappingProxyType(own)))
            held.update(f"{package.prefix}/{path}" for path in own)
        stray = sorted(set(files) - held)
        if stray:
            raise ValueError(f"Built-in card {spec.name!r} holds files outside its packages: {', '.join(stray)}")
    return tuple(cells), tuple(packages), bodies


def tree_listing(root: str) -> list[dict]:
    """What the list must hold for the built-in tree at ``root``: each
    package's kind, id and content hash (``collimate._host.package_hash``, over
    its files as the build reads them), sorted by kind and id. The updater,
    ``build/sync_builtins.py``, writes it to :data:`LIST_PATH`."""
    from collimate._host.package_hash import content_hash
    _cells, packages, _bodies = _read_tree(root)
    return [{"kind": package.kind, "id": package.id, "content_hash": content_hash(package.files)}
            for package in sorted(packages, key=lambda package: (package.kind, package.id))]


def _unlisted(packages: tuple[_Package, ...], pins: Mapping[tuple[str, str], str]) -> list[str]:
    """Each way the tree's ``packages`` differ from the list's ``pins``."""
    from collimate._host.package_hash import content_hash
    found = {(package.kind, package.id): content_hash(package.files) for package in packages}
    return ([f"the tree holds the {kind} {identifier!r}, which the list does not name"
             for kind, identifier in sorted(found.keys() - pins.keys())]
            + [f"the list names the {kind} {identifier!r}, which the tree does not hold"
               for kind, identifier in sorted(pins.keys() - found.keys())]
            + [f"the {kind} {identifier!r} hashes {found[kind, identifier]}, the list pins {pins[kind, identifier]}"
               for kind, identifier in sorted(found.keys() & pins.keys())
               if found[kind, identifier] != pins[kind, identifier]])


@lru_cache(maxsize=4)
def _load(root: str, list_path: str) -> Build:
    """Read, check and compile the built-in tree once, as the list at
    ``list_path`` names it. A tree that is not the list's
    (:func:`_unlisted`) refuses to load before any of it is validated or
    compiled; so does a broken build (a symlink, a definition problem, a
    handler whose accepted derived folder does not match, a renderer that
    does not compile, a duplicate id), rather than serve quietly."""
    from collimate.handlers import derived_hash, validate_handler
    from collimate._host.renderer_bundle import bundle_renderer

    cells, packages, bodies = _read_tree(root)
    problems = _unlisted(packages, _read_list(list_path))
    if problems:
        raise ValueError(f"The built-in tree does not match {LIST_NAME}: {'; '.join(problems)} "
                         "(for an intended edit run build/sync_builtins.py and commit the list with it)")
    # Every package is a listed entry from here on, and only a listed entry
    # declares the reserved types and accepts its own derived folder.
    handlers, renderers = {}, {}
    for package in packages:
        if package.kind == "handler":
            manifest = validate_handler(package.manifest, allow_reserved=True)
            derived = {path[len("derived/"):]: body for path, body in package.files.items()
                       if path.startswith("derived/")}
            if manifest["id"] != package.prefix:
                raise ValueError(f"Built-in handler {manifest['id']!r} must be the one package "
                                 f"in its own folder {package.prefix!r}")
            if "verify.ts" not in derived or derived_hash(derived) != manifest.get("derived"):
                raise ValueError(f"Built-in handler {manifest['id']!r} has no matching accepted derived folder")
            assets = frozenset(path.split("/")[2] for path in package.files
                               if path.startswith("derived/skills/") and path.split("/")[3:4] == ["assets"])
            owned = {path: body for path, body in derived.items() if path.startswith("skills/")
                     and not _pinned(manifest["id"], assets, "derived/" + path)}
            handlers[manifest["id"]] = BuiltinHandler(manifest, package.cell, package.files, assets,
                                                      derived_hash(owned))
        else:
            manifest = package.manifest
            if not isinstance(manifest.get("entrypoint"), str):
                raise ValueError(f"Invalid built-in renderer {package.definition}")
            compiled = bundle_renderer(dict(package.files), entrypoint=manifest["entrypoint"])
            renderers[manifest["id"]] = BuiltinRenderer(
                manifest, package.cell, package.prefix, manifest["entrypoint"], package.files,
                compiled.source_hash, compiled.code.encode("utf-8"))
    return Build(cells, MappingProxyType(handlers), MappingProxyType(renderers), MappingProxyType(bodies))


def _language(path: str) -> str:
    from models import language_for
    return language_for(path)


def build() -> Build:
    """The bundled build: the tree at ``genesis.BUILTIN_DIR`` as the list
    (:data:`LIST_PATH`) names it, read once per tree and list."""
    import genesis
    return _load(genesis.BUILTIN_DIR, str(LIST_PATH))


def publish_bundles() -> None:
    """Store each built-in renderer's bundle as its server bundle (startup),
    so it is served from the first request."""
    from renderer_bundles import publish_server_bundle
    for renderer in build().renderers.values():
        publish_server_bundle(renderer.source_hash, renderer.bundle)


def builtin_cells() -> tuple[BuiltinCell, ...]:
    return build().cells


def builtin_handlers() -> Mapping[str, BuiltinHandler]:
    return build().handlers


def builtin_renderers() -> Mapping[str, BuiltinRenderer]:
    return build().renderers


def builtin_types() -> dict[str, str]:
    """Every built-in handler type (casefold) → the handler that owns it."""
    return {kind["ext"]: handler.id for handler in build().handlers.values()
            for kind in handler.manifest["types"]}


def vendored_body(digest: str) -> bytes | None:
    """The vendored bytes of a projected entry's ``body_hash``."""
    return build().bodies.get(digest)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def builtin_cell_id(channel_uuid: str, spec_id: str) -> str:
    """A card's id in a channel: deterministic in the channel and the
    build's cell (every other cell's id is random)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"builtin-{channel_uuid}-{spec_id}"))


def card_cell_id(channel, spec_id: str) -> str:
    from state import channel_db_name
    return builtin_cell_id(channel_db_name(channel), spec_id)


def card_spec(channel, cell) -> BuiltinCell | None:
    """The build's cell behind the card ``cell`` of ``channel``, or None for
    any other cell."""
    if cell is None or not cell.builtin:
        return None
    return next((spec for spec in build().cells if card_cell_id(channel, spec.spec_id) == str(cell.id)), None)


def card_for(channel, cell_id) -> BuiltinCell | None:
    """The card spec whose id in ``channel`` (a Channel or its id) is
    ``cell_id``; None for any other cell. Card ids are deterministic and no
    other cell gets one, so this reads no layout and takes no lock."""
    from database import canonical_channel_id
    from models import Channel
    try:
        channel_uuid = canonical_channel_id(channel.id if isinstance(channel, Channel) else channel)
    except ValueError:
        return None
    return next((spec for spec in build().cells
                 if builtin_cell_id(channel_uuid, spec.spec_id) == str(cell_id)), None)


def handler_card(channel) -> str:
    """The id of the card holding the built-in handlers in ``channel``."""
    cells = {handler.cell for handler in build().handlers.values()}
    spec = next(spec for spec in build().cells if spec.spec_id in cells)
    return card_cell_id(channel, spec.spec_id)


def renderer_card(channel, renderer: BuiltinRenderer) -> str:
    return card_cell_id(channel, renderer.cell)


# ---------------------------------------------------------------------------
# The overlay
# ---------------------------------------------------------------------------

def pinned(handler_id: str, path: str) -> bool:
    """Whether the package-relative ``path`` of a built-in handler is always
    the build's bytes (never writable, never hidden by regenerated skills):
    ``derived/verify.ts``; every skill folder holding an ``assets/`` folder,
    whole (``catalog.skills()`` keeps serving the build's assets); every
    ``references/`` folder (byte-pinned mirrors of the canonical guides); and
    the ``handler`` built-in's ``build``, ``regenerate`` and ``create``
    skills."""
    return _pinned(handler_id, build().handlers[handler_id].asset_skills, path)


def _pinned(handler_id: str, asset_skills: frozenset[str], path: str) -> bool:
    if path == "derived/verify.ts":
        return True
    if not path.startswith("derived/skills/"):
        return False
    parts = path.split("/")[2:]
    return (parts[0] in asset_skills or "references" in parts[1:-1]
            or parts[0] in _MACHINERY_SKILLS.get(handler_id, frozenset()))


def owned_skills(handler_id: str, skills: Mapping[str, bytes]) -> dict[str, bytes]:
    """The non-pinned files of ``skills`` (keyed ``skills/…``, relative to
    ``derived/``): the part of a built-in handler's skills a channel owns."""
    return {path: body for path, body in skills.items() if not pinned(handler_id, "derived/" + path)}


def owned_hash(handler_id: str, skills: Mapping[str, bytes]) -> str:
    """The skills hash a regeneration is accepted under: the ``derived_hash``
    of its :func:`owned_skills` only, so an app upgrade that changes a pinned
    file (a re-synced reference, an asset) leaves an accepted
    regeneration accepted, and one that changes nothing hashes like the
    build's skills (``BuiltinHandler.skills_hash``)."""
    from collimate.handlers import derived_hash
    return derived_hash(owned_skills(handler_id, skills))


def complete_skills(handler_id: str, skills: Mapping[str, bytes]) -> dict[str, bytes]:
    """``skills`` (keyed ``skills/…``, relative to ``derived/``) as a whole
    skills tree: their non-pinned files and the CURRENT build's pinned ones."""
    vendored = build().handlers[handler_id].skills()
    return owned_skills(handler_id, skills) | {path: body for path, body in vendored.items()
                                               if pinned(handler_id, "derived/" + path)}


def overlay_path(card: BuiltinCell, path: str) -> bool:
    """Whether ``path`` belongs to ``card``'s overlay: one of the card's
    built-in handlers' added example folders (any folder the build does not
    ship), regenerated non-pinned skill files or ``additions.yaml``. The one
    rule every writer of a card consults."""
    head, _, rest = path.partition("/")
    handler = build().handlers.get(head)
    if handler is None or handler.cell != card.spec_id or not rest:
        return False
    if rest == ADDITIONS:
        return True
    if rest.startswith("derived/skills/"):
        return not pinned(head, rest)
    parts = rest.split("/")
    return len(parts) >= 3 and parts[0] == "examples" and parts[1] not in handler.example_folders


def regenerated(handler_id: str, paths) -> bool:
    """Whether the overlay ``paths`` hold the handler's regenerated skills."""
    prefix = f"{handler_id}/derived/skills/"
    return any(path.startswith(prefix) for path in paths)


def _vendored_descriptor(card: BuiltinCell, path: str) -> dict:
    return {"path": path, "file_revision": 0, "body_hash": card.digests[path],
            "deleted": False, "writer_id": WRITER, "edit_seq": 0, "language": card.languages[path],
            "encoding": "binary" if path in card.binary else "utf-8", "builtin": True}


def projection(card: BuiltinCell, overlay: list[dict]) -> list[dict]:
    """The card's files: its overlay rows (``persistence.snapshot_files``)
    over the vendored entries, one per path, sorted. A vendored entry has
    revision 0, the build's ``body_hash``, writer ``host:builtin`` and
    ``builtin: True``. While the overlay holds a handler's regenerated
    skills, the build's non-pinned skill files of that handler are hidden
    (the overlay is the complete tree); pinned paths always show the build's
    bytes."""
    rows = {row["path"]: row for row in overlay}
    hidden = {handler.id for handler in build().handlers.values()
              if handler.cell == card.spec_id and regenerated(handler.id, rows)}
    merged = dict(rows)
    for path in card.files:
        head, _, rest = path.partition("/")
        if (_holds_handler(card, head) and pinned(head, rest)) or (
                path not in rows and not (head in hidden and rest.startswith("derived/skills/"))):
            merged[path] = _vendored_descriptor(card, path)
    return [merged[path] for path in sorted(merged)]


def _holds_handler(card: BuiltinCell, handler_id: str) -> bool:
    handler = build().handlers.get(handler_id)
    return handler is not None and handler.cell == card.spec_id


def projected_entry(card: BuiltinCell, overlay: list[dict], path: str) -> dict | None:
    """The vendored descriptor ``path`` shows on the card, or None when it
    is not one (an overlay row, hidden, or not in the build)."""
    entry = next((row for row in projection(card, overlay) if row["path"] == path), None)
    return entry if entry is not None and entry.get("builtin") else None


# ---------------------------------------------------------------------------
# additions.yaml
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Additions:
    """A handler's channel additions: its added examples, in order, and the
    hash of its accepted regenerated skills (None: the build's)."""
    examples: tuple[dict, ...] = ()
    skills: str | None = None

    def dump(self) -> bytes:
        data: dict = {"examples": [dict(example) for example in self.examples]}
        if self.skills is not None:
            data["skills"] = self.skills
        return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def parse_additions(body: bytes | None) -> Additions:
    """Read an ``additions.yaml`` body (None: no additions). Raises
    ValueError for one that is not what the host writes."""
    if body is None:
        return Additions()
    data = yaml.safe_load(body)
    if not isinstance(data, dict) or set(data) - {"examples", "skills"}:
        raise ValueError("additions.yaml must map examples and skills")
    examples = data.get("examples") or []
    if not isinstance(examples, list):
        raise ValueError("additions.yaml examples must be a list")
    for example in examples:
        _check_example(example)
    skills = data.get("skills")
    if skills is not None and (not isinstance(skills, str) or len(skills) != 64):
        raise ValueError("additions.yaml skills must be an accepted skills hash")
    return Additions(tuple(dict(example) for example in examples), skills)


def _check_example(example) -> None:
    if not isinstance(example, dict) or set(example) != {"folder", "description", "expect", "feedback"}:
        raise ValueError("An added example needs folder, description, expect and feedback")
    if example["expect"] not in ("valid", "invalid"):
        raise ValueError("An example's expected verdict is valid or invalid")
    if not all(isinstance(example[key], str) for key in ("folder", "description", "feedback")):
        raise ValueError("An example's folder, description and feedback are text")


def new_example_folder() -> str:
    return "example-" + uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# Renderers: surfaces and admission holds
# ---------------------------------------------------------------------------

def surface_of(ext: str) -> str | None:
    """The surface extension ``ext`` is or ends with (casefold), or None."""
    if not isinstance(ext, str):
        return None
    folded = ext.casefold()
    return next((surface for surface in SURFACE_EXTENSIONS if folded.endswith(surface)), None)


def surface_owner(surface: str) -> str | None:
    """The built-in renderer that claims ``surface``, or None."""
    return next((renderer.id for renderer in build().renderers.values()
                 if any(isinstance(claim, str) and claim.casefold() == surface
                        for claim in renderer.manifest.get("extensions") or [])), None)


def preference_locked(key: str, renderer_id: str) -> bool:
    """Whether a renderer preference ``key → renderer_id`` would move a
    surface off its built-in renderer. The browser checks preferences before
    the extension table and matches a key as a plain case-folded suffix of
    the file name (no leading dot required), so a key is locked when some
    surface file name could end with it: the key ends with a surface
    (``x.run``) or a surface ends with it (``run``, ``un``). It may then
    name only that surface's built-in renderer (any built-in renderer for a
    surface no built-in renderer claims). ``*`` is the fallback, which never
    outranks a surface's own claim."""
    if not isinstance(key, str) or key == "*":
        return False
    folded = key.casefold()
    surfaces = [surface for surface in SURFACE_EXTENSIONS if surface.endswith(folded) or folded.endswith(surface)]
    for surface in surfaces:
        owner = surface_owner(surface)
        if (renderer_id != owner) if owner is not None else (renderer_id not in build().renderers):
            return True
    return False


def held_reason(kind: str, manifest: dict) -> str | None:
    """Why an incoming package collides with the app's built-ins (it is
    held, never admitted), or None. A renderer may not take a built-in id or
    claim a surface extension; a handler may not take a built-in id or type,
    nor a type on a surface extension (its ``render_as`` would move that
    surface off its built-in renderer)."""
    identifier = manifest.get("id")
    if kind == "renderer":
        if identifier in build().renderers:
            return f"Renderer id {identifier!r} belongs to the app's built-in renderer"
        claims = manifest.get("extensions")
        for claim in claims if isinstance(claims, list) else []:
            if surface_of(claim) is not None:
                return f"Renderer extension {claim} is a built-in renderer's surface"
    if kind == "handler":
        if identifier in build().handlers:
            return f"Handler id {identifier!r} belongs to the app's built-in handler"
        types = builtin_types()
        kinds = manifest.get("types")
        for value in kinds if isinstance(kinds, list) else []:
            ext = value.get("ext") if isinstance(value, dict) else None
            if isinstance(ext, str) and ext.casefold() in types:
                return f"Handler type {ext} is reserved for the built-in {types[ext.casefold()]} handler"
            if isinstance(ext, str) and surface_of(ext) is not None:
                return f"Handler type {ext} is a built-in renderer's surface"
    return None


def verifier_for(definition_path: str) -> bytes | None:
    """The built-in verifier of a definition file (``.generator``,
    ``.renderer``, ``.runner``, ``.handler``): admission always runs the
    build's."""
    from collimate.handlers import handler_for
    handlers = list(build().handlers.values())
    selected = handler_for([handler.manifest for handler in handlers], definition_path)
    return next((handler.verify_ts for handler in handlers if handler.manifest is selected), None)


__all__ = ["ADDITIONS", "Additions", "BUILTIN_REFUSAL", "BuiltinCell", "BuiltinHandler", "BuiltinRenderer",
           "LIST_NAME", "LIST_PATH", "SURFACE_EXTENSIONS", "WRITER", "build", "builtin_cell_id", "builtin_cells", "builtin_handlers",
           "builtin_renderers", "builtin_types", "card_cell_id", "card_for", "card_spec",
           "complete_skills",
           "handler_card", "held_reason", "new_example_folder", "overlay_path", "owned_hash", "owned_skills",
           "parse_additions",
           "pinned", "preference_locked", "projected_entry", "projection", "regenerated", "renderer_card",
           "surface_of", "surface_owner", "tree_listing", "vendored_body", "verifier_for"]
