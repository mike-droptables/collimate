"""Uniform sorted cell locks before the optional brief state lock."""
from contextlib import contextmanager

@contextmanager
def cell_write_scope(channel,cell_ids,*,hold_state=False):
    from cell_commits import cell_commit_locks
    from state import state_lock
    with cell_commit_locks(cell_ids):
        if hold_state:
            with state_lock:
                yield
        else:
            yield
