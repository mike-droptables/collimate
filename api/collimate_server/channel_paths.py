"""Channel folders and the locations that hold them: the one resolver.

Every channel is one self-contained folder, ``<location>/<channel uuid>/``::

    channel.json        {"format", "channel_id", "name", "archived", "archived_at",
                         "installation_id", "created_at"}, rewritten atomically
    channel.db          cells, versions, runs, settings, the layout and the cell
                        metadata journal (+-wal, -shm)
    git/  git-rescue/   the channel's git object store and its rescues
    objects/            the channel's body store (cas.channel_store), objects.trash/
    cell-state/<cell>/  per-cell state a run keeps (COLLIMATE_CELL_STATE)

A location is a visible folder of channel folders, identified by its marker
``.collimate-location.json`` ({"format", "location_id", "installation_id",
"data_dir", "claimed_at"}), which names the one installation that owns it.
``app.db`` lists the locations (``locations``) and holds the installation id
(``installation``); the channel -> folder index lives in memory only, built
at startup by scanning every owned location's ``channel.json`` files
(:func:`establish`), and changed only under ``database.channel_files_lock``
by create, delete and claim.

Paths of a channel come only from here: :func:`channel_dir`, :func:`db_path`,
:func:`git_dir`, :func:`git_rescue_dir`, :func:`objects_dir`,
:func:`cell_state_dir`. A channel that is not indexed, or whose folder is
gone, raises :class:`ChannelUnavailable` (409 ``channel_unavailable`` over
HTTP); nothing here ever re-creates a folder.

The registry is per data dir (the process CWD by the cli_main contract), so
a test that changes into a fresh directory starts with an empty one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FORMAT = 1
MANIFEST = "channel.json"
MARKER = ".collimate-location.json"
DB_FILE = "channel.db"
GIT_DIR = "git"
GIT_RESCUE_DIR = "git-rescue"
OBJECTS_DIR = "objects"
CELL_STATE_DIR = "cell-state"
ORPHANED_DIR = ".orphaned"
STAGING_SUFFIX = ".moving"
DELETING_PREFIX = ".deleting-"
# A moved channel's record of where it came from (``channel_move``): written
# into the verified ``<uuid>.moving`` copy before its rename, removed once
# the source has gone. Startup removes a source a record still names.
MOVE_RECORD = ".collimate-move.json"

# The default location's path when the installation owns none yet:
# ``--channels-dir`` (configure_startup), else this variable, else
# ~/Collimate/Channels.
CHANNELS_ENV = "COLLIMATE_CHANNELS"
DEFAULT_CHANNELS = os.path.join("~", "Collimate", "Channels")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DELETING = re.compile(re.escape(DELETING_PREFIX) + r".*")


class ChannelUnavailable(LookupError):
    """The channel has no available folder: not indexed, its folder or its
    location gone, or its layout did not validate."""

    code = "channel_unavailable"

    def __init__(self, channel_id: str, reason: str):
        super().__init__(f"Channel {channel_id} is unavailable: {reason}")
        self.channel_id = channel_id
        self.reason = reason


class ChannelNotFound(ChannelUnavailable):
    """No channel folder answers to the id (none was ever created here, or
    it is not a channel id): 404 over HTTP."""

    code = "channel_not_found"


class NoDefaultLocation(RuntimeError):
    """New channels have nowhere to go: the default location is missing or
    owned by another installation."""

    code = "no_default_location"


class LocationOwned(RuntimeError):
    """The location's marker names another installation."""

    code = "location_owned"

    def __init__(self, path: Path, owner_data_dir: str | None, channels: int):
        self.path = path
        self.owner_data_dir = owner_data_dir
        self.channels = channels
        super().__init__(refusal_message(path, owner_data_dir, channels))


class OwnerRunning(RuntimeError):
    """A claim found the owner's server holding its data dir's lock."""

    code = "owner_running"

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        super().__init__(f"Stop the Collimate server at {data_dir} first.")


class OwnerUnchecked(RuntimeError):
    """A claim whose owner cannot be checked (its data dir is gone, or on
    another machine) needs an explicit confirmation naming it (D18)."""

    code = "owner_unchecked"

    def __init__(self, data_dir: str | None):
        self.data_dir = data_dir
        super().__init__(f"The owner at {data_dir or 'an unknown data dir'} cannot be checked; "
                         "confirm the claim to take the location anyway.")


