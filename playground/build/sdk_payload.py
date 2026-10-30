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

import tomllib

NAME = "collimate_playground_sdk"
HOST_RESOURCES = {"src/collimate/native_verifier", "build/renderer_compilers"}
MACHINE_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"MZ")


def build(sdk_dir: Path, out_dir: Path) -> Path:
    project = tomllib.loads((sdk_dir / "pyproject.toml").read_text())
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    version = project["project"]["version"]
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

    for package in wheel["packages"]:
        collect(sdk_dir / package, Path(package).name)
    for source, destination in wheel["force-include"].items():
        if source not in HOST_RESOURCES:
            collect(sdk_dir / source, destination)
    # Runtime dependency identity comes from canonical, build-checked metadata;
    # the installed distribution name deliberately identifies this private build.
    if b"Name: collimate-sdk\n" not in files.get("collimate/_sdk_metadata/METADATA", b""):
        raise ValueError("Canonical SDK runtime metadata is absent or invalid")
    info = f"{NAME}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.3", "Name: collimate-playground-sdk", f"Version: {version}",
                "Summary: Private source payload for the Collimate browser demo",
                f"Requires-Python: {project['project']['requires-python']}"]
    metadata.extend(f"Requires-Dist: {dependency}" for dependency in project["project"]["dependencies"])
    files[f"{info}/METADATA"] = ("\n".join(metadata) + "\n").encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: collimate-playground\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[f"{info}/licenses/LICENSE"] = (sdk_dir / "LICENSE").read_bytes()
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
