"""The channel's handler catalog and the handler builds in use.

Every catalog starts with the app's built-in handlers (``builtin_packages``),
merged with the channel's additions from the Definitions card's overlay, then
the handlers of the channel's Capabilities cells
(``cell_placement.capabilities_stack_cells``). A built-in is never disabled; a
user handler that claims a built-in id or type is left out with a catalog
error.

A handler's ``derived:`` line names its build. The build is in use only when
that hash resolves (:func:`accepted_derived`) in ``handler_derived_files``,
which only Review and accept and the host's built-in overlay commit write.
Reading the catalog never accepts a build, so bytes that reach a
Capabilities cell by a move, an import or a direct write never put their own
build in use.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import copy
import hashlib
import logging
import os
import threading

import yaml

from collimate.definitions import discover_definitions
from collimate.handlers import derived_hash, handler_for, validate_handler, validate_added_skills

logger = logging.getLogger(__name__)


def save_derived_skill(identity: str, files: dict[str, bytes]) -> None:
    """Store ``files`` as the accepted build ``identity``. Its writers are
    Review and accept (``handler_build``) and a built-in card's overlay."""
    from cas import publication_lock, store_bytes
    from database import get_global_db_connection
    if derived_hash(files) != identity:
        raise ValueError('Accepted handler derived identity mismatch')
    with publication_lock:
        rows = [(identity, path, store_bytes(body)) for path, body in files.items()]
        conn = get_global_db_connection()
        try:
            with conn:
                conn.executemany('INSERT OR IGNORE INTO handler_derived_files VALUES (?, ?, ?)', rows)
        finally:
            conn.close()


def accepted_derived(identity: str | None) -> dict[str, bytes]:
    """The build ``identity`` names when it is in use: a stored accepted
    folder (exactly those bytes); ``{}`` otherwise."""
    if not identity:
        return {}
    from cas import read_blob
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        rows = conn.execute('SELECT path, blob_hash FROM handler_derived_files WHERE derived_hash=?',
                            (identity,)).fetchall()
    finally:
        conn.close()
    files = {}
    for path, digest in rows:
        body = read_blob(digest)
        if body is None or hashlib.sha256(body).hexdigest() != digest:
            files = {}
            break
        files[path] = body
    if files and derived_hash(files) == identity:
        return files
    return {}


@dataclass(frozen=True)
class HandlerRecord:
    manifest: dict
    channel: str
    cell_id: str
    prefix: str
    definition_path: str
    accepted_files: dict[str, bytes]
    built_hash: str | None
    clashes: tuple[str, ...] = ()
    disabled: bool = False
    added_files: dict[str, str] = field(default_factory=dict)
    # A built-in record: its skills hash in effect (the accepted regenerated
    # skills, else the build's), the build's own skills hash and the folders
    # of the channel's added examples.
    builtin: bool = False
    accepted_skills_hash: str | None = None
    app_skills_hash: str | None = None
    added_examples: frozenset[str] = frozenset()

    def public(self):
        if self.builtin:
            accepted = self.built_hash is None or self.built_hash == self.accepted_skills_hash
            accepted_derived = self.accepted_skills_hash
        else:
            accepted = bool(self.accepted_files) and self.built_hash == self.manifest.get('derived')
            accepted_derived = self.manifest.get('derived') if self.accepted_files else None
        return {key: copy.deepcopy(self.manifest[key]) for key in ('id', 'name', 'description', 'types')} | {
            'examples': [dict(example, added=example['folder'] in self.added_examples)
                         for example in self.manifest['examples']],
            'cell_id': self.cell_id, 'folder': self.prefix, 'definition_path': self.definition_path,
            'builtin': self.builtin, 'accepted_skills_hash': self.accepted_skills_hash,
            'uses_app_skills': self.builtin and self.accepted_skills_hash == self.app_skills_hash,
            'has_derived': self.built_hash is not None, 'built_hash': self.built_hash,
            'accepted': accepted, 'accepted_derived': accepted_derived,
            'clashes': list(self.clashes), 'disabled': self.disabled,
            'accepted_skills': sorted({path.split('/')[1] for path in self.accepted_files
                                       if path.startswith('skills/') and len(path.split('/')) == 3 and path.endswith('/SKILL.md')}),
            'added_skills': sorted({path.split('/')[1] for path in self.added_files
                                    if len(path.split('/')) == 3 and path.endswith('/SKILL.md')}),
        }


