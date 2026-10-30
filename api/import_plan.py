"""Atomic channel publisher for imported stacks and cells.

``ImportPlan`` captures a detached candidate of a channel, collects validated
additions on it, and publishes them in one channel transaction: every new
cell's immutable roots and current refs land together, then the new cells join
the live layout. Package imports, a new channel's seeding and file imports all
publish through it. Every plan is checked against the Capabilities stack it
captured (``package_import.capture_sources``): a change to that stack's
membership or accepted files while the plan was prepared refuses the
publication. Each admitted package's provenance record (``provenance``) is
written in the publishing transaction, for the cells it publishes. Once the
cells are published, each admitted renderer's compiled bundle is stored as
its server bundle; a plan that is refused or never published stores none.
The app's built-in cards are placed by genesis, never imported: a plan that
carries them publishes no rows for them.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

from cas import blob_path


def initialize_imported_cells(conn, cells) -> None:
    """Publish complete imported immutable roots and current refs together.
    A built-in card has no rows until its overlay is first written."""
    from persistence import initialize_files, snapshot_files
    from versions import initialize_file_snapshot
    for cell in cells:
        if cell.builtin:
            continue
        cell_id = str(cell.id)
        initialize_files(conn, cell_id, cell.files)
        conn.execute("INSERT INTO cells(id,source_type,version_name) VALUES (?,'import','Imported')", (cell_id,))
        for entry in snapshot_files(conn, cell_id):
            digest = entry['body_hash']
            size = os.path.getsize(blob_path(digest))
            conn.execute("INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,?)", (digest, size))
            conn.execute("INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)", (cell_id, entry['path'], digest, entry['encoding'], entry['language']))
        initialize_file_snapshot(conn, cell_id)


def _capabilities_ids(channel) -> set[str]:
    """The ids on the channel's Capabilities stack, cards included."""
    from package_import import capabilities_stacks
    return {str(c.id) for stack in capabilities_stacks(channel) for c in stack.cells}


class ImportPlan:
    """Detached, validated additions published in one channel transaction."""
    def __init__(self, channel, *, new_channel=False):
        from package_import import capture_sources
        from state import state, state_lock
        self.channel = channel
        self.new_channel = new_channel
        self.identity = str(channel.id)
        with state_lock:
            self._validate_identity(state.channels)
        self.candidate, self.signature = capture_sources(channel)
        self.existing_ids = {str(c.id) for stack in self.candidate.main_stacks + self.candidate.nav_stacks for c in stack.cells}
        self.source_ids = _capabilities_ids(self.candidate)
        self.actions = []
        self.bundles = []
        self.provenance = []

    def _validate_identity(self, channels):
        registered = next((ch for ch in channels if str(ch.id) == self.identity), None)
        if self.new_channel:
            if registered is not None:
                raise HTTPException(409,'New channel identity is already registered')
        elif registered is not self.channel:
            raise HTTPException(409,'Import channel is no longer registered')

    def add_packages(self, prepared):
        from package_import import attach_packages
        if prepared.signature != self.signature:
            raise HTTPException(409,'Source packages changed during approval; retry import')
        result = attach_packages(self.candidate, prepared)
        if prepared.renderer_ids:
            self.actions.append(('availability', prepared.renderer_ids))
        self.bundles.extend(prepared.bundles)
        self.provenance.append((prepared.source, prepared.provenance))
        return result

    def _apply_action(self, identity, action, conn):
        from channel_config import read_renderer_availability, write_ui_state_json, RENDERER_AVAILABILITY_KEY
        available = read_renderer_availability(identity,conn=conn)
        available.update({identifier:True for identifier in action[1]})
        write_ui_state_json(identity,RENDERER_AVAILABILITY_KEY,available,conn=conn)

    def publish(self):
        from uuid import uuid4
        from cell_commits import cell_commit_lock, cell_commit_locks
        from channel_config import ui_state_merge_lock
        from database import channel_transaction, changing_channels
        from package_import import capture_sources
        from provenance import write as write_provenance
        from renderer_bundles import publish_server_bundle
        from state import state, state_lock, channel_db_name
        channel = self.channel
        identity = channel_db_name(channel)
        additions = [(kind,stack,[c for c in stack.cells if str(c.id) not in self.existing_ids])
                     for kind in ('main_stacks','nav_stacks') for stack in getattr(self.candidate,kind)]
        additions = [(kind,stack,cells) for kind,stack,cells in additions if cells]
        with cell_commit_lock('channel-publication:' + identity), cell_commit_locks(self.source_ids), ui_state_merge_lock():
            with state_lock:
                self._validate_identity(state.channels)
                if identity in changing_channels:
                    raise HTTPException(409,'Channel changed during import; retry')
                if _capabilities_ids(channel) != self.source_ids:
                    raise HTTPException(409,'Source membership changed during import; retry')
            # Rechecking accepted source refs may take cell/state locks, so it
            # finishes before SQLite's writer transaction starts.
            if capture_sources(channel)[1] != self.signature:
                raise HTTPException(409,'Source packages changed during import; retry')
            with channel_transaction(identity) as conn:
                published = [c for _kind,_stack,cells in additions for c in cells]
                initialize_imported_cells(conn,published)
                published_ids = {str(c.id) for c in published}
                for source, records in self.provenance:
                    write_provenance(conn, source, [r for r in records if r['cell_id'] in published_ids])
                for action in self.actions:
                    self._apply_action(identity,action,conn)
            # Only new UUID additions are published. Concurrent unrelated cell
            # changes are preserved; an empty destination removed meanwhile
            # becomes a fresh stack rather than resurrecting its old identity.
            with state_lock:
                for kind,prepared,cells in additions:
                    current = next((s for s in channel.main_stacks + channel.nav_stacks if s.id == prepared.id),None)
                    if current is None:
                        current = prepared.model_copy(update={'id':uuid4(),'cells':[]})
                        getattr(channel,kind).append(current)
                    for cell in cells:
                        predecessors = {str(c.id) for c in prepared.cells[:prepared.cells.index(cell)]}
                        at = max((i+1 for i,c in enumerate(current.cells) if str(c.id) in predecessors),default=0)
                        current.cells.insert(at,cell)
        for source, body in self.bundles:
            publish_server_bundle(source, body)
