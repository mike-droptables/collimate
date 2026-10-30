"""Renderer-contract facts no host run can show.

The renderer hosts themselves (NativeRenderer, the pooled SandboxIframe and
the fullscreen StandaloneSandboxIframe) are tested by running a probe renderer
on each of them: frontend/src/components/__browser__/
renderer-host-conformance.browser.test.tsx, whose member table the compiler
checks against the SDK's RendererAPI. What stays here has no host to run:

  * the milkdown renderer's Insert reads only universal members
    (`getFilePaths`, `getFileUrl`, `revealFile`) and names no writer, since an
    iframe host stores no uploaded bytes (a rule about that renderer's source);
  * the files pane's drag type is the SDK's `CELL_FILES_MIME`;
  * the runtime the app serves is the SDK's own built runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _sdk_source(rel: str) -> Path:
    """A file of this repo's ``sdk/`` source tree; a wheel install ships no
    ``packages/`` tree, so the guard skips there instead of lying."""
    path = REPO_ROOT / "sdk" / rel
    if path.is_file():
        return path
    pytest.skip(f"no SDK source file {rel} (wheel install)")


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"{rel} not found — did the file move?"
    return path.read_text(encoding="utf-8")


MILKDOWN_MAIN = REPO_ROOT / "content/renderers/documents/milkdown/main.tsx"


def test_markdown_insert_uses_only_universal_members():
    """The milkdown renderer runs in the iframe host, so its Insert reads
    only members every host has: it lists `getFilePaths()` and writes paths
    that `getFileUrl` displays and `revealFile` follows. It takes no uploads:
    the iframe api stages no bytes (`stageDraftFile` is native only) and its
    sibling write is text, so the renderer never names either writer, and a
    `blob:` URL (`createObjectURL`), which dies with the frame, never reaches
    the markdown."""
    source = MILKDOWN_MAIN.read_text(encoding="utf-8")
    for member in ("api.getFilePaths()", "api.getFileUrl(", "api.revealFile("):
        assert member in source, f"the milkdown renderer no longer reads {member}"
    for name in ("stageDraftFile", "saveSiblingFile", "createObjectURL"):
        assert name not in source, f"the milkdown renderer names {name}: an upload path is back"


def test_cell_file_drag_type_matches_the_sdk_RS03():
    """The files pane's drag type (frontend lib/fileDrag.ts) is the SDK's
    CELL_FILES_MIME, and the pane's drag start puts the JSON {cellId, paths}
    on it, which the SDK's readCellFileDrop parses at drop time."""
    app_mime = re.search(r"COLLIMATE_DND_MIME = '([^']+)'", _read("frontend/src/lib/fileDrag.ts"))
    assert app_mime, "lib/fileDrag.ts no longer declares COLLIMATE_DND_MIME"
    sdk_index = _sdk_source("packages/renderer-hooks/src/index.tsx").read_text(encoding="utf-8")
    sdk_mime = re.search(r"CELL_FILES_MIME\s*=\s*['\"]([^'\"]+)['\"]", sdk_index)
    assert sdk_mime, "the SDK no longer declares CELL_FILES_MIME"
    assert app_mime.group(1) == sdk_mime.group(1) == "application/x-collimate-files"
    start = _read("frontend/src/hooks/useFileTransfer.ts")
    assert "setData(COLLIMATE_DND_MIME, JSON.stringify({ cellId, paths:" in start


def _exports(module: str) -> set[str]:
    """The names a bundled ES module's final ``export { … };`` lists."""
    block = module.rsplit("export {", 1)[1].split("};", 1)[0]
    return {name.strip() for name in block.split(",") if name.strip()}


def test_served_runtime_is_the_sdk_theme_contract_R06():
    """The app serves the installed SDK's built renderer runtime as
    /api/sdk/react.js (routes/blobs.py ``_sdk_runtime``): the SDK's own
    built bytes, exporting every theme member a renderer imports."""
    from collimate.shared import shared_path
    served = shared_path("renderer-runtime", "react.js")
    sdk_runtime = _sdk_source("shared/renderer-runtime/react.js")
    assert served.read_bytes() == sdk_runtime.read_bytes(), (
        "the installed SDK's renderer runtime differs from the SDK source's built runtime"
    )
    names = _exports(served.read_text(encoding="utf-8"))
    assert {"useTheme", "tokens", "THEME_VAR_NAMES", "themeCss", "resolveVar", "toHex8"} <= names
    # The typed specifier renderers compile against declares the same members.
    platform = _sdk_source("packages/renderer-hooks/platform.d.ts").read_text(encoding="utf-8")
    for name in ("useTheme", "tokens", "THEME_VAR_NAMES", "themeCss", "resolveVar", "toHex8"):
        assert re.search(rf"\b{name}\b", platform), name