@dataclass(frozen=True)
class HandlerCatalog:
    records: tuple[HandlerRecord, ...]
    errors: tuple[dict, ...]

    def public(self):
        return [record.public() for record in self.records]

    def render_aliases(self) -> dict[str, str]:
        """Direct, unambiguous metadata rules; never resolve their targets again."""
        types = [value for record in self.records if not record.disabled
                 for value in record.manifest['types']]
        counts = Counter(value['ext'].casefold() for value in types)
        return {value['ext'].casefold(): value['render_as'].casefold()
                for value in types
                if value.get('render_as') and counts[value['ext'].casefold()] == 1}

    def for_path(self, path: str) -> HandlerRecord | None:
        records = [record for record in self.records if not record.disabled]
        selected = handler_for([record.manifest for record in records], path)
        return next((record for record in records if record.manifest is selected), None)

    def skills(self, ext_or_id: str) -> dict[str, str]:
        by_id = [record for record in self.records if record.manifest['id'] == ext_or_id and not record.disabled]
        record = by_id[0] if len(by_id) == 1 else self.for_path(ext_or_id)
        if record is None:
            return {}
        files = record.accepted_files
        skills = {path.split('/')[1] for path in files if path.startswith('skills/')
                  and len(path.split('/')) == 3 and path.endswith('/SKILL.md')}
        accepted = {record.manifest['id'] + '--' + path[len('skills/'):]: body.decode('utf-8')
                for path, body in files.items() if path.startswith('skills/')
                and len(path.split('/')) >= 3 and path.split('/')[1] in skills}
        if not files:
            return {}
        return accepted | {record.manifest['id'] + '--' + path[len('skills/'):]: body
                           for path, body in record.added_files.items()}


_cache = OrderedDict()
_cache_lock = threading.Lock()


def clear_catalog_cache():
    with _cache_lock:
        _cache.clear()


def handler_catalog_snapshot(channel):
    """Layout supplies membership; accepted SQL supplies file identity and
    paths: the channel's Capabilities cells and the built-in handlers' card
    (its overlay). Run folders are left out of the key: their churn changes
    no catalog input."""
    from builtin_packages import handler_card
    from cell_placement import capabilities_stack_cells, find_cell
    from database import get_db_connection
    from persistence import snapshot_files
    from run_folders import is_run_path
    from state import state_lock, channel_db_name
    with state_lock:
        db = channel_db_name(channel)
        members = [str(cell.id) for cell in capabilities_stack_cells(channel)]
        card = handler_card(channel)
        if find_cell(channel, card)[1] is None:
            card = None
    cells, overlay = [], []
    if members or card is not None:
        conn = get_db_connection(db, create=False)
        try:
            conn.execute('BEGIN')
            cells = [(cell, [f for f in snapshot_files(conn, cell) if not is_run_path(f['path'])])
                     for cell in members]
            if card is not None:
                overlay = [f for f in snapshot_files(conn, card) if not is_run_path(f['path'])]
        finally:
            conn.close()

    def described(files):
        return tuple((file['path'], file['file_revision'], file['body_hash'],
                      file['encoding'], file['language']) for file in files)
    key = (os.getcwd(), db, str(channel.id), described(overlay),
           tuple((cell, described(files)) for cell, files in cells))
    return key, db, cells, overlay


def _overlay_bodies(overlay, prefix):
    """The overlay files under ``prefix`` (package-relative bytes)."""
    from cas import read_blob
    bodies = {}
    for file in overlay:
        if file['path'].startswith(prefix):
            body = read_blob(file['body_hash'])
            if body is None:
                raise OSError('Missing accepted file ' + file['path'])
            bodies[file['path'][len(prefix):]] = body
    return bodies


def builtin_record(channel, db, handler, overlay) -> HandlerRecord:
    """A built-in handler's record in ``channel``: the build's manifest and
    verifier, its examples plus the channel's added ones, and the skills in
    effect (``accepted_skills``). ``overlay`` is the Definitions card's
    accepted file descriptors. Every skills hash here is the channel-owned
    (non-pinned) one, :func:`builtin_packages.owned_hash`: the pinned files
    are always the current build's, so an upgrade changing them moves no
    hash."""
    from builtin_packages import ADDITIONS, complete_skills, handler_card, owned_hash, owned_skills, parse_additions
    files = _overlay_bodies(overlay, handler.id + '/')
    additions = parse_additions(files.get(ADDITIONS))
    overlay_skills = {path[len('derived/'):]: body for path, body in files.items()
                      if path.startswith('derived/skills/')}
    built_hash = owned_hash(handler.id, overlay_skills) if overlay_skills else None
    effective = handler.skills()
    if additions.skills is not None:
        if overlay_skills and built_hash == additions.skills:
            save_derived_skill(additions.skills, owned_skills(handler.id, overlay_skills))
        accepted = accepted_derived(additions.skills)
        if accepted:
            effective = accepted
        else:
            logger.warning('accepted skills %s of the built-in %s handler cannot be resolved; '
                           'serving the build\'s', additions.skills, handler.id)
    accepted_files = complete_skills(handler.id, {path: body for path, body in effective.items()
                                                  if path.startswith('skills/')}) | {'verify.ts': handler.verify_ts}
    manifest = dict(handler.manifest, examples=[*handler.manifest['examples'], *additions.examples])
    return HandlerRecord(manifest, db, handler_card(channel), handler.id, handler.definition_path,
                         accepted_files, built_hash, builtin=True,
                         accepted_skills_hash=additions.skills or handler.skills_hash,
                         app_skills_hash=handler.skills_hash,
                         added_examples=frozenset(example['folder'] for example in additions.examples))


