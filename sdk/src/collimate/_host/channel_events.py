"""Channel SSE wire records. The app emits these; clients share the fixture.

Serialization is shallow: state and event payloads are already wire values.
Building an envelope must never copy the contents of a large cell or run.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import ClassVar, TypedDict


@dataclass(frozen=True, slots=True)
class ChannelEvent:
    type: ClassVar[str]

    def to_dict(self) -> dict:
        return {"type": self.type, **{f.name: getattr(self, f.name) for f in fields(self)}}


@dataclass(frozen=True, slots=True)
class Connected(ChannelEvent):
    type: ClassVar[str] = "connected"


@dataclass(frozen=True, slots=True)
class Overflow(ChannelEvent):
    type: ClassVar[str] = "overflow"


@dataclass(frozen=True, slots=True)
class Shutdown(ChannelEvent):
    type: ClassVar[str] = "shutdown"


@dataclass(frozen=True, slots=True)
class StateSnapshot(ChannelEvent):
    type: ClassVar[str] = "state_snapshot"
    revision: str
    state: dict


@dataclass(frozen=True, slots=True)
class StateDelta(ChannelEvent):
    type: ClassVar[str] = "state_delta"
    base_revision: str
    revision: str
    channel_scope: str | None
    patches: list[dict]


@dataclass(frozen=True, slots=True)
class FileChanges(ChannelEvent):
    """A committed mutation receipt; file descriptors carry immutable revisions."""
    type: ClassVar[str] = "file_changes"
    cell_id: str
    operation_id: str
    outcome: str
    files: list[dict]
    conflicts: list[dict]


@dataclass(frozen=True, slots=True)
class DagCommit(ChannelEvent):
    type: ClassVar[str] = "dag_commit"
    cell_id: str
    files: list[str]
    run_id: str | None = None
    generator_name: str | None = None


class RunEventData(TypedDict):
    id: int
    type: str
    payload: str
    created_at: str


class CommitChange(TypedDict):
    """One entry of a ``commit`` run event's payload ``changes`` (the payload
    is JSON text): a cell path the commit created, edited or deleted, and
    whether it is a folder."""
    path: str
    op: str  # one of COMMIT_CHANGE_OPS
    folder: bool


#: The ``op`` values of a :class:`CommitChange`.
COMMIT_CHANGE_OPS: frozenset = frozenset({"create", "edit", "delete"})


@dataclass(frozen=True, slots=True)
class RunEvents(ChannelEvent):
    """New rows of a run's event log. A ``commit`` row's payload JSON
    carries ``changes: list[CommitChange]``, the files the commit touched."""
    type: ClassVar[str] = "run_events"
    run_id: str
    events: list[RunEventData]


@dataclass(frozen=True, slots=True)
class RunStatus(ChannelEvent):
    """A run's terminal status. ``ended_at`` is UTC ISO 8601 with an offset;
    ``note`` (one line) and ``outcome`` ('pass' | 'fail') are the generator's
    ``finish`` extension or the host's fallback note, null when absent."""
    type: ClassVar[str] = "run_status"
    run_id: str
    status: str
    failure_reason: str | None = None
    ended_at: str | None = None
    note: str | None = None
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class RunPhase(ChannelEvent):
    type: ClassVar[str] = "run_phase"
    run_id: str
    phase: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class RunStarted(ChannelEvent):
    """A run was accepted. ``opens`` lists the cell-relative surface files
    the host created for it (the manifest's resolved ``opens``), in
    declaration order: a client reveals the first instead of the run card."""
    type: ClassVar[str] = "run_started"
    run_id: str
    cell_id: str
    runner_id: str | None
    runner_name: str | None
    label: str
    folder: str
    generator_name: str
    stack_id: str | None = None
    opens: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RevealFile(ChannelEvent):
    type: ClassVar[str] = "reveal_file"
    run_id: str
    cell_id: str
    path: str


@dataclass(frozen=True, slots=True)
class SurfaceChanged(ChannelEvent):
    type: ClassVar[str] = "surface_changed"
    cell_id: str
    path: str


@dataclass(frozen=True, slots=True)
class LineageChanged(ChannelEvent):
    """The lineage of these logical cells changed (duplicate, merge, split,
    remove, restore, a run in a new cell): the Removed shelf, the journal and
    the lineage lines refetch for them."""
    type: ClassVar[str] = "lineage_changed"
    cell_ids: list[str]


@dataclass(frozen=True, slots=True)
class RunsActiveChanged(ChannelEvent):
    """A run on ``channel`` (its id) started or reached a terminal status.
    Sent on every channel's stream: clients refetch the live runs for it."""
    type: ClassVar[str] = "runs_active_changed"
    channel: str


@dataclass(frozen=True, slots=True)
class AnnotationsChanged(ChannelEvent):
    """Doorbell: the annotation context ``context_id`` (a run id, or
    ``"cell"``) of ``cell_id`` changed — a message posted, edited, deleted,
    reordered, sent or pulled read; the queue bar, an interrupt, the run's
    listening flag, its seal. ``rev`` is the context's revision after it;
    clients re-GET the context."""
    type: ClassVar[str] = "annotations_changed"
    cell_id: str
    context_id: str
    rev: int


EVENT_TYPES: dict[str, type[ChannelEvent]] = {
    cls.type: cls for cls in (
        Connected, Overflow, Shutdown, StateSnapshot, StateDelta, FileChanges, DagCommit, RunEvents,
        RunStatus, RunPhase, RunStarted, RevealFile, SurfaceChanged, LineageChanged,
        RunsActiveChanged, AnnotationsChanged,
    )
}
