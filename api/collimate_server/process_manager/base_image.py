"""The host's base image, ``collimate-linux-small``: Debian with Python, Node,
uv and the SDK installed, which any generator can name in its environment
placement (``base_image: collimate-linux-small``) to run on or layer its own
image over. The host owns its Dockerfile (``base_image/`` beside this module,
like the runner image's ``runner_image/``) and stages the SDK it runs into the
build context itself, so no generator supplies a base context.

The context is staged fresh for every placement that names the base, into a
private temporary folder, and the tag is the content hash of what was staged
(:func:`collimate.image_tags.derive_image_tag`): a Dockerfile edit or a new
SDK is a tag this machine has not built. Only the SDK subset of the
collimate distribution is staged: its Python packages ``collimate`` and
``gen`` (never their package data, the docs and renderer runtime, the
renderer compilers and bundler tools: a container imports the SDK's modules
and its native verifier only), the distribution's metadata, and the SDK's own
requirements (``collimate.shared.sdk_requirements``), never the server
(``collimate_server``) or its dependencies. No account material is copied.

Stdlib and the SDK only: ``sdk/tests/test_forward_image.py`` loads this file
on its own to pin what the image carries.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from collimate.image_tags import context_digest, derive_image_tag

#: The name a placement spec gives the base (``base_image``), and the
#: repository of its content-addressed tag.
BASE_IMAGE = "collimate-linux-small"
#: The build arg every layered Dockerfile takes for its base
#: (``ARG BASE=collimate-linux-small:v3`` / ``FROM ${BASE}``); the host
#: injects the resolved tag.
BASE_ARG = "BASE"
_NAME = BASE_IMAGE.removeprefix("collimate-")
SOURCE = Path(__file__).resolve().with_name("base_image")
# The SDK's package data under ``collimate/``, which the image leaves out.
_SDK_PACKAGE_DATA = frozenset({"renderer_compilers", "renderer_tools", "shared"})


@dataclass(frozen=True)
class StagedBase:
    """A staged base context: its folder, content digest and tag."""

    context: str
    digest: str
    tag: str


def _stage_tree(source: Path, target: Path, skip: frozenset[str] = frozenset()) -> None:
    for parent, directories, files in os.walk(source):
        top = Path(parent) == source
        directories[:] = sorted(d for d in directories
                                if d not in ("__pycache__", "llm") and not (top and d in skip))
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f"SDK build context contains a symlink: {path.name}")
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())


def stage(root: str | os.PathLike) -> StagedBase:
    """Stage the base's Dockerfile and the SDK this host runs into ``root``
    (an empty folder) and answer its identity."""
    from importlib.resources import files
    from collimate.shared import sdk_distribution, sdk_requirements

    root = Path(root)
    _stage_tree(SOURCE, root)
    sdk, facade = files("collimate"), files("gen")
    if not isinstance(sdk, Path) or not isinstance(facade, Path):
        raise RuntimeError("Base image staging requires filesystem-backed collimate and gen packages")
    if not sdk.is_dir() or not facade.is_dir():
        raise RuntimeError("Base image staging: the SDK package directories are missing")
    _stage_tree(sdk.resolve(), root / "sdk" / "collimate", _SDK_PACKAGE_DATA)
    _stage_tree(facade.resolve(), root / "sdk" / "gen")
    distribution = sdk_distribution()
    # The SDK packages' own requirements, never the server's.
    (root / "sdk-requirements.txt").write_text("\n".join(sdk_requirements()) + "\n", encoding="utf-8")
    # Source imports and the distribution's metadata travel together for draft
    # tools that introspect the collimate version inside uv's env.
    metadata = distribution.read_text("METADATA")
    if metadata:
        info = root / "sdk" / f"collimate-{distribution.version}.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "METADATA").write_text(metadata, encoding="utf-8")
    context = str(root)
    return StagedBase(context, context_digest(context), derive_image_tag(_NAME, context))


@contextmanager
def staged() -> Iterator[StagedBase]:
    """The base staged into a private temporary folder, removed afterwards."""
    root = tempfile.mkdtemp(prefix="collimate-base-")
    try:
        yield stage(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def layered_tag(repository: str, build_context: str, base: StagedBase, build_args: dict) -> str:
    """The tag of an image built from ``build_context`` over ``base``:
    ``repository`` (``collimate-<name>``, untagged) with the hash of the
    context, the base's digest and the build args (``BASE`` among them)."""
    if not repository.startswith("collimate-") or ":" in repository or "@" in repository:
        raise ValueError("an image layered on the base is named collimate-<name>, untagged; "
                         "the host tags it")
    return derive_image_tag(repository.removeprefix("collimate-"), build_context,
                            base_digest=base.digest, build_args=build_args)


__all__ = ["BASE_ARG", "BASE_IMAGE", "SOURCE", "StagedBase", "layered_tag", "stage", "staged"]
