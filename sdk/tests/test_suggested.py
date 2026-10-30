# SPDX-License-Identifier: MIT
"""The suggested channel setups in ``suggested/``: the catalog reader, the
Standard suggestion against the shipped trees, the welcome tutorial's
content and the runner packages a suggestion offers as drafts."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import yaml

from collimate.definitions import discover_definitions
from collimate.lint import GeneratorRef, lint_runner_manifest_data
from collimate._host.packaging import (
    CellRef,
    read_asset_cells,
    read_suggested_cell,
    read_suggestions,
    read_tutorial_cell,
    scan_cells,
)

from tests._workspace import CONTENT, GENERATORS, SUGGESTED


_NAMESPACE = uuid.UUID("0bda06ef-1a24-4c63-a6a4-3c6b86e85418")
_SLIDES = SUGGESTED / "tutorial" / "welcome" / "welcome-tour.slides"


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _suggestion(**overrides) -> dict:
    data = {"name": "Mini", "description": "A small setup.",
            "generators": [["Tools", "generators/tools"]],
            "renderers": [["Views", "renderers/views"]],
            "tutorial": [["Hello", "suggested/tutorial/hello"]],
            "quick_fields": {}}
    data.update(overrides)
    return data


def _tree(root: Path, data: dict | str | None = None) -> None:
    _write(root / "generators" / "_lib" / "__init__.py", "LIB = 1\n")
    _write(root / "generators" / "tools" / "cell.yaml",
           yaml.safe_dump({"name": "Tools", "icon": "wrench", "order": 5, "shared": ["_lib"]}))
    _write(root / "generators" / "tools" / "tool" / "tool.generator", "id: tool\n")
    _write(root / "renderers" / "views" / "view" / "view.renderer", "id: view\n")
    _write(root / "suggested" / "tutorial" / "hello" / "hello.slides", "# Hello\n")
    _write(root / "suggested" / "tutorial" / "hello" / "notes" / "more.md", "more\n")
    body = data if isinstance(data, str) else json.dumps(_suggestion() if data is None else data)
    _write(root / "suggested" / "mini.json", body)


# ── the reader ─────────────────────────────────────────────────────────────


def test_reads_each_suggestion_by_file_stem(tmp_path):
    _tree(tmp_path)
    _write(tmp_path / "suggested" / "another.json", json.dumps(_suggestion(name="Another", tutorial=[])))
    found = read_suggestions(tmp_path)
    assert [s.id for s in found] == ["another", "mini"]
    mini = found[1]
    assert (mini.name, mini.description) == ("Mini", "A small setup.")
    assert mini.generators == [CellRef("Tools", "generators/tools")]
    assert mini.renderers == [CellRef("Views", "renderers/views")]
    assert mini.tutorial == [CellRef("Hello", "suggested/tutorial/hello")]
    assert mini.quick_fields == {}


def test_quick_fields_keep_their_runner_and_reference_order(tmp_path):
    quick = {"sample": [{"account": "model"}, {"setting": "mode"}, {"prompt": True}]}
    _tree(tmp_path, _suggestion(quick_fields=quick))
    assert read_suggestions(tmp_path)[0].quick_fields == quick


def test_a_tree_without_suggested_is_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_suggestions(tmp_path)


@pytest.mark.parametrize("data, message", [
    ("[1, 2]", "JSON object"),
    ("{not json", "mini.json"),
    (_suggestion(extra=1), "unknown \\['extra'\\]"),
    ({k: v for k, v in _suggestion().items() if k != "tutorial"}, "missing \\['tutorial'\\]"),
    ({k: v for k, v in _suggestion().items() if k != "quick_fields"}, "missing \\['quick_fields'\\]"),
    (_suggestion(name=3), "'name' must be a string"),
    (_suggestion(description=None), "'description' must be a string"),
    (_suggestion(name=" "), "must not be empty"),
    (_suggestion(generators={"Tools": "generators/tools"}), "must be a list"),
    (_suggestion(generators=[["Tools"]]), "not a \\[name, folder\\] pair"),
    (_suggestion(generators=[["Tools", 1]]), "not a \\[name, folder\\] pair"),
    (_suggestion(generators=[["", "generators/tools"]]), "empty name"),
    (_suggestion(generators=[["Tools", "generators/tools"], ["Tools", "generators/tools"]]), "twice"),
    (_suggestion(generators=[["Tools", "/generators/tools"]]), "relative path"),
    (_suggestion(generators=[["Tools", "generators/../generators/tools"]]), "relative path"),
    (_suggestion(generators=[["Tools", "generators\\tools"]]), "relative path"),
    (_suggestion(generators=[["Tools", "generators//tools"]]), "relative path"),
    (_suggestion(generators=[["Tools", "generators/missing"]]), "not a directory"),
    (_suggestion(tutorial=[["Hello", "suggested/mini.json"]]), "not a directory"),
    (_suggestion(quick_fields=[]), "must be an object"),
    (_suggestion(quick_fields={"": [{"prompt": True}]}), "runner ids must be nonempty"),
    (_suggestion(quick_fields={"sample": []}), "must be a nonempty list"),
    (_suggestion(quick_fields={"sample": [{"setting": " "}]}), "setting must name a nonempty"),
    (_suggestion(quick_fields={"sample": [{"prompt": False}]}), "prompt: true"),
    (_suggestion(quick_fields={"sample": [{"account": "model", "prompt": True}]}), "one-key"),
    (_suggestion(quick_fields={"sample": [{"account": "model"}, {"account": "model"}]}), "repeats"),
])
def test_a_malformed_suggestion_is_refused_naming_its_file(tmp_path, data, message):
    _tree(tmp_path, data)
    with pytest.raises(ValueError, match=message) as exc:
        read_suggestions(tmp_path)
    assert "suggested/mini.json" in str(exc.value)


@pytest.mark.parametrize("quick_json, duplicate", [
    ('{"sample": [{"account": "model", "account": "other"}]}', "account"),
    ('{"sample": [{"prompt": true}], "sample": [{"setting": "mode"}]}', "sample"),
])
def test_duplicate_json_keys_cannot_hide_quick_field_references(tmp_path, quick_json, duplicate):
    raw = json.dumps(_suggestion()).replace('"quick_fields": {}', f'"quick_fields": {quick_json}')
    _tree(tmp_path, raw)
    with pytest.raises(ValueError, match=f"duplicate JSON key '{duplicate}'"):
        read_suggestions(tmp_path)


def test_a_folder_that_links_out_of_the_tree_is_refused(tmp_path):
    root = tmp_path / "root"
    _tree(root, _suggestion(renderers=[["Views", "renderers/out"]]))
    (tmp_path / "outside").mkdir()
    (root / "renderers" / "out").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="leaves the tree"):
        read_suggestions(root)


def test_a_suggested_cell_reads_like_its_tree_with_its_pair_name(tmp_path):
    _tree(tmp_path)
    ref = read_suggestions(tmp_path)[0].generators[0]
    cell = read_suggested_cell(tmp_path, ref, "generator")
    [tree_cell] = read_asset_cells(tmp_path / "generators", "generator")
    assert cell == tree_cell
    # The cell.yaml travels with the cell; its shared: resolves at admission.
    assert [f.name for f in cell.files] == ["cell.yaml", "tool/tool.generator"]
    renamed = read_suggested_cell(tmp_path, CellRef("Kit", "generators/tools"), "generator")
    assert renamed.name == "Kit"
    assert renamed.id == str(uuid.uuid5(_NAMESPACE, "generator-cell-Kit"))
    assert (renamed.icon, renamed.order) == ("wrench", 5)


def test_a_suggested_cell_refuses_an_unknown_kind_an_escape_or_no_files(tmp_path):
    _tree(tmp_path)
    with pytest.raises(ValueError, match="unknown asset kind"):
        read_suggested_cell(tmp_path, CellRef("Tools", "generators/tools"), "tutorial")
    with pytest.raises(ValueError, match="relative path"):
        read_suggested_cell(tmp_path, CellRef("Tools", "../generators/tools"), "generator")
    (tmp_path / "renderers" / "empty").mkdir()
    with pytest.raises(ValueError, match="holds no files"):
        read_suggested_cell(tmp_path, CellRef("Empty", "renderers/empty"), "renderer")
    # Its cell.yaml alone is no content either.
    _write(tmp_path / "renderers" / "empty" / "cell.yaml", "name: Empty\n")
    with pytest.raises(ValueError, match="holds no files"):
        read_suggested_cell(tmp_path, CellRef("Empty", "renderers/empty"), "renderer")


def test_a_tutorial_cell_holds_its_folder_files_verbatim(tmp_path):
    _tree(tmp_path)
    _write(tmp_path / "suggested" / "tutorial" / "hello" / "__pycache__" / "x.pyc", "")
    cell = read_tutorial_cell(tmp_path, CellRef("Hello", "suggested/tutorial/hello"))
    assert cell.id == str(uuid.uuid5(_NAMESPACE, "tutorial-cell-Hello"))
    assert cell.name == "Hello"
    assert {f.name: f.content for f in cell.files} == {"hello.slides": "# Hello\n", "notes/more.md": "more\n"}
    (tmp_path / "suggested" / "tutorial" / "empty").mkdir()
    with pytest.raises(ValueError, match="holds no files"):
        read_tutorial_cell(tmp_path, CellRef("Empty", "suggested/tutorial/empty"))


# ── the shipped catalog ────────────────────────────────────────────────────


def test_every_shipped_suggestion_reads_with_its_cells():
    suggestions = read_suggestions(CONTENT)
    assert [s.id for s in suggestions] == ["standard"]
    for suggestion in suggestions:
        for kind, refs in (("generator", suggestion.generators), ("renderer", suggestion.renderers)):
            for ref in refs:
                assert read_suggested_cell(CONTENT, ref, kind).files
        for ref in suggestion.tutorial:
            assert read_tutorial_cell(CONTENT, ref).files


def test_standard_lists_every_shipped_cell_by_its_display_name():
    """Every cell of the shipped trees, in display order, under its
    cell.yaml name: a host's reset finds a seeded cell by that name, and a
    new top-level group must be listed here to reach a new channel."""
    standard = next(s for s in read_suggestions(CONTENT) if s.id == "standard")
    for kind, refs in (("generators", standard.generators), ("renderers", standard.renderers)):
        assert [(ref.name, ref.folder) for ref in refs] == [
            (cell.name, f"{kind}/{cell.folder}") for cell in scan_cells(CONTENT / kind)]
    assert standard.tutorial == [CellRef("Welcome", "suggested/tutorial/welcome")]
    assert standard.quick_fields == {
        "agent-annotation": [{"account": "model"}],
        "agent-chat": [{"account": "model"}],
        "git-sync": [{"prompt": True}, {"account": "git"}],
        "git-push": [{"prompt": True}, {"account": "git"}],
    }


def test_standard_cells_equal_the_tree_reader_cells():
    standard = next(s for s in read_suggestions(CONTENT) if s.id == "standard")
    for kind, refs in (("generator", standard.generators), ("renderer", standard.renderers)):
        by_id = {cell.id: cell for cell in read_asset_cells(CONTENT / f"{kind}s", kind)}
        for ref in refs:
            cell = read_suggested_cell(CONTENT, ref, kind)
            assert cell == by_id[cell.id]


def test_the_welcome_tutorial_is_the_one_slides_file():
    cell = read_tutorial_cell(CONTENT, CellRef("Welcome", "suggested/tutorial/welcome"))
    assert [f.name for f in cell.files] == ["welcome-tour.slides"]
    assert cell.files[0].content == _SLIDES.read_text(encoding="utf-8")


# ── the welcome tutorial's content ─────────────────────────────────────────


def test_tutorial_guide_uses_orientation_and_handler_catalog_D24():
    text = _SLIDES.read_text(encoding="utf-8")
    guide = text.split('## The Collimate helper\n', 1)[1].split('\n---', 1)[0]
    assert 'overview and philosophy' in guide
    assert 'accepted handler catalog' in guide
    assert 'saved runners' in guide and 'user' in guide
    assert 'files in\nthe current cell' not in guide


def test_tutorial_names_the_commands_pane_overview_and_journal_X01():
    text = _SLIDES.read_text(encoding="utf-8")
    for gone in ('runner picker', 'or a click does', 'inside a border', 'fork a cell'):
        assert gone not in text
    assert '**Commands pane**' in text
    assert 'runners panel' in text and 'double-click runs it' in text
    assert '## The overview' in text and '**Removed** shelf' in text
    assert '**journal**' in text


def test_tutorial_runs_from_the_panel_and_edits_runners_in_capabilities():
    """The runners panel only runs; runners and their accounts are made and
    edited in Capabilities' Manage face."""
    text = _SLIDES.read_text(encoding="utf-8")
    run = text.split('## Do a run\n', 1)[1].split('\n---', 1)[0]
    assert '**Shift+Enter**' in run and '**Capabilities**' in run and '**Manage**' in run
    for action in ('**Edit**', '**Duplicate**', '**Delete**', '**Add runner**'):
        assert action in run.split('**Capabilities**', 1)[1], action
    assert 'mode' not in run
    keys = text.split('## Keys & tips\n', 1)[1]
    assert 'runner editor, under **Capabilities › Manage**' in keys


