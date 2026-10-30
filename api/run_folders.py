"""Run folders — the per-run lock band ``_runs/<label>/`` of a cell.

Every file inside a LIVE run's folder is that run's locked file: the folder
is the lock, there is no lock table. The host mints the label and the folder
at run start (``mint_run_label`` / ``run_folder``), writes the run card
``_runs/<label>/<label>.run`` before the generator starts and sweeps the
whole folder when the run ends (``cell_commits.sweep_run_folder``).

Predicates here are pure path arithmetic, normalized the way the workspace
hydration resolves a path (``a/../_runs/x`` counts as ``_runs/x``). Liveness
comes from the run registry (``run_registry``): a folder whose label
belongs to no live run on the cell is dead and belongs to the sweeps.

"""

from __future__ import annotations

import posixpath
from typing import Any, TypeVar, overload

RUNS_PREFIX = "_runs"
RUN_CARD_EXT = ".run"


def mint_run_label(runner_id: str | None, run_id: str) -> str:
    """The host-minted label of a run: the runner's id and the run's short
    id; a run without a saved runner id uses the ``run`` prefix."""
    return f"{runner_id}-{run_id[:8]}" if runner_id else f"run-{run_id[:8]}"


def run_folder(label: str) -> str:
    """The cell-relative run folder, without a trailing slash."""
    return f"{RUNS_PREFIX}/{label}"


def run_card_path(label: str) -> str:
    """The run card the host writes first and removes last."""
    return f"{run_folder(label)}/{label}{RUN_CARD_EXT}"


def normalize(path: str) -> str:
    """``path`` normalized like hydration resolves it: forward slashes, no
    leading slash, ``.``/``..`` collapsed."""
    norm = posixpath.normpath((path or "").replace("\\", "/").lstrip("/")).lstrip("/")
    return "" if norm == "." else norm


def is_run_path(path: str) -> bool:
    """Whether ``path`` lies in the ``_runs/`` band (the band itself counts)."""
    norm = normalize(path)
    return norm == RUNS_PREFIX or norm.startswith(RUNS_PREFIX + "/")


def label_of(path: str) -> str | None:
    """The run label whose folder holds ``path``, or None outside the band
    (and for the bare ``_runs`` entry)."""
    norm = normalize(path)
    if not norm.startswith(RUNS_PREFIX + "/"):
        return None
    label = norm[len(RUNS_PREFIX) + 1:].split("/", 1)[0]
    return label or None


def in_folder(path: str, label: str) -> bool:
    """Whether ``path`` lies inside ``_runs/<label>/``."""
    return normalize(path).startswith(run_folder(label) + "/")


def _path_of(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("filepath") or item.get("name") or item.get("path") or "")
    return str(getattr(item, "name", "") or "")


_V = TypeVar("_V")


@overload
def strip_run_paths(files: dict[str, _V]) -> dict[str, _V]: ...
@overload
def strip_run_paths(files: list) -> list: ...
def strip_run_paths(files):
    """``files`` without anything under ``_runs/`` — the one filter every
    cross-cell copy applies. A mapping keeps its type (keys are paths); any
    other iterable (paths, VirtualFiles, ``{filepath|name|path}`` dicts)
    comes back as a list."""
    if isinstance(files, dict):
        return {k: v for k, v in files.items() if not is_run_path(k)}
    return [f for f in (files or []) if not is_run_path(_path_of(f))]


__all__ = [
    "RUNS_PREFIX", "RUN_CARD_EXT", "mint_run_label", "run_folder", "run_card_path",
    "normalize", "is_run_path", "label_of", "in_folder", "strip_run_paths",
]
