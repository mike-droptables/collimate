# SPDX-License-Identifier: MIT
"""The selected file as a run's first message (``selected_file_first``), and
the one reader of a cell file a turn carries (:func:`read_file_content`).

The host passes the cell-relative path of the file the user had selected
when the run started (``gen.selected_file``) only to a runner that takes it;
the run reads the content itself. Text rides in the message, capped at
:data:`TEXT_CAP_BYTES`. A media file (an image, a PDF, audio, video) rides as
the turn's model input, like a chat attachment; any other binary file is
named and left for the agent's file tools. A chat submits it as its own
turn before its prompt, an annotation run folds it into its first turn (a
one-shot's opening, an interactive run's first batch of messages), and
claude-code's TTY takes it before the prompt
(``terminal.opening_message``). The annotation surface reads the files a
message links through :func:`read_file_content` too."""
from __future__ import annotations

from dataclasses import dataclass, field

import gen

from _agent_shared import ATTACH_BYTE_CAP
from _agent_shared.logging import safe_log as _secret_log

# The most of a text file the first message carries. A TTY takes it as one
# command-line argument (Linux bounds a single argument at 128 KiB).
TEXT_CAP_BYTES = 32 * 1024


@dataclass
class SelectedFile:
    """The selected file, read once at the run's start: its ``path``, the
    ``message`` that carries it and, for a media file, the engine ``media``
    input (``[{"data", "media_type", "name", "path"}]``) and the transcript's
    attachment ``refs``."""
    path: str
    message: str
    media: list = field(default_factory=list)
    refs: list = field(default_factory=list)


@dataclass
class FileContent:
    """One cell file as a turn may carry it. ``kind``: ``"text"`` (``text``,
    cut to the cap when ``truncated``), ``"media"`` (``media`` is the engine
    input ``{"data", "media_type", "name", "path"}``, or ``None`` when the
    file is over the engine's attachment cap), ``"binary"`` or
    ``"missing"``. ``size`` is the file's byte count."""
    path: str
    kind: str
    text: str = ""
    truncated: bool = False
    size: int = 0
    media: dict | None = None
    media_type: str = ""


def _media_type(path: str) -> str:
    from .media import _EXT_FOR_MEDIA
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    by_ext = {e: mt for mt, e in _EXT_FOR_MEDIA.items()}
    by_ext[".jpeg"] = "image/jpeg"
    return by_ext.get(ext, "")


def read_file_content(path: str, *, text_cap: int = TEXT_CAP_BYTES) -> FileContent:
    """Read the workspace file at ``path`` for a turn: its text up to
    ``text_cap`` bytes, its media input, or what it is when neither fits."""
    if not gen.has_file(path):
        return FileContent(path, "missing")
    data = gen.read_file(path)
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    media_type = _media_type(path)
    if media_type:
        if len(raw) > ATTACH_BYTE_CAP:
            return FileContent(path, "media", size=len(raw), media_type=media_type)
        name = path.rsplit("/", 1)[-1]
        return FileContent(path, "media", size=len(raw), media_type=media_type,
                           media={"data": raw, "media_type": media_type,
                                  "name": name, "path": path})
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileContent(path, "binary", size=len(raw))
    if len(raw) > text_cap:
        return FileContent(path, "text", text=raw[:text_cap].decode("utf-8", "ignore"),
                           truncated=True, size=len(raw))
    return FileContent(path, "text", text=text, size=len(raw))


def read_selected(cfg: dict) -> SelectedFile | None:
    """The run's selected file when ``selected_file_first`` is on and the
    host passed one that is still in the workspace; ``None`` otherwise."""
    if not cfg.get("selected_file_first"):
        return None
    path = gen.selected_file
    if not path:
        return None
    content = read_file_content(path)
    if content.kind == "missing":
        _secret_log(f"selected file {path} is not in the workspace; the run starts without it")
        return None
    lead = f'The user started this run from the selected file "{path}".'
    if content.kind == "media":
        if content.media is None:
            return SelectedFile(path, f"{lead} It is {content.media_type}, {content.size} bytes — too large to "
                                      "attach; read it with your file tools if the task needs it.")
        name = path.rsplit("/", 1)[-1]
        return SelectedFile(
            path, f"{lead} It is attached ({content.media_type}).",
            media=[content.media],
            refs=[{"path": path, "name": name, "media_type": content.media_type}])
    if content.kind == "binary":
        return SelectedFile(path, f"{lead} It is a binary file of {content.size} bytes; read it with "
                                  "your file tools if the task needs it.")
    tail = (f"\n[Truncated: the first {TEXT_CAP_BYTES} of {content.size} bytes. Read the file for "
            "the rest.]" if content.truncated else "")
    return SelectedFile(path, f"{lead} Its content:\n\n<file path=\"{path}\">\n{content.text}\n</file>{tail}")
