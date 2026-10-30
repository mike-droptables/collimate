"""Work stacks for Build, Regenerate skills and Create.

Nothing runs on the Capabilities stack, so making or changing a capability
happens in a WORK STACK: a new standard stack, opened right of Capabilities
(at the end of the board while Capabilities is hidden) and selected, whose
one cell holds what a run needs. The user runs any runner there and adds the
result back from that cell's Files panel.

- **Build** (a user handler in a Capabilities cell): a copy of its package
  without its added skills (``skills/``, which stay in the Capabilities
  cell), the ``build`` brief and the handler authoring guides. The copy is a
  'duplicate' of the handler's cell in the lineage.
- **Regenerate skills** (a built-in handler on the Definitions card): the
  built-in's projection (:func:`builtin_projection`), the ``regenerate``
  brief and the guides. The skills come back through
  ``POST …/builtin/handlers/{id}/skills``.
- **Create**: the user's answers as the ``create`` brief and the authoring
  guides of each kind to make.

Every work cell carries ``brief.md`` (a fenced YAML block the host writes,
then the body of the build's ``handler`` skill, ``handler_build.brief_body``)
as its pinned file, the user's pin: the work stack is the user's own action.
The guides are the build's bytes, copied from ``builtin_handlers()``. The
cell is published through ``transfer_import.commit_import``, as an import is.
"""
from __future__ import annotations

from typing import Iterable

import yaml
from fastapi import HTTPException

from handler_build import BriefName

BRIEF = "brief.md"
CAPABILITY_KINDS = ("generator", "runner", "renderer", "handler")


def _brief(fields: dict, name: BriefName) -> bytes:
    """``brief.md``: ``fields`` as a fenced ``yaml`` block at the top (a
    Markdown editor shows it as code, where front matter reads as garbled
    prose), a blank line, then the brief body, which opens with its H1."""
    from handler_build import brief_body
    data = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
    return b"```yaml\n" + data.encode("utf-8") + b"```\n\n" + brief_body(name)


def _authoring_guides(kinds: Iterable[str]) -> dict[str, bytes]:
    """``guides/<kind>-authoring/**``: each kind's built-in authoring skill."""
    from builtin_packages import builtin_handlers
    guides: dict[str, bytes] = {}
    for kind in kinds:
        prefix = f"derived/skills/{kind}-authoring/"
        guides.update({f"guides/{kind}-authoring/{path[len(prefix):]}": body
                       for path, body in builtin_handlers()[kind].files.items() if path.startswith(prefix)})
    return guides


def _brief_guides(name: BriefName) -> dict[str, bytes]:
    """``guides/<name>/references/**``: the brief's own references."""
    from handler_build import brief_references
    return {f"guides/{name}/references/{path}": body for path, body in brief_references(name).items()}


def _work_index(ch) -> int:
    """Where a work stack lands in ``main_stacks``: right of Capabilities,
    or at the end while Capabilities is hidden."""
    from cell_placement import is_capabilities_stack
    from state import state_lock
    with state_lock:
        at = next((i for i, stack in enumerate(ch.main_stacks) if is_capabilities_stack(stack)), None)
        return len(ch.main_stacks) if at is None else at + 1


def _publish(channel: str, ch, name: str, files: dict[str, bytes]) -> dict:
    """Publish ``files`` as the one cell of a new work stack ``name`` and pin
    its brief. Answers ``{stack_id, cell_id, brief_path}``."""
    from cas import store_bytes
    from cell_lineage import user_actor
    from cell_metadata import publish_cell_metadata
    from transfer_import import ImportCell, ImportFile, ImportRequest, ImportStack, commit_import

    cell = ImportCell(name=name, files=[ImportFile(path=path, blob_hash=store_bytes(body))
                                        for path, body in sorted(files.items())])
    published = commit_import(channel, ImportRequest(insert_index=_work_index(ch),
                                                     stacks=[ImportStack(name=name, cells=[cell])]))
    (stack_id,), (cell_id,) = published["stack_ids"], published["cell_ids"]
    publish_cell_metadata(channel, cell_id, {"pinned": BRIEF}, source_type="manual_edit", actor=user_actor())
    return {"stack_id": stack_id, "cell_id": cell_id, "brief_path": BRIEF}


def _record(ch, cell_id: str, folder: str):
    """The catalog record of the handler ``folder`` in ``cell_id``; 400 when
    it is disabled or did not load."""
    from handlers import catalog
    record = next((r for r in catalog(ch).records
                   if r.cell_id == cell_id and r.prefix == folder and not r.disabled), None)
    if record is None:
        raise HTTPException(400, "Handler declaration is unavailable or invalid")
    return record


def builtin_projection(channel, handler_id: str) -> dict[str, bytes]:
    """The built-in handler ``handler_id`` as a regenerate work cell holds it
    (package-relative): the build's package, its ``.handler`` listing the
    channel's added examples after the build's and their files, and its
    skills: the build's pinned ones and the effective non-pinned ones (the
    overlay's regenerated skills while they wait for review, else the
    accepted ones)."""
    from builtin_packages import ADDITIONS, builtin_handlers, handler_card, parse_additions, pinned
    from handler_build import handler_snapshot

    handler = builtin_handlers()[handler_id]
    card = handler_card(channel)
    _snap, _definition, overlay, _builtin = handler_snapshot(channel, card, handler_id)
    record = _record(channel, card, handler_id)
    definition = f"{handler_id}.handler"
    manifest = yaml.safe_load(handler.files[definition])
    manifest["examples"] = [*manifest.get("examples", []),
                            *(dict(example) for example in parse_additions(overlay.get(ADDITIONS)).examples)]
    regenerated = {path: body for path, body in overlay.items() if path.startswith("derived/skills/")}
    skills = regenerated or {"derived/" + path: body for path, body in record.accepted_files.items()
                             if path.startswith("skills/") and not pinned(handler_id, "derived/" + path)}
    files = {path: body for path, body in handler.files.items()
             if not (path.startswith("derived/skills/") and not pinned(handler_id, path))}
    files.update({path: body for path, body in overlay.items() if path.startswith("examples/")})
    files.update(skills)
    files[definition] = yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True).encode("utf-8")
    return files


