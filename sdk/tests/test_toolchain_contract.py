# SPDX-License-Identifier: MIT
"""The npm manifests, lockfiles and workflows restate the toolchain contracts exactly.

``shared/contracts/toolchain.json`` holds Node and the renderer runtime's
React; ``renderer_bundle_contract.json`` holds esbuild. ``scripts/sync_toolchain.py``
writes them into the fields that cannot read JSON; these tests hold the
checkout to it and exercise its rewrite and refusal on a scratch copy.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from collimate._host.renderer_bundle import CONTRACT
from collimate.shared import toolchain

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "sdk/scripts/sync_toolchain.py"
_spec = importlib.util.spec_from_file_location("sync_toolchain", SCRIPT)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

def run(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(root / "sdk/scripts/sync_toolchain.py"), *args],
                          capture_output=True, text=True, timeout=60, env=env)


def test_the_checkout_matches_the_contracts():
    result = run(WORKSPACE, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_toolchain_names_the_bundle_contract_as_its_esbuild_source():
    facts = toolchain()
    assert facts == json.loads((WORKSPACE / "sdk/shared/contracts/toolchain.json").read_text())
    source = WORKSPACE / "sdk" / facts["esbuild_version_source"]
    assert json.loads(source.read_text())["esbuild_version"] == CONTRACT["esbuild_version"]
    assert sync.facts(WORKSPACE)["esbuild"] == CONTRACT["esbuild_version"]


@pytest.fixture
def scratch(tmp_path) -> Path:
    """The contracts, the script, every dependent manifest with its lockfile,
    and the workflows, copied as they are."""
    names = {sync.TOOLCHAIN.as_posix(), "sdk/" + toolchain()["esbuild_version_source"], "sdk/scripts/sync_toolchain.py"}
    for manifest, _, _ in sync.MANIFEST_FIELDS:
        names |= {manifest, str(Path(manifest).with_name("package-lock.json"))}
    names |= {path.relative_to(WORKSPACE).as_posix() for path in sync._workflows(WORKSPACE)}
    for name in names:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WORKSPACE / name, tmp_path / name)
    return tmp_path


def test_drift_is_reported_with_the_command_and_rewritten_in_place(scratch):
    hooks = scratch / "sdk/packages/renderer-hooks/package.json"
    workflow = scratch / ".github/actions/setup-toolchain/action.yml"
    before = {path: path.read_bytes() for path in (hooks, workflow)}
    react = toolchain()["react"]
    hooks.write_text(hooks.read_text().replace(f'"react": "{react}"', '"react": "0.0.0-test"'))
    workflow.write_text(workflow.read_text().replace(toolchain()["node"], "22"))
    checked = run(scratch, "--check")
    assert checked.returncode == 1
    assert ("sdk/packages/renderer-hooks/package.json devDependencies.react is '0.0.0-test'; "
            f"the contract says '{react}'") in checked.stdout
    line = workflow.read_text().splitlines().index("        node-version: '22'") + 1
    assert f".github/actions/setup-toolchain/action.yml:{line} node-version is '22'" in checked.stdout
    assert checked.stdout.splitlines()[-1].endswith("Run: python3 sdk/scripts/sync_toolchain.py")
    synced = run(scratch)
    assert synced.returncode == 0, synced.stdout + synced.stderr
    # Only the values changed back: every byte of formatting is the original's.
    assert {path: path.read_bytes() for path in before} == before
    assert run(scratch, "--check").returncode == 0


def test_a_stale_lockfile_without_npm_is_refused_with_the_exact_command(scratch):
    lock = scratch / "frontend/package-lock.json"
    data = json.loads(lock.read_text())
    data["packages"][""]["dependencies"]["esbuild-wasm"] = "0.0.0-test"
    lock.write_text(json.dumps(data, indent=2) + "\n")
    refused = run(scratch, env={"PATH": ""})
    assert refused.returncode == 1
    assert "refused: npm or node is not on PATH" in refused.stdout
    assert "(cd frontend && npm install --package-lock-only)" in refused.stdout


def test_a_value_span_is_the_value_at_its_key_path_only():
    text = '{\n  "overrides": {"tsup": {"esbuild": "1"}, "esbuild": "2"},\n  "esbuild": "3"\n}\n'
    start, end = sync.value_span(text, ("overrides", "esbuild"))
    assert text[start:end] == '"2"'
    start, end = sync.value_span(text, ("esbuild",))
    assert text[start:end] == '"3"'
    assert sync.value_span(text, ("dependencies", "esbuild")) is None
