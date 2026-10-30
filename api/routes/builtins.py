"""The channel's additions to a built-in handler: examples and skills.

A built-in handler (``builtin_packages``) is part of the app; a channel may
only add examples to it and regenerate its skills. These routes write the
Definitions card's overlay (``<handler>/examples/<folder>/**``,
``<handler>/derived/skills/**`` and ``<handler>/additions.yaml``, writer
``host:builtin``):

- ``POST …/examples`` copies files of another cell into a new example
  folder ``example-<8 hex>`` and declares it;
- ``PATCH …/examples/{folder}`` edits an added example's description,
  expected verdict or feedback; ``DELETE`` removes it (the build's own
  examples are never changed);
- ``POST …/skills`` takes the regenerated skills a regenerate work stack
  wrote (``capability_spawn``) into the overlay, where they wait for review;
- ``DELETE …/skills`` returns the handler to the build's skills.

Accepting offered skills is the handler-build Accept on the card
(``handler_build``). Each answer carries the handler's catalog record.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class AddExampleRequest(BaseModel):
    """Files of ``source_cell_id`` (cell-relative paths, each a type the
    handler covers) to copy into a new example, and its declaration."""
    model_config = ConfigDict(extra="forbid")
    source_cell_id: str
    paths: list[str] = Field(min_length=1)
    description: str
    expect: Literal["valid", "invalid"]
    feedback: str


class BuiltinSkillsOffer(BaseModel):
    """The cell where a built-in handler's skills were regenerated and the
    handler's package folder in it (``<folder>/derived/skills/**``)."""
    model_config = ConfigDict(extra="forbid")
    source_cell_id: UUID
    folder: str = Field(min_length=1, max_length=200)


class ExamplePatch(BaseModel):
    """The added example fields to change."""
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    expect: Literal["valid", "invalid"] | None = None
    feedback: str | None = None


def _handler(channel, handler_id: str):
    """``(handler, card cell id)``; 404 for an id that is not a built-in."""
    from builtin_packages import builtin_handlers, handler_card
    handler = builtin_handlers().get(handler_id)
    if handler is None:
        raise HTTPException(404, f"No built-in handler {handler_id!r}")
    return handler, handler_card(channel)


def _answer(channel, handler_id: str, version_id: str | None) -> dict:
    from handlers import catalog
    record = next(r for r in catalog(channel).records if r.builtin and r.prefix == handler_id)
    return {"version_id": version_id, "handler": record.public()}


def _commit(channel, handler_id: str, change, label: str) -> dict:
    """Apply ``change`` to the handler's overlay and answer its record; a
    refused change is a 400."""
    from handler_build import commit_overlay
    _handler_obj, card = _handler(channel, handler_id)
    try:
        version = commit_overlay(channel, card, handler_id, change, label)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _answer(channel, handler_id, version)


def _additions(files):
    from builtin_packages import ADDITIONS, parse_additions
    return parse_additions(files.get(ADDITIONS))


@router.post("/api/channels/{channel}/builtin/handlers/{handler_id}/examples")
async def add_example(channel: str, handler_id: str, body: AddExampleRequest) -> dict:
    """Add an example to a built-in handler: the current bodies of
    ``paths`` in the source cell land in ``<handler>/examples/<folder>/``
    under their base names, and the example is declared in
    ``additions.yaml``. 404 for an unknown handler, source cell or file;
    400 for a file of a type the handler does not cover, two files with one
    base name or a built-in card as the source. Answers ``{folder,
    version_id, handler}``."""
    from builtin_packages import ADDITIONS, Additions, BUILTIN_REFUSAL, new_example_folder
    from cell_placement import find_cell
    from cell_write_scope import cell_write_scope
    from collimate.handlers import handler_for
    from package_approval import snapshot_cell
    from state import channel_db_name, require_channel, state_lock

    ch = require_channel(channel)
    handler, _card = _handler(ch, handler_id)

    def read_sources() -> dict[str, bytes]:
        with state_lock:
            source = find_cell(ch, body.source_cell_id)[1]
            if source is None:
                raise HTTPException(404, "Source cell not found")
            if source.builtin:
                raise HTTPException(400, BUILTIN_REFUSAL)
        names = [path.rsplit("/", 1)[-1] for path in body.paths]
        if len(set(names)) != len(names):
            raise HTTPException(400, "An example's files need distinct names")
        uncovered = [path for path in body.paths if handler_for([handler.manifest], path) is None]
        if uncovered:
            raise HTTPException(400, f"{', '.join(uncovered)}: not a type the {handler.manifest['name']} handler covers")
        with cell_write_scope(channel_db_name(ch), [body.source_cell_id], hold_state=True):
            snap = snapshot_cell(ch, body.source_cell_id)
        missing = [path for path in body.paths if path not in snap.refs]
        if missing:
            raise HTTPException(404, f"{', '.join(missing)} is not in the source cell")
        bodies = snap.bytes()
        return {name: bodies[path] for name, path in zip(names, body.paths)}

    copied = await asyncio.to_thread(read_sources)
    folder = new_example_folder()

    def change(files):
        additions = _additions(files)
        if folder in handler.example_folders or any(e["folder"] == folder for e in additions.examples):
            raise HTTPException(409, "Example folder already exists; retry")
        example = {"folder": folder, "description": body.description, "expect": body.expect,
                   "feedback": body.feedback}
        return (files | {f"examples/{folder}/{name}": data for name, data in copied.items()}
                | {ADDITIONS: Additions((*additions.examples, example), additions.skills).dump()})

    answer = await asyncio.to_thread(_commit, ch, handler_id, change, "Add example to " + handler_id)
    return {"folder": folder, **answer}


