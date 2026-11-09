"""Build the browser demo. Invoked by build.sh as ``python3 playground/build``.

Steps, in order (each a module beside this file):
  stage     copy APP/api + APP/frontend into playground/.stage/
  patches   anchored, fail-loud edits to the STAGED copies
  vendor    demo generators + the repo's renderers + the shim + demo content into
            the staged api tree (that tree is what pyodide unpacks)
  assemble  frontend build, SDK wheel, app tarball, dist/ layout, index.html
            injection, landing copy + hero button
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import assemble
import patches
import stage
import vendor


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python3 playground/build <app_dir> <playground_dir>", file=sys.stderr)
        return 2
    app_dir = Path(argv[0]).resolve()
    playground_dir = Path(argv[1]).resolve()
    sdk_dir = Path(os.environ["COLLIMATE_SDK_DIR"]).resolve()
    demo_dir = Path(os.environ.get("PLAYGROUND_DEMO_DIR") or playground_dir / "demo").resolve()
    for label, path in (("app", app_dir), ("sdk", sdk_dir), ("demo", demo_dir)):
        if not path.is_dir():
            print(f"build: {label} dir not found: {path}", file=sys.stderr)
            return 1

    started = time.monotonic()
    stage_dir = playground_dir / ".stage"
    dist_dir = playground_dir / "dist"

    print(f"[stage]    {app_dir} -> {stage_dir}")
    stage.run(app_dir, stage_dir)
    print("[patches]  applying anchored edits to the staged copies")
    patches.run(stage_dir)
    print(f"[vendor]   demo assets from {demo_dir}, renderers from {app_dir / 'content'}")
    vendor.run(stage_dir, demo_dir, app_dir, sdk_dir, shim_dir=playground_dir / "shim")
    print(f"[assemble] building dist/ at {dist_dir}")
    assemble.run(
        stage_dir=stage_dir, dist_dir=dist_dir, app_dir=app_dir,
        sdk_dir=sdk_dir, web_dir=playground_dir / "web",
        fresh_npm=os.environ.get("PLAYGROUND_FRESH_NPM") == "1",
    )
    print(f"[done]     {dist_dir} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