@dataclass
class Location:
    id: str
    path: Path
    is_default: bool
    # ok: ours; missing: listed, its folder not there; foreign: its marker
    # names another installation.
    state: str = "ok"
    owner_data_dir: str | None = None


@dataclass
class Home:
    channel_id: str
    path: Path
    location_id: str
    manifest: dict
    # ok; creating: written under <uuid>.moving and not yet renamed;
    # unavailable: indexed, but its layout did not validate or its folder went.
    state: str = "ok"
    reason: str | None = None


@dataclass
class _Registry:
    locations: dict[str, Location] = field(default_factory=dict)
    homes: dict[str, Home] = field(default_factory=dict)
    installation_id: str | None = None
    established: bool = False


_registries: dict[str, _Registry] = {}
# The CLI's two flags (cli_main): consulted only when the installation owns
# no location yet.
_startup = {"channels_dir": None, "claim": False}


def _registry() -> _Registry:
    key = os.path.abspath(os.getcwd())
    found = _registries.get(key)
    if found is None:
        found = _registries[key] = _Registry()
    return found


def _lock():
    from collimate_server.database import channel_files_lock
    return channel_files_lock


def configure_startup(*, channels_dir: str | None, claim: bool) -> None:
    """Record ``--channels-dir`` (resolved against the launch directory) and
    ``--claim-channels`` for the lifespan's ownership step."""
    _startup["channels_dir"] = os.path.abspath(os.path.expanduser(channels_dir)) if channels_dir else None
    _startup["claim"] = bool(claim)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(channel_id) -> str:
    from collimate_server.database import canonical_channel_id
    return canonical_channel_id(channel_id)


# --- durable small files ------------------------------------------------------------------------


def write_json_atomic(path: Path, value: dict) -> None:
    """Rewrite ``path`` with ``value``: a temp file beside it, fsync, rename,
    fsync the directory."""
    from collimate_server.cas import fsync_directory
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            json.dump(value, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def read_manifest(folder: Path) -> dict:
    """The folder's ``channel.json``, checked: ValueError (or OSError) when
    it is missing, unreadable or not this format, or names another id than
    the folder's name."""
    with open(folder / MANIFEST, encoding="utf-8") as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("not a format-1 channel.json")
    identity = manifest.get("channel_id")
    name = folder.name.removesuffix(STAGING_SUFFIX)
    if not isinstance(identity, str) or identity != name or not _UUID.fullmatch(identity):
        raise ValueError("channel.json names another channel than its folder")
    if not isinstance(manifest.get("name"), str) or not isinstance(manifest.get("archived", False), bool):
        raise ValueError("channel.json lacks a name or has an invalid archived flag")
    return manifest


def write_manifest(folder: Path, manifest: dict) -> None:
    write_json_atomic(folder / MANIFEST, manifest)


def read_marker(location: Path) -> dict | None:
    """The location's marker, None when there is none. An unreadable marker
    reads as one naming no installation (it is not ours)."""
    try:
        with open(location / MARKER, encoding="utf-8") as source:
            marker = json.load(source)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"format": FORMAT, "location_id": None, "installation_id": None, "data_dir": None}
    if not isinstance(marker, dict):
        return {"format": FORMAT, "location_id": None, "installation_id": None, "data_dir": None}
    return marker


def _write_marker(location: Path, location_id: str, *, claimed: bool) -> None:
    write_json_atomic(location / MARKER, {
        "format": FORMAT,
        "location_id": location_id,
        "installation_id": installation_id(),
        "data_dir": os.path.abspath(os.getcwd()),
        "claimed_at": _now() if claimed else None,
    })


# --- the installation and its locations in app.db -----------------------------------------------


def installation_id() -> str:
    """This installation's id (``app.db`` ``installation``, minted by global
    migration 14)."""
    registry = _registry()
    if registry.installation_id is None:
        from collimate_server.database import get_global_db_connection
        conn = get_global_db_connection()
        try:
            registry.installation_id = conn.execute("SELECT id FROM installation").fetchone()[0]
        finally:
            conn.close()
    assert registry.installation_id is not None
    return registry.installation_id


