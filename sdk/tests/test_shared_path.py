# SPDX-License-Identifier: MIT
"""``shared_path`` only ever returns a real filesystem path: a resource that
is not on disk (a zip install) is not found, rather than handed back as a
temporary extraction that is deleted before the caller can read it."""
from __future__ import annotations

import io
import zipfile

import pytest

from collimate import shared


def test_a_non_filesystem_resource_is_not_returned_as_a_dangling_path(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("collimate/shared/docs/x.md", "# x\n")
    root = zipfile.Path(zipfile.ZipFile(buf), "collimate/")
    monkeypatch.setattr(shared, "files", lambda _pkg: root)
    monkeypatch.setattr(shared, "_IN_TREE_SDK_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        shared.shared_path("docs", "x.md")


def test_a_shipped_doc_resolves_to_a_path_that_exists():
    path = shared.shared_path("docs", "how-to-write-a-generator.md")
    assert path.exists() and path.read_text().startswith("#")
