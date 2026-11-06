"""The cell paths the host reserves, and the repository paths it keys rows by.

Two bands are never cell paths:

- ``.collrun/``, the private workspace band. The host stages a run's inputs
  (prompt.md, config files, config folders) there for the generator to read,
  alongside the host's run-directory ownership receipt; the run's Config
  record is captured host-side at kickoff.
- any ``.git``-equivalent segment: a repository's ``.git`` is the host's,
  materialized from and harvested into the channel's git state, never a cell
  file. Equivalence follows git's own ``verify_path`` (t0060, t1450): case,
  ignorable code points, an NTFS stream suffix, trailing dots and spaces,
  and the 8.3 short name ``git~1`` all reach the same directory on some
  filesystem.

The predicate itself is the SDK's (``collimate.pointers.is_dotgit_segment``
and ``is_reserved_path``, normalized as ``run_folders.normalize`` resolves a
path, so ``a/../.collrun/x`` counts where it would land); the host reads it
from here. ``sdk/shared/paths-contract.json`` pins the rule for it and the
frontend mirror. A repository path (:func:`is_repo_path`) is the narrower
rule every writer of ``cell_repositories`` rows checks.
"""

from collimate.fs import WORKSPACE_SKIP_DIRS
from collimate.pointers import STAGING_PREFIX as RESERVED_PREFIX
from collimate.pointers import is_dotgit_segment, is_reserved_path
from collimate_server.run_folders import RUNS_PREFIX, normalize

# The segments a repository path may not hold, casefolded.
_REPO_FORBIDDEN = frozenset({RESERVED_PREFIX, RUNS_PREFIX.casefold()}
                            | {name.casefold() for name in WORKSPACE_SKIP_DIRS})


def is_repo_path(repo_path: str) -> bool:
    """Whether ``repo_path`` may key a repository row: ``""`` (the cell
    root) or a normalized cell-relative folder none of whose segments is
    ``.git``-equivalent, ``.collrun``, ``_runs`` or a workspace skip dir
    (casefolded)."""
    if repo_path == "":
        return True
    if not isinstance(repo_path, str) or normalize(repo_path) != repo_path or "\\" in repo_path:
        return False
    segments = repo_path.split("/")
    return all(segment not in ("", ".", "..") and segment.casefold() not in _REPO_FORBIDDEN
               and not is_dotgit_segment(segment) for segment in segments)


__all__ = ["RESERVED_PREFIX", "is_dotgit_segment", "is_repo_path", "is_reserved_path"]
