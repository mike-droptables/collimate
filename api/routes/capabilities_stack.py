"""Routes over the channel's Capabilities stack.

- ``POST …/capabilities/build`` opens the work stack that builds a user
  handler, or regenerates a built-in handler's skills (``capability_spawn``);
- ``POST …/capabilities/create`` opens the work stack that makes new
  capabilities from the user's Create answers;
- ``GET …/capabilities/catalog`` lists the suggested catalog's cells with
  whether the channel already holds each;
- ``POST …/capabilities/add`` adds a cell of the suggested catalog to the
  stack, its packages admitted as an import's are;
- ``GET …/capabilities/problems`` lists the files on the stack that are not
  working capabilities (``capability_problems``).

A spawn answers the channel layout with the new work stack and cell and the
brief's path, for the client to select the cell and open its brief.
"""
from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter()

CapabilityKind = Literal["generator", "runner", "renderer", "handler"]


class CapabilityBuildRequest(BaseModel):
    """Build the handler ``folder`` of a Capabilities cell, or regenerate the
    skills of the built-in handler ``folder`` on the Definitions card."""
    model_config = ConfigDict(extra="forbid")
    cell_id: UUID
    folder: str = Field(min_length=1, max_length=200)


class CreateAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(max_length=4000)


class RelatedCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: CapabilityKind
    id: str = Field(min_length=1, max_length=200)


class CapabilityCreateRequest(BaseModel):
    """The Create questionnaire's outcome: a title, the kinds to make (none:
    the work stack decides), every question shown with its answer, and the
    channel's capabilities the result should reuse."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80)
    make: list[CapabilityKind] = Field(default_factory=list, max_length=4)
    answers: list[CreateAnswer] = Field(min_length=1, max_length=16)
    related: list[RelatedCapability] = Field(default_factory=list, max_length=24)

    @field_validator("make")
    @classmethod
    def _unique(cls, make: list[str]) -> list[str]:
        if len(set(make)) != len(make):
            raise ValueError("make lists each kind once")
        return make


class CapabilityCatalogAdd(BaseModel):
    """A cell of the suggested catalog, by its suggestion and its ``folder``
    as ``GET /api/suggested`` lists it (either section)."""
    model_config = ConfigDict(extra="forbid")
    suggestion_id: str = Field(min_length=1)
    folder: str = Field(min_length=1)


def _with_layout(channel: str, answer: dict) -> dict:
    from routes.layout import channel_layout
    from state import require_channel
    return channel_layout(require_channel(channel)) | answer


@router.post("/api/channels/{channel}/capabilities/build")
async def build_capability(channel: str, body: CapabilityBuildRequest) -> dict:
    """Open a Build (or Regenerate skills) work stack right of Capabilities
    and select it. 404 for an unknown cell or folder; 400 for a folder that
    is not one handler package, a cell neither on Capabilities nor the
    Definitions card, an unavailable or invalid handler, or a card folder
    that is not a built-in handler. Answers the channel layout with
    ``{stack_id, cell_id, brief_path}``."""
    from capability_spawn import spawn_handler_build

    def _body() -> dict:
        return _with_layout(channel, spawn_handler_build(channel, str(body.cell_id), body.folder))

    return await asyncio.to_thread(_body)


@router.post("/api/channels/{channel}/capabilities/create")
async def create_capability(channel: str, body: CapabilityCreateRequest) -> dict:
    """Open a Create work stack right of Capabilities and select it.
    Answers the channel layout with ``{stack_id, cell_id, brief_path}``."""
    from capability_spawn import spawn_capability_create

    def _body() -> dict:
        return _with_layout(channel, spawn_capability_create(channel, body))

    return await asyncio.to_thread(_body)


def _sections(entry):
    """``(kind, ref)`` of each generator then renderer cell of a suggestion."""
    return [(kind, ref) for kind, refs in (("generator", entry.generators), ("renderer", entry.renderers))
            for ref in refs]


def _catalog_files(kind: str, files) -> list:
    """The files a catalog cell of section ``kind`` brings: a generator
    cell's build leaves its runner packages out."""
    import runners
    return runners.without_runners(files) if kind == "generator" else list(files)


def _held(bodies: dict[str, bytes], declared: set[tuple[str, str]],
          admitted: set[tuple[str, str, str]]) -> bool:
    """Whether the channel already holds the catalog cell whose files
    (``_catalog_files``) are ``bodies`` (cell path to bytes): every package
    they hold was admitted onto Capabilities with exactly these contents
    (``admitted``, ``_admitted_identities``: the ``(kind, id, content
    hash)`` of each Capabilities cell's provenance), or every package they
    declare is already declared on Capabilities (``declared``,
    ``package_import.declared_packages``). A partial overlap is not held: an
    add brings the rest."""
    from package_import import package_identities
    identities = package_identities(bodies)
    if not identities:
        return False
    return identities <= admitted or {(kind, ident) for kind, ident, _ in identities} <= declared


def _spec_bytes(files) -> dict[str, bytes]:
    """An asset-tree cell spec's files as cell path to bytes."""
    return {f.name: f.data if f.data is not None else f.content.encode("utf-8") for f in files}