def _listed() -> list[tuple[str, Path, bool]]:
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return [(row["id"], Path(row["path"]), bool(row["is_default"]))
                for row in conn.execute("SELECT id, path, is_default FROM locations ORDER BY is_default DESC, added_at")]
    finally:
        conn.close()


def _list_location(location_id: str, path: Path, *, default: bool) -> None:
    """Insert or update the location row; ``default`` makes it the one default."""
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM locations WHERE path = ? AND id != ?", (str(path), location_id))
            if default:
                conn.execute("UPDATE locations SET is_default = 0 WHERE is_default = 1")
            conn.execute(
                "INSERT INTO locations (id, path, is_default, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET path = excluded.path, "
                "is_default = MAX(locations.is_default, excluded.is_default)",
                (location_id, str(path), int(default), _now()))
    finally:
        conn.close()


def _channel_count(location: Path) -> int:
    """How many channel folders with a readable channel.json ``location`` holds."""
    count = 0
    try:
        entries = list(os.scandir(location))
    except OSError:
        return 0
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            read_manifest(Path(entry.path))
        except (OSError, ValueError):
            continue
        count += 1
    return count


def _display(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        return str(path)


def refusal_message(path: Path, owner_data_dir: str | None, channels: int) -> str:
    return (f"The channel location {_display(path)} belongs to another Collimate installation "
            f"(data dir {owner_data_dir or 'unknown'}, {channels} channel{'' if channels == 1 else 's'}). "
            "Start with --claim-channels to make this installation its owner and adopt its channels "
            "(the other installation then shows the location as owned by another installation), "
            "or with --channels-dir PATH to keep this installation's channels in a new location at PATH.")


def refusal_line(refused: LocationOwned) -> str:
    """The machine-readable stderr line of a location-owned startup refusal."""
    return json.dumps({"collimate_startup_refused": "location_owned", "path": str(refused.path),
                       "owner_data_dir": refused.owner_data_dir, "channels": refused.channels})


def default_channels_path() -> Path:
    chosen = _startup["channels_dir"] or os.environ.get(CHANNELS_ENV) or DEFAULT_CHANNELS
    return Path(os.path.abspath(os.path.expanduser(chosen)))


def owner_running(data_dir: str | None) -> bool | None:
    """Whether the server at ``data_dir`` holds its lock: True or False, or
    None when it cannot be checked (no data dir recorded, or not on this
    machine). The data dir this process serves is never "running" for a
    claim: its lock is ours."""
    from collimate_server.data_dir_lock import probe
    if not data_dir:
        return None
    if os.path.abspath(data_dir) == os.path.abspath(os.getcwd()):
        return False
    if not os.path.isdir(data_dir):
        return None
    return probe(os.path.join(data_dir, ".collimate", "server.lock"))


# --- startup ------------------------------------------------------------------------------------


def previous_layout_note() -> str | None:
    """The one startup line about the previous storage layout's trees, when
    any is there; nothing reads them."""
    found = [os.path.join(".collimate", name) for name in ("channels", "objects", "objects.trash")
             if os.path.lexists(os.path.join(".collimate", name))]
    if not found:
        return None
    shown = " and ".join(os.path.abspath(path) for path in found)
    return (f"{shown} {'is' if len(found) == 1 else 'are'} from the previous storage layout, "
            f"{'is' if len(found) == 1 else 'are'} no longer used, and can be removed by hand.")


def establish() -> list[str]:
    """Startup steps 4-6 (after app.db's migrations): ownership, then each
    owned location's reconcile, then the index scan. Raises
    :class:`LocationOwned` when the installation owns no location and the
    default path's marker names another installation (without
    ``--claim-channels``), :class:`OwnerRunning` when the claim finds that
    owner's server running, and OSError when the default cannot be made.
    Returns the lines to log."""
    registry = _registry()
    with _lock():
        registry.locations = {}
        registry.homes = {}
        registry.installation_id = None
        outcomes = _ownership(registry)
        for location in registry.locations.values():
            if location.state == "ok":
                outcomes += _reconcile(location)
        outcomes += _recover_moves(registry)
        _scan(registry)
        registry.established = True
    return outcomes


def _ownership(registry: _Registry) -> list[str]:
    ours = installation_id()
    outcomes: list[str] = []
    for location_id, path, is_default in _listed():
        location = Location(location_id, path, is_default)
        if not path.is_dir():
            location.state = "missing"
        else:
            marker = read_marker(path)
            if marker is None:
                _write_marker(path, location_id, claimed=False)
                outcomes.append(f"wrote the missing location marker of {path}")
            elif marker.get("installation_id") != ours:
                location.state = "foreign"
                location.owner_data_dir = marker.get("data_dir")
        registry.locations[location_id] = location
    owned = [location for location in registry.locations.values() if location.state != "foreign"]
    flags = [name for name, given in (("--channels-dir", _startup["channels_dir"]),
                                      ("--claim-channels", _startup["claim"])) if given]
    if owned:
        if flags:
            outcomes.append(f"{' and '.join(flags)} ignored: this installation already owns the channel "
                            f"location{'s' if len(owned) > 1 else ''} "
                            f"{', '.join(str(location.path) for location in owned)} "
                            "(manage locations in the Locations tab)")
        return outcomes
    path = default_channels_path()
    data = Path(os.path.abspath(".collimate"))
    if path == data or data in path.parents or path in data.parents:
        raise OSError(f"the channel location {path} overlaps the data dir's {data}")
    marker = read_marker(path) if path.is_dir() else None
    if marker is None:
        path.mkdir(parents=True, exist_ok=True)
        location_id = str(uuid.uuid4())
        _write_marker(path, location_id, claimed=False)
        outcomes.append(f"created the channel location {path}")
    elif marker.get("installation_id") == ours:
        location_id = marker.get("location_id") or str(uuid.uuid4())
        outcomes.append(f"re-adopted the channel location {path}")
    elif _startup["claim"]:
        result = _claim(path, marker, confirm_unchecked=True)
        location_id = result.location_id
        outcomes.append(f"claimed the channel location {path} from the installation at "
                        f"{marker.get('data_dir') or 'an unknown data dir'}")
    else:
        raise LocationOwned(path, marker.get("data_dir"), _channel_count(path))
    _list_location(location_id, path, default=True)
    for location in registry.locations.values():
        location.is_default = False
    registry.locations[location_id] = Location(location_id, path, True)
    return outcomes


@dataclass
class Claimed:
    location_id: str
    adopted: list[str]
    skipped: list[dict]


def _claim(path: Path, marker: dict, *, confirm_unchecked: bool) -> Claimed:
    owner = marker.get("data_dir")
    running = owner_running(owner)
    if running:
        raise OwnerRunning(str(owner))
    if running is None and not confirm_unchecked:
        raise OwnerUnchecked(owner)
    location_id = marker.get("location_id") or str(uuid.uuid4())
    _write_marker(path, location_id, claimed=True)
    return Claimed(location_id, [], [])


def claim(path: str | os.PathLike, *, confirm_unchecked: bool = False, default: bool = False) -> Claimed:
    """S2a: take ownership of the location at ``path`` whose marker names
    another installation, and adopt its channels.

    Refused with :class:`OwnerRunning` while the owner's server holds its
    data dir's lock, and with :class:`OwnerUnchecked` when the owner cannot
    be checked unless ``confirm_unchecked`` (D18). The marker is rewritten
    with this installation's id and data dir, the location is listed (as the
    default when ``default``), and every channel folder in it is indexed: a
    channel id this installation already has is skipped as
    ``duplicate_channel_id``. Adopted channels arrive whole; their handler
    builds ask for acceptance again and their bundles recompile on first use."""
    location = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    registry = _registry()
    with _lock():
        marker = read_marker(location) if location.is_dir() else None
        if marker is None or marker.get("installation_id") == installation_id():
            raise ValueError(f"{location} is not a location another installation owns")
        claimed = _claim(location, marker, confirm_unchecked=confirm_unchecked)
        _list_location(claimed.location_id, location, default=default)
        if default:
            for other in registry.locations.values():
                other.is_default = False
        found = registry.locations.get(claimed.location_id)
        entry = Location(claimed.location_id, location, default or bool(found and found.is_default))
        registry.locations[claimed.location_id] = entry
        _reconcile(entry)
        claimed.adopted, claimed.skipped = _index_location(registry, entry)
    return claimed


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(4)


def _park(location: Path, folder: Path, why: str) -> str:
    """Move a channel folder that does not open into ``.orphaned/``."""
    from collimate_server.cas import fsync_directory
    parked = location / ORPHANED_DIR
    parked.mkdir(exist_ok=True)
    target = parked / f"{folder.name}-{_stamp()}"
    os.rename(folder, target)
    fsync_directory(location)
    fsync_directory(parked)
    return f"parked {folder} ({why}) as {target}"


def _reconcile(location: Location) -> list[str]:
    """A location's leftovers: ``<uuid>.moving`` (a create or move that did
    not finish) and ``.deleting-*`` (a delete's renamed folder)."""
    import collimate_server.readonly_trees as readonly_trees
    outcomes = []
    try:
        entries = sorted(os.listdir(location.path))
    except OSError as exc:
        return [f"could not read the channel location {location.path}: {exc}"]
    for name in entries:
        path = location.path / name
        if name.endswith(STAGING_SUFFIX) or _DELETING.fullmatch(name):
            try:
                readonly_trees.remove_tree(str(path))
                outcomes.append(f"removed the leftover {path}")
            except OSError as exc:
                outcomes.append(f"could not remove the leftover {path}: {exc}")
    return outcomes


def _scan(registry: _Registry) -> list[str]:
    outcomes = []
    for location in registry.locations.values():
        if location.state == "ok":
            _adopted, skipped = _index_location(registry, location)
            outcomes += [f"{item['id']}: {item['reason']}" for item in skipped]
    return outcomes


def _index_location(registry: _Registry, location: Location) -> tuple[list[str], list[dict]]:
    """Index every channel folder of ``location``; park the ones that do not
    open. Two folders with one channel id keep the one with ``channel.db``
    (the indexed one when both or neither have it) and park the other."""
    adopted: list[str] = []
    skipped: list[dict] = []
    try:
        entries = sorted(os.scandir(location.path), key=lambda entry: entry.name)
    except OSError as exc:
        logger.warning("cannot scan the channel location %s: %s", location.path, exc)
        return adopted, skipped
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        folder = Path(entry.path)
        try:
            manifest = read_manifest(folder)
        except (OSError, ValueError) as exc:
            logger.warning("startup: %s", _park(location.path, folder, f"channel.json does not open: {exc}"))
            continue
        channel_id = manifest["channel_id"]
        known = registry.homes.get(channel_id)
        if known is not None:
            if (folder / DB_FILE).is_file() and not (known.path / DB_FILE).is_file():
                logger.warning("startup: %s", _park(known.path.parent, known.path, "a second copy of a channel"))
            else:
                logger.warning("startup: %s", _park(location.path, folder, "a second copy of a channel"))
                skipped.append({"id": channel_id, "reason": "duplicate_channel_id"})
                continue
        registry.homes[channel_id] = Home(channel_id, folder, location.id, manifest)
        adopted.append(channel_id)
    return adopted, skipped


def is_established() -> bool:
    return _registry().established


# --- the resolver -------------------------------------------------------------------------------


def home(channel_id) -> Home | None:
    return _registry().homes.get(_canonical(channel_id))


def channel_dir(channel_id) -> Path:
    """The channel's folder. Raises :class:`ChannelUnavailable` when it is
    not indexed, is unavailable, or its folder is gone (which marks it
    unavailable: nothing re-creates it)."""
    try:
        identity = _canonical(channel_id)
    except ValueError:
        raise ChannelNotFound(str(channel_id), "not a channel id") from None
    found = _registry().homes.get(identity)
    if found is None:
        raise ChannelNotFound(identity, "it has no channel folder")
    if found.state == "unavailable":
        raise ChannelUnavailable(identity, found.reason or "unavailable")
    if not found.path.is_dir():
        found.state, found.reason = "unavailable", f"its folder {found.path} is missing"
        raise ChannelUnavailable(identity, found.reason)
    return found.path


def db_path(channel_id) -> Path:
    return channel_dir(channel_id) / DB_FILE


def git_dir(channel_id) -> Path:
    return channel_dir(channel_id) / GIT_DIR


def git_rescue_dir(channel_id) -> Path:
    return channel_dir(channel_id) / GIT_RESCUE_DIR


def objects_dir(channel_id) -> Path:
    return channel_dir(channel_id) / OBJECTS_DIR


def cell_state_dir(channel_id, cell_id: str) -> Path:
    """``<channel folder>/cell-state/<cell id>``: per-cell state a run keeps
    across runs of the cell (an Agent's sessions). Not created here."""
    cell = str(uuid.UUID(str(cell_id)))
    return channel_dir(channel_id) / CELL_STATE_DIR / cell


def indexed_channels() -> list[str]:
    """Every available channel's id (not one being created)."""
    return sorted(identity for identity, found in _registry().homes.items() if found.state == "ok")


def homes() -> list[Home]:
    return list(_registry().homes.values())


def locations() -> list[Location]:
    return list(_registry().locations.values())


def manifest(channel_id) -> dict:
    found = home(channel_id)
    if found is None:
        raise ChannelNotFound(_canonical(channel_id), "it has no channel folder")
    return dict(found.manifest)


def mark_unavailable(channel_id, reason: str) -> None:
    """Keep the channel indexed but refuse its use, its data untouched (a
    layout that does not validate)."""
    found = home(channel_id)
    if found is not None:
        found.state, found.reason = "unavailable", reason


def unavailable_channels() -> list[Home]:
    return [found for found in _registry().homes.values() if found.state == "unavailable"]


def boundary_of(path: str | os.PathLike) -> str:
    """The directory up to which a write under ``path`` confirms its
    ancestors' names: the location holding it, else the data dir.

    ``path`` and a location may name the same folder through different
    symlinks: SQLite reports a database by its resolved path, while a
    location keeps the path it was given (on macOS ``$TMPDIR`` and a home
    under ``/var/folders`` resolve to ``/private/var/folders``). So a path no
    location holds as written is matched again with both resolved."""
    locations = list(_registry().locations.values())
    target = Path(os.path.abspath(path))
    for location in locations:
        if location.path == target or location.path in target.parents:
            return str(location.path)
    resolved = Path(os.path.realpath(target))
    for location in locations:
        root = Path(os.path.realpath(location.path))
        if root == resolved or root in resolved.parents:
            return str(location.path)
    return os.path.abspath(os.getcwd())


def default_location() -> Location:
    """The location new channels go to; :class:`NoDefaultLocation` when it is
    missing or foreign, or none is listed."""
    found = next((location for location in _registry().locations.values() if location.is_default), None)
    if found is None or found.state != "ok" or not found.path.is_dir():
        raise NoDefaultLocation("New channels have no default location: it is missing or owned by another "
                                "installation. Claim it, add a location or set another default.")
    return found


# --- create, rename, delete ---------------------------------------------------------------------


def begin_create(channel_id, name: str) -> Path:
    """Create S2 step 1: write ``<default>/<uuid>.moving/`` with its
    ``channel.json`` and index it as ``creating``; the creation then writes
    its DB, layout and stores there and calls :func:`finish_create`, or
    :func:`abandon_create` on failure."""
    from collimate_server.cas import fsync_directory
    identity = _canonical(channel_id)
    registry = _registry()
    with _lock():
        if identity in registry.homes:
            raise FileExistsError(f"channel {identity} already has a folder")
        location = default_location()
        staging = location.path / f"{identity}{STAGING_SUFFIX}"
        if (location.path / identity).exists():
            raise FileExistsError(f"{location.path / identity} already exists")
        staging.mkdir()
        contents = {"format": FORMAT, "channel_id": identity, "name": name, "archived": False,
                    "archived_at": None, "installation_id": installation_id(), "created_at": _now()}
        write_manifest(staging, contents)
        fsync_directory(location.path)
        registry.homes[identity] = Home(identity, staging, location.id, contents, state="creating")
    return staging


def finish_create(channel_id) -> Path:
    """Create S2 steps 2-3: rename the staging folder to ``<uuid>`` and
    index it. The caller has closed every connection to its DB."""
    from collimate_server.cas import fsync_directory
    from collimate_server.database import forget_db
    identity = _canonical(channel_id)
    with _lock():
        found = _registry().homes[identity]
        final = found.path.parent / identity
        forget_db(identity)
        os.rename(found.path, final)
        fsync_directory(final.parent)
        found.path, found.state = final, "ok"
    return final


def abandon_create(channel_id) -> None:
    """A failed create's rollback: the staging (or renamed) folder goes and
    the channel leaves the index."""
    import collimate_server.readonly_trees as readonly_trees
    from collimate_server.database import forget_db
    identity = _canonical(channel_id)
    with _lock():
        found = _registry().homes.pop(identity, None)
        if found is None:
            return
        forget_db(identity)
    readonly_trees.remove_tree(str(found.path), ignore_errors=True)


def rename(channel_id, name: str) -> None:
    """Rewrite the channel's ``channel.json`` with its new name."""
    identity = _canonical(channel_id)
    with _lock():
        folder = channel_dir(identity)
        found = _registry().homes[identity]
        updated = {**found.manifest, "name": name}
        write_manifest(folder, updated)
        found.manifest = updated


def retire(channel_id) -> Path:
    """Delete S2 step 2: rename the channel's folder to
    ``<location>/.deleting-<uuid>-<rand>`` and drop it from the index; the
    caller holds ``channel_files_lock`` and the store lock, and has closed
    the DB. :func:`purge` removes the renamed folder once the locks are
    gone; a failure there is left to the next start's reconcile."""
    from collimate_server.cas import fsync_directory
    identity = _canonical(channel_id)
    with _lock():
        folder = channel_dir(identity)
        target = folder.parent / f"{DELETING_PREFIX}{identity}-{secrets.token_hex(4)}"
        os.rename(folder, target)
        fsync_directory(folder.parent)
        del _registry().homes[identity]
    return target


def purge(path: Path) -> None:
    import collimate_server.readonly_trees as readonly_trees
    try:
        readonly_trees.remove_tree(str(path))
    except OSError as exc:
        logger.warning("could not remove %s (the next start retries): %s", path, exc)


# --- moves (channel_move) -----------------------------------------------------------------------


def _recover_moves(registry: _Registry) -> list[str]:
    """Startup step 5's two-copy recovery (S7), after each location's
    reconcile and before the index scan: a channel folder carrying a
    :data:`MOVE_RECORD` is a move's finished, verified copy, renamed into
    place before a crash kept its source from going. The source it names,
    when it is still there in an owned location, is removed; then the record
    goes. A record whose source location is listed but missing (an
    unplugged disk) stays until that location is back."""
    import collimate_server.readonly_trees as readonly_trees
    from collimate_server.cas import fsync_directory
    outcomes: list[str] = []
    for entry_location in list(registry.locations.values()):
        if entry_location.state != "ok":
            continue
        try:
            entries = sorted(os.scandir(entry_location.path), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            record_path = Path(entry.path) / MOVE_RECORD
            if not record_path.is_file():
                continue
            try:
                with open(record_path, encoding="utf-8") as source:
                    record = json.load(source)
                source_location = registry.locations.get(str(record.get("from_location_id")))
            except (OSError, ValueError, AttributeError):
                record, source_location = {}, None
            if source_location is not None and source_location.state == "missing":
                outcomes.append(f"kept the move record of {entry.path}: its source location "
                                f"{source_location.path} is missing")
                continue
            if (source_location is not None and source_location.state == "ok"
                    and source_location.id != entry_location.id and record.get("channel_id") == entry.name):
                stale = source_location.path / entry.name
                if stale.is_dir():
                    retired = source_location.path / f"{DELETING_PREFIX}{entry.name}-{secrets.token_hex(4)}"
                    os.rename(stale, retired)
                    fsync_directory(source_location.path)
                    readonly_trees.remove_tree(str(retired), ignore_errors=True)
                    outcomes.append(f"removed {stale}: the channel's move to {entry.path} had finished")
            record_path.unlink(missing_ok=True)
            fsync_directory(Path(entry.path))
    return outcomes


def relocate(channel_id, folder: Path, location_id: str) -> None:
    """Point the index at the channel's new folder (a move's switch). The
    caller holds ``channel_files_lock``."""
    found = _registry().homes[_canonical(channel_id)]
    found.path, found.location_id = folder, location_id


def home_location(channel_id) -> Location:
    """The location the channel's folder is in."""
    found = home(channel_id)
    if found is None:
        raise ChannelNotFound(_canonical(channel_id), "it has no channel folder")
    return _registry().locations[found.location_id]


# --- locations (the locations routes) -----------------------------------------------------------


def location(location_id: str) -> Location | None:
    return _registry().locations.get(location_id)


def channels_in(location_id: str) -> list[Home]:
    """The indexed channels (any state) whose folder is in the location."""
    return [found for found in _registry().homes.values() if found.location_id == location_id]


def channel_count(path: Path) -> int:
    """How many readable channel folders ``path`` holds (a foreign location's
    count comes from this scan, and nothing else of its contents)."""
    return _channel_count(path)


@dataclass
class Refreshed:
    #: Channels indexed again: their missing location is back.
    indexed: list[str] = field(default_factory=list)
    #: Channels dropped from the index: their location was claimed away.
    dropped: list[str] = field(default_factory=list)


def refresh_locations() -> Refreshed:
    """Re-read each listed location's folder and marker: one that went is
    ``missing``; one whose marker now names another installation (claimed
    away while this server ran) is ``foreign`` and its channels leave the
    index, as at a start; a missing one that is back with our marker is
    ``ok`` again and its channel folders are indexed."""
    registry = _registry()
    ours = installation_id()
    changed = Refreshed()
    with _lock():
        for entry in registry.locations.values():
            if not entry.path.is_dir():
                if entry.state == "ok":
                    entry.state = "missing"
                continue
            marker = read_marker(entry.path)
            if marker is not None and marker.get("installation_id") != ours:
                if entry.state != "foreign":
                    entry.state, entry.owner_data_dir = "foreign", marker.get("data_dir")
                for found in channels_in(entry.id):
                    del registry.homes[found.channel_id]
                    changed.dropped.append(found.channel_id)
            elif entry.state == "missing":
                entry.state = "ok"
                _reconcile(entry)
                _recover_moves(registry)
                changed.indexed += _index_location(registry, entry)[0]
    return changed


def add_location(path: Path, *, default: bool) -> Claimed:
    """List the folder at ``path`` (already validated: absolute, a directory
    or creatable, writable, overlapping nothing) as a location: create it,
    write our marker (keeping the location id of a marker already ours),
    reconcile it and index its channel folders. A marker naming another
    installation raises :class:`LocationOwned`: such a folder is added only
    by :func:`claim`."""
    from collimate_server.cas import fsync_directory
    registry = _registry()
    with _lock():
        marker = read_marker(path) if path.is_dir() else None
        if marker is not None and marker.get("installation_id") != installation_id():
            raise LocationOwned(path, marker.get("data_dir"), _channel_count(path))
        path.mkdir(parents=True, exist_ok=True)
        fsync_directory(path.parent)
        known = str((marker or {}).get("location_id") or "")
        location_id = known if known and known not in registry.locations else str(uuid.uuid4())
        _write_marker(path, location_id, claimed=False)
        _list_location(location_id, path, default=default)
        if default:
            for other in registry.locations.values():
                other.is_default = False
        entry = Location(location_id, path, default)
        registry.locations[location_id] = entry
        _reconcile(entry)
        _recover_moves(registry)
        adopted, skipped = _index_location(registry, entry)
    return Claimed(location_id, adopted, skipped)


def set_default(location_id: str) -> Location:
    """Make the location the one default (exactly one is). The caller has
    checked it is ours and available."""
    registry = _registry()
    with _lock():
        entry = registry.locations[location_id]
        _list_location(location_id, entry.path, default=True)
        for other in registry.locations.values():
            other.is_default = other.id == location_id
    return entry


def remove_location(location_id: str) -> None:
    """Drop the location from the list. For one of ours that is there, its
    marker goes, then the folder if that leaves it empty. The caller has
    checked it holds no channel and is not the default."""
    from collimate_server.cas import fsync_directory
    from collimate_server.database import get_global_db_connection
    registry = _registry()
    with _lock():
        entry = registry.locations[location_id]
        conn = get_global_db_connection()
        try:
            with conn:
                conn.execute("DELETE FROM locations WHERE id = ?", (location_id,))
        finally:
            conn.close()
        del registry.locations[location_id]
        for found in channels_in(location_id):
            del registry.homes[found.channel_id]
        if entry.state != "ok":
            return
        marker = read_marker(entry.path)
        if marker is not None and marker.get("installation_id") == installation_id():
            (entry.path / MARKER).unlink(missing_ok=True)
            fsync_directory(entry.path)
        try:
            entry.path.rmdir()
            fsync_directory(entry.path.parent)
        except OSError:
            pass

