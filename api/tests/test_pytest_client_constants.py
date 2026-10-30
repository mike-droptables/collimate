"""The client's copies of the api's constants are generated, byte for byte
(``frontend/scripts/sync_app_constants.py``): the tag limits in
``frontend/src/lib/tags.ts`` and the run-folder root in
``frontend/src/lib/runningCell.ts``."""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "frontend" / "scripts" / "sync_app_constants.py"


def _sync():
    spec = importlib.util.spec_from_file_location("sync_app_constants", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_tags_limits_and_runs_prefix_are_fresh():
    checked = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_the_generator_reads_the_tags_limits_and_runs_prefix_the_api_runs_with():
    sync = _sync()
    emitted = set()
    for source, names in sync.TARGETS.values():
        module = importlib.import_module(Path(source).stem)
        assert sync.constants(source, names) == {name: getattr(module, name) for name in names}
        emitted.update(names)
    assert emitted == {"TAG_MAX_LENGTH", "TAGS_PER_CELL_MAX", "RUNS_PREFIX"}
