"""The ``.collrun/`` private workspace band — single source of truth.

The host stages a run's inputs (prompt.md, config files, config folders)
under ``<workspace>/.collrun/`` for the generator to read, alongside the host's
run-directory ownership receipt. No cell path may lie inside it: the run's
Config record is captured host-side at kickoff.
"""

from run_folders import normalize

RESERVED_PREFIX = ".collrun"


def is_reserved_path(cell_path: str) -> bool:
    """True if ``cell_path`` falls inside the workspace staging band.

    Normalized with ``run_folders.normalize`` (``.``/``..`` collapsed), the
    same way hydration's ``safe_dest_under`` resolves the path it writes,
    so ``a/../.collrun/x`` counts as reserved exactly where it would land."""
    norm = normalize(cell_path)
    return norm == RESERVED_PREFIX or norm.startswith(RESERVED_PREFIX + "/")
