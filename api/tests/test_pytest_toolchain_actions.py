"""The supported Node toolchain applies to workflows and composite actions, the
module snapshot's bodies are fetched wherever the tree is set up, and the API
test job checks the generated files first."""
import re
from pathlib import Path

import yaml
from collimate.shared import toolchain


def test_workflows_use_supported_node_and_actions_F1852_F1062_F1095_F1875():
    # These are the verified Node24 action majors, not the installed Node version.
    actions = {"checkout": "v7", "setup-node": "v7", "setup-python": "v7",
               "cache": "v6", "upload-artifact": "v7", "download-artifact": "v8",
               "configure-pages": "v6", "upload-pages-artifact": "v5", "deploy-pages": "v5"}
    node = toolchain()["node"]

    def check(value, path):
        if isinstance(value, dict):
            if "node-version" in value:
                assert value["node-version"] == node, path
            action = value.get("uses", "")
            if action.startswith("actions/"):
                name, version = action.removeprefix("actions/").split("@")
                assert version == actions[name], (path, action)
            for child in value.values():
                check(child, path)
        elif isinstance(value, list):
            for child in value:
                check(child, path)

    root = Path(__file__).resolve().parents[2] / ".github"
    for path in root.rglob("*.yml"):
        check(yaml.safe_load(path.read_text()), path)


def test_uv_cache_precedes_sdk_preparation_and_workspace_resolution__F1113():
    root = Path(__file__).resolve().parents[2]
    setup = yaml.safe_load((root / ".github/actions/setup-toolchain/action.yml").read_text())
    shared = setup["runs"]["steps"]
    caches = [(i, step) for i, step in enumerate(shared)
              if step.get("uses") == "actions/cache@v6"
              and "-uv-" in step.get("with", {}).get("key", "")]
    assert len(caches) == 1, "toolchain setup must restore/save the uv package cache once"
    cache_index, cache = caches[0]
    source = next(step for step in shared[:cache_index] if step.get("id") == "uv")
    assert "uv cache dir" in source["run"]
    assert "uv --version" in source["run"]
    assert cache["with"]["path"] == "${{ steps.uv.outputs.cache-path }}"
    key = cache["with"]["key"]
    files = key.split("hashFiles(", 1)[1].rsplit(")", 1)[0]
    assert [name.strip().strip("'") for name in files.split(",")] == [
        "uv.lock", "pyproject.toml", "sdk/pyproject.toml", "api/pyproject.toml", "build/prepare_sdk_checkout.sh"]
    for identity in ("runner.os", "runner.arch", "steps.uv.outputs.version"):
        assert identity in key
    assert "restore-keys" not in cache["with"]
    prepare_index = next(i for i, step in enumerate(shared)
                         if "bash build/prepare_sdk_checkout.sh" in step.get("run", ""))
    assert cache_index < prepare_index

    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    checked = []
    for name, job in workflow["jobs"].items():
        steps = job["steps"]
        uses_uv = [i for i, step in enumerate(steps)
                   if "uv sync" in step.get("run", "") or "uv run" in step.get("run", "")]
        if not uses_uv:
            continue
        setup_index = next(i for i, step in enumerate(steps)
                           if step.get("uses") == "./.github/actions/setup-toolchain")
        assert all(setup_index < i for i in uses_uv), name
        assert not any("pip install uv" in step.get("run", "")
                       for step in steps[setup_index + 1:]), name
        checked.append(name)
    assert set(checked) == {"api-tests", "api-typecheck", "desktop-e2e"}


FETCH = {"name": "Fetch the module snapshot bodies",
         "run": "set -euo pipefail\ngit fetch --depth=1 origin module-snapshot\npython3 build/snapshot_store.py fetch\n"}


def test_api_tests_check_every_generated_file_first():
    """Once the toolchain is set up and the snapshot bodies are in the tree,
    the api-tests job's first step is the repository's one generated-files
    check; test.yml runs no generator of its own (the built-in list check it
    ran before is a generate.py step)."""
    path = Path(__file__).resolve().parents[2] / ".github/workflows/test.yml"
    steps = yaml.safe_load(path.read_text())["jobs"]["api-tests"]["steps"]
    assert [step.get("uses") for step in steps[:2]] == ["actions/checkout@v7", "./.github/actions/setup-toolchain"]
    assert steps[2] == FETCH
    assert steps[3]["run"] == "python3 scripts/generate.py --check"
    assert not re.search(r"sync_\w+\.py", path.read_text())


def test_snapshot_bodies_are_fetched_right_after_the_toolchain_everywhere():
    """main carries module-snapshot/index.json, not the bodies it names: every
    job that sets the tree up fetches them next, before any step reads them;
    a job that builds the app wheel without the action fetches them after
    preparing the checkout."""
    root = Path(__file__).resolve().parents[2] / ".github/workflows"
    toolchain_jobs = []
    for path in (root / "release.yml", root / "sdk-ci.yml", root / "test.yml"):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            steps = job.get("steps", [])
            for i, step in enumerate(steps):
                if step.get("uses") == "./.github/actions/setup-toolchain":
                    assert steps[i + 1] == FETCH, (path.name, name)
                    toolchain_jobs.append(f"{path.name}:{name}")
            assert sum(step.get("name") == FETCH["name"] for step in steps) <= 1, (path.name, name)
    assert set(toolchain_jobs) == {
        "release.yml:app-dist", "sdk-ci.yml:test", "sdk-ci.yml:wheel-312",
        *(f"test.yml:{job}" for job in ("frontend-typecheck", "frontend-unit", "api-tests", "api-typecheck",
                                        "frontend-browser", "desktop-e2e"))}
    candidate = yaml.safe_load((root / "native-install.yml").read_text())["jobs"]["candidate"]["steps"]
    names = [step.get("name") for step in candidate]
    fetch = names.index(FETCH["name"])
    assert {key: candidate[fetch][key] for key in FETCH} == FETCH
    assert "prepare_sdk_checkout.sh" in candidate[fetch - 1]["run"]
    assert "python -m build --wheel --no-isolation api" in candidate[fetch + 1]["run"]