def test_tutorial_names_the_one_agent_and_its_surface_setting_GN02():
    """One Agents slide names the Agent runners and their Surface setting
    (Annotation, Chat, CLI), interrupting a run, the selected file, and
    carrying work on through a `.llm_summary` the agent writes on request.
    Launch's slide never says 'Terminal' in its heading, so the two
    Terminals cannot be confused."""
    text = _SLIDES.read_text(encoding="utf-8")
    agents = text.split('## Agents\n', 1)[1].split('\n---', 1)[0]
    assert '**Agent**' in agents and '**Surface**' in agents
    for name in ('**Annotation**', '**Chat**', '**CLI**', '**interrupt**'):
        assert name in agents
    assert '**Start from the selected file**' in agents and 'selected file' in agents
    assert '`.llm_summary`' in agents and 'ask for a summary' in agents
    for gone in ('Agent Terminal', 'file mode', '## Work against a file', '**Todo**',
                 '**Document**', '**Con' + 'clude**', 'status-hand' + 'off',
                 '## Agents: Todo, Chat & Terminal', '## Chat & Agent Terminal',
                 '## Terminal & VS Code'):
        assert gone not in text
    launch = text.split('## Launch: shell & VS Code\n', 1)[1].split('\n---', 1)[0]
    assert '**Launch › Terminal** — a real shell' in launch


