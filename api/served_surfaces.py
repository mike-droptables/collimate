"""Generic run-owned surface claims, serialized with cell publication.

A surface is ``starting`` from the moment the host accepts a run that opens
it (``claim_starting``, before the run's process exists) and ``live`` once
the run registers it (``register``, which upgrades the run's own starting
claim) or writes it (``mark_written``: an artifact file such as a webview
tab has no session, so the run's own write of the placeholder's path is what
makes it live). A claim without a session is indexed by cell, run and path
only; ``release_run`` frees whatever a run still claims. A run's
registration is always its one inbox: a ``.chat`` pointer it serves, or a
transport-only path in its run folder that holds no claim (broadcast when
several viewers may attach for approval cards)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

STARTING: Literal['starting'] = 'starting'
LIVE: Literal['live'] = 'live'


@dataclass(frozen=True)
class Surface:
    channel: str
    cell_id: str
    run_id: str
    path: str
    label: str
    chat_id: str
    token: str
    owner_label: str
    state: Literal['starting', 'live'] = LIVE

    def public(self):
        return {'path': self.path, 'label': self.label, 'state': self.state}

    def coordinates(self):
        if self.state == STARTING or not self.chat_id:
            return {'state': self.state, 'run_id': self.run_id, 'label': self.label,
                    'chat_id': '', 'ws_path': '', 'token': ''}
        return {'state': LIVE, 'chat_id': self.chat_id, 'ws_path': f'/api/chats/{self.chat_id}',
                'token': self.token, 'label': self.label, 'run_id': self.run_id}


_surfaces: dict[tuple[str, str], Surface] = {}
_by_cell: dict[str, dict[str, Surface]] = {}
_by_run: dict[str, dict[tuple[str, str], Surface]] = {}
_by_chat: dict[str, Surface] = {}


class SurfaceTaken(ValueError):
    def __init__(self, owner: str, run_id: str, path: str):
        super().__init__('surface_taken')
        self.owner, self.run_id, self.path = owner, run_id, path

    def response(self):
        return {'error': 'surface_taken', 'owner': self.owner, 'run_id': self.run_id,
                'path': self.path,
                'message': f'{self.path} belongs to {self.owner}; choose another path or Run in new cell'}


def get(cell_id: str, path: str) -> Surface | None:
    return _surfaces.get((str(cell_id), path))


def for_run(run_id: str) -> list[Surface]:
    return list(_by_run.get(run_id, {}).values())


def identity(cell_id: str):
    return tuple((s.path, s.run_id) for s in list(_by_cell.get(str(cell_id), {}).values()))


def _discard(surface: Surface):
    """Remove the current record and every index while its cell lock is held."""
    key = (surface.cell_id, surface.path)
    if _surfaces.get(key) is not surface:
        return
    del _surfaces[key]
    cell_bucket = _by_cell[surface.cell_id]
    del cell_bucket[surface.path]
    if not cell_bucket:
        del _by_cell[surface.cell_id]
    run_bucket = _by_run[surface.run_id]
    del run_bucket[key]
    if not run_bucket:
        del _by_run[surface.run_id]
    if surface.chat_id and _by_chat.get(surface.chat_id) is surface:
        del _by_chat[surface.chat_id]


def _store(surface: Surface):
    key = (surface.cell_id, surface.path)
    _surfaces[key] = surface
    _by_cell.setdefault(surface.cell_id, {})[surface.path] = surface
    _by_run.setdefault(surface.run_id, {})[key] = surface
    if surface.state == LIVE and surface.chat_id:
        _by_chat[surface.chat_id] = surface


def changed(surface: Surface):
    from collimate._host.channel_events import SurfaceChanged
    from events import broadcast
    try:
        broadcast(surface.channel, SurfaceChanged(cell_id=surface.cell_id, path=surface.path).to_dict())
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Surface notification failed for %s', surface.path)


def claim_starting(channel: str, cell_id: str, run_id: str, paths, owner_label: str,
                   *, label: str | None = None) -> list[Surface]:
    """Claim the files an accepted run opens as its starting surfaces, before
    its process exists. Each path is its own claim, owned by ``run_id``;
    ``owner_label`` (the run's label) names the owner in a refusal and
    ``label`` (default: the owner label) is what a renderer shows. A path
    another run claims raises SurfaceTaken and claims nothing. The run's
    ``register`` of the same path upgrades the claim to live; ``release_run``
    frees it."""
    from cell_write_scope import cell_write_scope
    cell_id = str(cell_id)
    paths = list(dict.fromkeys(paths))
    made: list[Surface] = []
    with cell_write_scope(channel, [cell_id]):
        for path in paths:
            owner = get(cell_id, path)
            if owner is not None and owner.run_id != run_id:
                raise SurfaceTaken(owner.owner_label, owner.run_id, path)
        for path in paths:
            if get(cell_id, path) is not None:
                continue
            surface = Surface(channel, cell_id, run_id, path, label or owner_label,
                              '', '', owner_label, STARTING)
            _store(surface)
            made.append(surface)
    for surface in made:
        changed(surface)
    return made


def release_run(run_id: str) -> None:
    """Free every surface ``run_id`` still claims: the starting claims of a
    run that never registered them, at its start's unwind or at its end."""
    from cell_write_scope import cell_write_scope
    released: list[Surface] = []
    for cell_id in {s.cell_id for s in for_run(run_id)}:
        surfaces = [s for s in for_run(run_id) if s.cell_id == cell_id]
        with cell_write_scope(surfaces[0].channel, [cell_id]):
            for surface in surfaces:
                if get(cell_id, surface.path) is surface:
                    _discard(surface)
                    released.append(surface)
    for surface in released:
        changed(surface)


def mark_written(run_id: str, cell_id: str, paths) -> list[Surface]:
    """A run's own write to a path it holds a starting claim on makes that
    claim live: the file now holds the generator's artifact (a webview tab,
    a website pointer) in place of the host's placeholder, and no session
    will ever register it. A path the run later registers goes through
    ``register`` as before. Returns the surfaces that went live."""
    from dataclasses import replace
    from cell_write_scope import cell_write_scope
    cell_id = str(cell_id)
    went_live: list[Surface] = []
    candidates: list[Surface] = []
    for path in dict.fromkeys(paths):
        found = get(cell_id, path)
        if found is not None and found.run_id == run_id and found.state == STARTING:
            candidates.append(found)
    if not candidates:
        return went_live
    with cell_write_scope(candidates[0].channel, [cell_id]):
        for path in [c.path for c in candidates]:
            current = get(cell_id, path)
            if current is None or current.run_id != run_id or current.state != STARTING:
                continue
            _discard(current)
            live = replace(current, state=LIVE)
            _store(live)
            went_live.append(live)
    for surface in went_live:
        changed(surface)
    return went_live


def release_chat(chat_id: str):
    from cell_write_scope import cell_write_scope
    surface = _by_chat.get(chat_id)
    if surface is None:
        return
    with cell_write_scope(surface.channel, [surface.cell_id]):
        if _by_chat.get(chat_id) is not surface:
            return
        _discard(surface)
    changed(surface)


def register(manager, path: str, *, label: str, transport_only: bool = False, broadcast: bool = False):
    """Register the run's inbox at ``path``: a pointer the run serves (its
    starting claim goes live), or a ``transport_only`` path in its run
    folder that serves no file; only a transport-only inbox may be
    ``broadcast`` (several viewers attach). Returns the coordinates."""
    from collimate.surfaces import surface_declaration
    from cell_write_scope import cell_write_scope
    from run_folders import in_folder, is_run_path, label_of
    from sessions.chat import register_surface_session
    from state import get_channel_by_id
    from cell_placement import find_cell

    if not isinstance(transport_only, bool) or not isinstance(broadcast, bool):
        raise ValueError('invalid_surface_declaration')
    # Preserve the named refusal for paths in any foreign/dead run folder.
    if isinstance(path, str) and is_run_path(path) and not in_folder(path, manager.label):
        raise SurfaceTaken(label_of(path) or 'another run', '', path)
    path = surface_declaration(path, folder=manager.folder, label=label)['path']
    if broadcast and not transport_only:
        raise ValueError('invalid_surface_declaration')
    if transport_only and not in_folder(path, manager.label):
        raise ValueError('invalid_surface_declaration')
    if not transport_only and is_run_path(path) and not any(path.endswith(ext) for ext in manager.ui_files):
        raise ValueError('ui_type')
    with cell_write_scope(manager.channel, [manager.cell_id], hold_state=True):
        if (manager.sealed or getattr(manager, 'stop_requested', False)
                or getattr(manager, 'final_status', None)):
            raise ValueError('run_ended')
        if find_cell(get_channel_by_id(manager.channel), manager.cell_id)[1] is None:
            raise ValueError('cell_not_found')
        if transport_only:
            # A transport-only inbox publishes no file and claims nothing.
            # Actual UI writes still require the manifest grant.
            return register_surface_session(manager.run_id, manager.cell_id, path,
                broadcast=broadcast, inbox=True, answer_route='inbox')
        owner = get(manager.cell_id, path)
        if owner is not None and owner.run_id != manager.run_id:
            raise SurfaceTaken(owner.owner_label, owner.run_id, path)
        result = register_surface_session(manager.run_id, manager.cell_id, path,
            broadcast=False, inbox=True, answer_route='inbox')
        if owner is not None:
            _discard(owner)
        surface = Surface(manager.channel, manager.cell_id, manager.run_id, path, label,
                          result['chat_id'], result['token'], manager.label)
        _store(surface)
    changed(surface)
    return surface.coordinates()


def write_refusals(manager, paths):
    result = {}
    for path in paths:
        owner = get(manager.cell_id, path)
        if owner is not None and owner.run_id != manager.run_id:
            result[path] = {'path': path, 'reason': 'locked', 'owner': owner.owner_label}
    return result
