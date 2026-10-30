"""Definition packages govern reset identity and runner drafts."""


import pytest

from models import Cell, Channel, Stack, VirtualFile


@pytest.mark.parametrize("kind", ["generator", "renderer"])
def test_reset_counts_only_visible_package_definition_ids__D04(kind):
    from package_import import declared_definition_ids

    files = [
        VirtualFile(name=f"unrelated-folder/arbitrary.{kind}", content="id: visible\n"),
        VirtualFile(name=f"ordinary/deep/another.{kind}", content="id: deep\n"),
        VirtualFile(name=f"at-root.{kind}", content="id: root\n"),
        VirtualFile(name="handler/barrier.handler", content="id: handler\n"),
        VirtualFile(name=f"handler/examples/hidden/example.{kind}", content="id: example\n"),
        VirtualFile(name="ambiguous/a.runner", content="id: runner\n"),
        VirtualFile(name=f"ambiguous/b.{kind}", content="id: ambiguous\n"),
        VirtualFile(name=f"_runs/run/hidden/hidden.{kind}", content="id: run\n"),
    ]
    assert declared_definition_ids(files) == {(kind, "visible"), (kind, "deep"), ("handler", "handler")}


def test_reset_runner_stripping_keeps_binary_and_package_content__D04(fresh_cwd):
    from cas import hash_and_store_bytes
    from cell_commits import blob_bytes
    from runners import without_runners

    binary = b"\x00\xff original asset"
    digest = hash_and_store_bytes(binary)
    files = [
        VirtualFile(name="gen/package.generator", content="id: source\n"),
        VirtualFile(name="gen/examples/embedded.runner", content="id: content\n"),
        VirtualFile(name="gen/asset.bin", language="binary", blob_hash=digest),
        VirtualFile(name="_shared/common.py", content="VALUE = 1\n"),
        VirtualFile(name="arbitrary/deep/saved.runner", content="id: remove\n"),
        VirtualFile(name="arbitrary/deep/files/asset.bin", language="binary", blob_hash=digest),
    ]
    kept = without_runners(files)
    assert kept == files[:4]
    assert kept[2].blob_hash == digest and blob_bytes(digest) == binary


@pytest.mark.parametrize("duplicate", [False, True])
def test_new_channel_runner_packages_avoid_current_and_prepared_files__D04(
    fresh_cwd, monkeypatch, duplicate,
):
    import state
    import runners
    from collimate.definitions import discover_definitions
    from suggested import _with_drafts

    source = Cell(name="Provider", files=[
        VirtualFile(name="provider/deep/unrelated.generator", content=(
            "id: echo\nname: Echo\nentrypoint: main.py\nmain_config: settings.set_yaml\n"
        )),
        VirtualFile(name="provider/deep/settings.set_yaml", content="fields: []\n"),
        VirtualFile(name="provider/deep/main.py", content="print('ok')\n"),
        VirtualFile(name="runner-alpha/preserve.bin", content="preserved original"),
    ])
    channel = Channel(name="Seed packages", nav_stacks=[
        Stack(name="Capabilities", stack_type="capabilities", cells=[source]),
    ])
    monkeypatch.setattr(state.state, "channels", [channel])
    monkeypatch.setattr(state.state, "active_channel_id", channel.id)
    before = source.model_dump(mode="json")

    def draft(identity):
        return {"runner": {
            "id": identity, "generator": "echo", "name": identity,
            "description": "", "tags": [], "prompt": "",
            "settings": {"settings.set_yaml": {}}, "accounts": {},
            "folders": [], "permissions": {}, "packages": {},
        }, "files": []}

    drafts = [draft("alpha"), draft("alpha" if duplicate else "alpha-2")]
    if duplicate:
        with pytest.raises(ValueError, match="unique"):
            _with_drafts([source], drafts)
        assert source.model_dump(mode="json") == before
        return

    _with_drafts([source], drafts)
    assert len({file.name for file in source.files}) == len(source.files)
    by_name = {file.name: file.content for file in source.files}
    assert by_name["runner-alpha/preserve.bin"] == "preserved original"
    assert by_name["provider/deep/unrelated.generator"].startswith("id: echo\n")
    packages = [p for p in discover_definitions(by_name).packages if p.kind == "runner"]
    assert {p.prefix for p in packages} == {"runner-alpha-2", "runner-alpha-2-2"}
    assert {row["id"] for row in runners.build_catalog(channel)} == {"alpha", "alpha-2"}