def _admitted_identities(ch) -> set[tuple[str, str, str]]:
    """``(package kind, package id, content hash)`` of every provenance
    record of the channel's Capabilities cells."""
    from cell_placement import capabilities_stack_cells
    from database import get_db_connection
    from provenance import read
    from state import channel_db_name, state_lock
    with state_lock:
        ids = [str(cell.id) for cell in capabilities_stack_cells(ch)]
    conn = get_db_connection(channel_db_name(ch))
    try:
        records = read(conn, ids)
    finally:
        conn.close()
    return {(r["package_kind"], r["package_id"], r["content_hash"]) for rows in records.values() for r in rows}


@router.get("/api/channels/{channel}/capabilities/catalog")
async def capabilities_catalog(channel: str) -> dict:
    """The suggested catalog for Add from catalog: ``{suggestions: [{id,
    name, description, cells: [{folder, name, section, held}]}]}``, each
    suggestion's generator cells then its renderer cells. ``held`` is
    ``_held`` (the channel's Capabilities provenance already records each
    of the cell's packages with the same contents, or every package the
    cell declares is already on Capabilities), exactly when
    ``…/capabilities/add`` answers 409 'Already in this channel'. 400 when
    the catalog cannot be read."""
    def _body() -> dict:
        import suggested
        from collimate._host.packaging.suggested import read_suggested_cell, read_suggestions
        from channel_sources import admission_errors
        from package_import import capture_sources, declared_packages
        from state import require_channel

        ch = require_channel(channel)
        root = suggested.current_root()
        with admission_errors():
            declared = declared_packages(capture_sources(ch)[0])
            admitted = _admitted_identities(ch)
            suggestions = []
            for entry in read_suggestions(root):
                cells = []
                for kind, ref in _sections(entry):
                    spec = read_suggested_cell(root, ref, kind)
                    held = _held(_spec_bytes(_catalog_files(kind, spec.files)), declared, admitted)
                    cells.append({"folder": ref.folder, "name": ref.name, "section": kind, "held": held})
                suggestions.append({"id": entry.id, "name": entry.name, "description": entry.description,
                                    "cells": cells})
        return {"suggestions": suggestions}

    return await asyncio.to_thread(_body)


@router.post("/api/channels/{channel}/capabilities/add")
async def add_from_catalog(channel: str, body: CapabilityCatalogAdd) -> dict:
    """Add a suggested catalog cell to the end of Capabilities; a generator
    cell leaves its build's runner packages out, as a channel seeded from the
    suggestion without drafts does. Its
    packages pass admission as an import's do: a package whose id or types
    Capabilities already holds is left out and reported in
    ``package_skips``. 404 for an unknown suggestion or folder; 409 'Already
    in this channel' when the channel holds the cell (``_held``, the
    catalog's ``held``), and 'Nothing to add: …' (each reason once) when
    every package was held otherwise. Answers
    the channel layout with ``{cell_id, package_skips}``."""
    def _body() -> dict:
        return _with_layout(channel, _add_catalog_cell(channel, body.suggestion_id, body.folder))

    result = await asyncio.to_thread(_body)
    from state import flush_persist_state
    await asyncio.to_thread(flush_persist_state)
    return result


def _add_catalog_cell(channel: str, suggestion_id: str, folder: str) -> dict:
    import suggested
    from collimate.definitions import discover_definitions
    from collimate._host.packaging.suggested import read_suggested_cell
    from cell_lineage import announce_lineage
    from channel_sources import admission_errors, specs_to_cells
    from package_import import apply_packages, capture_sources, cell_bytes, declared_packages, prepare_packages
    from state import persist_state, require_channel

    ch = require_channel(channel)
    source = suggested.resolve(refresh=False)
    root = source.root
    with admission_errors():
        try:
            entry = suggested._suggestion(root, suggestion_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        found = next(((kind, ref) for kind, ref in _sections(entry) if ref.folder == folder), None)
        if found is None:
            raise HTTPException(404, f"No cell in {folder!r} of this suggestion")
        kind, ref = found
        cell, = specs_to_cells([read_suggested_cell(root, ref, kind)])
        cell.files = _catalog_files(kind, cell.files)
        sources = capture_sources(ch)
        if _held(cell_bytes(cell), declared_packages(sources[0]), _admitted_identities(ch)):
            raise HTTPException(409, "Already in this channel")
        prepared = prepare_packages(ch, [cell], source=source.provenance(), source_state=sources)
        # Every package held: the cell's other files are not worth a cell.
        prepared.cells = [c for c in prepared.cells if discover_definitions([f.name for f in c.files]).packages]
        if not prepared.cells:
            reasons = "; ".join(dict.fromkeys(skip["reason"] for skip in prepared.skipped)) or "it holds no package"
            raise HTTPException(409, f"Nothing to add: {reasons}")
        report = apply_packages(ch, prepared)
    persist_state()
    added = str(prepared.cells[0].id)
    announce_lineage(channel, [added])
    return {"cell_id": added, "package_skips": report["skipped"]}


@router.get("/api/channels/{channel}/capabilities/problems")
async def capabilities_problems(channel: str) -> dict:
    """The files on Capabilities that are not working capabilities:
    ``{problems: [{cell_id, path, kind, error}]}``."""
    from capability_problems import capability_problems
    from state import require_channel

    ch = require_channel(channel)
    return {"problems": await asyncio.to_thread(capability_problems, ch)}
