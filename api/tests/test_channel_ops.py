"""
Smoke test for channel CRUD, the folder export/import round trip, copying a
channel and the channel settings document.

Designed to be cross-platform (Linux / macOS / Windows). Run from the
repository root with:

    python api/tests/test_channel_ops.py

The script chdirs into a fresh temp directory so it never touches your
real .collimate/. It uses FastAPI's TestClient so the routes execute
exactly as they would for the live frontend.

Export writes plain folders, so the round trip asserts that a binary file
survives byte-for-byte through a zip export and a streamed import, and that
an import path escaping its cell (``..``) is refused.
"""

import io
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from uuid import uuid4


# Make api/ importable when run from anywhere
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.dirname(THIS_DIR)
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x02\x03\x04"


def green(s): return f"\033[32m{s}\033[0m"
def red(s): return f"\033[31m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"


class TestRun:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  {green('PASS')} {label}")
        else:
            self.failed += 1
            print(f"  {red('FAIL')} {label}" + (f"  --  {detail}" if detail else ""))

    def section(self, name):
        print(f"\n{yellow('==')} {name}")


def seed_retained_binary(channel_id: str, path: str, digest: str) -> None:
    """Keep an explicit immutable binary snapshot for cross-channel GC checks."""
    from collimate_server.database import get_db_connection
    from collimate_server.models import VirtualFile
    from collimate_server.versions import synthesize_genesis_row
    conn = get_db_connection(channel_id)
    try:
        run_id, cell_id = str(uuid4()), str(uuid4())
        conn.execute("INSERT INTO runs (id, prompt, status) VALUES (?, 'test', 'completed')", (run_id,))
        assert synthesize_genesis_row(conn, cell_id, run_id, 0, [
            VirtualFile(name=path, content="", blob_hash=digest, encoding="binary", language="binary"),
        ])
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    tmp_root = tempfile.mkdtemp(prefix="collimate_smoke_")
    original_cwd = os.getcwd()
    os.chdir(tmp_root)
    # The channels live in their own location (default <data dir>/Channels).
    os.environ["COLLIMATE_CHANNELS"] = os.path.join(tmp_root, "Channels")
    print(f"Test workspace: {tmp_root}")

    # Defer imports until after chdir so bootstrap targets the temp dir
    from fastapi.testclient import TestClient
    from collimate_server.main import app  # noqa: E402
    from collimate_server.state import state, channel_db_name, persist_state  # noqa: E402
    from collimate_server.models import Stack, Cell, VirtualFile  # noqa: E402
    from collimate_server.cas import channel_store  # noqa: E402
    import collimate_server.channel_paths as channel_paths  # noqa: E402
    from collimate_server.auth_middleware import origin_guard  # noqa: E402

    # A tokenless server answers only loopback Host names; the TestClient
    # sends ``Host: testserver``.
    origin_guard.trusted_hosts.add("testserver")
    t = TestRun()

    try:
        with TestClient(app) as client:
            # ──────────────────────────────────────────────────────────
            t.section("initial state")
            r = client.get("/api/state")
            t.check("GET /api/state succeeds", r.status_code == 200)
            t.check("boots with zero channels",
                    len(r.json()["channels"]) == 0,
                    f"got {len(r.json()['channels'])}")
            r = client.post("/api/channels", params={"name": "Starting"})
            t.check("create Starting returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
            r = client.get("/api/state")
            initial = r.json()
            t.check("active_channel_id set after create", bool(initial["active_channel_id"]))
            starting_id = initial["active_channel_id"]
            starting_ch = next(c for c in initial["channels"] if c["id"] == starting_id)
            capabilities = [s for s in starting_ch["nav_stacks"] + starting_ch["main_stacks"]
                            if s.get("stack_type") == "capabilities"]
            t.check("starting channel seeded with one Capabilities stack",
                    [s["name"] for s in capabilities] == ["Capabilities"])

            # ──────────────────────────────────────────────────────────
            t.section("create channel")
            r = client.post("/api/channels", params={"name": "Alpha"})
            t.check("create Alpha returns 200", r.status_code == 200)
            alpha = r.json()
            alpha_id = alpha["id"]
            t.check("Alpha is now active",
                    state.active_channel_id and str(state.active_channel_id) == alpha_id)
            alpha_obj = next(c for c in state.channels if str(c.id) == alpha_id)
            alpha_db = str(channel_paths.db_path(channel_db_name(alpha_obj)))
            t.check("Alpha db file exists on disk", os.path.exists(alpha_db),
                    f"expected {alpha_db}")

            # ──────────────────────────────────────────────────────────
            t.section("build a format-fidelity cell (icon/settings/pinned/binary)")
            blob_hash = channel_store(alpha_id).hash_and_store_bytes(PNG_BYTES)
            t.check("binary bytes stored in CAS", bool(blob_hash) and len(blob_hash) == 64)
            rich_cell = Cell(
                name="Rich",
                icon="sparkles",
                settings={"knob": "value", "n": 3},
                pinned="logo.png",
                files=[
                    VirtualFile(name="readme.md", language="markdown", content="hello\n"),
                    VirtualFile(name="logo.png", language="binary", content="", blob_hash=blob_hash),
                ],
            )
            from collimate_server.database import get_db_connection
            from collimate_server.persistence import initialize_files
            conn = get_db_connection(str(alpha_obj.id))
            try:
                initialize_files(conn, str(rich_cell.id), rich_cell.files)
                conn.commit()
            finally:
                conn.close()
            alpha_obj.main_stacks.append(Stack(
                name="RichStack",
                cells=[rich_cell], stack_type="standard",
            ))
            persist_state()
            rich_cell_id = str(rich_cell.id)

            # ──────────────────────────────────────────────────────────
            t.section("export the stack as a zip of plain folders")
            rich_stack_id = str(alpha_obj.main_stacks[-1].id)
            r = client.get(f"/api/channels/{alpha_id}/export.zip",
                           params={"scope": "stack", "stack_id": rich_stack_id})
            t.check("export.zip returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
            exported = {}
            if r.status_code == 200:
                t.check("export.zip is an attachment named after the stack",
                        'filename="RichStack.zip"' in r.headers.get("content-disposition", ""))
                with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
                    exported = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
            t.check("zip holds the stack/cell/file tree",
                    set(exported) == {"RichStack/Rich/readme.md", "RichStack/Rich/logo.png"}, str(sorted(exported)))
            t.check("text bytes exported exactly", exported.get("RichStack/Rich/readme.md") == b"hello\n")
            t.check("binary bytes exported exactly", exported.get("RichStack/Rich/logo.png") == PNG_BYTES)
            r = client.get(f"/api/channels/{alpha_id}/export/manifest",
                           params={"scope": "stack", "stack_id": rich_stack_id})
            t.check("export manifest returns 200", r.status_code == 200)
            if r.status_code == 200:
                t.check("manifest root is the stack", r.json()["root"] == "RichStack")
                t.check("manifest lists the same files",
                        {e["path"] for e in r.json()["entries"]} == set(exported))

            # ──────────────────────────────────────────────────────────
            t.section("import the exported folder as a new stack")
            r = client.post("/api/channels", params={"name": "LoadTarget"})
            t.check("create LoadTarget", r.status_code == 200)
            loadtarget_id = r.json()["id"]
            refs = []
            for path in ("readme.md", "logo.png"):
                r = client.post(f"/api/channels/{loadtarget_id}/blob/stream", content=exported.get(f"RichStack/Rich/{path}", b""))
                t.check(f"stream upload of {path} returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
                if r.status_code == 200:
                    refs.append({"path": path, "blob_hash": r.json()["blob_hash"]})
            r = client.post(f"/api/channels/{loadtarget_id}/import", json={"insert_index": None, "stacks": [
                {"name": "RichStack", "cells": [{"name": "Rich", "files": refs, "dirs": []}]}]})
            t.check("import returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")

            lt_obj = next(c for c in state.channels if str(c.id) == loadtarget_id)
            imported = next(
                (c for s in lt_obj.main_stacks for c in s.cells if c.name == "Rich"), None,
            )
            t.check("Rich cell imported", imported is not None)
            if imported:
                t.check("imported cell has a fresh identity", str(imported.id) != rich_cell_id)
                logo = next((f for f in imported.files if f.name == "logo.png"), None)
                t.check("imported binary file has a blob_hash", logo is not None and bool(logo.blob_hash))
                if logo and logo.blob_hash:
                    p = channel_store(loadtarget_id).blob_path(logo.blob_hash)
                    with open(p, "rb") as fh:
                        t.check("imported binary bytes byte-identical", fh.read() == PNG_BYTES)
                readme = next((f for f in imported.files if f.name == "readme.md"), None)
                t.check("imported text decoded", readme is not None and readme.content == "hello\n")

            # ──────────────────────────────────────────────────────────
            t.section("an import path escaping its cell is refused (400)")
            r = client.post(f"/api/channels/{loadtarget_id}/import", json={"insert_index": None, "stacks": [
                {"name": "Bad", "cells": [{"name": "Bad", "files": [{"path": "../evil.txt", "blob_hash": refs[0]["blob_hash"] if refs else "0" * 64}], "dirs": []}]}]})
            t.check("`..` import path returns 400", r.status_code == 400, f"got {r.status_code}")

            # ──────────────────────────────────────────────────────────
            t.section("rename channel")
            r = client.post(f"/api/channels/{alpha_id}/select")
            t.check("select Alpha", r.status_code == 200)

            ch_obj = next(c for c in state.channels if str(c.id) == alpha_id)
            old_db_id = channel_db_name(ch_obj)
            old_db_path = str(channel_paths.db_path(old_db_id))
            t.check("Alpha db exists before rename", os.path.exists(old_db_path))

            r = client.put(f"/api/channels/{alpha_id}/rename", params={"name": "Alpha Renamed"})
            t.check("rename returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
            if r.status_code == 200:
                ch_obj = next(c for c in state.channels if str(c.id) == alpha_id)
                t.check("channel name updated in state", ch_obj.name == "Alpha Renamed")
                new_db_id = channel_db_name(ch_obj)
                new_db_path = str(channel_paths.db_path(new_db_id))
                t.check("database identity stays fixed after rename",
                        new_db_path == old_db_path and os.path.exists(new_db_path))
                from collimate_server.persistence import read_file
                t.check("accepted files survive channel rename",
                        read_file(alpha_id, rich_cell_id, "readme.md")[1] == b"hello\n"
                        and read_file(alpha_id, rich_cell_id, "logo.png")[1] == PNG_BYTES)

            # ──────────────────────────────────────────────────────────
            t.section("duplicate display labels retain distinct identities")
            r = client.put(f"/api/channels/{alpha_id}/rename", params={"name": "Starting"})
            t.check("rename to existing display label succeeds", r.status_code == 200)
            t.check("both channel identities retain the shared label",
                    {str(c.id) for c in state.channels if c.name == "Starting"} == {alpha_id, starting_id})
            t.check("same-label channel cannot read another channel's cell",
                    client.get(f"/api/channels/{starting_id}/cells/{rich_cell_id}/files/readme.md").status_code == 404)
            t.check("original UUID still reads its accepted bytes",
                    client.get(f"/api/channels/{alpha_id}/cells/{rich_cell_id}/files/readme.md").content == b"hello\n")
            t.check("display labels do not address settings",
                    client.get("/api/channels/Starting/settings").status_code == 404)

            # ──────────────────────────────────────────────────────────
            t.section("delete channel + blob GC")
            r = client.post("/api/channels", params={"name": "ToDelete"})
            t.check("create ToDelete", r.status_code == 200)
            todelete_id = r.json()["id"]
            todelete_obj = next(c for c in state.channels if str(c.id) == todelete_id)
            todelete_db_id = channel_db_name(todelete_obj)
            todelete_folder = str(channel_paths.channel_dir(todelete_db_id))
            todelete_db = str(channel_paths.db_path(todelete_db_id))
            t.check("ToDelete db exists", os.path.exists(todelete_db))

            # Each channel keeps its bodies in its own store, inside its folder.
            todelete_store, starting_store = channel_store(todelete_db_id), channel_store(starting_id)
            unique_bytes = b"unique-to-todelete-" + os.urandom(16)
            unique_hash = todelete_store.hash_and_store_bytes(unique_bytes)
            t.check("body stored in the channel's store", unique_hash is not None)
            blob_path_before = todelete_store.blob_path(unique_hash)
            t.check("blob file on disk", os.path.exists(blob_path_before))
            t.check("the store is inside the channel folder", blob_path_before.startswith(todelete_folder + os.sep))

            seed_retained_binary(todelete_db_id, "test.txt", unique_hash)

            shared_bytes = b"shared-blob-" + os.urandom(16)
            shared_hash = starting_store.hash_and_store_bytes(shared_bytes)
            shared_blob_path = starting_store.blob_path(shared_hash)
            seed_retained_binary(starting_id, "shared.txt", shared_hash)
            orphan_hash = starting_store.hash_and_store_bytes(b"orphan-" + os.urandom(16))
            orphan_path = starting_store.blob_path(orphan_hash)

            r = client.delete(f"/api/channels/{todelete_id}")
            t.check("delete returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
            t.check("ToDelete removed from state",
                    not any(str(c.id) == todelete_id for c in state.channels))
            t.check("ToDelete db file unlinked", not os.path.exists(todelete_db))
            t.check("ToDelete folder and its store gone at once",
                    not os.path.exists(todelete_folder) and not os.path.exists(blob_path_before))
            t.check("no deleted folder is left in the location",
                    not [n for n in os.listdir(os.path.dirname(todelete_folder)) if todelete_db_id in n])
            # Recent publications are protected: age both fixtures, then
            # exercise the actual maintenance endpoint.
            import time
            from collimate_server.cas_gc import MIN_BLOB_AGE_SECONDS
            aged = time.time() - MIN_BLOB_AGE_SECONDS - 60
            for path in (shared_blob_path, orphan_path):
                os.utime(path, (aged, aged))
            gc = client.post("/api/storage/collect")
            t.check("explicit collection completes", gc.status_code == 200 and not gc.json().get("aborted"))
            t.check("old unreferenced body swept from a living channel's store", not os.path.exists(orphan_path))
            t.check("referenced body preserved", os.path.exists(shared_blob_path))

            # ──────────────────────────────────────────────────────────
            t.section("delete only-remaining channel is allowed (falls back to zero channels)")
            ids_to_delete = [str(c.id) for c in state.channels if str(c.id) != starting_id]
            for cid in ids_to_delete:
                client.delete(f"/api/channels/{cid}")
            t.check("only one channel left", len(state.channels) == 1,
                    f"got {len(state.channels)}: {[c.name for c in state.channels]}")
            r = client.delete(f"/api/channels/{starting_id}")
            t.check("deleting last channel returns 200", r.status_code == 200, r.text if r.status_code != 200 else "")
            t.check("workspace now has zero channels", len(state.channels) == 0,
                    f"got {len(state.channels)}")
            t.check("active_channel_id cleared", state.active_channel_id is None)
            r = client.post("/api/channels", params={"name": "Starting"})
            t.check("re-create Starting after empty returns 200", r.status_code == 200)
            recreated_id = r.json()["id"]

            # ──────────────────────────────────────────────────────────
            t.section("channel settings document")
            r = client.put(f"/api/channels/{recreated_id}/settings", json={
                "keys": {"SYNTH_API_KEY": {"alias": "synth_api_work"}},
            })
            t.check("channel-local key aliases are rejected", r.status_code == 400)
            r = client.put(f"/api/channels/{recreated_id}/settings", json={"sync": {"localBrowse": False}})
            t.check("the retired sync section is rejected", r.status_code == 400)
            r = client.put(f"/api/channels/{recreated_id}/settings", json={
                "keyboard": {"overrides": {"command-palette": "Mod+K"}},
            })
            t.check("PUT channel settings returns 200", r.status_code == 200,
                    r.text if r.status_code != 200 else "")
            doc = client.get(f"/api/channels/{recreated_id}/settings").json()
            t.check("settings doc has kind channel-settings", doc.get("kind") == "channel-settings")
            t.check("settings doc excludes global account data",
                    "keys" not in doc and "accounts" not in doc and "sync" not in doc)

            # ──────────────────────────────────────────────────────────
            t.section("copy the current channel's packages")
            source = next(c for c in state.channels if str(c.id) == recreated_id)
            from collimate_server.cell_placement import capabilities_stack_cells
            source_capabilities = capabilities_stack_cells(source)
            r = client.post("/api/channels", params={"name": "Copied"}, json={"config": {
                "packages": {"channel_id": recreated_id}}})
            t.check("create channel copying the current returns 200", r.status_code == 200,
                    r.text if r.status_code != 200 else "")
            copied_id = r.json()["id"] if r.status_code == 200 else ""
            copied = next((c for c in state.channels if str(c.id) == copied_id), None)
            t.check("the copy is active", copied is not None and state.active_channel_id == copied.id)
            if copied is not None:
                copied_capabilities = capabilities_stack_cells(copied)
                t.check("capability cells copied by name",
                        [c.name for c in copied_capabilities] == [c.name for c in source_capabilities])
                t.check("copied cells take fresh identities",
                        not {str(c.id) for c in copied_capabilities} & {str(c.id) for c in source_capabilities})
                seeded = client.get(f"/api/channels/{copied_id}/settings").json()
                t.check("the copy starts with default settings",
                        seeded["keyboard"]["overrides"] == {} and seeded["theme"] == {"override": None})

            # ──────────────────────────────────────────────────────────
            t.section("keyboard round trip into a fresh channel")
            r = client.post("/api/channels", params={"name": "LoadedSettings"})
            t.check("create LoadedSettings", r.status_code == 200)
            loaded_id = r.json()["id"]
            r = client.put(f"/api/channels/{loaded_id}/settings", json={"keyboard": doc["keyboard"]})
            t.check("PUT the keyboard section returns 200", r.status_code == 200,
                    r.text if r.status_code != 200 else "")
            t.check("loaded keyboard section matches",
                    client.get(f"/api/channels/{loaded_id}/settings").json()["keyboard"] == doc["keyboard"])

    except Exception:
        traceback.print_exc()
        t.failed += 1
    finally:
        os.chdir(original_cwd)
        import time
        for _ in range(5):
            try:
                shutil.rmtree(tmp_root)
                break
            except (OSError, PermissionError):
                time.sleep(0.1)

    print()
    total = t.passed + t.failed
    color = green if t.failed == 0 else red
    print(color(f"{t.passed}/{total} passed"))
    return 0 if t.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
