"""The bundled trees against the installed ``collimate`` wheel's RECORD.

An installed wheel ships the workspace's ``content/``, ``builtins/`` and
``module-snapshot/`` as the package data ``collimate_content/``,
``collimate_builtins/`` and ``collimate_module_snapshot/``
(api/hatch_build.py), and its RECORD names every file with its sha256.
``/ready`` re-hashes the files the server reads (``genesis.bundled_dir``)
against that RECORD (read as text: ``Distribution.files`` leaves out a
listed file that is no longer on disk), so an edited, truncated or stray file in an
installation reads as drift. A checkout reads the live trees beside api/
and has nothing to verify: it reports ``checkout``.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import os
from pathlib import Path


def is_packaged(main_path: str) -> bool:
    """Whether the installed app distribution owns this running main module."""
    from importlib.metadata import PackageNotFoundError, distribution
    try:
        installed = distribution("collimate").locate_file("main.py")
    except PackageNotFoundError:
        return False
    return Path(str(installed)).resolve() == Path(main_path).resolve()


def _record_digest(path: Path) -> str:
    """A file's sha256 in RECORD's encoding: urlsafe base64, unpadded."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def _files(root: Path):
    """Every regular file under ``root``, relative and sorted; interpreter
    bytecode (``__pycache__``, which an installer may compile) skipped."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith((".pyc", ".pyo")):
                yield Path(dirpath, name).relative_to(root).as_posix()


def verify_bundled() -> dict:
    """Compare the bundled trees the server reads with the RECORD of the
    installed ``collimate`` distribution::

        {"status": "ok" | "drift" | "checkout", "checked": <int>,
         "mismatched": [...], "missing": [...], "extra": [...]}

    Paths are the RECORD's (``collimate_content/...``). ``checkout`` when
    any tree resolves to the checkout beside api/, or no installed
    ``collimate`` RECORD lists the trees' files."""
    from importlib.metadata import PackageNotFoundError, distribution
    import genesis

    result = {"status": "checkout", "checked": 0, "mismatched": [], "missing": [], "extra": []}
    workspace = Path(genesis.__file__).resolve().parent.parent
    served = {}
    for tree, package in genesis.BUNDLED_PACKAGES.items():
        path = Path(genesis.bundled_dir(tree))
        if path.resolve() == (workspace / tree).resolve():
            return result
        served[package] = path
    try:
        record = distribution("collimate").read_text("RECORD") or ""
    except PackageNotFoundError:
        return result
    expected = {}
    for row in csv.reader(record.splitlines()):
        if len(row) >= 2 and row[1] and row[0].split("/", 1)[0] in served:
            expected[row[0]] = row[1]
    if not expected:
        return result
    on_disk = {f"{package}/{rel}" for package, root in served.items() for rel in _files(root)}
    for name, recorded in sorted(expected.items()):
        package, rel = name.split("/", 1)
        path = served[package] / rel
        if not path.is_file():
            result["missing"].append(name)
        elif recorded != "sha256=" + _record_digest(path):
            result["mismatched"].append(name)
    result["extra"] = sorted(on_disk - set(expected))
    result["checked"] = len(expected)
    result["status"] = "drift" if result["mismatched"] or result["missing"] or result["extra"] else "ok"
    return result
