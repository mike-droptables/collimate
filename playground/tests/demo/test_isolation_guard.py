"""Exercise the actual isolation guard in a small Git repository."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "playground/tests/check_isolation.sh"

# A line that each guard token must catch, case variants included.
TOKEN_SAMPLES = {
    "playground": ["playground"],
    "pyodide": ["pyodide"],
    "micropip": ["micropip"],
    "emscripten": ["emscripten"],
    r"quickjs\.wasm": ["quickjs.wasm"],
    r"verifier-worker\.js": ["verifier-worker.js"],
    "JSPI": ["JSPI"],
    "is_?wasm": ["useIsWasm", "IS_WASM"],
    "COLLIMATE_RUNTIME": ["COLLIMATE_RUNTIME"],
    "IDBFS": ["IDBFS"],
    "sw-gate": ["sw-gate"],
    r"boot\.js": ["boot.js"],
    "assemble_site": ["assemble_site"],
    "gh-pages": ["gh-pages"],
}
SAMPLES = [sample for samples in TOKEN_SAMPLES.values() for sample in samples]

# Main files that name wasm as the app's own technology (the browser
# compiler, binary file kinds, the module mirror, the pdf renderer's pdfium)
# and the lockfiles: no mention of the demo.
TECHNOLOGY_FILES = (
    "frontend/src/lib/rendererBundle.worker.ts",
    "frontend/src/lib/fileKinds.ts",
    "frontend/package-lock.json",
    "sdk/src/collimate/_host/module_mirror.py",
    "api/tests/test_pytest_module_mirror.py",
    "content/renderers/visual/pdf/main.tsx",
    "uv.lock",
)


def _guard(tmp_path, files):
    """The guard run in a fresh repository holding ``files`` ({path: text})."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    guard = tmp_path / "playground/tests/check_isolation.sh"
    guard.parent.mkdir(parents=True)
    # In the installed test this resolves to the canonical guard.
    shutil.copyfile(GUARD, guard)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return subprocess.run(["bash", str(guard)], text=True, capture_output=True, check=False)


def _guard_result(tmp_path, *, altered_owner=False, leaked_source=False, date="2026-10-05"):
    owner = f"docs/plan0928/owner-decisions-{date}.md"
    if not (ROOT / owner).is_file():
        # The public mirror excludes docs/ (build/push_public.py): the lines
        # these tests preserve are not in that tree, so there is nothing to check.
        pytest.skip(f"{owner} is not in this tree")
    text = (ROOT / owner).read_text()
    if altered_owner:
        text += "\nNew playground integration outside its folder.\n"
    files = {owner: text}
    if leaked_source:
        files["api/new_feature.py"] = "playground_enabled = True\n"
    return _guard(tmp_path, files)


def test_exact_owner_provenance_is_preserved_P4R03(tmp_path):
    result = _guard_result(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_added_owner_runtime_reference_is_refused_P4R03(tmp_path):
    result = _guard_result(tmp_path, altered_owner=True)
    assert result.returncode == 1
    assert "New playground integration" in result.stdout


def test_product_runtime_reference_remains_refused_P4R03(tmp_path):
    result = _guard_result(tmp_path, leaked_source=True)
    assert result.returncode == 1
    assert "api/new_feature.py" in result.stdout


def test_exact_hosted_deferral_owner_provenance_is_preserved_P5F04(tmp_path):
    result = _guard_result(tmp_path, date="2026-10-06")
    assert result.returncode == 0, result.stdout + result.stderr


def test_added_hosted_deferral_runtime_reference_is_refused_P5F04(tmp_path):
    result = _guard_result(tmp_path, date="2026-10-06", altered_owner=True)
    assert result.returncode == 1
    assert "New playground integration" in result.stdout


def test_product_runtime_reference_after_deferral_remains_refused_P5F04(tmp_path):
    result = _guard_result(tmp_path, date="2026-10-06", leaked_source=True)
    assert result.returncode == 1
    assert "api/new_feature.py" in result.stdout


def test_every_guard_token_has_a_planted_sample_P9():
    (line,) = [line for line in GUARD.read_text().splitlines() if line.startswith("TOKENS='")]
    assert set(line[len("TOKENS='"):-1].split("|")) == set(TOKEN_SAMPLES)


def test_the_clean_tree_passes_P9():
    result = subprocess.run(["bash", str(GUARD)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_technology_is_no_mention_of_the_demo_P9(tmp_path):
    result = _guard(tmp_path, {name: (ROOT / name).read_text() for name in TECHNOLOGY_FILES})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("main", ["frontend/src/lib/rendererBundle.worker.ts", "api/verification.py"])
def test_a_planted_token_in_a_main_file_is_a_leak_P9(tmp_path, main, sample):
    comment = "//" if main.endswith(".ts") else "#"
    result = _guard(tmp_path, {main: (ROOT / main).read_text() + f"{comment} {sample}\n"})
    assert result.returncode == 1
    (leak,) = result.stdout.splitlines()
    assert leak.startswith(f"LEAK: {main}:") and leak.endswith(f"{comment} {sample}")


def test_a_demo_token_beside_an_allowed_third_party_use_is_a_leak_P9(tmp_path):
    name, line = "uv.lock", 'name = "pyodide-build"'
    allowed = "    { name = \"uvicorn\", marker = \"sys_platform != 'emscripten'\" },\n"
    result = _guard(tmp_path, {name: allowed + line + "\n"})
    assert result.returncode == 1
    assert result.stdout.splitlines() == [f"LEAK: {name}:2:{line}"]
