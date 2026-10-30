# SPDX-License-Identifier: MIT
"""mirrorUrl, the served runtime's map from a module-CDN URL to the app's
module mirror beside it (``packages/renderer-hooks/src/mirror.ts``).

The runtime is served at ``<api>/sdk/react.js`` and the mirror at
``<api>/mirror/<host>/…``, so the mirror is found from the runtime's own
``import.meta.url``. The module is bundled with that URL defined, the way a
host serves it, and called under Node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from collimate.shared import shared_path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packages/renderer-hooks/src/mirror.ts"
SCRIPT = r"""
import { build } from 'esbuild';
import { readFileSync } from 'node:fs';
const { source, runtime, urls } = JSON.parse(readFileSync(0, 'utf8'));
const out = await build({ entryPoints: [source], bundle: true, write: false, format: 'esm',
  platform: 'neutral', logLevel: 'silent', define: { 'import.meta.url': JSON.stringify(runtime) } });
const mod = await import('data:text/javascript;base64,' + Buffer.from(out.outputFiles[0].text).toString('base64'));
process.stdout.write(JSON.stringify(urls.map(url => mod.mirrorUrl(url))));
"""

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (ROOT / "scripts/node_modules/esbuild").is_dir(),
    reason="needs node and scripts/node_modules (npm ci --prefix scripts)")


def mirrored(runtime: str, urls: list[str]) -> list[str]:
    run = subprocess.run(["node", "--input-type=module", "-e", SCRIPT], cwd=str(ROOT / "scripts"),
                         input=json.dumps({"source": str(SOURCE), "runtime": runtime, "urls": urls}),
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_upstream_urls_map_to_the_mirror_beside_the_runtime():
    runtime = "https://app.test/prefix/api/sdk/react.js"
    cases = {
        "https://esm.sh/@xterm/xterm@5.5.0/css/xterm.css":
            "https://app.test/prefix/api/mirror/esm.sh/@xterm/xterm@5.5.0/css/xterm.css",
        "https://esm.sh/x@1.0.0?deps=a@1&target=es2022":
            "https://app.test/prefix/api/mirror/esm.sh/x@1.0.0?deps=a@1&target=es2022",
        "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs":
            "https://app.test/prefix/api/mirror/cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs",
        "https://cdn.jsdelivr.net/npm/@excalidraw/excalidraw@0.18.1/dist/prod/":
            "https://app.test/prefix/api/mirror/cdn.jsdelivr.net/npm/@excalidraw/excalidraw@0.18.1/dist/prod/",
        "https://unpkg.com/x@1.0.0/x.js": "https://unpkg.com/x@1.0.0/x.js",
        "http://esm.sh/x@1.0.0": "http://esm.sh/x@1.0.0",
        "https://user@esm.sh/x@1.0.0": "https://user@esm.sh/x@1.0.0",
        "/api/sdk/session.js": "/api/sdk/session.js",
        "data:text/css,a{}": "data:text/css,a{}",
        "not a url": "not a url",
    }
    assert mirrored(runtime, list(cases)) == list(cases.values())


@pytest.mark.parametrize("runtime", ["blob:https://app.test/0b5a", "data:text/javascript,", "file:///x/react.js"])
def test_without_an_app_beside_the_runtime_every_url_is_unchanged(runtime):
    urls = ["https://esm.sh/x@1.0.0", "https://cdn.jsdelivr.net/npm/a@1.0.0/b.css"]
    assert mirrored(runtime, urls) == urls


def test_the_served_runtime_exports_mirror_url():
    react = shared_path("renderer-runtime", "react.js").read_text(encoding="utf-8")
    exports = re.findall(r"export\s*\{([^}]*)\}", react)
    assert any(re.search(r"\bmirrorUrl\b", block) for block in exports)
    assert "mirrorUrl" in (ROOT / "packages/renderer-hooks/platform.d.ts").read_text(encoding="utf-8")
