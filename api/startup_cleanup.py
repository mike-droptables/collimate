"""Startup orphan selection and background cleanup for this server instance."""
import logging
import os
import signal
from instance import instance_id, owns_container, reap_all_instances
logger = logging.getLogger("collimate.startup_cleanup")

def orphan_generator_container_ids(
    containers, boot_ts: float, mine: str, reap_all: bool = False,
) -> list[str]:
    """Of ``containers`` (every container carrying the run_id label), the ids
    this server may force-remove at boot. Pure — no docker calls — so the two
    cutoffs that decide a live container's fate are unit-testable.

    Two independent guards, both of which a container must clear:

    1. **Created before this boot.** No run survives a server restart, so a
       labelled container older than our boot timestamp is an orphan from a
       previous session (a force-stop or crash that skipped the generator's
       own cleanup). The cutoff is load-bearing because the reap runs as a
       delayed background thread — under a slow daemon the listing can land
       AFTER the first post-boot run placed its container, and an unfiltered
       sweep would kill that live run's container out from under it.

    2. **Stamped by us (or by nobody).** A SECOND Collimate server can be live
       on this dockerd — the dev run beside the installed app, separate data
       dirs and ports — and its containers carry run_id labels of exactly the
       same shape and are, from our point of view, always "created before this
       boot". Without the owner stamp, booting either server would force-remove
       the other's LIVE generator containers. See api/instance.py.

    Guard 2 is what ``reap_all`` (``COLLIMATE_REAP_ALL_INSTANCES=1``) waives,
    restoring the pre-label global sweep for a machine that knows it has no
    peer; guard 1 still applies, because the delayed-sweep race it defends
    against is entirely within this one server.
    """
    from datetime import datetime

    ids: list[str] = []
    for c in containers:
        attrs = getattr(c, "attrs", None) or {}
        config = attrs.get("Config")
        if isinstance(config, dict):
            labels = config.get("Labels")
        elif reap_all:
            # The pre-label sweep never read a label at all, so a payload
            # whose Config we can't parse was reapable then and stays reapable
            # under the opt-in; owns_container waves an absent stamp through.
            labels = None
        else:
            # An inspect payload we can't read means we can't prove the
            # container is ours; leaving it costs a stale container until its
            # real owner boots, reaping it could kill a live run.
            continue
        if not owns_container(labels, mine, reap_all=reap_all):
            continue
        created_raw = str(attrs.get("Created") or "")
        try:
            # Docker reports RFC3339 with nanoseconds — trim to
            # microseconds for fromisoformat.
            trimmed = created_raw.replace("Z", "+00:00")
            head, _, frac = trimmed.partition(".")
            if frac:
                digits = frac[: frac.index("+")] if "+" in frac else frac
                tz = frac[len(digits):]
                trimmed = f"{head}.{digits[:6]}{tz}"
            created = datetime.fromisoformat(trimmed).timestamp()
        except Exception:
            continue  # unparseable → don't risk a live container
        if created < boot_ts and c.id:
            ids.append(c.id)
    return ids


def orphan_host_terminal_pids(labelled, mine: str, alive) -> list[int]:
    """Of ``labelled`` (``(pid, label)`` pairs: each process carrying the
    host-terminal label, ``<instance>:<server pid>:<run id>``), the pids a
    previous server of this instance left: stamped ``mine``, by a server
    process ``alive(pid)`` says is gone. Pure, like the container cutoffs:
    a label of another instance, a malformed one, or one whose server still
    runs is left alone — wrongly killing ends a live terminal, wrongly
    keeping costs a stray until the next start."""
    out = []
    for pid, label in labelled:
        parts = str(label).split(":", 2)
        if len(parts) != 3 or parts[0] != mine or not parts[1].isdigit() or not parts[2]:
            continue
        if alive(int(parts[1])):
            continue
        out.append(int(pid))
    return out


def _labelled_processes() -> list[tuple[int, str]]:
    """``(pid, label)`` for every process of this user whose environment
    carries the host-terminal label (``/proc/<pid>/environ``, owner-only);
    ``[]`` where there is no ``/proc``."""
    from sessions.terminal import HOST_TERMINAL_LABEL
    prefix = (HOST_TERMINAL_LABEL + "=").encode()
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/environ", "rb") as fh:
                environ = fh.read()
        except OSError:
            continue
        for entry in environ.split(b"\0"):
            if entry.startswith(prefix):
                found.append((int(name), entry[len(prefix):].decode("utf-8", "replace")))
                break
    return found


def _server_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def reap_orphan_host_terminals() -> int:
    """Kill the host terminals a previous server of this instance left
    running (a crash or a kill skipped its run finalization, which kills the
    PTY child's process group): every process labelled with that server
    (``sessions.terminal.HOST_TERMINAL_LABEL``, inherited by the CLI's own
    children) has its process group killed. Returns how many processes
    were found."""
    pids = orphan_host_terminal_pids(_labelled_processes(), instance_id(), _server_alive)
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if pids:
        logger.info("reaper killed %d orphaned host terminal process(es)", len(pids))
    return len(pids)


def reap_orphan_containers(boot_ts: float) -> None:
    try:
        reap_orphan_host_terminals()
    except Exception:
        logger.exception("host terminal reap failed")
    try:
        # Match every generator container by the run_id label that
        # DockerEnvironment stamps on all of them (vscode, lazygit,
        # claude_cli, …), not just vscode by name — then hand the
        # listing to orphan_generator_container_ids, which applies
        # the created-before-boot and same-instance cutoffs (see its
        # docstring for why each one is load-bearing). instance_id()
        # is resolved HERE, not at module import: this thread runs
        # long after cli_main chdir'd into the data dir the id is
        # derived from.
        import docker as _docker
        client = _docker.from_env()
        ids = orphan_generator_container_ids(
            client.containers.list(
                all=True, filters={"label": "com.collimate.run_id"},
            ),
            boot_ts,
            instance_id(),
            reap_all=reap_all_instances(),
        )
        if ids:
            logger.info("reaper removing %d orphaned collimate generator container(s)", len(ids))
            import subprocess
            subprocess.run(
                ["docker", "rm", "-f", *ids],
                capture_output=True, timeout=30,
            )
    except (FileNotFoundError, ImportError):
        # Docker may not be installed/running; that's fine, only matters
        # for container-backed generators (vscode, lazygit, claude_cli).
        pass
    except Exception as e:
        # docker-py wraps the missing-socket case (no daemon on this
        # host) in DockerException — same benign story as above.
        # Anything else is a real reaper failure.
        import docker.errors
        if not isinstance(e, docker.errors.DockerException):
            logger.exception("reaper failed")

    try:
        from sandbox import reap_orphan_containers
        reap_orphan_containers()
    except Exception:
        logger.exception("sandbox orphan reap failed")

    # Account leases a previous server process never released (a crash,
    # a kill): its CLI stores hold plaintext login documents, collected
    # back into the registry before they are deleted. It runs after the
    # reaps, so a run container this instance's previous server orphaned is
    # normally gone before its login is collected; a run holding a rotating credential
    # sweeps again as it starts.
    try:
        from accounts import sweep_expired_account_leases
        sweep_expired_account_leases()
    except Exception:
        logger.exception("account lease sweep failed")


    try:
        from run_workspace_cleanup import reclaim_abandoned_directories
        result = reclaim_abandoned_directories()
        logger.info('run-directory crash cleanup: %s', result)
    except Exception:
        logger.exception('run-directory crash cleanup failed')
