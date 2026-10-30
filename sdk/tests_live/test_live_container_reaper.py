"""A container the live suite placed outlives a killed pytest process: the
fixture teardown that removes it never runs, and ``sleep infinity`` keeps it
up until someone notices. So every live container carries its session's pid
(``live_container_labels``) and ``reap_live_containers`` removes those of a
session that is gone, and only those."""
from __future__ import annotations

import subprocess
import sys
import uuid

from .conftest import (
    LIVE_CONTAINER_LABEL,
    docker_client,
    live_container_labels,
    reap_live_containers,
    requires_docker,
)

# Small, and the only pull the live suite makes that is not a harness image.
IMAGE = "busybox:latest"


def _image(client) -> str:
    import docker

    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        client.images.pull(IMAGE)
    return IMAGE


def _dead_session() -> str:
    """The pid of a process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return str(proc.pid)


def _place(client, image: str, labels: dict):
    return client.containers.create(
        image, "sleep infinity", labels=labels,
        name=f"collimate-live-reaper-{uuid.uuid4().hex[:8]}")


@requires_docker
def test_a_dead_sessions_containers_are_reaped_and_a_live_sessions_are_kept():
    client = docker_client()
    image = _image(client)
    orphan = _place(client, image, {LIVE_CONTAINER_LABEL: _dead_session()})
    mine = _place(client, image, live_container_labels())
    try:
        removed = reap_live_containers(client)
        assert orphan.name in removed
        assert mine.name not in removed
        left = {c.id for c in client.containers.list(all=True, filters={"label": LIVE_CONTAINER_LABEL})}
        assert orphan.id not in left
        assert mine.id in left

        # Session end names its own pid and takes only its own.
        assert reap_live_containers(client, of_session=live_container_labels()[LIVE_CONTAINER_LABEL]) == [mine.name]
    finally:
        for container in (orphan, mine):
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001 — already removed is the expected case
                pass


def test_an_unlabelled_or_unparseable_session_counts_as_gone():
    from .conftest import _session_alive

    assert _session_alive("") is False
    assert _session_alive("not-a-pid") is False
    assert _session_alive(live_container_labels()[LIVE_CONTAINER_LABEL]) is True
