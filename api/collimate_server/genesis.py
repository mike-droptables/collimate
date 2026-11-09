"""Genesis bootstrapping — initialises .collimate/ on first CWD launch."""
import hashlib
import importlib.util
import os
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GENESIS_MARKER = os.path.join(".collimate", ".genesis")
_API_DIR = os.path.dirname(os.path.abspath(__file__))

# The first-party trees installed with the app: workspace-root folder ->
# its package inside the ``collimate`` wheel (api/hatch_build.py).
BUNDLED_PACKAGES = {
    "content": "collimate_content",
    "builtins": "collimate_builtins",
    "module-snapshot": "collimate_module_snapshot",
}


def bundled_dir(tree: str) -> str:
    """Where the bundled tree ``tree`` (``"content"``, ``"builtins"`` or
    ``"module-snapshot"``) is: the installed ``collimate`` wheel's package
    data when it is importable, else this checkout's root, two levels above
    this package (api/collimate_server/).

    The wheel's trees carry no ``__init__.py``: each imports as a namespace
    package, whose spec names its directory."""
    spec = importlib.util.find_spec(BUNDLED_PACKAGES[tree])
    if spec is not None and spec.submodule_search_locations:
        return next(iter(spec.submodule_search_locations))
    return os.path.join(os.path.dirname(os.path.dirname(_API_DIR)), tree)


def _resolve_assets_dir(kind: str) -> str:
    """The bundled directory of an asset kind: ``"generators"`` and
    ``"renderers"`` under the content tree, ``"builtin"`` the built-ins tree."""
    return bundled_dir("builtins") if kind == "builtin" else os.path.join(bundled_dir("content"), kind)


GENERATORS_DIR = _resolve_assets_dir("generators")
RENDERERS_DIR = _resolve_assets_dir("renderers")
# The app's own handlers and renderers, served for every channel
# (builtin_packages.py); never seeded as channel cells.
BUILTIN_DIR = _resolve_assets_dir("builtin")


def is_bootstrapped() -> bool:
    return os.path.exists(GENESIS_MARKER)


def seed_cell_from_vfiles(conn, run_id: str, cell_id: str, cell_name: str,
                           seq: int, vfiles) -> None:
    """Persist an in-memory cell: insert the cell row and each VirtualFile's
    content into the CAS + blobs + cell_files tables.

    A binary VirtualFile (``blob_hash`` set, empty ``content``) references bytes
    the caller already stored in the CAS — its ``blob_hash`` is used directly and
    the blob size read back from the CAS object."""
    from collimate_server.versions import find_tip, initialize_file_snapshot, insert_version, tip_file_map, version_file_entries
    from collimate_server.persistence import initialize_files, restore_snapshot, snapshot_files
    from collimate_server.cas import channel_store_of

    vfiles = list(vfiles)
    exists = conn.execute("SELECT 1 FROM cells WHERE id=?", (cell_id,)).fetchone()
    if exists:
        restore_snapshot(conn, cell_id, vfiles, writer_id='host:genesis')
    else:
        initialize_files(conn, cell_id, vfiles)
    entries = snapshot_files(conn, cell_id)
    files = {f['path']: f['body_hash'] for f in entries}
    if exists:
        tip = find_tip(conn, cell_id)
        previous = tip_file_map(conn, tip)
        def shape(fs):
            return sorted((f['path'],f['body_hash'],f['encoding'],f['language']) for f in fs)
        if shape(entries) != shape(version_file_entries(conn, tip)):
            insert_version(conn, tip_id=tip, parent_files=previous, files=files, file_entries=entries,
                           run_id=run_id, owner_id=cell_id, version_name=cell_name,
                           source_type="genesis", generator_name="genesis")
        return
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num, version_name, source_type) "
        "VALUES (?, ?, 'genesis', ?, ?, 'genesis')", (cell_id, run_id, seq, cell_name),
    )
    for f in entries:
        raw = channel_store_of(conn).require_blob(f['body_hash'])
        conn.execute('INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,?)', (f['body_hash'],len(raw)))
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)',
                     (cell_id,f['path'],f['body_hash'],f['encoding'],f['language']))
    initialize_file_snapshot(conn, cell_id)


