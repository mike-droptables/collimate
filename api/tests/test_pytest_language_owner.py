"""Language inference has one owner across restoration and import paths."""

import versions

import importlib

import pytest


@pytest.mark.parametrize("path,expected", [
    ("dir/X.PY", "python"), ("config.TOML", "toml"), ("file.scss", "scss"),
    ("dir.v2/README", "plaintext"), (".bashrc", "plaintext"),
    ("settings.set_yaml", "plaintext"),
])
def test_public_language_owner_uses_exact_fs_map__F0048_F0440(path, expected):
    assert importlib.import_module("models").language_for(path) == expected


def test_typed_restore_preserves_language_and_encoding__F0048_F0440(fresh_cwd):
    from cas import hash_and_store_bytes
    entries=[dict(path='UPPER.PY',body_hash=hash_and_store_bytes(b"print('ok')"),encoding='utf-8',language='python'),
             dict(path='config.toml',body_hash=hash_and_store_bytes(b'key = 1'),encoding='utf-8',language='toml'),
             dict(path='asset.bin',body_hash=hash_and_store_bytes(b'valid UTF8'),encoding='binary',language='custom-binary')]
    actual={file.name:file for file in versions.build_virtual_files(entries)}
    assert {name:file.language for name,file in actual.items()} == {
        'UPPER.PY':'python','config.toml':'toml','asset.bin':'custom-binary'}
    assert actual['asset.bin'].blob_hash==entries[2]['body_hash']
    assert actual['asset.bin'].content==''


def test_runner_missing_text_language_is_derived__F0048_F0440(fresh_cwd):
    from cas import hash_and_store_bytes
    from runners import _vfiles

    blob = hash_and_store_bytes(b"\xff bytes")
    rows = [{"name": "config.TOML", "content": "key = 1"},
            {"name": "custom.PY", "content": "text", "language": "custom"},
            {"name": "asset.bin", "blob_hash": blob, "language": "binary"},
            {"name": "unlabelled.bin", "blob_hash": blob}]
    actual = {file.name: file for file in _vfiles("", rows)}
    assert actual["config.TOML"].language == "toml"
    assert actual["custom.PY"].language == "custom"
    assert actual["asset.bin"].language == "binary"
    assert actual["unlabelled.bin"].language == "text"
