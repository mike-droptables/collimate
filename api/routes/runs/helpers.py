"""Helpers shared across the run-route modules.

Docker container and app-dir cleanup and process reaping.
"""

import asyncio
import logging
import os
import shutil
import subprocess


from process_manager import ProcessManager

from run_registry import active_managers

logger = logging.getLogger(__name__)


_DOCKER_CHECKED = False
_DOCKER_PATH: str | None = None


def _docker_bin() -> str | None:
    """Resolve the docker CLI once and cache it; None when docker isn't on PATH
    (no docker → no generator containers to reap, so the reaper is a no-op)."""
    global _DOCKER_CHECKED, _DOCKER_PATH
    if not _DOCKER_CHECKED:
        _DOCKER_PATH = shutil.which("docker")
        _DOCKER_CHECKED = True
    return _DOCKER_PATH


def _reap_run_containers(run_id: str) -> None:
    """Force-remove any Docker containers a generator started for this run.

    Generator containers (``DockerEnvironment``) run **detached** and labeled
    ``com.collimate.run_id``. Their normal teardown is the generator's own
    ``finally: env.close()`` — but a force-stop SIGKILLs the generator before
    that runs (and a crash skips it too), leaking a *live* container: the
    process inside it (e.g. lazygit) keeps running. The container lives in
    Docker's own lifecycle, not the generator's process group, so signalling
    the generator never reaches it. Reap by the run_id label as a host-side
    backstop on every process exit.

    Idempotent — on a clean/graceful exit the generator already removed the
    container and this finds nothing. Best-effort; never raises. The empty
    run_id guard matters: ``label=com.collimate.run_id=`` would match unrelated
    containers, so we never reap without a concrete id.
    """
    if not run_id:
        return
    docker = _docker_bin()
    if not docker:
        return
    try:
        out = subprocess.run(
            [docker, "ps", "-aq", "--filter", f"label=com.collimate.run_id={run_id}"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        logger.debug("container reap: `docker ps` failed for run %s", run_id, exc_info=True)
        return
    ids = [c for c in out.stdout.split() if c]
    if not ids:
        return
    logger.info("reaping %d leaked container(s) for run %s", len(ids), run_id)
    try:
        subprocess.run([docker, "rm", "-f", *ids], capture_output=True, timeout=30)
    except Exception:
        logger.debug("container reap: `docker rm -f` failed for run %s", run_id, exc_info=True)


# The per-run app tree is mkdtemp'd as ``app_<run8>_<rand>`` under
# .collimate/tmp (create.py); its sibling workspace is ``ws_*``. The prefix
# is the reason a tree
# can be identified — and a workspace ruled out — from ``manager.app_dir``.
_APP_TREE_PREFIX = "app_"


def _run_app_tree(manager) -> str | None:
    """The per-run temp tree to reclaim, or None when we can't prove one.

    ``manager.app_dir`` is the generator's OWN subdir inside that tree (the
    tree also carries the ``_*`` shared siblings, see create.py), so the tree
    is one level up — but accept ``app_dir`` itself too, for a manager built
    straight onto a tmpdir. Requiring the mkdtemp prefix is what keeps this
    from ever walking up into ``.collimate/tmp`` and deleting every run's
    tree, or into a manager whose app_dir came from somewhere else entirely.
    """
    app_dir = manager.app_dir or ""
    if not app_dir:
        return None
    app_dir = os.path.abspath(app_dir)
    for cand in (os.path.dirname(app_dir), app_dir):
        if os.path.basename(cand).startswith(_APP_TREE_PREFIX):
            return cand
    return None


def _reclaim_run_app_dir(manager) -> None:
    """Delete a finished run's app tree. Best-effort; never raises.

    Valid once the run's process, and any container bind-mounting the tree,
    is gone: app_dir is the launch cwd / bind-mount (backends.py,
    docker_runner.py) and the source of the generator definition for
    ``env_passthrough``, read while the run starts and while it is live. No
    run-end cleanup step reads it (finalize, the run-folder sweep and the
    session, environment and lease steps work from the DB, the cell and the
    workspace). The reaper calls it once the cleanup task has finished and
    the run has left ``active_managers``; stop_all_runs (Stop all, server
    shutdown) calls it for every run in its sweep, still registered and
    whatever its cleanup's progress, because at shutdown the reapers die
    with the loop. Completed
    runs did not, so .collimate/tmp grew one generator-tree copy per run,
    forever.

    Two things this deliberately does NOT touch. The run's WORKSPACE, the
    sibling ``ws_*`` dir holding hydrated inputs and generator output. And
    ``~/.cache/uv``, where the same per-run tree costs the most: uv keys a
    script's env on the entrypoint's ABSOLUTE path, so a fresh tree per run
    mints a fresh env under ``environments-v2`` (5.9 GB across 748 envs on one
    dev box). Deleting the tree doesn't reclaim those, and that cache is
    machine-wide — shared with everything else the user runs — so pruning it is
    uv's business, not ours. This therefore bounds OUR footprint under
    .collimate/tmp and nothing else; bounding the uv side needs a STABLE app
    path per generator version, not a sweeper.
    """
    try:
        tree = _run_app_tree(manager)
        if tree is None:
            return
        real = os.path.realpath(tree)
        ws = os.path.realpath(manager.workspace_dir or "")
        if ws and (ws == real or ws.startswith(real + os.sep)):
            logger.warning(
                "app-dir reclaim skipped for run %s: workspace %s lives inside %s",
                manager.run_id, ws, real,
            )
            return
        shutil.rmtree(tree, ignore_errors=True)
    except Exception:
        logger.debug(
            "app-dir reclaim failed for run %s",
            manager.run_id, exc_info=True,
        )


async def _reap_process(run_id: str, manager: ProcessManager):
    """Wait for the process to exit and its run-end cleanup to finish, then
    remove the run from active_managers, reap its containers and reclaim its
    app tree."""
    if hasattr(manager, '_cleanup_task') and manager._cleanup_task is not None:
        # A cleanup task cancelled out from under us (loop shutdown mid-run)
        # re-raises CancelledError here, and one whose finalize crashed
        # re-raises that error after its own finally. Neither stops the
        # reaper: it is the only thing that removes the run from
        # active_managers, so it still drains the writer and pops below.
        try:
            await manager._cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("run-end cleanup of run %s failed", run_id)
    elif manager.process:
        await manager.process.wait()
    # Drain the log writer's final flush before removing the manager so
    # tail log lines (a final push_log notice, the stop event) actually
    # land in run_events. _wait_and_cleanup awaits it too, so this is a
    # no-op on the normal path.
    writer = getattr(manager, '_writer_task', None)
    if writer is not None:
        try:
            await writer
        except (Exception, asyncio.CancelledError):
            pass
    active_managers.pop(run_id, None)
    # Backstop: force-remove any Docker container the generator started
    # for this run. On a clean/graceful exit the generator's
    # `finally: env.close()` already did this (no-op here); on force-stop
    # (SIGKILL) or a crash that finally never ran, so without this the
    # container — and the process inside it, e.g. lazygit — keeps running
    # after the run is "stopped". Offloaded to a thread: `docker rm -f`
    # can block for seconds and must not stall the loop.
    if getattr(manager.permissions, "docker", None) != "none":
        try:
            await asyncio.to_thread(_reap_run_containers, run_id)
        except Exception:
            logger.debug("run-container reap failed for %s", run_id, exc_info=True)

    # Reclaim the run's temp app tree. Every exit lands here — clean,
    # crashed, force-stopped — so this is the one place that catches all
    # of them; the container reap above is the reason it comes after (a
    # container bind-mounts the tree). Off the loop: rmtree walks a real
    # directory tree.
    await asyncio.to_thread(_reclaim_run_app_dir, manager)