def _reserved(raw) -> str | None:
    """Why a user handler manifest claims a built-in, or None."""
    from builtin_packages import held_reason
    return held_reason('handler', raw) if isinstance(raw, dict) else None


def catalog(channel) -> HandlerCatalog:
    """The channel's handler catalog, cached by Capabilities membership and
    accepted file revisions. It only reads: a user handler's build is in use
    when :func:`accepted_derived` resolves its ``derived:`` line."""
    from dataclasses import replace
    from builtin_packages import builtin_handlers, handler_card
    from renderer_bundles import package_bytes
    for _attempt in range(2):
        key, db, cells, overlay = handler_catalog_snapshot(channel)
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
        builtins, records, errors = [], [], []
        for handler in builtin_handlers().values():
            try:
                builtins.append(builtin_record(channel, db, handler, overlay))
            except (ValueError, OSError, yaml.YAMLError) as exc:
                logger.warning('built-in %s handler additions are unreadable: %s', handler.id, exc)
                errors.append({'cell_id': handler_card(channel), 'path': handler.id + '/additions.yaml',
                               'error': str(exc)})
                builtins.append(builtin_record(channel, db, handler, []))
        for cell_id, files in cells:
            discovered = discover_definitions(file['path'] for file in files)
            errors.extend({'cell_id': cell_id, 'path': problem.path, 'error': problem.message}
                          for problem in discovered.problems)
            for package in discovered.packages:
                if package.kind != 'handler':
                    continue
                try:
                    prefix = package.prefix + '/'
                    bodies, _refs = package_bytes(prefix=prefix, entrypoint=package.definition_path[len(prefix):],
                                                  channel=db, cell_id=cell_id)
                    raw = yaml.safe_load(bodies[package.definition_path[len(prefix):]])
                    reserved = _reserved(raw)
                    if reserved is not None:
                        raise ValueError(reserved)
                    manifest = validate_handler(raw)
                    derived = {path[len('derived/'):]: body for path, body in bodies.items() if path.startswith('derived/')}
                    current_hash = derived_hash(derived) if derived else None
                    accepted = accepted_derived(manifest.get('derived'))
                    added = validate_added_skills(bodies, accepted)
                    records.append(HandlerRecord(manifest, db, cell_id, package.prefix,
                                                  package.definition_path, accepted, current_hash, added_files=added))
                except (ValueError, KeyError, UnicodeError, yaml.YAMLError) as exc:
                    errors.append({'cell_id': cell_id, 'path': package.definition_path, 'error': str(exc)})
        ids = Counter(record.manifest['id'] for record in records)
        ext_counts = Counter(value['ext'] for record in records if ids[record.manifest['id']] == 1
                             for value in record.manifest['types'])
        resolved = []
        for record in records:
            disabled = ids[record.manifest['id']] > 1
            clashes = tuple(value['ext'] for value in record.manifest['types'] if ext_counts[value['ext']] > 1)
            if disabled:
                errors.append({'cell_id': record.cell_id, 'path': record.definition_path,
                               'error': f"Duplicate handler id {record.manifest['id']!r}"})
            if clashes:
                errors.append({'cell_id': record.cell_id, 'path': record.definition_path,
                               'error': 'Handler type clash: ' + ', '.join(clashes)})
            resolved.append(replace(record, disabled=disabled, clashes=clashes))
        result = HandlerCatalog((*builtins, *resolved), tuple(errors))
        if handler_catalog_snapshot(channel)[0] != key:
            continue
        with _cache_lock:
            # At most the current snapshot per channel, with bounded inactive channels.
            for previous in list(_cache):
                if previous[:3] == key[:3]:
                    del _cache[previous]
            _cache[key] = result
            while len(_cache) > 8:
                _cache.popitem(last=False)
        return result
    raise ValueError('Handler catalog changed while being read; retry')
