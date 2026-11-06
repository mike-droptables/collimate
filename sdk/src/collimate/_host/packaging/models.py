# SPDX-License-Identifier: MIT
from dataclasses import dataclass, field

# A cell's display order when its cell.yaml records none.
DEFAULT_CELL_ORDER = 100


@dataclass
class FileSpec:
    name: str
    language: str
    content: str = ""
    # Binary-safe body. When ``data`` is set the file's bytes are RAW
    # (``content`` stays "") and ``blob_hash`` is their sha256, the
    # content-addressed id a host stores the bytes under. Text files leave
    # both None and carry utf-8 ``content``.
    blob_hash: str | None = None
    data: bytes | None = None


@dataclass
class CellSpec:
    id: str
    name: str
    files: list[FileSpec] = field(default_factory=list)
    # Tree-driven layout metadata (collimate._host.packaging.layout): the parent
    # folder the cell came from, a lucide-style icon name, the display
    # order, and arbitrary passthrough extras.
    folder: str = ""
    icon: str = ""
    order: int = DEFAULT_CELL_ORDER
    extra: dict = field(default_factory=dict)
