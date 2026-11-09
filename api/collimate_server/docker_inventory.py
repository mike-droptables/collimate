"""The Collimate-built images on the Docker daemon: inventory, a selection's
reclaim, preview and delete (``/api/storage/docker*``).

**Which images.** An image is Collimate-built when one of its tags has a
``collimate-*`` repository (``collimate-linux-small``, ``collimate-runner``,
the layered ``collimate-<name>``). Builds also add the label
``com.collimate.built=1``, but images built before it have none, so the
name decides.

**Sizes.** Each image reports Docker's own figures from ``df()``: ``size``
(every layer, shared ones included), ``shared_size``, and ``unique_size =
size - shared_size``, the bytes deleting that image alone frees. A
selection's reclaim is the sum of the layers only images in the selection
reference: layers are identified by chain id (from ``RootFS.Layers``), every
image ``df()`` lists counts as a reference, and a layer's size is that of the
``history`` entry that created it, in order. The figure is ``exact`` only
when that layer model reproduces Docker's ``Size`` and ``SharedSize`` of
every selected image; otherwise (the containerd image store, whose history
sizes are unpacked bytes while ``Size`` also counts the compressed content,
or a history whose layer entries cannot be told apart) it falls back to
``sum(unique_size)`` with ``exact: false``, a lower bound ("at least").

**Candidates.** An image with any container (any state) is in use and never
a candidate; nor is one carrying a tag outside ``collimate-*``. ``unused``
keeps the images whose last recorded use (:mod:`collimate_server.image_uses`)
is older than the given days, or that have none.

**Delete** takes a preview (valid ten minutes, used once) and re-checks each
image under a removal claim: still on the daemon, no container, no foreign
tag, no use recorded since the preview (nor within the days, for
``unused``), no ensure of its tags running. It then removes each of its
``collimate-*`` tags with ``force=False``; Docker removes the image with its
last tag.

The daemon is machine-wide and each server records only its own uses, so
another Collimate server's images look unused here; deleting one only makes
that server build it again on its next use.

Docker's build cache is reported, not offered for clean-up: it cannot be
attributed to Collimate, and pruning it affects every project on the daemon.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Sequence

from collimate_server import image_uses

#: How long a preview may be deleted from, in seconds.
PREVIEW_TTL_SECONDS = 600

Mode = Literal["all", "unused"]

_clock = time.monotonic


class DockerUnavailable(Exception):
    """The daemon cannot be reached; ``str()`` is the reason."""


def docker_client():
    """A client of the local daemon, pinged; :class:`DockerUnavailable` when
    there is none."""
    try:
        import docker
        import docker.errors
    except ImportError as exc:  # pragma: no cover - docker is a server dependency
        raise DockerUnavailable(f"the docker package is not installed: {exc}") from None
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        raise DockerUnavailable(str(exc)) from None
    return client


@dataclass(frozen=True)
class ImageRow:
    id: str
    refs: tuple[str, ...]
    other_refs: tuple[str, ...]
    size: int
    shared_size: int
    unique_size: int
    containers: int
    created_at: str
    last_used_at: str | None

    def wire(self) -> dict:
        row: dict = {
            "id": self.id, "refs": list(self.refs), "other_refs": list(self.other_refs),
            "size": self.size, "shared_size": self.shared_size, "unique_size": self.unique_size,
            "containers": self.containers, "created_at": self.created_at,
        }
        if self.last_used_at is not None:
            row["last_used_at"] = self.last_used_at
        return row


@dataclass(frozen=True)
class Inventory:
    images: tuple[ImageRow, ...]
    build_cache_bytes: int
    #: The id of every image ``df()`` lists, Collimate's or not: the
    #: reference set of the layer model.
    daemon: tuple[str, ...]


def last_use(refs: Iterable[str], image_id: str, uses: dict[str, image_uses.ImageUse]) -> str | None:
    """The latest recorded use of ``image_id`` under any of ``refs``. A
    record that names another image id was a use of an image the tag no
    longer points to."""
    times = [use.last_used_at for ref in refs if (use := uses.get(ref)) is not None
             and use.image_id in (None, image_id)]
    return max(times) if times else None


def _container_counts(client) -> Counter[str]:
    return Counter(str(c.get("ImageID") or "") for c in client.api.containers(all=True))


def _build_cache_bytes(df: dict) -> int:
    # As ``docker system df`` counts it: an entry shared with another is
    # counted there, not here.
    return sum(int(entry.get("Size") or 0) for entry in df.get("BuildCache") or ()
               if not entry.get("Shared"))


def read_inventory(client, uses: dict[str, image_uses.ImageUse]) -> Inventory:
    """Every Collimate-built image with its sizes, containers and last use."""
    df = client.df()
    containers = _container_counts(client)
    rows: list[ImageRow] = []
    daemon: list[str] = []
    for entry in df.get("Images") or ():
        image_id = str(entry["Id"])
        size = int(entry.get("Size") or 0)
        shared = max(int(entry.get("SharedSize") or 0), 0)
        daemon.append(image_id)
        tags = [tag for tag in entry.get("RepoTags") or () if tag and tag != "<none>:<none>"]
        refs = tuple(sorted(tag for tag in tags if image_uses.is_collimate_ref(tag)))
        if not refs:
            continue
        rows.append(ImageRow(
            id=image_id, refs=refs,
            other_refs=tuple(sorted(tag for tag in tags if not image_uses.is_collimate_ref(tag))),
            size=size, shared_size=shared, unique_size=max(size - shared, 0),
            containers=containers[image_id],
            created_at=_iso(datetime.fromtimestamp(int(entry.get("Created") or 0), timezone.utc)),
            last_used_at=last_use(refs, image_id, uses),
        ))
    rows.sort(key=lambda row: (row.refs[0], row.id))
    return Inventory(tuple(rows), _build_cache_bytes(df), tuple(daemon))


def _iso(moment: datetime) -> str:
    """``moment`` in the wire form: UTC, whole seconds, with its offset."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _older_than(last_used_at: str | None, days: int, now: datetime) -> bool:
    return last_used_at is None or datetime.fromisoformat(last_used_at) < now - timedelta(days=days)


