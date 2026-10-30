"""POSIX signaling for owned subprocess groups."""
import os


def signal_process_group(pid: int, signum: int, *, known_group: bool = False) -> bool:
    """Signal a live process tree, or an already known owned process group.

    A live PID resolves its group and retains the direct-PID fallback. For a
    known group (including an exited session leader), never look up or signal
    the bare PID: it may already have been reused by an unrelated process.
    Return whether a signal reached the kernel; an absent/inaccessible group
    is a best-effort cleanup refusal, not proof that its processes exited.
    """
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