def package_cell_vfiles(cspec, store) -> list:
    """Rebuild VirtualFiles for a package cell (binary-safe): a blob_hash
    file's raw bytes are stored into ``store`` (the target channel's body
    store; None names them by hash only, for a cell never committed) and
    referenced by hash; text files carry ``content``."""
    from collimate_server.models import VirtualFile
    vfiles = []
    for f in cspec.files:
        if f.blob_hash:
            stored = (None if f.data is None else store.store_bytes(f.data) if store is not None
                      else hashlib.sha256(f.data).hexdigest())
            vfiles.append(VirtualFile(
                name=f.name, language=f.language, content="", blob_hash=stored,
            ))
        else:
            vfiles.append(VirtualFile(name=f.name, language=f.language, content=f.content))
    return vfiles


def ensure_capabilities_stack(ch) -> None:
    """Guarantee the channel's one Capabilities stack, headed by the app's
    built-in cards. The only code that creates the stack.

    A missing stack is added hidden (a navigation stack). Two stacks are a
    bug and raise ``ValueError``; nothing merges them at runtime. The cards
    (``builtin_packages.builtin_cells``, in the build's order) get their
    deterministic ids, and their name and icon from the build on every call,
    so an app upgrade renames them; a card the build no longer ships leaves
    the layout. Like insert_blank_cell, a file-less card needs no DB row until
    its overlay is first written. Runs on a channel's candidate before its
    packages are admitted, at channel creation, for every restored channel
    and at reset."""
    from collimate_server.builtin_packages import builtin_cell_id, builtin_cells
    from collimate_server.cell_placement import (
        CAPABILITIES_STACK_NAME, CAPABILITIES_STACK_TYPE, is_capabilities_stack, iter_stacks,
    )
    from collimate_server.models import Stack, Cell
    from collimate_server.state import channel_db_name

    found = [s for s in iter_stacks(ch) if is_capabilities_stack(s)]
    if len(found) > 1:
        raise ValueError("Channel has more than one Capabilities stack")
    if found:
        stack = found[0]
    else:
        stack = Stack(name=CAPABILITIES_STACK_NAME, stack_type=CAPABILITIES_STACK_TYPE)
        ch.nav_stacks.append(stack)
    current = {str(c.id): c for c in stack.cells if c.builtin}
    cards = []
    for spec in builtin_cells():
        cell_id = builtin_cell_id(channel_db_name(ch), spec.spec_id)
        card = current.get(cell_id) or Cell(id=uuid.UUID(cell_id), name=spec.name, files=[], builtin=True)
        card.name, card.icon = spec.name, spec.icon
        cards.append(card)
    stack.name = CAPABILITIES_STACK_NAME
    stack.cells = cards + [c for c in stack.cells if not c.builtin]


def bootstrap():
    """Initialise the data dir's ``.collimate/`` for the CWD if not already
    done. Channels live in their locations (``channel_paths``), not here.

    No starter channel is created: the app opens to the channel-creation view
    when no channel exists, where the user names the first channel)."""
    if is_bootstrapped():
        return
    logger.info("Bootstrapping workspace...")
    os.makedirs(".collimate", exist_ok=True)
    with open(GENESIS_MARKER, "w") as f:
        json.dump({"bootstrapped_at": datetime.now(timezone.utc).isoformat()}, f)
    logger.info("Bootstrap complete.")


# The app_state row of app.db: only the installation's active channel. Each
# channel's layout is its own channel.db's ``layout`` row (cell_metadata).
APP_STATE_FORMAT = "sql-layout-v3"


def load_active_channel_id() -> str | None:
    """The saved active channel id from app.db, or None. A read error raises
    (main.py's startup restore answers it)."""
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        row = conn.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()
    finally:
        conn.close()
    if row is None or not row["value"]:
        return None
    try:
        data = json.loads(row["value"])
    except ValueError:
        return None
    active = data.get("active_channel_id") if isinstance(data, dict) else None
    return active if isinstance(active, str) else None


def save_active_channel_id(active: str | None) -> None:
    from collimate_server.database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO ui_state (key, value) VALUES ('app_state', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps({"storage_format": APP_STATE_FORMAT, "active_channel_id": active}),))
    finally:
        conn.close()


