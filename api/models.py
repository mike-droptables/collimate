import os
from pydantic import BaseModel, Field, PrivateAttr
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID, uuid4
from datetime import datetime




_EXT_LANGUAGE = {
    "py": "python", "js": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "json": "json",
    "html": "html", "css": "css", "scss": "scss", "less": "less",
    "sh": "shell", "bash": "shell", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "xml": "xml", "sql": "sql", "rs": "rust",
    "go": "go", "java": "java", "c": "c", "cpp": "cpp", "cs": "csharp",
    "rb": "ruby", "php": "php", "swift": "swift", "kt": "kotlin",
    "md": "markdown", "markdown": "markdown", "txt": "plaintext",
}


def language_for(path: str) -> str:
    """Infer a text language from its case-insensitive suffix; unknown is plaintext."""
    ext = os.path.splitext(os.path.basename(path))[1].lstrip(".").lower()
    return _EXT_LANGUAGE.get(ext, "plaintext")


class VirtualFile(BaseModel):
    name: str
    language: str = "markdown"
    content: str = ""
    file_revision: int = 0
    body_hash: Optional[str] = None
    writer_id: Optional[str] = None
    edit_seq: int = 0
    encoding: str = "utf-8"
    # Binary decoded-cache entries retain the immutable body reference.
    blob_hash: Optional[str] = None
    # Private digest cache for source snapshots and explicit file ingestion.
    _content_hash_cache: tuple[str, str] | None = PrivateAttr(default=None)

    def __eq__(self, other: object) -> bool:
        # A warmed digest is an implementation detail, not a file edit.
        # Compare fields directly so equality never copies file bodies.
        if not isinstance(other, VirtualFile):
            return NotImplemented
        return type(self) is type(other) and all(
            getattr(self, name) == getattr(other, name)
            for name in type(self).model_fields
        )


class Cell(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = "New Cell"
    # Optional picker glyph for generator-source cells: a free-form
    # lucide-style icon name the frontend resolves via its ICON_REGISTRY.
    # Stored alongside the name; surfaced as the "group icon" for every
    # generator declared in this cell. Null = no custom icon.
    icon: Optional[str] = None
    files: List[VirtualFile] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    # Flat {key: value} dict of settings the user chose from the
    # generator's settings schema form. Null if the generator has no
    # settings schema.
    settings: Optional[Dict[str, Any]] = None
    channel: Optional[str] = None
    # File path (within this cell's files) pinned as the cell's preview —
    # rendered in the UI's "Pinned" tab. The user's choice only: runs show
    # a file with reveal_file, never by pinning it.
    pinned: Optional[str] = None
    # Plain-text chips in insertion order, on any cell. The
    # rules (normalized, casefold-unique, capped) live in cell_tags.py.
    tags: List[str] = Field(default_factory=list)
    # A built-in card: it heads the Capabilities stack, and its packages are the
    # app's own build, so it is never removed, moved, renamed or duplicated.
    # Server-set only; no client request model carries it.
    builtin: bool = False


class Stack(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = ""
    cells: List[Cell] = Field(default_factory=list)
    # The one Capabilities stack per channel (cell_placement.capabilities_stack)
    # or an ordinary stack.
    stack_type: Literal["standard", "capabilities"] = "standard"


class Channel(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str = "General"
    nav_stacks: List[Stack] = Field(default_factory=list)
    main_stacks: List[Stack] = Field(default_factory=list)
    nav_selection_id: Optional[UUID] = None
    main_selection_id: Optional[UUID] = None


class AppState(BaseModel):
    channels: List[Channel] = Field(default_factory=list)
    active_channel_id: Optional[UUID] = None
