"""Imports into the Capabilities stack pass the same exact package admission
as Add to: held ids and types are reported, handlers keep ``derived/`` and
always lose their ``derived:`` line. Synthetic packages."""

import pytest
import yaml

from collimate._host.packaging.models import FileSpec
from models import Channel


def _accept(channel, cells):
    from database import get_db_connection
    from genesis import ensure_capabilities_stack
    from persistence import initialize_files
    from state import state
    ensure_capabilities_stack(channel)
    if not any(ch is channel for ch in state.channels):
        state.channels.append(channel)
    conn=get_db_connection(str(channel.id))
    try:
        for cell in cells:
            initialize_files(conn,str(cell.id),cell.files)
        conn.commit()
    finally:
        conn.close()


def generator(identifier="echo", folder="echo"):
    return [
        FileSpec(
            name=f"{folder}/choice.generator",
            language="yaml",
            content=yaml.safe_dump(
                {
                    "id": identifier,
                    "name": identifier,
                    "summary": "Test package",
                    "entrypoint": "main.py",
                    "main_config": "settings.set_yaml",
                }
            ),
        ),
        FileSpec(
            name=f"{folder}/main.py", language="python", content='print("test")\n'
        ),
        FileSpec(
            name=f"{folder}/settings.set_yaml", language="yaml", content="fields: []\n"
        ),
    ]


def _cell(files, name="Source"):
    from cas import store_bytes
    from models import Cell, VirtualFile
    return Cell(name=name, files=[
        VirtualFile(name=f.name, language=f.language, content="", blob_hash=store_bytes(f.data))
        if f.data is not None else VirtualFile(name=f.name, language=f.language, content=f.content)
        for f in files
    ])


def import_packages(channel, *cells_files):
    """Admit fresh source cells holding ``cells_files`` (one file list each)
    into the Capabilities stack and publish them."""
    from package_import import apply_packages, prepare_packages
    from provenance import ZIP
    return apply_packages(channel, prepare_packages(channel, [_cell(files) for files in cells_files], source=ZIP))


def _capability_cells(channel):
    from cell_placement import capabilities_stack_cells
    return capabilities_stack_cells(channel)


def test_import_skips_held_definition_id_even_with_fresh_cell_identity__D16(fresh_cwd):
    channel = Channel(name="Admission")
    _accept(channel,[])
    import_packages(channel, generator())
    result = import_packages(channel, generator(folder="another"))
    assert len(_capability_cells(channel)) == 1
    assert result["loaded_cells"] == 0
    assert result["skipped"][0]["id"] == "echo"


def test_invalid_import_refuses_before_any_source_cell_is_attached__D16_F0263_F0312(fresh_cwd):
    from fastapi import HTTPException

    channel = Channel(name="Admission")
    _accept(channel,[])
    files = generator()
    files[0].content = "id: broken\nname: Broken\nentrypoint: missing.py\n"
    with pytest.raises((ValueError, HTTPException)):
        import_packages(channel, files)
    assert _capability_cells(channel) == []


def test_import_admission_keeps_explicit_binary_and_derived_without_its_line__D16(fresh_cwd):
    import hashlib
    from cas import read_blob

    body = b"UTF8 binary\r\n"
    definition = {
        "id": "custom",
        "name": "Custom",
        "description": "Custom test",
        "types": [{"ext": ".example"}],
        "guidance": "Accept examples",
        "examples": [],
        "derived": "0" * 64,
    }
    files = [
        FileSpec(name="custom/custom.handler", language="yaml", content=yaml.safe_dump(definition)),
        FileSpec(name="custom/data.bin", language="binary", data=body,
                 blob_hash=hashlib.sha256(body).hexdigest()),
        FileSpec(name="custom/derived/verify.ts", language="typescript",
                 content="export default ()=>({valid:true})"),
    ]
    ch = Channel(name="Custom import")
    _accept(ch, [])
    result = import_packages(ch, files)
    assert result["loaded_cells"] == 1
    imported = {f.name: f for f in _capability_cells(ch)[0].files}
    assert read_blob(imported["custom/data.bin"].blob_hash) == body
    assert imported["custom/data.bin"].language == "binary" and not imported["custom/data.bin"].content
    # The build travels for review; the line naming it is never trusted.
    assert imported["custom/derived/verify.ts"].content == "export default ()=>({valid:true})"
    assert "derived" not in yaml.safe_load(imported["custom/custom.handler"].content)
    second = dict(definition, id="second")
    result = import_packages(ch, [FileSpec(name="second/second.handler", language="yaml",
                                           content=yaml.safe_dump(second))])
    assert result["loaded_cells"] == 0 and "clash" in result["skipped"][0]["reason"]


