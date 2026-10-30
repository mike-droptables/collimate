# SPDX-License-Identifier: MIT
"""Accepted handler and Guide references equal ``shared/docs`` byte for byte,
and shipped accepted hashes track those generated files. After editing a doc, run
``python scripts/sync_skill_references.py``."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_skill_references.py"


def _sync():
    spec = importlib.util.spec_from_file_location("sync_skill_references", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_skill_reference_mirror_equals_shared_docs():
    sync = _sync()
    differs = [str(p.relative_to(sync.WORKSPACE)) for p in sync.differences()]
    assert not differs, (
        "skill reference mirrors differ from shared/docs — run "
        f"`python scripts/sync_skill_references.py`: {differs}")




def test_sync_removes_a_stray_subfolder_from_a_mirror(tmp_path, monkeypatch):
    sync = _sync()
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in {"collimate-overview.md", "why-collimate.md", *(name for names in sync.HANDLER_DOCS.values() for name in names)}:
        (docs / name).write_text(name)
    monkeypatch.setattr(sync, "DOCS", docs)
    monkeypatch.setattr(sync, "AGENT", tmp_path / "agent")
    monkeypatch.setattr(sync, "HANDLERS", tmp_path / "handlers")
    stray = next(iter(sync.expected())).parent / "images"
    stray.mkdir(parents=True)
    (stray / "a.png").write_bytes(b"x")
    sync.sync()
    assert not stray.exists()
    assert sync.differences() == []


def test_guide_reference_mirror_contains_the_two_user_guides__D02():
    sync = _sync()
    want = {p: data for p, data in sync.expected().items() if p.is_relative_to(sync.AGENT)}
    assert len({p.parent for p in want}) == 1
    assert {p.name for p in want} == {"collimate-overview.md", "why-collimate.md"}


def test_renderer_derived_references_equal_canonical_docs__D12_P4F11():
    sync = _sync()
    want = {p: data for p, data in sync.expected().items() if p.is_relative_to(sync.HANDLERS / 'renderer')}
    assert {p.name for p in want} == {"how-to-write-a-renderer.md", "api-reference.md", "how-to-write-a-verifier.md", "interface.md"}
    assert all(p.read_bytes() == data for p, data in want.items())


def test_handler_references_and_accepted_hashes_sync_together__D12():
    sync = _sync()
    want = sync.expected()
    assert set(sync.HANDLER_DOCS) == {'generator', 'renderer', 'runner', 'handler', 'options'}
    assert all(path.read_bytes() == data for path, data in want.items())
    assert all(path.read_bytes() == data for path, data in sync._accepted_manifests(want).items())


def test_canonical_doc_edit_updates_skill_and_accepted_hash_in_one_sync__D12(tmp_path,monkeypatch):
    import shutil
    import yaml
    from collimate.handlers import derived_hash
    sync=_sync()
    docs=tmp_path/'docs'
    shutil.copytree(sync.DOCS,docs)
    handler=tmp_path/'handlers'/'renderer'
    shutil.copytree(sync.HANDLERS/'renderer',handler)
    monkeypatch.setattr(sync,'DOCS',docs)
    monkeypatch.setattr(sync,'HANDLERS',tmp_path/'handlers')
    monkeypatch.setattr(sync,'AGENT',tmp_path/'agent')
    monkeypatch.setattr(sync,'PRESETS',{})
    monkeypatch.setattr(sync,'HANDLER_DOCS',{'renderer':('how-to-write-a-renderer.md','api-reference.md','how-to-write-a-verifier.md')},raising=False)
    def actual_hash():
        return derived_hash({p.relative_to(handler/'derived').as_posix():p.read_bytes()
                             for p in (handler/'derived').rglob('*') if p.is_file()})
    manifest=handler/'renderer.handler'
    original=yaml.safe_load(manifest.read_text())['derived']
    assert original==actual_hash()
    guide=docs/'how-to-write-a-renderer.md'
    guide.write_bytes(guide.read_bytes()+b'\nThe SDK renderer runtime exports prebuilt bytes without a runtime compiler.\n')
    assert sync.differences()
    sync.sync()
    assert (handler/'derived/skills/renderer-authoring/references/how-to-write-a-renderer.md').read_bytes()==guide.read_bytes()
    updated=yaml.safe_load(manifest.read_text())['derived']
    assert updated!=original
    assert updated==actual_hash()
    assert sync.differences()==[]


def test_generator_guide_describes_partial_handler_refusal__D10_D12():
    guide = (_SCRIPT.parents[1] / "shared/docs/how-to-write-a-generator.md").read_text()
    section = guide.split("## Your run folder and locked files", 1)[1].split("UI files are created", 1)[0]
    assert "host accepts every such write" not in section
    assert "only writes the host refuses" not in section.lower()
    for contract in ("accepted handler", "handler_invalid", "PermissionError", "accepted siblings"):
        assert contract in section


def test_user_validation_contract_is_advisory_and_current_only__D10():
    guide = (_SCRIPT.parents[1] / 'shared/docs/how-to-write-a-generator.md').read_text()
    api = (_SCRIPT.parents[1] / 'shared/docs/api-reference.md').read_text()
    assert 'saves always succeed independently of handler validation' in guide
    assert 'coalesced draft saves' in guide
    assert 'source_revision' in api and 'pending' in api
    assert 'no stale verdicts' in api and 'revalidated after restart' in api