def candidates(inventory: Inventory, mode: Mode, days: int | None, now: datetime) -> list[ImageRow]:
    """The images ``mode`` selects: no container, no foreign tag, and for
    ``unused`` no recorded use in the last ``days``."""
    rows = [row for row in inventory.images if row.containers == 0 and not row.other_refs]
    if mode == "unused":
        assert days is not None
        rows = [row for row in rows if _older_than(row.last_used_at, days, now)]
    return rows


# -- the layer model ----------------------------------------------------------

def chain_ids(diff_ids: list[str]) -> list[str]:
    """The chain id of each layer (what the daemon stores a layer under): a
    layer is shared only where its whole stack below is too."""
    chains: list[str] = []
    for diff in diff_ids:
        chains.append(diff if not chains else
                      "sha256:" + hashlib.sha256(f"{chains[-1]} {diff}".encode()).hexdigest())
    return chains


def layer_sizes(history: list[dict], layers: int) -> list[int] | None:
    """Each layer's size from an image's ``history`` (newest first), or None
    when its layer-creating entries cannot be told apart: every entry is a
    layer when there are as many as layers, else the entries with bytes must
    be exactly the layers."""
    sizes = [int(entry.get("Size") or 0) for entry in reversed(history)]
    if len(sizes) == layers:
        return sizes
    sized = [size for size in sizes if size > 0]
    return sized if len(sized) == layers else None


@dataclass(frozen=True)
class LayerModel:
    #: image id → its chain ids, for every image ``df()`` lists.
    chains: dict[str, frozenset[str]]
    #: chain id → bytes, for the layers of the measured images.
    sizes: dict[str, int]


def measure(client, inventory: Inventory, selection: Sequence[ImageRow]) -> LayerModel | None:
    """The layer model for ``selection``, or None when it does not reproduce
    Docker's ``Size`` and ``SharedSize`` of every selected image. The
    selected images are measured first, so a daemon whose sizes do not map
    to layers (the containerd store) answers before every image is read."""
    import docker.errors

    def stack_of(image_id: str) -> list[str] | None:
        try:
            diff_ids = client.api.inspect_image(image_id).get("RootFS", {}).get("Layers")
        except docker.errors.NotFound:
            return None  # the daemon changed under the measurement
        return chain_ids([str(d) for d in diff_ids]) if isinstance(diff_ids, list) else None

    measured: dict[str, tuple[list[str], list[int]]] = {}
    for row in selection:
        stack = stack_of(row.id)
        if stack is None:
            return None
        found = layer_sizes(client.api.history(row.id), len(stack))
        if found is None or sum(found) != row.size:
            return None
        measured[row.id] = (stack, found)
    chains: dict[str, frozenset[str]] = {}
    for image_id in inventory.daemon:
        stack = measured[image_id][0] if image_id in measured else stack_of(image_id)
        if stack is None:
            return None
        chains[image_id] = frozenset(stack)
    holders: Counter[str] = Counter(chain for stack in chains.values() for chain in stack)
    sizes: dict[str, int] = {}
    for row in selection:
        stack, found = measured[row.id]
        if sum(size for chain, size in zip(stack, found) if holders[chain] > 1) != row.shared_size:
            return None
        sizes.update(zip(stack, found))
    return LayerModel(chains, sizes)


