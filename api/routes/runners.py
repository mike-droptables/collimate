"""Complete runner drafts and the generator-owned configs they load from."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import runners
from runner_permissions import CAPABILITY_ENUMS, PERMISSION_DESCRIPTIONS, PERMISSION_LABELS, RunnerPermissions
from state import find_active_channel, get_active_channel, state_lock

router = APIRouter()


def active_channel(required=True):
    channel = find_active_channel()
    if required and channel is None:
        raise HTTPException(404, "No active channel")
    return channel


def invoke(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except runners.RunnerError as exc:
        raise HTTPException(exc.status, exc.reason) from exc


def source_write(fn, *args, **kwargs):
    """Runner writes lock every Capabilities cell (the built-in cards
    included), in the same cell-before-state order as ordinary files."""
    from cell_placement import capabilities_stack
    from cell_commits import cell_commit_locks
    for _ in range(5):
        with state_lock:
            channel = get_active_channel()
            cells = {str(c.id) for c in capabilities_stack(channel).cells}
        with cell_commit_locks(cells):
            with state_lock:
                current = {str(c.id) for c in capabilities_stack(channel).cells}
                if not current <= cells:
                    continue
            return invoke(fn, channel, *args, **kwargs)
    raise HTTPException(409, 'Capabilities cells changed while saving')


def index():
    """The active channel's generators (none without an active channel)."""
    from cell_placement import capabilities_stack_cells
    channel = active_channel(False)
    return runners.generator_index(capabilities_stack_cells(channel)) if channel else {}


class RunnerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runner: dict
    files: list[dict] = Field(default_factory=list)
    revision: str | None = None
    # The Capabilities cell a NEW runner is saved into. Absent, it goes into
    # its generator's cell. An existing runner stays where it lives; naming
    # another cell on an update is a 400.
    cell_id: str | None = None
    # A NEW runner saved into a new cell at the end of Capabilities, named
    # after the runner; never with cell_id, never on an update (400).
    new_cell: bool = False


@router.get("/api/runner_vocabulary")
def runner_vocabulary():
    return {"enums": CAPABILITY_ENUMS, "defaults": RunnerPermissions.from_definition({}).to_dict(),
            "labels": PERMISSION_LABELS, "descriptions": PERMISSION_DESCRIPTIONS}


@router.get("/api/runner_generators")
def list_runner_generators():
    return [invoke(runners.generator_payload, gi) for gi in invoke(index).values()]


@router.get("/api/runner_generators/{generator_id}/configs/{config_id}")
def get_config(generator_id: str, config_id: str):
    gi = invoke(index).get(generator_id)
    if gi is None:
        raise HTTPException(404, "Generator not found")
    return invoke(runners.config_template, gi, config_id)


@router.get("/api/runners")
def list_runners():
    channel = active_channel(False)
    return invoke(runners.build_catalog, channel) if channel else []


@router.get("/api/runners/{runner_id}")
def get_runner(runner_id: str):
    return invoke(runners.build_editor, active_channel(), runner_id)


@router.post("/api/runners")
def create_runner(body: RunnerDraft):
    return source_write(runners.save_runner, body.runner, body.files, create=True, cell_id=body.cell_id,
                        new_cell=body.new_cell)


@router.put("/api/runners/{runner_id}")
def update_runner(runner_id: str, body: RunnerDraft):
    if body.runner.get("id") != runner_id:
        raise HTTPException(400, "Runner identity cannot change")
    return source_write(runners.save_runner, body.runner, body.files, expected_revision=body.revision,
                        cell_id=body.cell_id, new_cell=body.new_cell)


@router.delete("/api/runners/{runner_id}")
def delete_runner(runner_id: str):
    source_write(runners.delete_runner, runner_id)
    return {"ok": True}
