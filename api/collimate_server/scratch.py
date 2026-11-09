"""Clear scratch (``POST /api/storage/scratch/clear``, plan 1105 S8): the
children of the data dir's ``.collimate/tmp`` and ``.collimate/pycache``.

Refused while anything may still be using them: a run starting, live or
finishing on this server (its workspace, its git harvest, its bytecode), an
export holding pins in a body store, or a folder export serving its
staged ``.git`` files. Imports (``.collimate/imports``) and the verifier
workers (``.collimate/verifier-workers``) keep their files elsewhere.

Even then a run workspace (``ws_*``) goes only when its receipt proves it
abandoned (the startup reclaim's gates, ``run_workspace_cleanup``), and not
while its git state waits for the next start's recovery; and git scratch a
request may hold right now (anything else in ``tmp/`` changed within
:data:`IN_USE_SECONDS`) stays.
"""
from __future__ import annotations

import os
import re
import time

import collimate_server.readonly_trees as readonly_trees
from collimate_server.storage_survey import PYCACHE, TMP, measure

#: A ``tmp/`` entry other than a run workspace changed this recently may
#: belong to a request in flight (git scratch lives for one request).
IN_USE_SECONDS = 600
_WORKSPACE = re.compile(r"ws_[0-9a-f]{8}_[a-z0-9_]+")


def busy_reason() -> str | None:
    """Why scratch cannot be cleared now, or None."""
    import collimate_server.channel_paths as channel_paths
    from collimate_server.cas import app_store, channel_store, staging_store
    from collimate_server.run_registry import active_managers, busy_channels, held_cell_ids
    from collimate_server.transfer_export import held_stagings
    if active_managers or busy_channels() or held_cell_ids():
        return ("Runs are still running or finishing on this server. Stop them and let them finish "
                "before clearing scratch.")
    stores = [app_store(), staging_store()]
    for channel_id in channel_paths.indexed_channels():
        try:
            stores.append(channel_store(channel_id))
        except channel_paths.ChannelUnavailable:
            continue
    if any(store.holds_pins() for store in stores) or held_stagings():
        return "An export is in progress. Try again when it is done."
    return None


def _children(directory: str) -> list[str]:
    try:
        return sorted(os.path.join(directory, name) for name in os.listdir(directory))
    except OSError:
        return []


def clear() -> dict:
    """Remove what may go (blocking): ``{bytes_freed, removed, kept:
    [{name, reason}]}``. The caller checked :func:`busy_reason`."""
    from collimate_server.run_workspace_cleanup import reclaim_abandoned_directories
    from collimate_server.transfer_export import held_stagings
    freed = removed = 0
    kept: list[dict] = []
    workspaces = {path: measure([path]).bytes for path in _children(TMP)
                  if _WORKSPACE.fullmatch(os.path.basename(path))}
    reclaim_abandoned_directories(keep_unharvested=True)
    for path, size in workspaces.items():
        if os.path.lexists(path):
            kept.append({"name": os.path.basename(path), "reason": "its run's receipt does not prove it abandoned"})
        else:
            freed += size
            removed += 1
    serving = {os.path.realpath(path) for path in held_stagings()}
    recent = time.time() - IN_USE_SECONDS
    for path in _children(TMP):
        name = os.path.basename(path)
        if _WORKSPACE.fullmatch(name):
            continue
        if os.path.realpath(path) in serving:
            kept.append({"name": name, "reason": "an export is serving it"})
            continue
        try:
            if os.lstat(path).st_mtime > recent:
                kept.append({"name": name, "reason": "changed in the last ten minutes: a request may use it"})
                continue
        except OSError:
            continue
        size = measure([path]).bytes
        try:
            readonly_trees.remove_tree(path)
        except OSError as exc:
            kept.append({"name": name, "reason": f"could not be removed: {exc}"})
            continue
        freed += size
        removed += 1
    for path in _children(PYCACHE):
        size = measure([path]).bytes
        try:
            readonly_trees.remove_tree(path)
        except OSError as exc:
            kept.append({"name": os.path.join("pycache", os.path.basename(path)), "reason": f"could not be removed: {exc}"})
            continue
        freed += size
        removed += 1
    return {"bytes_freed": freed, "removed": removed, "kept": kept}
