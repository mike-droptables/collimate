"""POSIX data-directory lock shared by server startup and reset."""
import os


class LockOpenError(RuntimeError):
    def __init__(self, error: OSError):
        self.error = error
        super().__init__(str(error))


class LockHeld(RuntimeError):
    def __init__(self, holder: str):
        self.holder = holder
        super().__init__(holder)


def acquire(path: str) -> int:
    import fcntl
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise LockOpenError(exc) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
        os.close(fd)
        raise LockHeld(holder) from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def release(fd: int) -> None:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