def test_tour_names_the_git_runners():
    """The Git runners ship in Standard, so the tour gives them a slide of
    their own after Launch's, whose lines stay as they are."""
    text = _SLIDES.read_text(encoding="utf-8")
    git = text.split('## Git: clone, update & push\n', 1)[1].split('\n---', 1)[0]
    assert '**Git › Clone / Update**' in git and '**Git › Push**' in git
    assert text.index('## Launch: shell & VS Code\n') < text.index('## Git: clone, update & push\n')
    launch = text.split('## Launch: shell & VS Code\n', 1)[1].split('\n---', 1)[0]
    assert '**Launch › Terminal** — a real shell' in launch


# ── the runner packages a suggestion offers ────────────────────────────────


def _packages(cell_dir: Path, kind: str):
    names = [p.relative_to(cell_dir).as_posix() for p in cell_dir.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    return [package for package in discover_definitions(names).packages if package.kind == kind]


def test_every_shipped_runner_package_validates_against_its_generator():
    runners = []
    for cell in scan_cells(GENERATORS):
        targets = {}
        for package in _packages(cell.path, "generator"):
            manifest = cell.path / package.definition_path
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            targets[data["id"]] = GeneratorRef.from_manifest(
                data, read_file=lambda rel, base=manifest.parent: (base / rel).read_text(encoding="utf-8"))
        for package in _packages(cell.path, "runner"):
            folder = cell.path / package.prefix
            data = yaml.safe_load((cell.path / package.definition_path).read_text(encoding="utf-8"))
            present = frozenset(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
            issues = lint_runner_manifest_data(data, files_present=present, resolve=targets.get,
                                               pseudo_path=package.definition_path)
            assert issues == [], (package.definition_path, issues)
            runners.append(f"{cell.folder}/{package.definition_path}")
    assert sorted(runners) == [
        "agent/agent-annotation/agent-annotation.runner",
        "agent/agent-chat/agent-chat.runner",
        "git/git-push/git-push.runner",
        "git/git-sync/git-sync.runner",
        "launch/launch-terminal/launch-terminal.runner",
        "web/web-default/web-default.runner",
    ]
