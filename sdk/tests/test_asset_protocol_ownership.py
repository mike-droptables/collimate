# SPDX-License-Identifier: MIT
"""SDK-owned shared assets and renderer ready opt-ins (formerly APP scripts)."""

from pathlib import Path
import re

import pytest

from collimate.shared import shared_path
from tests.conftest import RENDERER_ROOTS
from tests._workspace import RENDERERS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shared_asset():
    root = shared_path()
    assert root.is_dir()
    assets = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and len(path.relative_to(root).parts) >= 2
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]
    assert assets, "the shipped shared tree must contain a nested asset"
    path = assets[0]
    return root, path.relative_to(root).parts, path.read_bytes()[:32]


def test_shared_asset_resolution_and_bytes__F1898_F1905(shared_asset):
    _root, segments, head = shared_asset
    resolved = shared_path(*segments)
    assert isinstance(resolved, Path)
    assert resolved.is_file() and resolved.stat().st_size > 0
    assert resolved.read_bytes()[: len(head)] == head


def test_shared_missing_asset_reports_both_resolution_paths__F1898_F1905():
    missing = "f1898-f1905-missing-shared-asset.txt"
    with pytest.raises(FileNotFoundError) as caught:
        shared_path("docs", missing)
    message = str(caught.value)
    assert missing in message
    assert "installed" in message.lower() and "in-tree" in message.lower()


def test_shared_segments_compose_under_the_resolved_root__F1898_F1905(shared_asset):
    root, segments, _head = shared_asset
    assert shared_path(*segments).resolve() == root.joinpath(*segments).resolve()


def test_every_deferred_renderer_explicitly_signals_ready__F1898_F1905():
    deferred = [
        path
        for root in RENDERER_ROOTS
        for path in root.rglob("main.tsx")
        if re.search(r"deferReady\s*:\s*true", path.read_text())
    ]
    assert deferred, "canonical deferred renderer discovery must execute"
    for path in deferred:
        assert re.search(r"\bapi\.ready\(\)", path.read_text()), str(path)


@pytest.mark.parametrize(
    "renderer", ["documents/milkdown", "documents/monaco", "visual/excalidraw"]
)
def test_heavy_renderer_retains_its_deferred_setup_contract__F1898_F1905(renderer):
    source = (RENDERERS / renderer / "main.tsx").read_text()
    assert re.search(r"deferReady\s*:\s*true", source)
    assert "api.ready()" in source


def test_deferred_editor_theme_and_css_precede_ready__F1898_F1905():
    # Keep the existing SDK-specific setup order checks at their canonical owner.
    source = (RENDERERS / "documents/milkdown/main.tsx").read_text()
    injected = re.search(r"api\.injectCss\(await crepeThemeCss\(\)\)", source)
    assert injected and injected.start() < source.find("api.ready()")
    assert re.search(r"await\s+\w+\.create\(", source)
    source = (RENDERERS / "visual/excalidraw/main.tsx").read_text()
    assert ".then(" in source and "api.ready" in source
