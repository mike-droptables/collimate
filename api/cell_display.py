"""Strict app display coordinates stored separately from accepted file bytes.

A cell's display record holds its tab tree, the active leaf, the files pane
(collapsed, the edge it docks on — left or top —, its stored width and height,
view mode and panel filters), the runs pane (open, the edge it docks on — right
or bottom —, its stored width and height), the pinned-tab compaction
and the history selection. A null pane width or height means the app's default
size; the app clamps both at render, so the server keeps loose bounds."""
from __future__ import annotations

import json
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, model_validator

MAX_DISPLAY_BYTES = 1024 * 1024
MAX_DISPLAY_DEPTH = 32
MAX_DISPLAY_NODES = 4096
MAX_DISPLAY_TABS = 4096
KEY_PREFIX = "cell_display:"

Identity = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Path = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Renderer = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Label = Annotated[str, StringConstraints(max_length=1024)]
PaneSize = Annotated[StrictInt, Field(ge=1, le=65535)]


class DisplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FileTab(DisplayModel):
    id: Identity
    kind: Literal["file"]
    path: Path
    rendererId: Renderer = cast(Renderer, Field(default=None))


class HistoryTab(DisplayModel):
    id: Identity
    kind: Literal["history"]
    category: Literal["config", "file", "log"]
    path: Path
    rendererId: Renderer = cast(Renderer, Field(default=None))


class EmptyTab(DisplayModel):
    id: Identity
    kind: Literal["empty"]


class CompareSide(DisplayModel):
    channel: Identity
    cellId: Identity
    fileName: Path
    label: Label

    @model_validator(mode="after")
    def canonical_coordinates(self):
        for value in (self.channel, self.cellId):
            if str(UUID(value)) != value:
                raise ValueError("compare coordinates must use canonical UUIDs")
        return self


class CompareTab(DisplayModel):
    id: Identity
    kind: Literal["compare"]
    base: CompareSide
    current: CompareSide


DisplayTab = Annotated[FileTab | HistoryTab | EmptyTab | CompareTab,
                       Field(discriminator="kind")]


class DisplayLeaf(DisplayModel):
    type: Literal["leaf"]
    id: Identity
    tabs: list[DisplayTab] = Field(max_length=MAX_DISPLAY_TABS)
    activeTabId: Identity | None


class DisplaySplit(DisplayModel):
    type: Literal["split"]
    id: Identity
    dir: Literal["row", "column"]
    children: list["DisplayNode"] = Field(min_length=2, max_length=MAX_DISPLAY_NODES)


DisplayNode = Annotated[DisplayLeaf | DisplaySplit, Field(discriminator="type")]
DisplaySplit.model_rebuild()


class PanelFilters(DisplayModel):
    runsOnly: StrictBool
    sortByRecency: StrictBool


class FilesPane(DisplayModel):
    collapsed: StrictBool | None
    side: Literal["left", "top"]
    width: PaneSize | None
    height: PaneSize | None
    viewMode: Literal["tree", "folder"]
    panelFilters: PanelFilters


class RunsPane(DisplayModel):
    open: StrictBool
    side: Literal["right", "bottom"]
    width: PaneSize | None
    height: PaneSize | None


class HistorySections(DisplayModel):
    config: StrictBool
    files: StrictBool
    logs: StrictBool


class HistorySelection(DisplayModel):
    nodeId: Identity | None
    versionId: Identity | None
    sectionsOpen: HistorySections


class CellDisplay(DisplayModel):
    version: Literal[1]
    root: DisplayNode
    activeLeafId: Identity
    filesPane: FilesPane
    runsPane: RunsPane
    pinnedTabCompact: StrictBool
    history: HistorySelection | None

    @model_validator(mode="before")
    @classmethod
    def bounded_tree(cls, value):
        if not isinstance(value, dict):
            raise ValueError("display must be an object")
        if type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("display version must be 1")
        pending = [(value.get("root"), 1)]
        nodes = tabs = 0
        while pending:
            node, depth = pending.pop()
            nodes += 1
            if depth > MAX_DISPLAY_DEPTH or nodes > MAX_DISPLAY_NODES:
                raise ValueError("display tree exceeds structural bounds")
            if isinstance(node, dict):
                children = node.get("children", [])
                leaves = node.get("tabs", [])
                if isinstance(leaves, list):
                    tabs += len(leaves)
                if tabs > MAX_DISPLAY_TABS:
                    raise ValueError("display has too many tabs")
                if isinstance(children, list):
                    pending.extend((child, depth + 1) for child in children)
        return value

    @model_validator(mode="after")
    def valid_selection(self):
        pending = [self.root]
        node_ids, tab_ids, leaves = set(), set(), set()
        while pending:
            node = pending.pop()
            if node.id in node_ids:
                raise ValueError("display node IDs must be unique")
            node_ids.add(node.id)
            if isinstance(node, DisplaySplit):
                pending.extend(node.children)
                continue
            leaves.add(node.id)
            own_tabs = set()
            for tab in node.tabs:
                if tab.id in tab_ids:
                    raise ValueError("display tab IDs must be unique")
                tab_ids.add(tab.id)
                own_tabs.add(tab.id)
            if node.activeTabId is not None and node.activeTabId not in own_tabs:
                raise ValueError("active tab must belong to its leaf")
            if node.tabs and node.activeTabId is None:
                raise ValueError("a nonempty leaf must select a tab")
        if self.activeLeafId not in leaves:
            raise ValueError("active leaf must exist")
        return self


def validate_display(value: object) -> dict:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DISPLAY_BYTES:
        raise ValueError("display exceeds the 1 MiB size limit")
    return CellDisplay.model_validate(value).model_dump(mode="json", exclude_none=False,
                                                       exclude_unset=True)


def display_key(cell_id: str) -> str:
    return KEY_PREFIX + str(cell_id)


def read_display(conn, cell_id: str) -> dict | None:
    row = conn.execute("SELECT value FROM ui_state WHERE key=?", (display_key(cell_id),)).fetchone()
    return validate_display(json.loads(row[0])) if row else None


def write_display(conn, cell_id: str, display: dict | None) -> None:
    """Use the caller's transaction; display has no file/CAS/history effects."""
    if display is None:
        conn.execute("DELETE FROM ui_state WHERE key=?", (display_key(cell_id),))
        return
    encoded = json.dumps(validate_display(display), ensure_ascii=False, separators=(",", ":"))
    conn.execute("INSERT INTO ui_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (display_key(cell_id), encoded))


def take_display(conn, cell_id: str) -> dict | None:
    display = read_display(conn, cell_id)
    write_display(conn, cell_id, None)
    return display
