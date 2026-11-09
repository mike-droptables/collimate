"""Helpers shared across the run-route modules.

Docker container cleanup, process reaping and releasing a run's generator tree.
"""

import asyncio
import logging
import shutil
import subprocess


from collimate_server.process_manager import ProcessManager

from collimate_server.run_registry import active_managers

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
    backstop on every process exit: the run-end fence calls it before the
    final git harvest (``FinalizationMixin._fence_run_processes``), and
    Stop all for every run it stops.

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


def _release_run_tree(manager) -> None:
    """The run no longer uses its generator version's stable tree: release
    its live count (env_uses), and after a native run find the uv
    environment it built, off the loop in the background. Never raises."""
    import collimate_server.env_uses as env_uses
    from collimate_server.task_utils import track_task
    key = getattr(manager, "env_key", None)
    if key is None:
        return
    env_uses.release(key)
    launch = getattr(manager, "native_launch", None)
    if launch is None:
        return
    try:
        track_task(asyncio.to_thread(env_uses.resolve_uv_env, key, *launch),
                   name=f"resolve_uv_env:{manager.run_id}")
    except Exception:
        logger.debug("uv environment lookup for run %s failed", manager.run_id, exc_info=True)


async def _reap_process(run_id: str, manager: ProcessManager):
    """Wait for the process to exit and its run-end cleanup to finish (whose
    fence reaped its containers, or, for a cleanup cut short before its
    fence, reap them here), then remove the run from active_managers and
    release its generator tree."""
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
    # A cleanup cancelled or crashed before its fence left the run's
    # containers running (lazygit, say): reap them by label here. Off the
    # loop: `docker rm -f` can block.
    if not getattr(manager, "_fenced", False) and getattr(manager.permissions, "docker", None) != "none":
        try:
            await asyncio.to_thread(_reap_run_containers, run_id)
        except Exception:
            logger.debug("run-container reap failed for %s", run_id, exc_info=True)
    # Every exit lands here — clean, crashed, force-stopped — so this is the
    # one place that releases the run's stable tree for all of them; after
    # the container reap, because a container bind-mounts the tree.
    _release_run_tree(manager)
