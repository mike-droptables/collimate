"""Path-traversal hardening for artifact hashing.

``local_path`` in IPC artifact payloads is generator-controlled input: a
hostile or buggy generator could point it at ``../../etc/passwd`` (or an
absolute path, or a symlink out of the workspace) and have the host-side
process manager pull arbitrary readable host files into the CAS, where
they become servable blobs. ``_hash_artifacts`` must refuse any workspace
artifact that resolves outside ``workspace_dir``; these tests pin that.
"""
from __future__ import annotations

import os
import pytest

from process_manager.artifacts import ArtifactsMixin


class _Harness(ArtifactsMixin):
    """Minimal host: records hashed paths instead of touching the CAS."""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.hashed: list[str] = []
        self.log_buffer: list[dict] = []

    def _hash_and_store_file(self, abs_path: str) -> str | None:
        self.hashed.append(abs_path)
        return "fakehash"

    def _hash_and_store_bytes(self, raw: bytes) -> str | None:
        return "fakehash-bytes"


def _payload(local_path: str, cell_path: str = "out.txt") -> dict:
    return {"artifacts": [{"type": "workspace", "cell_path": cell_path,
                           "local_path": local_path}]}


def test_normal_relative_path_is_hashed(tmp_path):
    (tmp_path / "out.txt").write_text("data")
    h = _Harness(str(tmp_path))
    files = h._hash_artifacts(_payload("out.txt"))
    assert files == {"out.txt": "fakehash"}
    assert h.hashed == [os.path.realpath(str(tmp_path / "out.txt"))]


def test_nested_relative_path_is_hashed(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("data")
    h = _Harness(str(tmp_path))
    files = h._hash_artifacts(_payload("sub/f.txt"))
    assert files == {"out.txt": "fakehash"}


def test_parent_traversal_is_refused(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    ws = tmp_path / "ws"
    ws.mkdir()
    h = _Harness(str(ws))
    files = h._hash_artifacts(_payload("../outside.txt"))
    assert files == {}
    assert h.hashed == []


def test_absolute_path_is_refused(tmp_path):
    outside = tmp_path / "host_secret.txt"
    outside.write_text("secret")
    ws = tmp_path / "ws"
    ws.mkdir()
    h = _Harness(str(ws))
    # os.path.join discards the base when the second arg is absolute.
    files = h._hash_artifacts(_payload(str(outside)))
    assert files == {}
    assert h.hashed == []


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link.txt").symlink_to(outside)
    h = _Harness(str(ws))
    files = h._hash_artifacts(_payload("link.txt"))
    assert files == {}
    assert h.hashed == []


def test_memory_artifacts_are_unaffected(tmp_path):
    h = _Harness(str(tmp_path))
    files = h._hash_artifacts({"artifacts": [
        {"type": "memory", "cell_path": "mem.txt", "content": "hello"},
    ]})
    assert files == {"mem.txt": "fakehash-bytes"}


class _RefusingHarness(_Harness):
    """The CAS refuses every blob (oversized or write failure)."""

    def _hash_and_store_file(self, abs_path: str) -> str | None:
        self.hashed.append(abs_path)
        return None

    def _hash_and_store_bytes(self, raw: bytes) -> str | None:
        return None


def test_cas_refused_workspace_artifact_fails_the_operation(tmp_path):
    (tmp_path / "big.bin").write_text("data")
    h = _RefusingHarness(str(tmp_path))
    with pytest.raises(OSError,match='CAS refused run file big.bin: 4 bytes'):
        h._hash_artifacts(_payload("big.bin", cell_path="big.bin"))
    assert h.hashed == [os.path.realpath(str(tmp_path / 'big.bin'))]


def test_cas_refused_memory_artifact_fails_the_operation(tmp_path):
    h = _RefusingHarness(str(tmp_path))
    with pytest.raises(OSError,match='CAS refused run file mem.txt: 5 bytes'):
        h._hash_artifacts({"artifacts": [
            {"type": "memory", "cell_path": "mem.txt", "content": "hello"},
        ]})
    assert h.hashed == []
