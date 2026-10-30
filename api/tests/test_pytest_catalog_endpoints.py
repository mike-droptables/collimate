"""Coverage for the generator + renderer catalog endpoints.

These tests install their own generator and renderer via the
``test_generator`` / ``test_renderer`` fixtures (see conftest.py), so
they don't depend on whatever ``genesis`` happens to seed from the SDK.
The catalog endpoints walk the Capabilities cells of the active channel —
the fixtures append a cell with a self-contained manifest to its
Capabilities stack, and the test asserts on that.

Assertions pin:
  * response is a JSON list (not None, not a dict-of-items)
  * every entry has the fields the frontend + e2e matrix rely on
  * the test-installed entry is present and shaped correctly
  * manifest endpoint returns the parsed YAML for a given id
"""
from __future__ import annotations


def test_list_generators_returns_installed_catalog(client, test_generator):
    r = client.get("/api/generators")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), f"expected list, got {type(body)}"

    names = {g["name"] for g in body}
    assert test_generator in names, (
        f"expected test-installed generator {test_generator!r} in catalog, "
        f"got {sorted(names)}"
    )

    # Every entry must expose the identity/behavior fields the manage view's
    # generator rows read. Generators are never picked directly — saved
    # runners are the UI unit — so a row shows display + behavior badges +
    # the config folders it accepts, and none of the per-generator picking or
    # tag-resolution fields.
    for g in body:
        for key in ("name", "display_name", "chat", "config_folders"):
            assert key in g, f"generator {g.get('name')!r} missing key {key!r}: {g}"
        assert "execution_mode" not in g
        assert "available_ui" not in g
        # The old input-type trichotomy is gone (every generator is
        # single-shaped), along with the modify/create ``type`` array +
        # input-bounds fields.
        assert "input" not in g
        assert "type" not in g
        assert "input_kind" not in g


def test_list_renderers_returns_installed_catalog(client, test_renderer):
    r = client.get("/api/renderers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)

    ids = {e["id"] for e in body}
    assert test_renderer in ids, (
        f"expected test-installed renderer {test_renderer!r} in catalog, "
        f"got {sorted(ids)}"
    )

    # Every entry must expose the contract the NativeRenderer +
    # SandboxIframe branches read.
    for e in body:
        for key in (
            "id",
            "name",
            "version",
            "extensions",
            "default_for",
            "supports_diff",
            "embed",
            "content_hash",
        ):
            assert key in e, f"renderer {e.get('id')!r} missing key {key!r}: {e}"

        assert e["embed"] in {"native", "iframe"}, (
            f"renderer {e['id']} has unknown embed {e['embed']!r}"
        )
        assert isinstance(e["extensions"], list)
    # default_for passes through as the manifest declares it: the built-in
    # plain text is a default for every text file, a renderer that declares
    # nothing a default for nothing.
    by_id = {e["id"]: e for e in body}
    assert by_id["text"]["default_for"] == ["*"] and by_id["text"]["builtin"] is True
    assert by_id[test_renderer]["default_for"] == []


def test_list_renderers_deduplicates_by_id(client, test_renderer):
    r = client.get("/api/renderers")
    assert r.status_code == 200
    body = r.json()
    ids = [e["id"] for e in body]
    assert len(ids) == len(set(ids)), f"duplicate renderer ids: {ids}"


def test_renderer_content_hash_tracks_binary_files_and_names(client, test_renderer):
    """content_hash changes when a binary sibling's bytes or a file name
    change, not just when text content changes."""
    from state import find_active_channel
    from models import VirtualFile
    from cas import store_bytes
    from cell_commits import commit_source_cell
    from cell_placement import capabilities_stack_cells

    def current_hash():
        r = client.get("/api/renderers")
        assert r.status_code == 200, r.text
        return next(e for e in r.json() if e["id"] == test_renderer)["content_hash"]

    channel = find_active_channel()
    cell = next(c for c in capabilities_stack_cells(channel)
                if any(f.name == f"{test_renderer}/main.tsx" for f in c.files))
    asset_path = f"{test_renderer}/icon.png"
    cell.files.append(VirtualFile(name=asset_path, blob_hash=store_bytes(b'\x00first binary'), language='binary'))
    commit_source_cell(channel, cell, cell.files, persist=False)
    before = current_hash()

    # Replace the binary asset's bytes (new CAS hash, content still empty).
    asset = next(f for f in cell.files if f.name == asset_path)
    asset.blob_hash = store_bytes(b'\x00second binary')
    commit_source_cell(channel, cell, cell.files, persist=False)
    after_binary = current_hash()
    assert after_binary != before

    # Rename a file without touching any content.
    next(f for f in cell.files if f.name == asset_path).name = f"{test_renderer}/logo.png"
    commit_source_cell(channel, cell, cell.files, persist=False)
    assert current_hash() != after_binary
