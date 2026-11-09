# SPDX-License-Identifier: MIT
"""Bounded workspace input media and model-output attachment writes."""
import os
import gen
from _agent_shared import ATTACH_BYTE_CAP

# Chat cell dir for files exchanged in the conversation — both what the user
# attaches and what the model returns. Kept together so the Files tab / the
# inline chat display resolve them the same way.
_ATTACH_DIR = "attachments"

_EXT_FOR_MEDIA = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/ogg": ".ogg", "video/mp4": ".mp4", "video/webm": ".webm",
    "application/pdf": ".pdf",
}


# Bounds for the one-shot's workspace-media scan — a cell full of media must
# not balloon the model input.
_WORKSPACE_MEDIA_MAX = 12
_WORKSPACE_MEDIA_BYTE_CAP = ATTACH_BYTE_CAP  # the engine's own input cap: a file past it would be dropped after taking a slot


def _workspace_media_inputs() -> list:
    """Workspace media files as the inputs of the one-shot's opening turn.

    The opening turn feeds every media-typed workspace file
    (the ``_EXT_FOR_MEDIA`` vocabulary — images/PDF/audio/video) to the model,
    exactly like a chat turn's attachments; each backend still drops what it
    can't express. Best-effort and bounded: the run's own ``_agent/`` files
    and the runs' interface files under ``_runs/`` are skipped,
    unreadable/empty/oversized files are skipped (the size is checked with a
    stat before the file is read), and at most
    ``_WORKSPACE_MEDIA_MAX`` ride along (sorted order, so the pick is stable).
    """
    media_for_ext = {ext: mt for mt, ext in _EXT_FOR_MEDIA.items()}
    media_for_ext[".jpeg"] = "image/jpeg"
    out: list = []
    root = gen.workspace_path()
    for path in sorted(gen.list_files()):
        if path.startswith(("_agent/", "_runs/")):
            continue
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        mt = media_for_ext.get(ext)
        if not mt:
            continue
        try:
            size = os.path.getsize(os.path.join(root, path))
        except OSError:
            continue
        if size <= 0 or size > _WORKSPACE_MEDIA_BYTE_CAP:
            continue
        try:
            data = gen.read_file(path)
        except Exception:  # noqa: BLE001 — a racy/unreadable file just skips
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data or len(data) > _WORKSPACE_MEDIA_BYTE_CAP:
            continue
        out.append({
            "data": data, "media_type": mt,
            "name": path.rsplit("/", 1)[-1],
            "path": path,     # its cell location — referenced, never re-staged
        })
        if len(out) >= _WORKSPACE_MEDIA_MAX:
            break
    return out


def _read_turn_attachments(turn: gen.ChatTurn) -> list:
    """Read the bytes of each file the user attached to this turn.

    ``turn.attachments`` are cell-relative refs ``{path, name, media_type}``;
    the bytes are already in the workspace (the host flushes attachment drafts
    before the turn). Returns the engine's input shape
    ``[{"data": bytes, "media_type", "name"}]``, skipping anything unreadable.
    """
    out: list = []
    for a in turn.attachments or []:
        path = a.get("path") if isinstance(a, dict) else None
        if not path or not gen.has_file(path):
            continue
        try:
            data = gen.read_file(path)
        except Exception:  # noqa: BLE001 — a missing/racy attachment just skips
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        out.append({
            "data": data,
            "media_type": (a.get("media_type") or "") if isinstance(a, dict) else "",
            "name": (a.get("name") or path.rsplit("/", 1)[-1]) if isinstance(a, dict) else path,
            # The file's location IN THE CELL (attachments/<name>): the
            # harness is pointed at this path, never at a staged copy.
            "path": path,
        })
    return out