def _added(files, handler, folder: str):
    """The added example ``folder`` of ``files``' additions (404 when
    there is none; 400 for one of the build's)."""
    from builtin_packages import BUILTIN_REFUSAL
    if folder in handler.example_folders:
        raise HTTPException(400, BUILTIN_REFUSAL)
    additions = _additions(files)
    if not any(example["folder"] == folder for example in additions.examples):
        raise HTTPException(404, f"No added example {folder!r}")
    return additions


@router.patch("/api/channels/{channel}/builtin/handlers/{handler_id}/examples/{folder}")
async def patch_example(channel: str, handler_id: str, folder: str, body: ExamplePatch) -> dict:
    """Change an added example's description, expected verdict or feedback;
    its files stay. 404 for an unknown handler or added example, 400 for an
    example of the build. Answers ``{version_id, handler}``."""
    from builtin_packages import ADDITIONS, Additions
    from state import require_channel

    ch = require_channel(channel)
    handler, _card = _handler(ch, handler_id)
    updates = body.model_dump(exclude_none=True)

    def change(files):
        additions = _added(files, handler, folder)
        examples = tuple(dict(example, **updates) if example["folder"] == folder else example
                         for example in additions.examples)
        return files | {ADDITIONS: Additions(examples, additions.skills).dump()}

    return await asyncio.to_thread(_commit, ch, handler_id, change, "Edit example of " + handler_id)


@router.delete("/api/channels/{channel}/builtin/handlers/{handler_id}/examples/{folder}")
async def delete_example(channel: str, handler_id: str, folder: str) -> dict:
    """Remove an added example: its files and its declaration. 404 for an
    unknown handler or added example, 400 for an example of the build.
    Answers ``{version_id, handler}``."""
    from builtin_packages import ADDITIONS, Additions
    from state import require_channel

    ch = require_channel(channel)
    handler, _card = _handler(ch, handler_id)

    def change(files):
        additions = _added(files, handler, folder)
        kept = {path: data for path, data in files.items() if not path.startswith(f"examples/{folder}/")}
        examples = tuple(example for example in additions.examples if example["folder"] != folder)
        return kept | {ADDITIONS: Additions(examples, additions.skills).dump()}

    return await asyncio.to_thread(_commit, ch, handler_id, change, "Remove example of " + handler_id)


@router.post("/api/channels/{channel}/builtin/handlers/{handler_id}/skills")
async def offer_skills(channel: str, handler_id: str, body: BuiltinSkillsOffer) -> dict:
    """Use the skills regenerated in a work cell: the non-pinned files under
    ``<folder>/derived/skills/`` of the source cell's tip replace the
    overlay's regenerated skills; ``additions.yaml`` keeps its accepted
    hash, so they wait for review and accept on the card. 404 for an
    unknown handler or source cell; 400 for a source on Capabilities (a
    built-in card included) or one with no such skills. Answers
    ``{handler}``."""
    from builtin_packages import pinned
    from cell_placement import find_cell, is_capabilities_stack
    from cell_write_scope import cell_write_scope
    from package_approval import snapshot_cell
    from state import channel_db_name, require_channel, state_lock

    ch = require_channel(channel)
    _handler(ch, handler_id)
    source_id = str(body.source_cell_id)
    prefix = body.folder.strip("/") + "/derived/skills/"

    def read_skills() -> dict[str, bytes]:
        with state_lock:
            stack, source = find_cell(ch, source_id)
            if stack is None or source is None:
                raise HTTPException(404, "Source cell not found")
            if source.builtin or is_capabilities_stack(stack):
                raise HTTPException(400, "Offer skills from the cell where they were written")
        with cell_write_scope(channel_db_name(ch), [source_id], hold_state=True):
            snap = snapshot_cell(ch, source_id)
        skills = {"derived/skills/" + path[len(prefix):]: data for path, data in snap.bytes().items()
                  if path.startswith(prefix)}
        owned = {path: data for path, data in skills.items() if not pinned(handler_id, path)}
        if not owned:
            raise HTTPException(400, f"No regenerated skills under {prefix.rstrip('/')}")
        return owned

    skills = await asyncio.to_thread(read_skills)

    def change(files):
        kept = {path: data for path, data in files.items() if not path.startswith("derived/skills/")}
        return kept | skills

    answer = await asyncio.to_thread(_commit, ch, handler_id, change, "Offer skills for " + handler_id)
    return {"handler": answer["handler"]}


@router.delete("/api/channels/{channel}/builtin/handlers/{handler_id}/skills")
async def delete_skills(channel: str, handler_id: str) -> dict:
    """Use the app's skills again: remove the regenerated skills and the
    accepted skills hash. 404 for an unknown handler. Answers ``{version_id,
    handler}`` (``version_id`` null when the handler already used the
    build's skills)."""
    from builtin_packages import ADDITIONS, Additions
    from state import require_channel

    ch = require_channel(channel)
    _handler(ch, handler_id)
    current = await asyncio.to_thread(_answer, ch, handler_id, None)
    record = current["handler"]
    if not record["has_derived"] and record["uses_app_skills"]:
        return current

    def change(files):
        additions = _additions(files)
        kept = {path: data for path, data in files.items() if not path.startswith("derived/skills/")}
        return kept | {ADDITIONS: Additions(additions.examples).dump()}

    return await asyncio.to_thread(_commit, ch, handler_id, change, "Use the app's skills for " + handler_id)
