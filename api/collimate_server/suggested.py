"""The suggested channel setups: the catalog the New channel page shows,
and seeding a channel from one.

A suggestion is a ``suggested/<id>.json`` of a content tree (read by
``collimate._host.packaging.suggested``): its cells of the generator and renderer
asset trees, which a channel seeded from it holds on its Capabilities
stack, and the tutorial cells that start on the canvas.

The catalog reads one content tree, the server's own: the ``content/`` tree
installed with it (``genesis.bundled_dir``), which is a checkout's own
``content/`` when the server runs from one. Nothing is fetched: content
changes arrive with the server that carries them. A seeded cell records the
app's content as its upstream (``provenance.APP``).

The runner drafts a suggestion offers are the runner packages inside its
asset cells. They are stripped from the seeded cells; the drafts the user
keeps (each complete) are written back into their generator's cell before
admission, so every cell starts with one version.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from collimate_server.provenance import APP

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


def content_root() -> Path:
    """The content tree suggestions are read from: the one installed with
    the server, a checkout's own ``content/`` when it runs from one."""
    import collimate_server.genesis as genesis
    return Path(genesis.bundled_dir("content"))



# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

def _capability_refs(suggestion):
    """``(kind, ref)`` of the suggestion's Capabilities cells: generators,
    renderers, then libraries."""
    return [(kind, ref) for kind, refs in (("generator", suggestion.generators), ("renderer", suggestion.renderers),
                                           ("library", suggestion.libraries))
            for ref in refs]


def _asset_specs(root: Path, suggestion):
    """The suggestion's Capabilities cells, generators first."""
    from collimate._host.packaging.suggested import read_suggested_cell
    return [read_suggested_cell(root, ref, kind) for kind, ref in _capability_refs(suggestion)]


def _entry(root: Path, suggestion) -> dict:
    import collimate_server.runners as runners

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
        "libraries": refs(suggestion.libraries), "tutorial": refs(suggestion.tutorial),
        "runner_generators": [runners.generator_payload(gi) for gi in index.values()],
        "runners": [{"draft": draft, "quick_fields": suggestion.quick_fields.get(draft["runner"]["id"], [])}
                    for draft in packaged],
    }


def catalog() -> dict:
    """``GET /api/suggested``: the suggestions of the content tree with their
    runner generators and drafts; a tree that cannot be read answers its
    error and no suggestions."""
    from collimate._host.packaging.suggested import read_suggestions
    root = content_root()
    try:
        return {"suggestions": [_entry(root, suggestion) for suggestion in read_suggestions(root)], "error": None}
    except _TREE_ERRORS as exc:
        return {"suggestions": [], "error": f"Cannot read the suggestions in {root}: {exc}"}


def _suggestion(root: Path, suggestion_id: str):
    from collimate._host.packaging.suggested import read_suggestions
    found = next((s for s in read_suggestions(root) if s.id == suggestion_id), None)
    if found is None:
        raise ValueError(f"Unknown suggestion {suggestion_id!r}")
    return found


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def _with_drafts(store, cells, drafts: list[dict]) -> None:
    """Replace every cell's runner packages with the complete ``drafts``,
    each written into the cell of its generator (in place)."""
    import collimate_server.runners as runners
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
        from collimate_server.accounts import resolve_public_accounts
        public = resolve_public_accounts(definition.get("accounts", {}),
                                         gi["manifest"].get("account_requirements", {}))
        effective = {**definition, "settings": runners._account_settings(
            gi["main_config"], gi["fields"], gi["defaults"], definition["settings"], public)}
        runners.validate_definition(gi, effective, files, runnable=True)
        occupied = prepared[str(gi["cell"].id)]
        occupied.extend(runners.runner_vfiles(store, definition, files, occupied_files=occupied))
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
    from collimate_server.channel_sources import admission_errors, admit_packages, specs_to_cells
    from collimate_server.cas import channel_store
    from collimate_server.genesis import package_cell_vfiles
    from collimate_server.import_plan import ImportPlan
    from collimate_server.models import Cell, Stack

    root = content_root()
    with admission_errors():
        try:
            suggestion = _suggestion(root, selection.id)
            incoming = specs_to_cells([read_suggested_cell(root, ref, kind)
                                       for kind, ref in _capability_refs(suggestion)], channel_store(ch.id))
            tutorial_specs = [read_tutorial_cell(root, ref) for ref in suggestion.tutorial] if selection.tutorial else []
        except yaml.YAMLError as exc:
            raise ValueError(f"Cannot read the suggestion in {root}: {exc}") from exc
        _with_drafts(channel_store(ch.id), incoming, selection.runners)
        tutorial = []
        for spec in tutorial_specs:
            files = package_cell_vfiles(spec, channel_store(ch.id))
            tutorial.append(Stack(name=spec.name, cells=[
                Cell(name=spec.name, icon=spec.icon or None, files=files,
                     pinned=files[0].name if len(files) == 1 else None)]))
        plan = ImportPlan(ch, new_channel=True)
        plan.candidate.main_stacks.extend(tutorial)
        skipped = admit_packages(ch, plan, incoming, APP)
        plan.publish()
    return skipped
