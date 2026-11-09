# SPDX-License-Identifier: MIT
"""Content-addressed image tags, shared by generators and the host.

A generator tags an image it builds from its own context with
:func:`derive_image_tag`; the host tags its base image
(``collimate-linux-small``) and every image layered on it the same way, so a
change to any input is a tag no machine has built yet. Stdlib only."""
from __future__ import annotations

import hashlib
import os

__all__ = ["context_digest", "derive_image_tag"]


def context_digest(path: str) -> str:
    """A stable content hash of a build context (sorted, framed relpath + size + streamed bytes).
    Python bytecode caches (``__pycache__`` / ``*.pyc`` / ``*.pyo``) are
    excluded: a test importing a context's script must not re-tag the image.

    This is an image's IDENTITY. Deriving the tag from it is what makes a
    Dockerfile edit propagate: a changed context is a different tag, and a
    tag the host has never seen is built. With a fixed tag like
    ``collimate-goose:latest`` the opposite is true — the host's
    build-if-missing check sees the tag present and never reads the Dockerfile
    again, so a corrected harness pin can sit unused forever."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fname in sorted(filenames):
            if fname.endswith((".pyc", ".pyo")):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel = os.path.relpath(abs_path, path).replace("\\", "/")
            try:
                with open(abs_path, "rb") as fh:
                    h.update(rel.encode("utf-8") + b"\0")
                    h.update(str(os.fstat(fh.fileno()).st_size).encode("ascii") + b"\0")
                    while chunk := fh.read(1024 * 1024):
                        h.update(chunk)
            except OSError:
                continue
    return h.hexdigest()


def derive_image_tag(
    name: str, context: str, *, base_digest: str = "", build_args: "dict | None" = None,
) -> str:
    """``collimate-<name>:<12 hex>`` — the content-addressed tag for a build.

    The identity is every input that changes what the image CONTAINS:

      * the build context's bytes — so a Dockerfile edit re-tags;
      * ``base_digest`` — so rebuilding the shared base re-tags everything
        layered on it;
      * ``build_args`` — so a runner asking for a different artifact version
        re-tags. This one is load-bearing for the runner-declared version
        design: the Dockerfile's install line reads ``@${TOOL_VERSION}``, so
        the version lives in the args, not in the context bytes. Omit it and
        two different requested versions would collide on one tag, which is
        exactly the stale-image bug in a new place."""
    digest = context_digest(context)
    extra = base_digest
    for key in sorted(build_args or {}):
        extra += f"\0{key}={build_args[key]}"
    if extra:
        digest = hashlib.sha256((digest + extra).encode()).hexdigest()
    return f"collimate-{name}:{digest[:12]}"
