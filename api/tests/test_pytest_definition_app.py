"""D04 app integration uses the actual package boundary and runner APIs."""

import run_staging
from pathlib import Path
from types import SimpleNamespace

import pytest

from models import VirtualFile


def vf(name, content=""):
    return VirtualFile(name=name, content=content)


def serve(monkeypatch, cell):
    """Make ``cell`` the only Capabilities cell of the channel staging resolves."""
    import state
    monkeypatch.setattr(state, "get_channel_by_id", lambda channel: SimpleNamespace())
    monkeypatch.setattr(run_staging, "capabilities_stack_cells", lambda channel: [cell])


def source():
    return SimpleNamespace(id="source", name="Source", files=[
        vf("group/odd/choice.generator", "id: echo\nname: Echo\nmain_config: settings.set_yaml\n"),
        vf("group/odd/settings.set_yaml", "fields: []\n"),
        vf("group/odd/main.py", "chosen"),
        vf("group/_nearby/helper.py", "wrong shared scope"),
        vf("_shared/helper.py", "top level"),
        vf("other/view.renderer", "id: other\n"),
        vf("other/hidden/deep.generator", "id: hidden\n"),
        vf("ambiguous/a.generator", "id: ambiguous\n"),
        vf("ambiguous/b.runner", "id: ambiguous-runner\n"),
        vf("root.generator", "id: root\n"),
        vf("_runs/history/leak.generator", "id: leak\n"),
    ])


def test_catalog_and_materialization_obey_package_boundaries__D04(monkeypatch):
    import runners
    cell = source()
    assert set(runners.generator_index([cell])) == {"echo"}
    serve(monkeypatch, cell)
    directory = Path(run_staging.find_generator_dir("echo", "channel"))
    try:
        assert (directory / "choice.generator").is_file()
        assert (directory / "main.py").read_text() == "chosen"
        assert (directory.parent / "_shared/helper.py").read_text() == "top level"
        assert not (directory.parent / "_nearby").exists()
        assert not (directory.parent / "other").exists()
        assert run_staging.resolve_generator_cell("hidden", "channel") is None
        assert run_staging.resolve_generator_cell("odd", "channel") is None
    finally:
        import shutil
        shutil.rmtree(directory.parent)


def test_runner_allocation_and_arbitrary_edit_path__D04():
    import runners
    definition = {"id": "alpha", "generator": "echo"}
    occupied = [vf("runner-alpha/source.generator", "id: echo\n")]
    created = runners.runner_vfiles(definition, [], occupied_files=occupied)
    assert created[0].name == "runner-alpha-2/alpha.runner"
    assert len(occupied) == 1
    edited = runners.runner_vfiles(definition, [{"name": "script.py", "content": "pass"}],
                                   occupied_files=occupied, definition_path="deep/place/arbitrary.runner")
    assert [f.name for f in edited] == ["deep/place/arbitrary.runner", "deep/place/files/script.py"]
    cell = SimpleNamespace(files=edited, builtin=False)
    channel = SimpleNamespace(main_stacks=[], nav_stacks=[SimpleNamespace(stack_type="capabilities", cells=[cell])])
    assert runners.find_runner(channel, "alpha")[1].name == "deep/place/arbitrary.runner"
    assert runners.runner_files(cell, "alpha")[0]["name"] == "script.py"
    assert runners.without_runners(edited + occupied) == occupied
    for path in ("bad.runner", "../bad/name.runner", "deep/place/name.generator"):
        with pytest.raises(runners.RunnerError):
            runners.runner_vfiles(definition, [], occupied_files=occupied, definition_path=path)


def test_shared_folder_name_cannot_overwrite_selected_package__D04(monkeypatch):
    cell = SimpleNamespace(id="collision", files=[
        vf("group/_shared/arbitrary.generator", "id: collision\n"),
        vf("group/_shared/main.py", "selected entrypoint"),
        vf("_shared/main.py", "shared helper"),
    ])
    serve(monkeypatch, cell)
    directory = Path(run_staging.find_generator_dir("collision", "channel"))
    try:
        assert (directory / "main.py").read_text() == "selected entrypoint"
        assert (directory.parent / "_shared/main.py").read_text() == "shared helper"
        assert directory.name != "_shared"
    finally:
        import shutil
        shutil.rmtree(directory.parent)


def test_materialization_has_only_recorded_cell_source_files__D04(monkeypatch):
    import shutil
    from runners import generator_source_files
    cell = source()
    serve(monkeypatch, cell)
    directory = Path(run_staging.find_generator_dir("echo", "channel"))
    try:
        actual = {p.relative_to(directory.parent).as_posix() for p in directory.parent.rglob("*") if p.is_file()}
        expected = {"odd/" + f.name.removeprefix("group/odd/") if f.name.startswith("group/odd/") else f.name
                    for f in generator_source_files(cell, "group/odd/")}
        assert actual == expected
    finally:
        shutil.rmtree(directory.parent)
