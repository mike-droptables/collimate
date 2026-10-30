"""Docker image preparation and orphan-container cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from typing import Callable

from instance import INSTANCE_LABEL, instance_id, owns_container, reap_all_instances

_logger = logging.getLogger("sandbox")

LogSink = Callable[[str], None]


def _noop_log(_line: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Image build / probe — one-per-image, lazy, idempotent.
# ---------------------------------------------------------------------------

_image_locks: dict[str, asyncio.Lock] = {}


def _image_lock(image: str) -> asyncio.Lock:
    lock = _image_locks.get(image)
    if lock is None:
        lock = asyncio.Lock()
        _image_locks[image] = lock
    return lock


async def _probe_docker_image(image: str) -> tuple[str, str]:
    """Return (status, detail). status ∈ {present, missing, daemon_down,
    no_docker}."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return "no_docker", ""
    assert proc.stdout is not None
    output = (await proc.stdout.read()).decode("utf-8", errors="replace")
    from process_manager.finalization import wait_process_exit
    await wait_process_exit(proc)
    rc = proc.returncode
    if rc == 0:
        return "present", ""
    if "Cannot connect to the Docker daemon" in output:
        return "daemon_down", output.strip()
    return "missing", output.strip()


async def _build_image(
    image: str, dockerfile_dir: str, log: LogSink, *, refresh: bool = True,
    build_args: dict[str, str] | None = None,
) -> None:
    if not os.path.isdir(dockerfile_dir):
        raise RuntimeError(
            f"cannot build {image}: dockerfile_dir {dockerfile_dir!r} does "
            f"not exist."
        )
    if not os.path.isfile(os.path.join(dockerfile_dir, "Dockerfile")):
        raise RuntimeError(
            f"cannot build {image}: no Dockerfile at "
            f"{os.path.join(dockerfile_dir, 'Dockerfile')!r}."
        )

    intro = (
        f"[sandbox] building image {image} from {dockerfile_dir} — first "
        f"run only, takes 2-5 minutes depending on network speed. "
        f"Subsequent runs reuse the cached image."
    )
    log(intro)
    print(intro, file=sys.stderr, flush=True)

    try:
        # --pull re-resolves the floating upstream FROM (python:3.13-slim)
        # when the run's grant permits reaching upstream. It only costs
        # anything on a build, and a build only happens when the image is
        # absent, so a warm host pays nothing either way.
        argv = ["docker", "build"]
        if refresh:
            argv.append("--pull")
        for name, value in sorted((build_args or {}).items()):
            argv += ["--build-arg", f"{name}={value}"]
        argv += ["-t", image, dockerfile_dir]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"`docker` not found on PATH while trying to build {image}. "
            "Install Docker Desktop or docker-ce."
        )
    assert proc.stdout is not None

    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        log(f"[sandbox build {image}] {line}")
        print(f"[sandbox build {image}] {line}", file=sys.stderr, flush=True)

    from process_manager.finalization import wait_process_exit
    await wait_process_exit(proc)
    rc = proc.returncode
    if rc != 0:
        raise RuntimeError(
            f"`docker build` failed (exit {rc}) for {image}. See "
            f"[sandbox build {image}] lines above."
        )

    done = f"[sandbox] image {image} built"
    log(done)
    print(done, file=sys.stderr, flush=True)


async def ensure_image(
    image: str, dockerfile_dir: str, log: LogSink, *, refresh: bool = True,
    build_args: dict[str, str] | None = None,
) -> None:
    """Inspect ``image`` before each use and build it when absent. ``refresh`` (the
    run's ``update_packages`` grant) lets the build re-pull its base."""
    lock = _image_lock(image)
    if lock.locked():
        log(f"[sandbox] waiting for in-progress {image} build...")
    async with lock:
        status, detail = await _probe_docker_image(image)
        if status == "present":
            return
        if status == "no_docker":
            raise RuntimeError(
                "`docker` not found on PATH. Install Docker Desktop or "
                "docker-ce."
            )
        if status == "daemon_down":
            raise RuntimeError(
                "Docker is installed but the daemon is not reachable. Start "
                "Docker Desktop (or `sudo systemctl start docker` on Linux)."
                f"\nRaw error:\n{detail}"
            )
        if build_args is None:
            await _build_image(image, dockerfile_dir, log, refresh=refresh)
        else:
            await _build_image(image, dockerfile_dir, log, refresh=refresh, build_args=build_args)


def reap_orphan_containers() -> None:
    """Remove any collimate-sandbox-* containers THIS server left behind by a
    previous API process that crashed. Best-effort; failures are swallowed.

    The name prefix alone no longer identifies our own orphans: a second
    Collimate server can be live on the same dockerd (the dev run beside the
    installed app, separate data dirs, separate ports), and its containers
    carry the identical ``collimate-sandbox-`` prefix. We ask Docker for the
    owner label alongside each id — one `docker ps` call, no per-container
    inspect — and keep only what ``instance.owns_container`` says is ours
    (our own id, or unlabelled and therefore pre-dating this label).

    ``COLLIMATE_REAP_ALL_INSTANCES=1`` waives the ownership filter and sweeps
    every ``collimate-sandbox-`` container, which is what a machine with no
    second server — or a harness whose data dir is thrown away, so that
    nothing will ever claim its stamp again — wants. See api/instance.py."""
    try:
        # Inside the try because instance_id() goes through os.getcwd(), which
        # raises when the data dir this process was started in has been
        # deleted underneath it (a test harness's tmp dir, an unmounted
        # volume). This function promises swallowed failures to a caller on
        # the startup path, so nothing in it may escape — not even the part
        # that runs before the first docker call.
        mine = instance_id()
        reap_all = reap_all_instances()
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "name=^collimate-sandbox-",
                "--format", '{{.ID}} {{.Label "' + INSTANCE_LABEL + '"}}',
            ],
            capture_output=True, text=True, timeout=5,
        )
        ids: list[str] = []
        for line in result.stdout.strip().splitlines():
            # "<id> <label>" — the label field is empty (so the line is just
            # the id) for a container created before the label existed.
            cid, _, label = line.strip().partition(" ")
            if cid and owns_container(label, mine, reap_all=reap_all):
                ids.append(cid)
        if ids:
            _logger.info(
                "[sandbox] removing %d orphaned collimate-sandbox container(s)",
                len(ids),
            )
            subprocess.run(
                ["docker", "rm", "-f", *ids],
                capture_output=True, timeout=30,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception as exc:
        _logger.warning("[sandbox] orphan reaper failed: %s", exc)
