"""Bundle the workspace-root content trees that live beside this member.

A checkout keeps them at api/../<tree>. An sdist can only carry files under its
own root, so the sdist stores them at <sdist>/<tree> and the wheel built from
that sdist reads them there (an unpacked sdist is recognised by its PKG-INFO).

main git-ignores the module snapshot's bodies (module-snapshot/modules/); a
build refuses while one its index names is missing or changed, which
build/snapshot_store.py fetch writes from the orphan branch module-snapshot.
"""
import hashlib
import json
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# workspace-root directory -> location inside the wheel
TREES = {
    "content": "collimate_content",
    "builtins": "collimate_builtins",
    "module-snapshot": "collimate_module_snapshot",
}
FETCH = "python3 build/snapshot_store.py fetch"


def check_snapshot(snapshot: Path) -> None:
    """Every body ``snapshot/index.json`` names is under ``modules/`` with its sha256."""
    entries = json.loads((snapshot / "index.json").read_bytes())["entries"]
    for digest in sorted({entry["hash"] for entry in entries.values()}):
        body = snapshot / "modules" / digest
        if not body.is_file() or hashlib.sha256(body.read_bytes()).hexdigest() != digest:
            raise FileNotFoundError(f"module-snapshot/modules/{digest} is missing or differs from its "
                                    f"sha256 ({body}); run {FETCH}")


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if version == "editable":
            return  # a dev install reads the live trees from the checkout, never a copy
        root = Path(self.root)
        base = root if (root / "PKG-INFO").is_file() else root.parent
        for tree, wheel_path in TREES.items():
            source = base / tree
            if not source.is_dir():
                raise FileNotFoundError(f"bundled tree missing: {source}")
            build_data["force_include"][str(source)] = wheel_path if self.target_name == "wheel" else tree
        check_snapshot(base / "module-snapshot")