def spawn_handler_build(channel: str, cell_id: str, folder: str) -> dict:
    """Open the work stack that builds the handler ``folder`` of the
    Capabilities cell ``cell_id``, or regenerates the skills of the built-in
    handler ``folder`` when ``cell_id`` is the Definitions card. 404 for an
    unknown cell or folder; 400 for a cell off Capabilities, a folder that
    is not one handler package, an unavailable handler or a card folder that
    names no built-in handler. Answers ``{stack_id, cell_id, brief_path}``."""
    from builtin_packages import builtin_handlers, card_spec
    from cell_placement import find_cell, is_capabilities_stack
    from state import require_channel, state_lock

    ch = require_channel(channel)
    cell_id = str(cell_id)
    folder = folder.strip("/")
    with state_lock:
        stack, cell = find_cell(ch, cell_id)
        if stack is None or cell is None:
            raise HTTPException(404, "Cell not found")
        if not is_capabilities_stack(stack):
            raise HTTPException(400, "Handlers are built from their Capabilities cell")
        card = card_spec(ch, cell)
        paths = [f.name for f in cell.files]
    if card is not None:
        handler = builtin_handlers().get(folder)
        if handler is None or handler.cell != card.spec_id:
            raise HTTPException(400, "Regenerate skills names a built-in handler on its card")
        return _spawn_regenerate(channel, ch, folder)
    if not any(path.startswith(folder + "/") for path in paths):
        raise HTTPException(404, f"No folder {folder!r} in this cell")
    return _spawn_build(channel, ch, cell_id, folder)


def _spawn_build(channel: str, ch, cell_id: str, folder: str) -> dict:
    from collimate.definitions import discover_definitions
    from cell_files import checkpoint_cells, record_duplicates
    from cell_lineage import announce_lineage
    from cell_write_scope import cell_write_scope
    from package_approval import snapshot_cell
    from run_folders import is_run_path
    from state import channel_db_name

    channel_uuid = channel_db_name(ch)
    with cell_write_scope(channel_uuid, [cell_id], hold_state=True):
        snap = snapshot_cell(ch, cell_id)
    discovery = discover_definitions(snap.refs)
    package = next((p for p in discovery.packages if p.prefix == folder), None)
    if package is None or package.kind != "handler":
        raise HTTPException(400, f"{folder}/ needs exactly one .handler")
    record = _record(ch, cell_id, folder)
    prefix = folder + "/"
    files = {path: body for path, body in snap.bytes().items()
             if path.startswith(prefix) and not path.startswith(prefix + "skills/") and not is_run_path(path)}
    files[BRIEF] = _brief({"brief": "build", "handler": record.manifest["id"], "package": prefix}, "build")
    files.update(_brief_guides("build"))
    files.update(_authoring_guides(["handler"]))
    promoted = checkpoint_cells(channel_uuid, [cell_id], "duplicate", best_effort=True)
    spawned = _publish(channel, ch, f"Build ‘{record.manifest['name']}’", files)
    record_duplicates(channel_uuid, [(cell_id, spawned["cell_id"])], promoted)
    announce_lineage(channel, [cell_id, spawned["cell_id"]])
    return spawned


def _spawn_regenerate(channel: str, ch, handler_id: str) -> dict:
    from builtin_packages import builtin_handlers
    handler = builtin_handlers()[handler_id]
    prefix = handler_id + "/"
    files = {prefix + path: body for path, body in builtin_projection(ch, handler_id).items()}
    files[BRIEF] = _brief({"brief": "regenerate", "handler": handler_id, "package": prefix}, "regenerate")
    files.update(_brief_guides("regenerate"))
    files.update(_authoring_guides(["handler"]))
    return _publish(channel, ch, f"Regenerate ‘{handler.manifest['name']}’ skills", files)


def spawn_capability_create(channel: str, body) -> dict:
    """Open the work stack that makes new capabilities from the user's
    Create answers (``routes.capabilities_stack.CapabilityCreateRequest``):
    ``brief.md`` holds them in its YAML block, and ``guides/`` the authoring
    guides of each kind in ``make`` (all four when it is empty). No template
    definitions are copied: the Files panel would offer them for adding.
    Answers ``{stack_id, cell_id, brief_path}``."""
    from state import require_channel

    ch = require_channel(channel)
    fields = {
        "brief": "create",
        "title": body.title,
        "make": list(body.make),
        "answers": [{"question": a.question, "answer": a.answer} for a in body.answers],
        "related": [{"kind": r.kind, "id": r.id} for r in body.related],
    }
    files = {BRIEF: _brief(fields, "create")}
    files.update(_authoring_guides(body.make or CAPABILITY_KINDS))
    return _publish(channel, ch, f"Create ‘{body.title}’", files)