def reclaim(rows: Iterable[ImageRow], model: LayerModel | None) -> tuple[int, bool]:
    """``(bytes, exact)`` deleting ``rows`` frees: the layers only they
    reference when the model is there, else ``sum(unique_size)``, a lower
    bound."""
    rows = list(rows)
    if not rows:
        return 0, True
    if model is None:
        return sum(row.unique_size for row in rows), False
    chosen = {row.id for row in rows}
    inside = set().union(*(model.chains[image_id] for image_id in chosen))
    outside = set().union(*(stack for image_id, stack in model.chains.items() if image_id not in chosen))
    return sum(model.sizes[chain] for chain in inside - outside), True


# -- preview and delete -------------------------------------------------------

@dataclass(frozen=True)
class Preview:
    id: str
    mode: Mode
    days: int | None
    created_at: str
    expires_at: str
    deadline: float
    images: tuple[ImageRow, ...]
    model: LayerModel | None

    def wire(self) -> dict:
        size, exact = reclaim(self.images, self.model)
        return {"preview_id": self.id, "count": len(self.images), "bytes": size, "exact": exact,
                "expires_at": self.expires_at, "images": [row.wire() for row in self.images]}


_previews: dict[str, Preview] = {}
_previews_lock = threading.Lock()


def make_preview(client, mode: Mode, days: int | None, now: datetime) -> Preview:
    """The selection ``mode`` makes now, held for :data:`PREVIEW_TTL_SECONDS`."""
    inventory = read_inventory(client, image_uses.last_uses())
    rows = candidates(inventory, mode, days, now)
    model = measure(client, inventory, rows) if rows else None
    preview = Preview(
        id=uuid.uuid4().hex, mode=mode, days=days,
        created_at=_iso(now), expires_at=_iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
        deadline=_clock() + PREVIEW_TTL_SECONDS, images=tuple(rows), model=model,
    )
    with _previews_lock:
        for stale in [key for key, held in _previews.items() if held.deadline <= _clock()]:
            del _previews[stale]
        _previews[preview.id] = preview
    return preview


def take_preview(preview_id: str) -> Preview | None:
    """The preview, once: None when it is unknown, used or expired."""
    with _previews_lock:
        preview = _previews.pop(preview_id, None)
    if preview is None or preview.deadline <= _clock():
        return None
    return preview


def _refusal(client, row: ImageRow, preview: Preview, now: datetime) -> tuple[tuple[str, ...], str | None]:
    """The image's ``collimate-*`` tags now, and why it may not go (None
    when it may)."""
    import docker.errors
    try:
        tags = [tag for tag in client.api.inspect_image(row.id).get("RepoTags") or () if tag]
    except docker.errors.NotFound:
        return row.refs, "it is no longer on the daemon"
    refs = tuple(sorted(tag for tag in tags if image_uses.is_collimate_ref(tag)))
    foreign = sorted(tag for tag in tags if not image_uses.is_collimate_ref(tag))
    if foreign:
        return refs or row.refs, f"it also carries a tag Collimate did not make ({', '.join(foreign)})"
    if not refs:
        return row.refs, "it no longer carries a collimate tag"
    if _container_counts(client)[row.id]:
        return refs, "a container uses it"
    used = last_use(refs, row.id, image_uses.last_uses())
    if used is not None and used > preview.created_at:
        return refs, "it was used after the preview"
    if preview.mode == "unused" and not _older_than(used, preview.days or 0, now):
        return refs, f"it was used in the last {preview.days} days"
    return refs, None


def delete(client, preview: Preview, now: datetime) -> dict:
    """Remove the preview's images that still qualify, tag by tag:
    ``{removed: [ref], failed: [{ref, reason}], bytes_freed_estimate, exact}``."""
    import docker.errors
    removed: list[str] = []
    failed: list[dict] = []
    gone: list[ImageRow] = []
    for row in preview.images:
        with image_uses.removal_claim(row.refs) as claimed:
            if not claimed:
                failed += [{"ref": ref, "reason": "a run is preparing it"} for ref in row.refs]
                continue
            refs, reason = _refusal(client, row, preview, now)
            if reason is None and not set(refs) <= set(row.refs):
                reason = "it was tagged again after the preview"
            if reason is not None:
                failed += [{"ref": ref, "reason": reason} for ref in refs]
                continue
            left = len(refs)
            for ref in refs:
                try:
                    client.images.remove(image=ref, force=False, noprune=False)
                except docker.errors.APIError as exc:
                    failed.append({"ref": ref, "reason": str(exc.explanation or exc)})
                    continue
                removed.append(ref)
                left -= 1
            if not left:
                gone.append(row)
    size, exact = reclaim(gone, preview.model)
    return {"removed": removed, "failed": failed, "bytes_freed_estimate": size, "exact": exact}


def listing() -> dict:
    """``GET /api/storage/docker``'s answer."""
    try:
        client = docker_client()
    except DockerUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    try:
        inventory = read_inventory(client, image_uses.last_uses())
    finally:
        client.close()
    return {"available": True, "images": [row.wire() for row in inventory.images],
            "build_cache_bytes": inventory.build_cache_bytes}
