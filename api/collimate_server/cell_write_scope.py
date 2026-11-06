"""Uniform sorted cell locks before the optional brief state lock.

Every cell write takes :func:`cell_write_scope`, so it is also the
archived-channel backstop: a write to a channel in a refusing state
(``channel_guard``) raises its refusal there, under the cell locks, even
when it slipped past the route guard. A read that wants the same
consistent view takes :func:`cell_read_scope`, which refuses nothing: an
archived channel is still read."""
from contextlib import contextmanager

@contextmanager
def cell_read_scope(cell_ids,*,hold_state=False):
    from collimate_server.cell_commits import cell_commit_locks
    from collimate_server.state import state_lock
    with cell_commit_locks(cell_ids):
        if hold_state:
            with state_lock:
                yield
        else:
            yield

@contextmanager
def cell_write_scope(channel,cell_ids,*,hold_state=False):
    from collimate_server.cell_commits import cell_commit_locks
    from collimate_server.channel_guard import require_writable
    from collimate_server.state import state_lock
    with cell_commit_locks(cell_ids):
        require_writable(channel)
        if hold_state:
            with state_lock:
                yield
        else:
            yield
