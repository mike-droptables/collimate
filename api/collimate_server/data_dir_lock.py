"""POSIX data-directory lock taken by server startup."""
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


def probe(path: str) -> bool:
    """Whether another process holds the lock at ``path`` (a server running
    against that data dir). Never creates the file: one that is not there is
    a lock nobody holds."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