def snapshot_channel(channel) -> dict:
    """One channel's layout and name, without file bodies."""
    channel_data = channel.model_dump(mode="json", exclude={"main_stacks", "nav_stacks"})
    for kind in ("main_stacks", "nav_stacks"):
        stacks = channel_data[kind] = []
        for stack in getattr(channel, kind):
            stack_data = stack.model_dump(mode="json", exclude={"cells"})
            stack_data["cells"] = []
            for cell in stack.cells:
                cell_data = cell.model_dump(mode="json", exclude={"files"})
                stack_data["cells"].append(cell_data)
            stacks.append(stack_data)
    return channel_data


def snapshot_app_state(app_state) -> dict:
    """Layout and CAS references only; never copy file text into a JSON dump."""
    snapshot = app_state.model_dump(mode="json", exclude={"channels"})
    snapshot["storage_format"] = APP_STATE_FORMAT
    snapshot["channels"] = [snapshot_channel(channel) for channel in app_state.channels]
    return snapshot


def load_channel(channel_id: str):
    """The channel as its folder holds it: the name from ``channel.json``,
    the layout from its ``channel.db``. Raises ValueError (pydantic's
    ValidationError is one) when the layout is missing or does not
    validate, OSError or sqlite3.Error when the folder cannot be read."""
    import collimate_server.channel_paths as channel_paths
    from collimate_server.cell_metadata import read_layout
    from collimate_server.database import get_db_connection
    from collimate_server.models import Channel
    manifest = channel_paths.manifest(channel_id)
    conn = get_db_connection(channel_id)
    try:
        layout = read_layout(conn)
    finally:
        conn.close()
    if layout is None:
        raise ValueError("it has no saved layout")
    if layout.get("id") != channel_id:
        raise ValueError("its saved layout names another channel")
    return Channel.model_validate({**layout, "name": manifest["name"]})


def save_app_state(snapshot: dict) -> None:
    """Persist a state snapshot: each channel whose layout changed gets its
    ``layout`` row (and cell metadata heads) in its own channel.db, and the
    active channel id goes to app.db when it changed."""
    from collimate_server.cell_metadata import save_channel_layout
    from collimate_server.channel_guard import is_moving
    for channel in snapshot["channels"]:
        if is_moving(str(channel["id"])):
            continue  # its folder is being copied; the move persists it after the switch
        save_channel_layout(channel)
    active = snapshot.get("active_channel_id")
    if _saved_active.get(os.getcwd(), _UNSAVED) != active:
        save_active_channel_id(active)
        _saved_active[os.getcwd()] = active


_UNSAVED = object()
# The active channel id last written to app.db, per data dir.
_saved_active: dict[str, object] = {}


def reconcile_restored_cell_files(channel) -> None:
    """Restore derived body caches solely from accepted SQL working refs."""
    from collimate_server.database import get_db_connection
    from collimate_server.state import channel_db_name
    from collimate_server.persistence import snapshot_files
    from collimate_server.models import VirtualFile
    from collimate_server.cas import channel_store_of
    conn = get_db_connection(channel_db_name(channel))
    try:
        store = channel_store_of(conn)
        for stack in [*channel.main_stacks, *channel.nav_stacks]:
            for cell in stack.cells:
                metadata = snapshot_files(conn, str(cell.id), include_deleted=True)
                files = []
                for entry in metadata:
                    if entry['deleted']:
                        continue
                    raw = store.read_blob(entry['body_hash'])
                    if raw is None:
                        raise OSError('Missing accepted content ' + entry['path'])
                    binary = entry['encoding'] == 'binary'
                    files.append(VirtualFile(name=entry['path'], content='' if binary else raw.decode('utf-8'),
                                             blob_hash=entry['body_hash'] if binary else None,
                                             language=entry['language'], body_hash=entry['body_hash'],
                                             file_revision=entry['file_revision'], encoding=entry['encoding'],
                                             writer_id=entry['writer_id'], edit_seq=entry['edit_seq']))
                cell.files = files
        conn.commit()
    finally:
        conn.close()
