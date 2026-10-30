"""P4F05/P4F46: source payload compatibility without relabeling native wheels."""
import base64
import csv
import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import collimate
import pytest

ROOT = Path(__file__).resolve().parents[3]
SDK = Path(collimate.__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("aux_sdk_payload", ROOT / "playground/build/sdk_payload.py")
PAYLOAD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAYLOAD)


def test_P4F05_P4F46_source_payload_is_pure_and_canonical(tmp_path):
    wheel = PAYLOAD.build(SDK, tmp_path)
    assert wheel.name.startswith("collimate_playground_sdk-")
    assert wheel.name.endswith("-py3-none-any.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not any("native_verifier/" in name or "renderer_compilers/" in name for name in names)
        assert not any(name.endswith((".so", ".pyc", ".exe")) for name in names)
        for source in (SDK / "src").rglob("*"):
            if not source.is_file() or "__pycache__" in source.parts or "native_verifier" in source.parts:
                continue
            assert archive.read(source.relative_to(SDK / "src").as_posix()) == source.read_bytes()
        count = 0
        for source in (SDK / "shared").rglob("*"):
            if not source.is_file() or any(part in {"__pycache__", "node_modules"} for part in source.parts):
                continue
            assert archive.read("collimate/" + source.relative_to(SDK).as_posix()) == source.read_bytes()
            count += 1
        assert count > 0
        # The first-party content trees ship in the app, never in the SDK payload.
        for tree in ("generators", "renderers", "builtin", "suggested"):
            assert not any(name.startswith(f"collimate/{tree}/") for name in names), tree
        info = next(name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/WHEEL"))
        assert b"Root-Is-Purelib: true\nTag: py3-none-any\n" in archive.read(info + "/WHEEL")
        metadata = archive.read(info + "/METADATA")
        assert b"Name: collimate-playground-sdk\n" in metadata
        # Canonical runtime distribution lookup still reads its unchanged source resource.
        canonical = archive.read("collimate/_sdk_metadata/METADATA")
        assert canonical == (SDK / "src/collimate/_sdk_metadata/METADATA").read_bytes()
        assert b"Name: collimate-sdk\n" in canonical
        unpacked = tmp_path / "unpacked"
        archive.extractall(unpacked)
        result = subprocess.run([
            sys.executable, "-I", "-c",
            ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from collimate.shared import sdk_distribution; "
            "d=sdk_distribution(); assert d.metadata['Name']=='collimate-sdk'; "
            "assert d.version; assert any('pyyaml' in r for r in d.requires)"),
            str(unpacked),
        ], capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        records = list(csv.reader(io.StringIO(archive.read(info + "/RECORD").decode())))
        assert {row[0] for row in records} == set(names)
        for name, digest, size in records:
            if name.endswith("/RECORD"):
                assert (digest, size) == ("", "")
                continue
            raw = archive.read(name)
            assert int(size) == len(raw)
            assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()


@pytest.mark.parametrize("hostile", ["binary", "symlink"])
def test_P4F05_source_payload_refuses_host_binary_or_symlink(tmp_path, hostile):
    sdk = tmp_path / "sdk"
    (sdk / "src/collimate/_sdk_metadata").mkdir(parents=True)
    (sdk / "src/collimate/_sdk_metadata/METADATA").write_text("Name: collimate-sdk\n")
    shutil.copy2(SDK / "LICENSE", sdk / "LICENSE")
    (sdk / "pyproject.toml").write_text('''[project]
version = "0.2.0"
requires-python = ">=3.12"
dependencies = []
[tool.hatch.build.targets.wheel]
packages = ["src/collimate"]
[tool.hatch.build.targets.wheel.force-include]
''')
    source = sdk / "src/collimate/unexpected"
    if hostile == "binary":
        source.write_bytes(b"\x7fELF" + b"native executable")
    else:
        source.symlink_to(sdk / "LICENSE")
    with pytest.raises(ValueError, match="Host executable|must not be a symlink"):
        PAYLOAD.build(sdk, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.whl"))
