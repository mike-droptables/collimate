"""Pydantic request models for run endpoints."""

from pydantic import BaseModel, ConfigDict


class RunRequest(BaseModel):
    """Run a saved definition on one cell. The prompt, settings and accounts
    are the saved runner's; the request's only per-run input is
    ``selected_file``, the cell-relative path of the file the user has
    selected, for a runner that takes one (the run reads it as
    ``gen.selected_file``). Any other field is refused (422). ``new_cell``
    runs on a clone of the cell in a new stack to its right instead of on
    the cell itself. The cell is never on the Capabilities stack."""
    model_config = ConfigDict(extra="forbid")

    runner: str
    runner_revision: str | None = None
    cell_id: str
    new_cell: bool = False
    selected_file: str | None = None


class RendererActionRequest(BaseModel):
    """A click in a live renderer. Any other field is refused (422)."""
    model_config = ConfigDict(extra="forbid")

    action_id: str
    # Tagged actions (agent-v2 §4.3.4): which surface/renderer emitted it and
    # an optional structured payload — a bare id can't carry an answer value,
    # and a bare FIFO can't serve concurrent waiters filtering by source.
    # A renderer's source is its file: the run whose folder holds it gets
    # the action.
    source: str = ""
    payload: "dict | None" = None
