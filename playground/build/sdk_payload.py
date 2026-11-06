"""Private pure-source SDK payload; native SDK wheels are never transformed.

The staged API's collimate_playground shim supplies compiler and verifier
execution through the supervised WASM bridge. Host executable resources have
no place in this separately named distribution.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

import ast
import tomllib

NAME = "collimate_playground_sdk"
HOST_RESOURCES = {"src/collimate/native_verifier", "build/renderer_compilers"}
# The SDK's pure-source part of the one `collimate` distribution (its wheel
# also carries collimate_server, which this payload never does), and the SDK
# package data build/hatch_build.py adds to it, minus the host resources
# (the prepared compilers). Tests pin RENDERER_TOOLS to the hook's tuple.
PACKAGES = ("src/collimate", "src/gen")
RENDERER_TOOLS = ("renderer_bundle.mjs", "renderer_bundle_core.mjs", "renderer_bundle_core.d.mts",
                  "package.json", "package-lock.json", "bundle_renderer_test.mjs")
DATA = {"shared": "collimate/shared",
        **{f"scripts/{name}": f"collimate/renderer_tools/{name}" for name in RENDERER_TOOLS}}


def sdk_constants(sdk_dir: Path) -> tuple[tuple[str, ...], str]:
    """``SDK_REQUIREMENTS`` and ``SDK_REQUIRES_PYTHON`` from collimate.shared,
    read from its source (importing it would need the package importable)."""
    tree = ast.parse((sdk_dir / "src/collimate/shared.py").read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in {"SDK_REQUIREMENTS", "SDK_REQUIRES_PYTHON"}:
                found[node.targets[0].id] = ast.literal_eval(node.value)
    if set(found) != {"SDK_REQUIREMENTS", "SDK_REQUIRES_PYTHON"}:
        raise ValueError("collimate.shared lacks SDK_REQUIREMENTS or SDK_REQUIRES_PYTHON")
    return tuple(found["SDK_REQUIREMENTS"]), found["SDK_REQUIRES_PYTHON"]
MACHINE_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"MZ")


def build(sdk_dir: Path, out_dir: Path) -> Path:
    root = sdk_dir.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    requirements, requires_python = sdk_constants(sdk_dir)
    files: dict[str, bytes] = {}

    def collect(source: Path, destination: str):
        if source.is_symlink():
            raise ValueError(f"SDK payload source must not be a symlink: {source}")
        relative = source.relative_to(sdk_dir).as_posix()
        if relative in HOST_RESOURCES or source.name in {"__pycache__", "node_modules", ".git", "llm"}:
            return
        if source.is_dir():
            for child in sorted(source.iterdir()):
                collect(child, f"{destination}/{child.name}")
        elif source.is_file():
            if source.suffix in {".pyc", ".pyo"}:
                return
            raw = source.read_bytes()
            if source.suffix in {".so", ".dylib", ".dll", ".exe"} or raw.startswith(MACHINE_MAGIC):
                raise ValueError(f"Host executable is forbidden in SDK source payload: {source}")
            if destination in files:
                raise ValueError(f"Duplicate SDK payload destination: {destination}")
            files[destination] = raw
        else:
            raise ValueError(f"Missing or nonregular SDK payload source: {source}")

    for package in PACKAGES:
        collect(sdk_dir / package, Path(package).name)
    for source, destination in DATA.items():
        collect(sdk_dir / source, destination)
    # Runtime dependency identity comes from canonical, build-checked metadata;
    # the installed distribution name deliberately identifies this private build.
    if b"Name: collimate\n" not in files.get("collimate/_sdk_metadata/METADATA", b""):
        raise ValueError("Canonical SDK runtime metadata is absent or invalid")
    info = f"{NAME}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.3", "Name: collimate-playground-sdk", f"Version: {version}",
                "Summary: Private source payload for the Collimate browser demo",
                f"Requires-Python: {requires_python}"]
    metadata.extend(f"Requires-Dist: {dependency}" for dependency in requirements)
    files[f"{info}/METADATA"] = ("\n".join(metadata) + "\n").encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: collimate-playground\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/licenses/LICENSE"] = (root / "LICENSE").read_bytes()
    record = io.StringIO(newline="")
    rows = csv.writer(record)
    for name, raw in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        rows.writerow([name, f"sha256={digest}", len(raw)])
    rows.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{NAME}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, raw)
    return target
