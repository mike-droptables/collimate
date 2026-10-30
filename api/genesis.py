"""Genesis bootstrapping — initialises .collimate/ on first CWD launch."""
import importlib.util
import os
import json
import logging
import uuid
from datetime import datetime, timezone

from cas import OBJECTS_DIR, store_bytes

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
    data when it is importable, else this checkout's workspace root beside
    api/.

    The wheel's trees carry no ``__init__.py``: each imports as a namespace
    package, whose spec names its directory."""
    spec = importlib.util.find_spec(BUNDLED_PACKAGES[tree])
    if spec is not None and spec.submodule_search_locations:
        return next(iter(spec.submodule_search_locations))
    return os.path.join(os.path.dirname(_API_DIR), tree)


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
    from versions import find_tip, initialize_file_snapshot, insert_version, tip_file_map, version_file_entries
    from persistence import initialize_files, restore_snapshot, snapshot_files
    from cas import require_blob

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
        raw = require_blob(f['body_hash'])
        conn.execute('INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,?)', (f['body_hash'],len(raw)))
        conn.execute('INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)',
                     (cell_id,f['path'],f['body_hash'],f['encoding'],f['language']))
    initialize_file_snapshot(conn, cell_id)


def package_cell_vfiles(cspec) -> list:
    """Rebuild VirtualFiles for a package cell (binary-safe): a blob_hash
    file's raw bytes are stored into the CAS and referenced by hash; text
    files carry ``content``."""
    from models import VirtualFile
    vfiles = []
    for f in cspec.files:
        if f.blob_hash:
            stored = store_bytes(f.data) if f.data is not None else None
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
    from builtin_packages import builtin_cell_id, builtin_cells
    from cell_placement import (
        CAPABILITIES_STACK_NAME, CAPABILITIES_STACK_TYPE, is_capabilities_stack, iter_stacks,
    )
    from models import Stack, Cell
    from state import channel_db_name

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
    """Initialise the workspace for the CWD if not already done.

    No starter channel is created: the app opens to the channel-creation view
    when no channel exists, where the user names the first channel)."""
    if is_bootstrapped():
        return

    logger.info("Bootstrapping workspace...")

    os.makedirs(".collimate/channels", exist_ok=True)
    os.makedirs(OBJECTS_DIR, exist_ok=True)

    with open(GENESIS_MARKER, "w") as f:
        json.dump({"bootstrapped_at": datetime.now(timezone.utc).isoformat()}, f)

    logger.info("Bootstrap complete.")


def load_global_ui_state() -> str | None:
    """The channel-independent AppState JSON text from the global db, or
    None when no state was ever saved.

    This is the source of truth for restoring app state at startup: it's
    readable even when zero channels exist (the channel-creation view),
    unlike the per-channel copies which vanish with their channel. A read
    error (a locked or unreadable app.db) raises: "nothing saved" and "could
    not read" need different answers (main.py's startup restore). The text
    is returned unparsed so a row that does not validate can be kept as is;
    a row whose value is NULL reads as empty text, which does not parse."""
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        row = conn.execute(
            "SELECT coalesce(value, '') AS value FROM ui_state WHERE key='app_state'"
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def parse_saved_app_state(raw: str):
    """The saved app state text as an ``AppState``. Raises ``ValueError``
    (pydantic's ValidationError is one) unless it is what persist_state
    writes: a JSON object whose ``channels`` is a list, validating as
    AppState. Without the ``channels`` check a row like ``{}`` would validate
    to "no channels" and let the startup reconcile park every channel db."""
    from models import AppState
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise ValueError('the saved app state is not a JSON object with a "channels" list')
    if data.get("storage_format") != "sql-layout-v2":
        raise ValueError("Unsupported saved layout storage format")
    layout = AppState.model_validate(data)
    return layout


def snapshot_app_state(app_state) -> dict:
    """Layout and CAS references only; never copy file text into a JSON dump."""
    snapshot = app_state.model_dump(mode="json", exclude={"channels"})
    snapshot["storage_format"] = "sql-layout-v2"
    snapshot["channels"] = []
    for channel in app_state.channels:
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
        snapshot["channels"].append(channel_data)
    return snapshot


# A saved app state that does not validate is kept in app.db under this
# prefix plus a UTC stamp (main.restore_app_state). While any such row
# exists, nothing may treat a channel database as unowned: the startup
# orphan reconcile stays off until recovery validates those channel identities.
APP_STATE_QUARANTINE_PREFIX = "app_state.invalid-"


def quarantine_app_state(raw: str) -> str:
    """Keep ``raw`` under a new ``app_state.invalid-<UTC stamp>`` key and
    return the key. A copy already kept verbatim (the same row failing on a
    later start) is reused."""
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        kept = conn.execute(
            "SELECT key FROM ui_state WHERE key GLOB ? AND value = ?",
            (APP_STATE_QUARANTINE_PREFIX + "*", raw),
        ).fetchone()
        if kept:
            return kept["key"]
        key = APP_STATE_QUARANTINE_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        conn.execute("INSERT INTO ui_state (key, value) VALUES (?, ?)", (key, raw))
        conn.commit()
        return key
    finally:
        conn.close()


def quarantined_app_states() -> list[str]:
    """The ``app_state.invalid-*`` keys in app.db, oldest first."""
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        return [
            row["key"] for row in conn.execute(
                "SELECT key FROM ui_state WHERE key GLOB ? ORDER BY key",
                (APP_STATE_QUARANTINE_PREFIX + "*",),
            )
        ]
    finally:
        conn.close()


def save_global_ui_state(state_dict: dict):
    """Persist placement metadata with SQL-owned files to the global db."""
    from database import get_global_db_connection
    from cell_metadata import write_layout_state, layout_publication
    conn = get_global_db_connection()
    try:
        with layout_publication(conn):
            conn.execute("BEGIN IMMEDIATE")
            write_layout_state(conn, state_dict)
    finally:
        conn.close()


def reconcile_restored_cell_files(restored) -> None:
    """Restore derived body caches solely from accepted SQL working refs."""
    from database import get_db_connection
    from state import channel_db_name
    from persistence import snapshot_files
    from models import VirtualFile
    from cas import read_blob
    for channel in restored.channels:
        conn = get_db_connection(channel_db_name(channel), create=False)
        try:
            for stack in [*channel.main_stacks, *channel.nav_stacks]:
                for cell in stack.cells:
                    metadata = snapshot_files(conn, str(cell.id), include_deleted=True)
                    files = []
                    for entry in metadata:
                        if entry['deleted']:
                            continue
                        raw = read_blob(entry['body_hash'])
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
