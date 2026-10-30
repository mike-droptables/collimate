"""Seed a channel's cells straight into its database, for fixtures that build
a data dir without the app's channel creation.

``seed_asset_channel`` gives the channel the asset trees' generator and
renderer cells on its Capabilities stack, after the built-in cards (fresh
ids, every file, runner packages included, no admission and so no
provenance) and writes every layout cell's one genesis version under a
single 'Channel initialization' run.
"""
from __future__ import annotations

import uuid


def seed_asset_channel(channel_id: str, ch, *, generators: str | None = None,
                       renderers: str | None = None) -> None:
    """Seed ``ch`` (id ``channel_id``) from the asset trees (the build's when
    not given); a tree that is absent adds no cells. The Capabilities stack
    (``genesis.ensure_capabilities_stack``) always holds the built-in cards."""
    import genesis
    from collimate._host.packaging.asset_cells import read_asset_cells
    from cell_placement import capabilities_stack
    from channel_sources import specs_to_cells
    from database import get_db_connection

    genesis.ensure_capabilities_stack(ch)
    for root, kind in ((generators or genesis.GENERATORS_DIR, "generator"),
                       (renderers or genesis.RENDERERS_DIR, "renderer")):
        try:
            specs = read_asset_cells(root, kind)
        except (OSError, ValueError):
            continue
        capabilities_stack(ch).cells.extend(specs_to_cells(specs))
    ch.main_selection_id = ch.main_stacks[0].id if ch.main_stacks else None
    conn = get_db_connection(channel_id)
    try:
        run_id = str(uuid.uuid4())
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,?,'completed')", (run_id, "Channel initialization"))
        cells = [cell for stack in ch.main_stacks + ch.nav_stacks for cell in stack.cells if not cell.builtin]
        for seq, cell in enumerate(cells, 1):
            genesis.seed_cell_from_vfiles(conn, run_id, str(cell.id), cell.name, seq, cell.files)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
