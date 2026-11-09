# SPDX-License-Identifier: MIT
"""The snapshot codecs a renderer package declares.

A renderer that registers a snapshot codec (``api.registerSnapshotCodec``)
ships it as a JSON descriptor in its package, ``{id, version,
implementationHash, source}`` (the codec's self-contained worker script and
its sha256), and lists the descriptor under ``snapshot_codecs:`` in its
manifest. A channel trusts the codecs of the renderers on its Capabilities
stack, beside the ones the SDK's renderer runtime builds; adding the renderer
to the channel is the only approval there is.

:func:`descriptor` checks one descriptor file and :func:`declared` reads
every one a manifest lists. Nothing here reads a channel or a store.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = ["CODEC_MAX_BYTES", "CodecDescriptor", "declared", "descriptor"]

CODEC_MAX_BYTES = 4 * 1024 * 1024
_CODEC_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CodecDescriptor:
    """One checked codec: its id, version and implementation hash (the
    sha256 of its worker script)."""
    id: str
    version: int
    implementation_hash: str

    def public(self) -> dict:
        """The codec as the browser's trust list names it."""
        return {"id": self.id, "version": self.version, "implementationHash": self.implementation_hash}


def descriptor(path: str, text: str) -> CodecDescriptor:
    """The descriptor file ``path`` (package-relative), its content ``text``,
    checked: exactly ``id`` (a lowercase name), ``version`` (a positive
    integer), ``source`` (the worker script, at most :data:`CODEC_MAX_BYTES`)
    and ``implementationHash`` (the sha256 of ``source``). ValueError says
    what is wrong."""
    where = f"snapshot codec {path!r}"
    if not path.endswith(".json") or path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"{where}: the descriptor is a .json file in the package")
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"{where}: not JSON") from None
    if not isinstance(data, dict) or set(data) != {"id", "version", "implementationHash", "source"}:
        raise ValueError(f"{where}: holds exactly id, version, implementationHash and source")
    if not isinstance(data["id"], str) or not _CODEC_ID.fullmatch(data["id"]):
        raise ValueError(f"{where}: id is a lowercase name (letters, digits, dots, hyphens)")
    if not isinstance(data["version"], int) or isinstance(data["version"], bool) or data["version"] < 1:
        raise ValueError(f"{where}: version is a positive integer")
    source = data["source"]
    if not isinstance(source, str) or not source or len(source.encode()) > CODEC_MAX_BYTES:
        raise ValueError(f"{where}: source is the codec's worker script, at most {CODEC_MAX_BYTES} bytes")
    digest = hashlib.sha256(source.encode()).hexdigest()
    claimed = data["implementationHash"]
    if not isinstance(claimed, str) or not _SHA256.fullmatch(claimed) or claimed != digest:
        raise ValueError(f"{where}: implementationHash is not the sha256 of its source ({digest})")
    return CodecDescriptor(data["id"], data["version"], digest)


def declared(manifest: Mapping, read: Callable[[str], str | None]) -> list[CodecDescriptor]:
    """Every codec a renderer manifest lists under ``snapshot_codecs``, in
    order, each file read with ``read(path)`` (package-relative; None when
    missing or not text). ValueError names the first problem."""
    paths = manifest.get("snapshot_codecs", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError("snapshot_codecs must be a list of file paths")
    result: list[CodecDescriptor] = []
    for path in paths:
        text = read(path)
        if text is None:
            raise ValueError(f"snapshot codec file {path!r} is missing or not text")
        codec = descriptor(path, text)
        if any(seen.implementation_hash == codec.implementation_hash for seen in result):
            raise ValueError(f"snapshot codec {path!r} is declared twice")
        result.append(codec)
    return result
