"""No retired name survives on the SDK side of the repo.

The names, the files each scan reads and the places a name is still allowed
live in ``shared/retired-names.json`` (the retired theme tokens of plan 1006
R05, the archive surfaces of plan 1011 TR-14, the agent packages of GN-02, the
stacks the Capabilities stack replaced, the machinery the run's annotation
context replaced (AN-01), the dark override and reader zoom of RS-01, the
local-edit notice of Phase 4 P5, the authoring contract's rename break of
Phase 5, SDK-10, and the GitHub-only git surface the provider-neutral Git
account replaced (GP-01)); ``scripts/scan_retired_tokens.py`` reads it.
This runs every scan, and pins what each one covers: its files, each of its
names and its examples, and the live neighbours it spares.
"""
import importlib.util

import pytest

from tests._workspace import WORKSPACE

_SPEC = importlib.util.spec_from_file_location("scan_retired_tokens", WORKSPACE / "sdk" / "scripts" / "scan_retired_tokens.py")
scan_retired_tokens = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scan_retired_tokens)

DATA_FILE = "sdk/shared/retired-names.json"
SCANS = scan_retired_tokens.scans()


def _scanned(set_name):
    return {p.relative_to(WORKSPACE).as_posix() for p in scan_retired_tokens.files(set_name)}


@pytest.mark.parametrize("scan_id", sorted(SCANS))
def test_no_retired_name_survives(scan_id):
    hits = scan_retired_tokens.scan(scan_id)
    assert not hits, f"retired names ({scan_id}):\n" + "\n".join(hits)


@pytest.mark.parametrize("scan_id", sorted(SCANS))
def test_each_scan_hits_its_names_and_examples_and_spares_its_neighbours(scan_id):
    scan = SCANS[scan_id]
    expression = scan_retired_tokens.pattern(scan)
    names = scan.get("names", []) + scan.get("whole_names", [])
    assert names or scan.get("patterns"), scan_id
    for name in names:
        assert expression.search(f"x {name} y"), name
        if scan.get("ignore_case"):
            assert expression.search(f"x {name.upper()} y") and expression.search(f"x {name.lower()} y"), name
    if scan.get("patterns"):
        # Every pattern has an example of its own.
        for raw in scan["patterns"]:
            single = scan_retired_tokens.pattern({"patterns": [raw], "ignore_case": scan.get("ignore_case")})
            assert any(single.search(example) for example in scan["matches"]), raw
    for example in scan.get("matches", []):
        assert expression.search(example), example
    for kept in scan.get("spares", []):
        assert not expression.search(kept), kept


def test_the_source_scan_reads_renderer_tests_and_allows_its_one_name_only_where_it_says():
    scanned = _scanned("sources")
    for rel in ("content/renderers/visual/html/links.test.ts", "content/renderers/documents/monaco/main.tsx",
                "builtins/live/terminal/main.tsx", "sdk/packages/renderer-hooks/src/document-view.tsx",
                "sdk/scripts/scan_retired_tokens.py"):
        assert rel in scanned, rel
    assert DATA_FILE not in scanned
    scan = SCANS["R05"]
    [(name, rule)] = scan["allowed"].items()
    assert scan_retired_tokens.allowed(scan, name, "content/renderers/documents/monaco/main.tsx")
    assert scan_retired_tokens.allowed(scan, name, "sdk/shared/docs/how-to-write-a-renderer.md")
    assert not scan_retired_tokens.allowed(scan, name, "content/renderers/visual/mermaid/main.tsx")
    assert rule == {"under": ["content/renderers/documents/monaco/"], "ending": ["/how-to-write-a-renderer.md"]}


def test_the_repo_scan_covers_the_whole_sdk_side_but_scratch_and_third_party_bodies():
    scanned = _scanned("repo")
    for rel in ("sdk/README.md", "content/renderers/CREDITS.md", "sdk/.gitignore",
                "sdk/src/collimate/_host/packaging/__init__.py", "content/generators/agent/cell.yaml",
                "content/suggested/standard.json", "builtins/README.md",
                "sdk/shared/docs/api-reference.md", "sdk/tests/test_asset_cells.py",
                "sdk/scripts/check_native_wheels.py", "sdk/scripts/scan_retired_tokens.py",
                "sdk/tests/test_retired_token_scan.py", "sdk/packages/renderer-hooks/src/types.ts",
                "sdk/packages/renderer-hooks/src/document-view.tsx"):
        assert rel in scanned, rel
    assert DATA_FILE not in scanned
    assert not any(rel.startswith("sdk/tmp/") for rel in scanned)
    # The module snapshot's index and licenses are scanned; its third-party bodies are not.
    assert {"module-snapshot/index.json", "module-snapshot/LICENSES.json"} <= scanned
    assert not any(rel.startswith("module-snapshot/modules/") for rel in scanned)


# How many names and patterns each scan holds: a name leaves the data file
# only with this table.
SIZES = {"R05": (38, 1), "TR-14": (10, 0), "GN-02": (0, 6), "capabilities-stack": (8, 0),
         "AN-01": (54, 5), "RS-01": (10, 0), "P5-edit-snapshots": (2, 0), "SDK-10": (8, 13),
         "GP-01": (19, 0)}


def test_each_scan_holds_its_names():
    assert {scan_id: (len(scan.get("names", []) + scan.get("whole_names", [])), len(scan.get("patterns", [])))
            for scan_id, scan in SCANS.items()} == SIZES


def test_every_scan_reads_a_declared_file_set():
    data = scan_retired_tokens.load()
    assert {scan["files"] for scan in data["scans"]} <= set(data["file_sets"])
    assert len(SCANS) == len(data["scans"]), "two scans share an id"
