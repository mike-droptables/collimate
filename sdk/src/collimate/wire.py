# SPDX-License-Identifier: MIT
"""Public file codec for JSON IPC and journal boundaries."""
from __future__ import annotations

import base64
from typing import Optional

__all__ = ["encode_files", "decode_files"]


def encode_files(files: Optional[dict]) -> dict:
    """Encode a ``{path: str | bytes}`` mapping to a JSON-safe
    ``{path: {"text" | "b64": ...}}``.

    ``str`` values ride as ``{"text": s}``; bytes-like values (``bytes``,
    ``bytearray``, ``memoryview``) as ``{"b64": base64}``. The inverse is
    :func:`decode_files`. Used wherever file contents cross a JSON boundary
    (IPC and journals)."""
    out: dict = {}
    for path, content in (files or {}).items():
        key = path if isinstance(path, str) else str(path)
        if isinstance(content, (bytes, bytearray, memoryview)):
            out[key] = {"b64": base64.b64encode(bytes(content)).decode("ascii")}
        elif isinstance(content, str):
            out[key] = {"text": content}
        else:
            out[key] = {"text": str(content)}
    return out


def decode_files(encoded: Optional[dict]) -> dict:
    """Inverse of :func:`encode_files`. Tolerant: a bare string/None entry
    decodes to text, a bad base64 payload to empty bytes."""
    out: dict = {}
    for path, payload in (encoded or {}).items():
        if not isinstance(payload, dict):
            out[path] = "" if payload is None else str(payload)
            continue
        if "b64" in payload:
            try:
                out[path] = base64.b64decode(payload["b64"])
            except (ValueError, TypeError):
                out[path] = b""
        else:
            out[path] = payload.get("text", "")
    return out


