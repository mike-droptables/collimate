"""The build's anchored patches must still find their text in main code.

The playground overlays the canonical app, so a restyle that moves an
anchor would otherwise surface only when the full build runs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _patches(monkeypatch):
    path = ROOT / 'playground/build/patches.py'
    spec = importlib.util.spec_from_file_location('anchor_check_patches', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_every_patch_anchor_occurs_once_in_main_code_S07(monkeypatch, tmp_path):
    """Each anchor occurs exactly once in its canonical file, and applying
    the whole list to a copy succeeds."""
    patches = _patches(monkeypatch)
    for patch in patches.PATCHES:
        text = (ROOT / patch.path).read_text(encoding='utf-8')
        assert text.count(patch.anchor) == 1, (patch.path, patch.why)
        dest = tmp_path / patch.path
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding='utf-8')
        patches.apply(tmp_path, patch)


@pytest.mark.parametrize('gone', ['New channel…', 'Rename current…', 'Delete current…'])
def test_top_bar_patch_removes_the_channel_operations_S07(monkeypatch, tmp_path, gone):
    patches = _patches(monkeypatch)
    rel = 'frontend/src/components/AppTopBar.tsx'
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True)
    dest.write_text((ROOT / rel).read_text(encoding='utf-8'), encoding='utf-8')
    for patch in patches.PATCHES:
        if patch.path == rel:
            patches.apply(tmp_path, patch)
    out = dest.read_text(encoding='utf-8')
    assert gone not in out
    # The channel list stays: its heading and one radio row per channel.
    assert "label: 'Channels'" in out
    assert "kind: 'radio'" in out
