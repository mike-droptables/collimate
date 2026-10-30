# SPDX-License-Identifier: MIT
"""Supported action runtimes, the generated-files check first, and a release
whose SDK upload publishes the wheels and sdist only."""
import re
from pathlib import Path

import yaml

from collimate.shared import toolchain


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def sdk_workflows():
    """The SDK's own workflows, and release.yml, which builds and publishes it."""
    paths = sorted(WORKFLOWS.glob("sdk-*.yml"))
    assert [path.name for path in paths] == ["sdk-ci.yml", "sdk-native-wheels.yml"]
    return [*paths, WORKFLOWS / "release.yml"]


def test_workflows_use_supported_node_and_actions_F1852_F1876():
    actions = {"checkout": "v7", "setup-node": "v7", "setup-python": "v7",
               "upload-artifact": "v7", "download-artifact": "v8"}
    for path in sdk_workflows():
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action.startswith("actions/"):
                    name, version = action.removeprefix("actions/").split("@")
                    assert version == actions[name], (path, action)
                if "node-version" in step.get("with", {}):
                    assert step["with"]["node-version"] == toolchain()["node"], path


def test_the_test_job_checks_every_generated_file_first():
    """Once the toolchain is set up and the snapshot bodies fetched, the SDK
    test job's first step is the repository's one generated-files check; the workflow runs no generator of
    its own (the toolchain check it ran before is a generate.py step)."""
    path = WORKFLOWS / "sdk-ci.yml"
    steps = yaml.safe_load(path.read_text())["jobs"]["test"]["steps"]
    assert [step.get("uses") for step in steps[:2]] == ["actions/checkout@v7", "./.github/actions/setup-toolchain"]
    # The module snapshot's bodies live off main; the check reads them.
    assert steps[2]["name"] == "Fetch the module snapshot bodies"
    assert steps[3]["run"] == "python3 scripts/generate.py --check"
    assert not re.search(r"sync_\w+\.py", path.read_text())


def test_release_publishes_only_wheel_and_sdist_TR04():
    """release.yml's SDK publish job verifies the native wheels and publishes
    them to PyPI; it builds or attaches no other release asset and cannot
    write the repository's releases, and no SDK workflow touches a release."""
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["publish-sdk"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    steps = job["steps"]
    names = [step.get("name") for step in steps if step.get("name")]
    assert names == ["Verify all four native wheels", "Publish to PyPI"]
    download = next(step for step in steps if step.get("uses") == "actions/download-artifact@v8")
    assert download["with"] == {"name": "native-dist", "path": "dist"}
    assert not any("gh release" in step.get("run", "") for step in steps)
    writers = [name for name, other in workflow["jobs"].items()
               if other.get("permissions", {}).get("contents") == "write"]
    assert writers == ["release"]
    for path in sdk_workflows()[:-1]:
        assert "gh release" not in path.read_text(), path
