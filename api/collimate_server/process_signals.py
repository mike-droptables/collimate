"""POSIX signaling for owned subprocess groups."""
import os
import subprocess

_PROC = "/proc"


def start_ticks(pid) -> str | None:
    """When process ``pid`` started, so a later look can tell it from a
    process that reused its pid: ``/proc``'s ``starttime`` (clock ticks
    since boot), or, on a system without ``/proc`` (macOS), what ``ps``
    says of its start; None when there is no such process or neither can
    tell."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[19].decode("ascii")
    except (IndexError, UnicodeDecodeError):
        return None
    except OSError:
        if os.path.isdir(_PROC):
            return None
    try:
        said = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, timeout=5,
                              env={**os.environ, "LC_ALL": "C"}, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    started = said.stdout.decode("ascii", "replace").strip()
    return f"ps:{started}" if said.returncode == 0 and started else None


def signal_process_group(pid: int, signum: int, *, known_group: bool = False) -> bool:
    """Signal a live process tree, or an already known owned process group.

    A live PID resolves its group and retains the direct-PID fallback. For a
    known group (including an exited session leader), never look up or signal
    the bare PID: it may already have been reused by an unrelated process.
    Return whether a signal reached the kernel; an absent/inaccessible group
    is a best-effort cleanup refusal, not proof that its processes exited.

    Only an integer above 1 is ever signalled. ``killpg(1)`` is ``kill(-1)``,
    every process of the user, and a missing or mocked pid indexes as 0 or
    1: such a target is refused here, the one place signals leave.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.killpg(pid if known_group else os.getpgid(pid), signum)
        return True
    except OSError:
        if known_group:
            return False
    try:
        os.kill(pid, signum)
        return True
    except OSError:
        return False
