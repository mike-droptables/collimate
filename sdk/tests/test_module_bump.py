# SPDX-License-Identifier: MIT
"""``python -m collimate._host.module_mirror bump`` against a mocked npm registry.

A small workspace stands in for the shipped renderers; the registry fetcher,
the runtime preparation, the skill-reference sync and the refreeze are
replaced, so nothing here reaches the network, npm or node.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from collimate._host import module_bump, module_mirror
from collimate._host.module_bump import DEPS_MARK, RegistryError, bump, classify, find_pins, plan, satisfies


def _packument(latest: str, versions: list[str], declared: dict[str, dict[str, str]] | None = None) -> dict:
    declared = declared or {}
    return {"dist-tags": {"latest": latest},
            "versions": {v: {"version": v, "peerDependencies": declared.get(v, {})} for v in versions}}


REGISTRY = {
    # A lockstep family whose members do not all publish the newest release.
    "@milkdown/crepe": _packument("7.22.3", ["7.21.2", "7.22.2", "7.22.3"]),
    "@milkdown/kit": _packument("7.22.2", ["7.21.2", "7.22.2"]),
    "react": _packument("19.3.1", ["19.2.0", "19.2.9", "19.3.1", "20.0.0-rc.1"]),
    "react-dom": _packument("19.3.1", ["19.2.0", "19.2.9", "19.3.1", "20.0.0-rc.1"]),
    "mermaid": _packument("12.2.0", ["10.9.8", "10.9.9", "11.0.0", "12.2.0"],
                          {v: {"katex": "^0.16.9"} for v in ("10.9.8", "10.9.9")}),
    # The editor's fonts: its crepe's newest declares a newer katex line.
    "katex": _packument("0.19.1", ["0.16.9", "0.17.0", "0.17.3", "0.19.1"]),
    # A mixed family: the markdown package's newest declares the next 0.x line.
    "@assistant-ui/react": _packument("0.15.31", ["0.14.24", "0.14.29", "0.15.31"],
                                      {"0.15.31": {"react": "^18 || ^19"}}),
    "@assistant-ui/react-markdown": _packument("0.14.24", ["0.14.5", "0.14.7", "0.14.24"],
                                               {"0.14.5": {"@assistant-ui/react": "^0.14.18"},
                                                "0.14.7": {"@assistant-ui/react": "^0.14.18"},
                                                "0.14.24": {"@assistant-ui/react": "^0.15.0"}}),
    # Pinned only inside the editor's deps= lists (the deps fixture).
    "@milkdown/core": _packument("7.22.3", ["7.21.2", "7.22.2", "7.22.3"]),
    "prosemirror-model": _packument("1.25.12", ["1.25.11", "1.25.12"]),
}


def _fetch(name: str) -> dict:
    if name == "flaky-lib":
        raise ConnectionResetError("connection reset by peer")
    if name not in REGISTRY:
        raise RegistryError("not on the npm registry")
    return REGISTRY[name]


EDITOR = """import React from 'https://esm.sh/react@19.2.0?target=es2022';
import { Crepe } from 'https://esm.sh/@milkdown/crepe@7.21.2?target=es2022';
import { $prose } from 'https://esm.sh/@milkdown/kit@7.21.2/utils?target=es2022';
// pulled from the same @milkdown/kit@7.21.2 graph; @scoped/react@19.2.0 is another package
const THEME = 'https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.21.2/lib/theme';
const FONTS = 'https://cdn.jsdelivr.net/npm/katex@0.17.0/dist/fonts/';
"""
CHART = """import React from 'https://esm.sh/react@19.2.0?target=es2022';
import { createRoot } from 'https://esm.sh/react-dom@19.2.0/client?target=es2022';
import mermaid from 'https://esm.sh/mermaid@10.9.8?target=es2022';
import other from 'https://esm.sh/unknown-lib@1.0.0?target=es2022';
import flaky from 'https://esm.sh/flaky-lib@2.0.0?target=es2022';
import ranged from 'https://esm.sh/ranged-lib@^1.0.0?target=es2022';
"""
CHAT = """import { Thread } from 'https://esm.sh/@assistant-ui/react@0.14.24?target=es2022';
import { MarkdownTextPrimitive } from 'https://esm.sh/@assistant-ui/react-markdown@0.14.5?target=es2022';
"""
CHART_TEST = "import mermaid from 'https://esm.sh/mermaid@10.9.8?target=es2022';\n"
ASSETS = {"assets": [{"base": "https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.21.2/lib/theme/", "files": ["a.css"]}]}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    files = {
        "content/renderers/documents/editor/main.tsx": EDITOR,
        "content/renderers/documents/editor/mirror_assets.json": json.dumps(ASSETS, indent=1) + "\n",
        "content/renderers/visual/chart/main.tsx": CHART,
        "content/renderers/visual/chart/main.test.ts": CHART_TEST,
        "content/renderers/visual/chart/node_modules/mermaid/index.js": "export default 'https://esm.sh/mermaid@10.9.8';\n",
        "builtins/live/chat/main.tsx": CHAT,
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(module_bump, "_fetch_packument", _fetch)
    refrozen: list[Path] = []
    monkeypatch.setattr(module_bump, "_refreeze", lambda root, log, esm_builder=None: refrozen.append(root) or [])
    monkeypatch.setattr(module_bump, "_prepare_runtime", lambda root: pytest.fail("no runtime pin moved"))
    monkeypatch.setattr(module_bump, "_sync_skill_references", lambda root: pytest.fail("no doc changed"))
    return tmp_path, refrozen


def _rows(workspace: Path, **options) -> dict[str, module_bump.Package]:
    return {row.name: row for row in plan(find_pins(workspace), fetch=_fetch, **options)}


def test_classification_is_semver_with_the_caret_rule_for_0x():
    assert classify("1.2.3", "1.2.4") == "patch"
    assert classify("1.2.3", "1.3.0") == "minor"
    assert classify("1.2.3", "2.0.0") == "major"
    assert classify("0.14.24", "0.14.29") == "patch"
    assert classify("0.14.24", "0.15.0") == "major"
    assert classify("0.0.3", "0.0.4") == "major"
    assert classify("19.2.0", "19.3.1") == "minor"


def test_declared_ranges_are_read_like_npm():
    assert satisfies("0.14.29", "^0.14.18") and not satisfies("0.15.0", "^0.14.18")
    assert satisfies("19.3.1", "^18 || ^19") and not satisfies("20.0.0", "^18 || ^19")
    assert satisfies("1.2.9", "~1.2.3") and not satisfies("1.3.0", "~1.2.3")
    assert satisfies("19.3.1", ">=16.8.0") and not satisfies("16.7.9", ">=16.8.0")
    assert satisfies("2.5.0", "1.0.0 - 2.5.0") and not satisfies("2.5.1", "1.0.0 - 2.5.0")
    assert satisfies("1.4.0", "1.x") and not satisfies("2.0.0", "1.x")
    assert satisfies("0.0.3", "^0.0.3") and not satisfies("0.0.4", "^0.0.3")
    assert satisfies("3.0.0", "*") and satisfies("3.0.0", "latest") and satisfies("3.0.0", "npm:other@1")


def test_pins_come_from_shipped_sources_and_mirror_assets_only(workspace):
    root, _ = workspace
    pins = find_pins(root)
    assert pins["mermaid"] == {"10.9.8": ["content/renderers/visual/chart/main.tsx:3"]}  # not the test, not node_modules
    assert sorted(pins["@milkdown/crepe"]["7.21.2"]) == [
        "content/renderers/documents/editor/main.tsx:2", "content/renderers/documents/editor/main.tsx:5",
        "content/renderers/documents/editor/mirror_assets.json:4"]
    assert "ranged-lib" not in pins  # a range is not an exact pin
    assert "@scoped/react" not in pins and "react" in pins


def test_a_shared_version_family_moves_to_one_version_every_member_publishes(workspace):
    rows = _rows(workspace[0])
    assert (rows["@milkdown/crepe"].target, rows["@milkdown/kit"].target) == ("7.22.2", "7.22.2")
    assert {rows["@milkdown/crepe"].status, rows["@milkdown/kit"].status} == {"applied"}
    assert rows["@milkdown/crepe"].move_class == "minor"
    assert (rows["react"].target, rows["react-dom"].target) == ("19.3.1", "19.3.1")  # never the prerelease


def test_a_major_moves_only_with_the_flag(workspace):
    rows = _rows(workspace[0])
    assert (rows["mermaid"].target, rows["mermaid"].move_class) == ("10.9.9", "patch")
    assert "12.2.0 is a major" in rows["mermaid"].reason
    rows = _rows(workspace[0], allow_major=True)
    assert (rows["mermaid"].target, rows["mermaid"].move_class, rows["mermaid"].status) == ("12.2.0", "major", "applied")


def test_a_family_whose_members_disagree_on_ranges_moves_together_or_not_at_all(workspace):
    rows = _rows(workspace[0])
    for name in ("@assistant-ui/react", "@assistant-ui/react-markdown"):
        assert rows[name].status == "skipped", name
        assert "declares @assistant-ui/react ^0.15.0" in rows[name].reason
    rows = _rows(workspace[0], allow_major=True)
    assert rows["@assistant-ui/react"].target == "0.15.31" and rows["@assistant-ui/react"].move_class == "major"
    assert rows["@assistant-ui/react-markdown"].target == "0.14.24"
    assert {rows["@assistant-ui/react"].status, rows["@assistant-ui/react-markdown"].status} == {"applied"}


def test_a_declared_range_binds_only_within_one_module_graph(workspace):
    # The chart's mermaid declares katex ^0.16.9, but the editor's katex is
    # its own graph's: the chart does not hold it back.
    rows = _rows(workspace[0], allow_major=True)
    assert (rows["katex"].status, rows["katex"].target) == ("applied", "0.19.1")
    REGISTRY["@milkdown/crepe"]["versions"]["7.22.2"]["peerDependencies"] = {"katex": "^0.18.0"}
    try:
        # Without --major katex could reach only 0.17.3, outside the new crepe's
        # range: both stay, since the editor's graph holds them together.
        rows = _rows(workspace[0])
        assert rows["katex"].status == "skipped" and "declares katex ^0.18.0" in rows["katex"].reason
        assert rows["@milkdown/crepe"].status == rows["@milkdown/kit"].status == "skipped"
        assert "declares katex ^0.18.0; katex stays 0.17.0" in rows["@milkdown/crepe"].reason
    finally:
        REGISTRY["@milkdown/crepe"]["versions"]["7.22.2"]["peerDependencies"] = {}


def test_an_unknown_package_and_a_registry_failure_are_reported_and_skipped(workspace):
    rows = _rows(workspace[0])
    assert (rows["unknown-lib"].status, rows["unknown-lib"].reason) == ("skipped", "not on the npm registry")
    assert rows["flaky-lib"].status == "skipped" and "connection reset" in rows["flaky-lib"].reason
    assert rows["react"].status == "applied"  # the rest still moves


def test_only_limits_the_bump_to_the_named_packages_and_their_families(workspace):
    rows = _rows(workspace[0], only=["@milkdown/kit", "not-pinned"])
    assert set(rows) == {"@milkdown/crepe", "@milkdown/kit", "not-pinned"}
    assert rows["not-pinned"].status == "skipped" and rows["not-pinned"].reason == "not pinned in the shipped sources"


def test_applying_rewrites_every_occurrence_and_refreezes(workspace):
    root, refrozen = workspace
    before_test = (root / "content/renderers/visual/chart/main.test.ts").read_text()
    before_vendored = (root / "content/renderers/visual/chart/node_modules/mermaid/index.js").read_text()
    out: list[str] = []
    assert bump(root, log=out.append) == 0
    editor = (root / "content/renderers/documents/editor/main.tsx").read_text()
    assert "@milkdown/crepe@7.22.2?target" in editor and "@milkdown/kit@7.22.2/utils" in editor
    assert "same @milkdown/kit@7.22.2 graph" in editor and "npm/@milkdown/crepe@7.22.2/lib/theme'" in editor
    assert "7.21.2" not in editor
    assert "react@19.3.1?target" in editor and "@scoped/react@19.2.0" in editor  # another package's token
    assets = json.loads((root / "content/renderers/documents/editor/mirror_assets.json").read_text())
    assert assets["assets"][0]["base"] == "https://cdn.jsdelivr.net/npm/@milkdown/crepe@7.22.2/lib/theme/"
    chart = (root / "content/renderers/visual/chart/main.tsx").read_text()
    assert "react-dom@19.3.1/client" in chart and "mermaid@10.9.9?" in chart and "unknown-lib@1.0.0" in chart
    assert "@assistant-ui/react@0.14.24" in (root / "builtins/live/chat/main.tsx").read_text()
    assert (root / "content/renderers/visual/chart/main.test.ts").read_text() == before_test
    assert (root / "content/renderers/visual/chart/node_modules/mermaid/index.js").read_text() == before_vendored
    assert refrozen == [root]
    table = out[0]
    assert table.splitlines()[0].split() == ["package", "old", "new", "class", "latest", "result"]
    assert any(line.split()[:4] == ["mermaid", "10.9.8", "10.9.9", "patch"] and "applied" in line
               for line in table.splitlines())


def test_a_family_whose_new_roots_stay_unfrozen_is_reverted_and_said_so(workspace, monkeypatch):
    root, refrozen = workspace
    before = (root / "content/renderers/visual/chart/main.tsx").read_text()
    builders: list[str | None] = []
    monkeypatch.setattr(module_bump, "_refreeze",
                        lambda root, log, esm_builder=None: refrozen.append(root) or builders.append(esm_builder) or [])
    # Upstream cannot build the moved mermaid root; every other root froze.
    monkeypatch.setattr(module_bump, "_unfrozen_roots", lambda root: {
        "https://esm.sh/mermaid@10.9.9?target=es2022": "HTTP 500: no space left on device"})
    out: list[str] = []
    assert bump(root, esm_builder="http://127.0.0.1:18080", log=out.append) == 1
    chart = (root / "content/renderers/visual/chart/main.tsx").read_text()
    assert "mermaid@10.9.8?" in chart and "mermaid@10.9.9" not in chart
    # The other families stay moved, frozen again without the reverted one.
    assert "react-dom@19.3.1/client" in chart and chart != before
    assert "@milkdown/crepe@7.22.2?target" in (root / "content/renderers/documents/editor/main.tsx").read_text()
    assert refrozen == [root, root] and builders == ["http://127.0.0.1:18080"] * 2
    assert any(line.startswith("bump: mermaid reverted: 1 root(s) still unfrozen after the freeze, e.g. "
                               "https://esm.sh/mermaid@10.9.9?target=es2022: HTTP 500") for line in out)
    tables = [entry for entry in out if entry.startswith("package ")]
    assert len(tables) == 2  # the plan, then the outcome
    final = next(line for line in tables[-1].splitlines() if line.startswith("mermaid "))
    assert "reverted" in final
    assert out[-1].startswith("bump: reverted mermaid: their new roots stayed unfrozen")


def test_a_dry_run_prints_the_table_and_writes_nothing(workspace):
    root, refrozen = workspace
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    out: list[str] = []
    assert bump(root, dry_run=True, log=out.append) == 0
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert refrozen == []
    assert "would apply" in out[0] and "nothing written" in out[-1]


# -- the runtime's React -------------------------------------------------------------
# The keys a bump reads, and the types it leaves alone.
TOOLCHAIN = """{
  "react": "19.2.0",
  "react_dom": "19.2.0",
  "types_react": "19.2.0",
  "types_react_dom": "19.2.0"
}
"""
GUIDE = """import React from 'https://esm.sh/react@19.2.0?target=es2022';
import chart from 'https://esm.sh/mermaid@10.9.8?target=es2022';
import lib from 'https://esm.sh/some-lib@1.0.0?deps=react@19.2.0,react-dom@19.2.0&target=es2022';
"""


@pytest.fixture
def runtime_workspace(workspace, monkeypatch):
    """The workspace with the toolchain contract and a guide quoting pins;
    each runtime preparation records the React the contract held."""
    root, _ = workspace
    contract = root / "sdk/shared/contracts/toolchain.json"
    contract.parent.mkdir(parents=True)
    contract.write_text(TOOLCHAIN)
    guide = root / "sdk/shared/docs/how-to-write-a-renderer.md"
    guide.parent.mkdir(parents=True)
    guide.write_text(GUIDE)
    prepared: list[str] = []
    synced: list[Path] = []
    monkeypatch.setattr(module_bump, "_prepare_runtime",
                        lambda root: prepared.append(json.loads(contract.read_text())["react"]))
    monkeypatch.setattr(module_bump, "_sync_skill_references", synced.append)
    return root, prepared, synced


def test_the_runtime_react_is_read_from_the_toolchain_contract_and_docs_hold_no_pins(runtime_workspace):
    root, _, _ = runtime_workspace
    pins = find_pins(root)
    assert "sdk/shared/contracts/toolchain.json:2" in pins["react"]["19.2.0"]
    assert "sdk/shared/contracts/toolchain.json:3" in pins["react-dom"]["19.2.0"]
    assert "some-lib" not in pins
    assert not any(where.startswith("sdk/shared/docs/") for found in pins.values()
                   for spots in found.values() for where in spots)


def test_a_runtime_react_move_writes_the_contract_then_prepares_the_runtime_and_the_docs(runtime_workspace):
    root, prepared, synced = runtime_workspace
    out: list[str] = []
    assert bump(root, only=["react"], log=out.append) == 0
    # Only the runtime's React moved, in place: the types and the formatting stay.
    assert (root / "sdk/shared/contracts/toolchain.json").read_text() == TOOLCHAIN.replace(
        '"react": "19.2.0"', '"react": "19.3.1"').replace('"react_dom": "19.2.0"', '"react_dom": "19.3.1"')
    assert prepared == ["19.3.1"]
    assert "react-dom@19.3.1/client" in (root / "content/renderers/visual/chart/main.tsx").read_text()
    guide = (root / "sdk/shared/docs/how-to-write-a-renderer.md").read_text()
    assert guide == GUIDE.replace("react@19.2.0", "react@19.3.1").replace("react-dom@19.2.0", "react-dom@19.3.1")
    assert synced == [root]


def test_a_reverted_runtime_react_restores_the_contract_and_prepares_the_runtime_again(runtime_workspace,
                                                                                         monkeypatch):
    root, prepared, synced = runtime_workspace
    monkeypatch.setattr(module_bump, "_unfrozen_roots", lambda root: {
        "https://esm.sh/react-dom@19.3.1/client?target=es2022": "HTTP 500: no space left on device"})
    out: list[str] = []
    assert bump(root, only=["react"], log=out.append) == 1
    assert (root / "sdk/shared/contracts/toolchain.json").read_text() == TOOLCHAIN
    assert (root / "sdk/shared/docs/how-to-write-a-renderer.md").read_text() == GUIDE
    assert prepared == ["19.3.1", "19.2.0"]  # moved, then back to what renderer-hooks held
    assert synced == []
    assert out[-1].startswith("bump: reverted react: their new roots stayed unfrozen")


def test_a_runtime_that_cannot_be_prepared_stops_the_bump_and_says_how_to_finish(runtime_workspace, monkeypatch):
    import subprocess
    root, _, synced = runtime_workspace

    def fail(root):
        raise subprocess.CalledProcessError(1, ["npm", "ci"])

    monkeypatch.setattr(module_bump, "_prepare_runtime", fail)
    out: list[str] = []
    assert bump(root, only=["react"], log=out.append) == 1
    assert out[-1].startswith("bump: the renderer runtime was not prepared")
    for step in ("python3 sdk/scripts/sync_toolchain.py", "npm ci in sdk/packages/renderer-hooks",
                 "node sdk/scripts/build_renderer_runtime.mjs", "python -m collimate._host.module_mirror freeze"):
        assert step in out[-1], step
    assert synced == []


def test_the_cli_passes_its_options_to_bump(monkeypatch, tmp_path):
    seen = {}
    # bump edits a workspace checkout, which _workspace_root does not find under an installed wheel.
    monkeypatch.setattr(module_mirror, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(module_bump, "bump", lambda root, **options: seen.update(options, root=root) or 0)
    assert module_mirror.main(["bump", "--major", "--dry-run", "--only", "react", "mermaid"]) == 0
    assert seen == {"allow_major": True, "dry_run": True, "only": ["react", "mermaid"], "esm_builder": None,
                    "root": tmp_path}
    assert module_mirror.main(["bump", "--esm-builder", "http://127.0.0.1:18080"]) == 0
    assert seen["esm_builder"] == "http://127.0.0.1:18080"


# -- deps= lists -------------------------------------------------------------------
DEPS = "@milkdown/core@7.21.2,@milkdown/crepe@7.21.2,@milkdown/kit@7.21.2,prosemirror-model@1.25.11"
DEPS_EDITOR = f"""import {{ Crepe }} from 'https://esm.sh/@milkdown/crepe@7.21.2?deps={DEPS}&target=es2022';
import {{ $prose }} from 'https://esm.sh/@milkdown/kit@7.21.2/utils?deps={DEPS}&target=es2022';
import {{ createRoot }} from 'https://esm.sh/react-dom@19.2.0/client?deps=react@19.2.0&target=es2022';
"""


@pytest.fixture
def deps_workspace(tmp_path, monkeypatch):
    path = tmp_path / "content/renderers/documents/editor/main.tsx"
    path.parent.mkdir(parents=True)
    path.write_text(DEPS_EDITOR)
    monkeypatch.setattr(module_bump, "_fetch_packument", _fetch)
    monkeypatch.setattr(module_bump, "_refreeze", lambda root, log, esm_builder=None: [])
    monkeypatch.setattr(module_bump, "_prepare_runtime", lambda root: pytest.fail("no runtime pin moved"))
    monkeypatch.setattr(module_bump, "_sync_skill_references", lambda root: pytest.fail("no doc changed"))
    return tmp_path


def test_a_deps_list_entry_is_a_pin(deps_workspace):
    pins = find_pins(deps_workspace)
    where = "content/renderers/documents/editor/main.tsx"
    assert pins["@milkdown/core"] == {"7.21.2": [f"{where}:1{DEPS_MARK}", f"{where}:2{DEPS_MARK}"]}
    assert pins["@milkdown/crepe"] == {"7.21.2": [f"{where}:1", f"{where}:1{DEPS_MARK}", f"{where}:2{DEPS_MARK}"]}
    assert pins["prosemirror-model"] == {"1.25.11": [f"{where}:1{DEPS_MARK}", f"{where}:2{DEPS_MARK}"]}
    assert pins["react"] == {"19.2.0": [f"{where}:3{DEPS_MARK}"]}


def test_a_family_moves_its_deps_only_members_and_holds_a_lone_floor(deps_workspace):
    rows = _rows(deps_workspace)
    # @milkdown/core is pinned only in the lists, and moves with the family.
    assert {rows[name].target for name in ("@milkdown/crepe", "@milkdown/kit", "@milkdown/core")} == {"7.22.2"}
    assert rows["@milkdown/core"].status == "applied"
    # prosemirror-model has no URL of its own: a floor the list holds on purpose.
    assert rows["prosemirror-model"].status == "skipped"
    assert rows["prosemirror-model"].reason == "pinned only inside deps= lists; --only prosemirror-model moves it"
    rows = _rows(deps_workspace, only=["prosemirror-model"])
    assert (rows["prosemirror-model"].status, rows["prosemirror-model"].target) == ("applied", "1.25.12")


def test_applying_rewrites_the_versions_inside_deps_lists(deps_workspace):
    out: list[str] = []
    assert bump(deps_workspace, log=out.append) == 0
    text = (deps_workspace / "content/renderers/documents/editor/main.tsx").read_text()
    moved = "@milkdown/core@7.22.2,@milkdown/crepe@7.22.2,@milkdown/kit@7.22.2,prosemirror-model@1.25.11"
    assert text.count(f"?deps={moved}&target=es2022") == 2  # one list per file, still byte-identical
    assert "@milkdown/crepe@7.22.2?deps=" in text and "@milkdown/kit@7.22.2/utils?deps=" in text
    assert "react-dom@19.3.1/client?deps=react@19.3.1&target=es2022" in text
    assert "7.21.2" not in text and "19.2.0" not in text
    assert bump(deps_workspace, only=["prosemirror-model"], log=out.append) == 0
    text = (deps_workspace / "content/renderers/documents/editor/main.tsx").read_text()
    assert text.count("prosemirror-model@1.25.12&target=es2022") == 2 and "1.25.11" not in text
