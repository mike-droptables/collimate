"""Asset-tree cells become channel cells: binary-safe, with their cell.yaml
(the shared libraries it names resolve at admission), each under a fresh id,
and their runner packages offered as drafts."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


API_DIR = Path(__file__).resolve().parent.parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mk_gen_tree(root: Path) -> None:
    """Two cells + a top-level shared library + a loose root file."""
    _write(root / "_shared_lib" / "util.py", "SHARED = 1\n")
    grp = root / "group_one"
    _write(grp / "alpha" / "choice.generator", "id: alpha\nname: Alpha\nsummary: Test generator\nentrypoint: alpha.py\nmain_config: settings.set_yaml\n")
    _write(grp / "alpha" / "alpha.py", "print('a')\n")
    _write(grp / "alpha" / "settings.set_yaml", "fields: []\n")
    _write(grp / "NOTES.md", "loose file\n")
    _write(grp / "cell.yaml", yaml.safe_dump({
        "name": "GroupOne", "icon": "scale",
        "order": 10, "shared": ["_shared_lib"],
    }))
    two = root / "group_two"
    _write(two / "beta" / "choice.generator", "id: beta\nname: Beta\nsummary: Test generator\nentrypoint: beta.py\nmain_config: settings.set_yaml\n")
    _write(two / "beta" / "beta.py", "print('b')\n")
    _write(two / "beta" / "settings.set_yaml", "fields: []\n")
    # no cell.yaml → defaults (titled folder name, order 100)


def test_specs_become_named_cells_with_their_files(fresh_cwd):
    from collimate._host.packaging.asset_cells import read_asset_cells
    from channel_sources import specs_to_cells

    gen_root = fresh_cwd / "assets" / "generators"
    _mk_gen_tree(gen_root)
    cells = {c.name: c for c in specs_to_cells(read_asset_cells(gen_root, "generator"))}
    assert list(cells) == ["GroupOne", "Group Two"]

    one = cells["GroupOne"]
    # cell.yaml metadata reaches the cell.
    assert one.icon == "scale"
    one_files = {f.name for f in one.files}
    # Content folders under their basenames; loose files, the cell.yaml
    # among them, at the cell root. The shared library the cell.yaml names
    # is not read from the tree: admission resolves it from the installed
    # helper set.
    assert one_files == {"alpha/alpha.py", "alpha/choice.generator", "alpha/settings.set_yaml",
                         "NOTES.md", "cell.yaml"}
    assert not any(n.startswith(("generators/", "group_one/", "_shared_lib/")) for n in one_files)

    two = cells["Group Two"]
    assert two.icon is None
    assert "beta/choice.generator" in {f.name for f in two.files}


def test_specs_become_binary_safe_cells_under_fresh_ids__F0047(fresh_cwd):
    from collimate._host.packaging.asset_cells import read_asset_cells
    from cas import read_blob
    from channel_sources import specs_to_cells

    gen_root = fresh_cwd / 'assets' / 'generators'
    _mk_gen_tree(gen_root)
    binary = b'\x00\xff\x89PNG'
    (gen_root / 'group_one' / 'alpha' / 'image.png').write_bytes(binary)
    specs = read_asset_cells(gen_root, 'generator')
    left, right = specs_to_cells(specs), specs_to_cells(specs)
    assert all(c.id.version == 4 for c in left)
    assert not {c.id for c in left} & {c.id for c in right}
    exclude = {'id': True, 'files': {'__all__': {'timestamp'}}, 'created_at': True, 'updated_at': True}
    assert [c.model_dump(exclude=exclude) for c in left] == [c.model_dump(exclude=exclude) for c in right]
    image = next(f for c in left for f in c.files if f.name == 'alpha/image.png')
    assert read_blob(image.blob_hash) == binary


def test_packaged_runners_are_offered_as_drafts__F0047(fresh_cwd):
    import base64
    import runners
    from collimate._host.packaging.asset_cells import read_asset_cells

    assets = fresh_cwd / "assets" / "generators"
    _mk_gen_tree(assets)
    definition = {"id": "preset", "generator": "alpha", "name": "Preset", "description": "", "tags": [],
                  "prompt": "", "settings": {"settings.set_yaml": {}}, "folders": [], "accounts": {},
                  "permissions": {}, "packages": {}}
    _write(assets / "group_one" / "preset" / "preset.runner", yaml.safe_dump(definition))
    _write(assets / "group_one" / "preset" / "files" / "notes" / "brief.md", "brief\n")
    (assets / "group_one" / "preset" / "files" / "logo.png").write_bytes(b"\x00\xff")
    first = read_asset_cells(assets, "generator")[0]
    (drafted,) = runners.packaged_runners(first)
    assert drafted["runner"] == definition
    files = {f["name"]: f for f in drafted["files"]}
    assert files["notes/brief.md"]["content"] == "brief\n"
    assert base64.b64decode(files["logo.png"]["content"]) == b"\x00\xff"
    assert files["logo.png"]["encoding"] == "base64"
    assert set(runners.generator_index([first])) == {"alpha"}
    assert runners.packaged_runners(read_asset_cells(assets, "generator")[1]) == []
