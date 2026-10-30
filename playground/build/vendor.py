"""Vendor the demo's assets into the staged api tree, and the SDK's
portable compiler beside the staged frontend.

The staged tree is the ONE archive pyodide unpacks, and pyodide puts it on
``sys.path`` (``/mnt/app``), so everything the browser needs on the Python
side goes in here. The bundled content sits beside the app's modules the
way the installed ``collimate`` wheel ships it, so the app's bundled
default (``genesis.bundled_dir``) finds it through ``find_spec`` and the
demo names no suggested source:

  collimate_content/generators/   the demo generators
  collimate_content/renderers/    the repo's unchanged renderer packages (content/renderers)
  collimate_content/suggested/    demo.json, the one suggestion the demo channel
                                  is created from: the demo generator cell(s)
                                  and every renderer cell
  collimate_builtins/             the repo's built-in handlers and renderers
                                  (builtins/; the app serves them for every channel)
  collimate_playground/           the shim package
  _demo/                          channel.yaml + channel/ (the welcome cells)

Beside it, ``.stage/sdk/`` holds the SDK's portable compiler (BROWSER_COMPILER)
at its repo paths, where the staged frontend's renderer worker and build
check import it, as the repo's frontend imports ``../sdk/``.

No renderer bundle is prebuilt: in the browser the app compiles its built-ins
at startup and every renderer at admission through the shim's compiler
(shim/collimate_playground/verification.py) and stores each as a server bundle.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

SUGGESTION_ID = "demo"

# The SDK files the frontend imports by relative path (the renderer worker's
# compiler core with its types, and the contract the worker and the build
# check read), at their paths under the SDK root.
BROWSER_COMPILER = (
    "scripts/renderer_bundle_core.mjs",
    "scripts/renderer_bundle_core.d.mts",
    "src/collimate/renderer_bundle_contract.json",
)


def _copy(src: Path, dst: Path, label: str) -> None:
    if not src.is_dir():
        raise SystemExit(f"vendor: {label} missing: {src}")
    shutil.copytree(
        src, dst, symlinks=False,
        ignore=lambda _d, names: {n for n in names if n in {"__pycache__", "node_modules"} or n.endswith(".pyc")},
    )


def _cell_name(cell: Path) -> str:
    """A generator cell's display name: ``name`` from its cell.yaml, else
    the titled folder name (the SDK's layout rule)."""
    manifest = cell / "cell.yaml"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            found = re.fullmatch(r"name:\s*(.+?)\s*", line)
            if found:
                return found.group(1).strip("'\"")
    return cell.name.replace("_", " ").replace("-", " ").title()


def write_suggestion(content: Path, app_dir: Path) -> Path:
    """``<content>/suggested/demo.json``: the demo generator cells and the
    renderer cells of the repo's Standard suggestion (the renderer tree is
    copied whole, so every one of them is there), no tutorial cells."""
    standard = json.loads((app_dir / "content" / "suggested" / "standard.json").read_text(encoding="utf-8"))
    generators = [[_cell_name(cell), f"generators/{cell.name}"]
                  for cell in sorted((content / "generators").iterdir())
                  if cell.is_dir() and not cell.name.startswith(("_", "."))]
    path = content / "suggested" / f"{SUGGESTION_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "name": "Demo", "description": "The browser demo's scripted generators and the SDK's renderers.",
        "generators": generators, "renderers": standard["renderers"], "tutorial": [], "quick_fields": {},
    }, indent=2) + "\n", encoding="utf-8")
    return path


def stage_bundled(root: Path, generators: Path, app_dir: Path) -> Path:
    """The bundled trees under ``root`` (a directory on the app's
    ``sys.path``): ``collimate_content/`` holding ``generators``, the repo's
    renderers and the demo suggestion, and ``collimate_builtins/`` holding
    the repo's built-ins. Returns the content tree."""
    content = root / "collimate_content"
    _copy(generators, content / "generators", "demo generators")
    _copy(app_dir / "content" / "renderers", content / "renderers", "renderers")
    write_suggestion(content, app_dir)
    _copy(app_dir / "builtins", root / "collimate_builtins", "built-ins")
    return content


def stage_browser_compiler(stage_dir: Path, sdk_dir: Path) -> None:
    """Copy BROWSER_COMPILER from ``sdk_dir`` to ``<stage_dir>/sdk/``, where
    the staged frontend's relative imports resolve, before Vite runs."""
    for name in BROWSER_COMPILER:
        source = sdk_dir / name
        if not source.is_file():
            raise SystemExit(f"vendor: SDK compiler file missing: {source}")
        target = stage_dir / "sdk" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def run(stage_dir: Path, demo_dir: Path, app_dir: Path, sdk_dir: Path, *, shim_dir: Path) -> None:
    api = stage_dir / "api"
    content = stage_bundled(api, demo_dir / "generators", app_dir)
    stage_browser_compiler(stage_dir, sdk_dir)
    _copy(shim_dir / "collimate_playground", api / "collimate_playground", "shim package")
    demo_out = api / "_demo"
    demo_out.mkdir()
    channel_yaml = demo_dir / "channel.yaml"
    if not channel_yaml.is_file():
        raise SystemExit(f"vendor: {channel_yaml} missing")
    shutil.copy2(channel_yaml, demo_out / "channel.yaml")
    _copy(demo_dir / "channel", demo_out / "channel", "demo channel cells")
    print(f"           generators: {sorted(p.name for p in (content / 'generators').iterdir() if p.is_dir())}")