def test_import_drops_the_derived_line_of_every_handler_and_keeps_its_derived_folder__D16(fresh_cwd):
    """A handler whose ``derived:`` line names exactly its own ``derived/``
    folder is still only a build for review: the line goes, the folder
    travels."""
    from collimate.handlers import derived_hash
    derived = {"verify.ts": "export default () => ({valid: true});", "skills/memo/SKILL.md": "# Write a memo"}
    definition = {"id": "memo", "name": "Memo", "description": "Synthetic memos", "guidance": "A title line",
                  "types": [{"ext": ".memo"}], "examples": [],
                  "derived": derived_hash({path: body.encode() for path, body in derived.items()})}
    specs = [FileSpec(name="memo/memo.handler", language="yaml", content=yaml.safe_dump(definition)),
             *[FileSpec(name="memo/derived/" + path, language="text", content=body) for path, body in derived.items()]]
    ch = Channel(name="Self-declared")
    _accept(ch, [])
    assert import_packages(ch, specs)["loaded_cells"] == 1
    imported = {f.name: f for f in _capability_cells(ch)[0].files}
    assert {name for name in imported if name.startswith("memo/derived/")} == {"memo/derived/" + p for p in derived}
    assert "derived" not in yaml.safe_load(imported["memo/memo.handler"].content)


def test_runners_are_admitted_after_the_generators_of_the_same_import(fresh_cwd):
    runner = {"id": "synth-run", "name": "Synth run", "description": "", "tags": [], "generator": "echo",
              "prompt": "", "settings": {"settings.set_yaml": {}}, "accounts": {}, "folders": [],
              "permissions": {}, "packages": {}}
    ch = Channel(name="Ordering")
    _accept(ch, [])
    runner_cell = [FileSpec(name="synth-run/synth-run.runner", language="yaml", content=yaml.safe_dump(runner))]
    result = import_packages(ch, runner_cell, generator())
    assert result == {"loaded_cells": 2, "skipped": []}
    assert [sorted(f.name for f in c.files)[0] for c in _capability_cells(ch)] == [
        "synth-run/synth-run.runner", "echo/choice.generator"]


def test_import_rechecks_capabilities_after_compilation__D16(fresh_cwd, monkeypatch):
    import package_import
    from cell_placement import capabilities_stack
    from fastapi import HTTPException
    from models import Cell

    ch = Channel(name="Race")
    _accept(ch, [])
    original = package_import.admit_package

    def race(*args, **kwargs):
        admitted = original(*args, **kwargs)
        capabilities_stack(ch).cells.append(Cell(name="Concurrent"))
        return admitted

    monkeypatch.setattr(package_import, "admit_package", race)
    with pytest.raises(HTTPException) as error:
        import_packages(ch, generator())
    assert error.value.status_code == 409
    assert [c.name for c in _capability_cells(ch)] == ["Concurrent"]


def test_held_import_keeps_independent_text_and_binary_files__D16(fresh_cwd):
    import hashlib
    from cas import read_blob

    channel = Channel(name='Loose files')
    _accept(channel, [])
    import_packages(channel, generator())
    body = b'independent binary\r\n'
    files = generator(folder='held') + [
        FileSpec(name='notes.txt', language='text', content='Keep this text'),
        FileSpec(name='data.bin', language='binary', data=body,
                 blob_hash=hashlib.sha256(body).hexdigest()),
    ]
    result = import_packages(channel, files)
    assert result['loaded_cells'] == 1 and result['skipped'][0]['id'] == 'echo'
    copied = _capability_cells(channel)[-1].files
    assert [file.name for file in copied] == ['notes.txt', 'data.bin']
    assert copied[0].content == 'Keep this text'
    assert copied[1].language == 'binary' and read_blob(copied[1].blob_hash) == body
