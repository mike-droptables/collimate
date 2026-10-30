"""Assemble dist/ from the staged, patched, vendored trees.

  dist/index.html, why.html, favicon.svg, fonts/   the landing (+ demo button)
  dist/sw.js                                        service worker, scope '/'; its
                                                    mirror hosts from the SDK contract
  dist/playground/                                  the React app (vite build)
  dist/playground/app.tar.gz                        the staged api tree
  dist/playground/pyodide/boot.js                   the pyodide bootstrap
  dist/playground/pyodide/collimate_playground_sdk-*.whl  private SDK source payload
  dist/playground/pyodide/*-py3-none-any.whl        websockets, vendored so
                                                    boot never consults a
                                                    package index
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
from pathlib import Path

import inject_landing
import sdk_payload

SDK_WHEEL_PLACEHOLDER = "__SDK_WHEEL__"
VENDORED_WHEELS_PLACEHOLDER = "__VENDORED_WHEELS__"
MIRROR_HOSTS_PLACEHOLDER = "__MIRROR_HOSTS__"

# Pure-Python packages the app imports that pyodide's distribution does not
# ship. Their versions, download URLs and hashes come from the workspace uv.lock, so
# the demo runs exactly what the app pins.
PURE_WHEELS = ("websockets",)


def _run(cmd: list[str], cwd: Path, **env) -> None:
    print(f"           $ {' '.join(cmd)}  (in {cwd})")
    result = subprocess.run(cmd, cwd=cwd, env={**os.environ, **env})
    if result.returncode != 0:
        raise SystemExit(f"assemble: `{' '.join(cmd)}` failed with {result.returncode}")


def install_frontend_deps(stage_frontend: Path, app_frontend: Path, *, fresh: bool) -> None:
    """Reuse APP/frontend/node_modules through a symlink when it exists (a
    local build), otherwise `npm ci` into the staged copy (CI)."""
    reusable = app_frontend / "node_modules"
    target = stage_frontend / "node_modules"
    if reusable.is_dir() and not fresh:
        target.symlink_to(reusable, target_is_directory=True)
        print(f"           node_modules -> {reusable}")
        return
    _run(["npm", "ci", "--no-audit", "--no-fund"], stage_frontend)


def build_frontend(stage_frontend: Path) -> Path:
    _run(["npx", "vite", "build"], stage_frontend)
    out = stage_frontend / "dist"
    if not (out / "index.html").is_file():
        raise SystemExit(f"assemble: vite produced no {out / 'index.html'}")
    return out


def build_sdk_wheel(sdk_dir: Path, out_dir: Path) -> Path:
    return sdk_payload.build(sdk_dir, out_dir)


def vendor_wheels(lockfile: Path, cache_dir: Path, out_dir: Path) -> list[str]:
    """Copy the ``py3-none-any`` wheel of every :data:`PURE_WHEELS` package
    into ``out_dir`` at the version ``lockfile`` (a uv.lock) pins, downloading
    it from the lock's URL into ``cache_dir`` once and checking its sha256
    against the lock on every use. Returns the wheel file names."""
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    by_name = {pkg["name"]: pkg for pkg in lock.get("package", [])}
    cache_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for name in PURE_WHEELS:
        pkg = by_name.get(name)
        if pkg is None:
            raise SystemExit(f"assemble: {name} is not in {lockfile}")
        pure = [w for w in pkg.get("wheels", []) if w["url"].endswith("-py3-none-any.whl")]
        if len(pure) != 1:
            raise SystemExit(f"assemble: {name} {pkg['version']} has no single py3-none-any wheel in {lockfile}")
        url, digest = pure[0]["url"], pure[0]["hash"].removeprefix("sha256:")
        filename = url.rsplit("/", 1)[-1]
        cached = cache_dir / filename
        if not cached.is_file() or hashlib.sha256(cached.read_bytes()).hexdigest() != digest:
            print(f"           downloading {filename}")
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise SystemExit(f"assemble: {filename} does not match the sha256 in {lockfile}")
            cached.write_bytes(data)
        shutil.copy2(cached, out_dir / filename)
        names.append(filename)
    return names


def pack_api(stage_api: Path, target: Path) -> None:
    def filter_member(info: tarfile.TarInfo):
        parts = Path(info.name).parts
        if any(p == "__pycache__" or p.endswith(".pyc") for p in parts):
            return None
        return info
    with tarfile.open(target, "w:gz") as tar:
        for item in sorted(stage_api.iterdir()):
            tar.add(item, arcname=item.name, filter=filter_member)


def build_verifier_runtime(stage_dir: Path, web_dir: Path, pyodide_out: Path, sdk_dir: Path) -> None:
    """Bundle the isolated engine from its own exact lock and SDK policy bytes."""
    dependencies = web_dir.parent / "verifier-runtime"
    _run(["npm", "ci", "--no-audit", "--no-fund"], dependencies)
    target = pyodide_out / "verifier"
    target.mkdir()
    esbuild = stage_dir / "frontend" / "node_modules" / ".bin" / "esbuild"
    _run([
        str(esbuild), str(web_dir / "verifier-worker.js"), "--bundle", "--format=esm",
        "--platform=browser", "--target=es2022", "--external:./renderer_bundle_core.mjs",
        f"--outfile={target / 'verifier-worker.js'}", "--log-level=warning",
    ], web_dir, NODE_PATH=str(dependencies / "node_modules"))
    modules = dependencies / "node_modules"
    shutil.copy2(modules / "esbuild-wasm" / "esbuild.wasm", target / "esbuild.wasm")
    shutil.copy2(modules / "@jitl" / "quickjs-wasmfile-release-asyncify" / "dist" / "emscripten-module.wasm",
                 target / "quickjs.wasm")
    shutil.copy2(sdk_dir / "scripts" / "renderer_bundle_core.mjs", target / "renderer_bundle_core.mjs")
    shutil.copy2(sdk_dir / "src" / "collimate" / "renderer_bundle_contract.json",
                 target / "renderer_bundle_contract.json")
    shutil.copy2(web_dir / "verifier-supervisor.js", pyodide_out / "verifier-supervisor.js")


def mirror_hosts(sdk_dir: Path) -> list[str]:
    """The hosts of the SDK module mirror contract's upstreams, each
    ``https://<host>/``: the hosts the app's ``/api/mirror/<host>/`` route
    serves, which the service worker answers from upstream instead."""
    contract = json.loads((sdk_dir / "src" / "collimate" / "module_mirror_contract.json").read_text(encoding="utf-8"))
    hosts = []
    for upstream in contract["upstreams"]:
        host = upstream.removeprefix("https://").removesuffix("/")
        if upstream != f"https://{host}/" or not host or "/" in host:
            raise SystemExit(f"assemble: module mirror upstream {upstream!r} is not https://<host>/")
        hosts.append(host)
    return hosts


def write_service_worker(sw_js: Path, target: Path, hosts: list[str]) -> None:
    text = sw_js.read_text(encoding="utf-8")
    if text.count(MIRROR_HOSTS_PLACEHOLDER) != 1:
        raise SystemExit(f"assemble: sw.js must contain {MIRROR_HOSTS_PLACEHOLDER} exactly once")
    target.write_text(text.replace(MIRROR_HOSTS_PLACEHOLDER, json.dumps(hosts)), encoding="utf-8")


def inject_index(index: Path, gate_js: Path, wheel_names: list[str]) -> None:
    """Three edits to the built index.html:
    1. the inline gate as the FIRST script in <head> (it must run before
       any module script issues an /api/* fetch or opens a WebSocket);
    2. a preconnect to the pyodide CDN plus preload hints for the same-origin
       assets boot.js fetches (the app tarball and every wheel);
    3. boot.js as a module script before </head>."""
    html = index.read_text(encoding="utf-8")
    gate = gate_js.read_text(encoding="utf-8")
    if "</script>" in gate:
        raise SystemExit("assemble: sw-gate.js must not contain '</script>'")
    if html.count("<head>") != 1 or html.count("</head>") != 1:
        raise SystemExit(f"assemble: {index} needs exactly one <head> and </head>")
    html = html.replace("<head>", f"<head>\n    <script>{gate}</script>", 1)
    tail = "\n".join([
        '    <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>',
        '    <link rel="preload" as="fetch" crossorigin="anonymous" href="./app.tar.gz">',
        *(f'    <link rel="preload" as="fetch" crossorigin="anonymous" href="./pyodide/{name}">' for name in wheel_names),
        '    <script type="module" src="./pyodide/boot.js"></script>',
        "  </head>",
    ])
    html = html.replace("</head>", tail, 1)
    index.write_text(html, encoding="utf-8")


def run(*, stage_dir: Path, dist_dir: Path, app_dir: Path, sdk_dir: Path, web_dir: Path,
        fresh_npm: bool) -> None:
    stage_frontend = stage_dir / "frontend"
    stage_api = stage_dir / "api"
    for name in ("boot.js", "sw.js", "sw-gate.js", "landing-hero.html", "verifier-worker.js", "verifier-supervisor.js"):
        if not (web_dir / name).is_file():
            raise SystemExit(f"assemble: {web_dir / name} missing")

    install_frontend_deps(stage_frontend, app_dir / "frontend", fresh=fresh_npm)
    frontend_out = build_frontend(stage_frontend)

    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    app_out = dist_dir / "playground"
    pyodide_out = app_out / "pyodide"
    shutil.copytree(frontend_out, app_out)
    pyodide_out.mkdir()

    build_verifier_runtime(stage_dir, web_dir, pyodide_out, sdk_dir)

    wheel = build_sdk_wheel(sdk_dir, pyodide_out)
    vendored = vendor_wheels(app_dir / "uv.lock", stage_dir.parent / ".wheels", pyodide_out)
    pack_api(stage_api, app_out / "app.tar.gz")

    boot_js = (web_dir / "boot.js").read_text(encoding="utf-8")
    for placeholder in (SDK_WHEEL_PLACEHOLDER, VENDORED_WHEELS_PLACEHOLDER):
        if boot_js.count(placeholder) != 1:
            raise SystemExit(f"assemble: boot.js must contain {placeholder} exactly once")
    boot_js = boot_js.replace(SDK_WHEEL_PLACEHOLDER, wheel.name)
    boot_js = boot_js.replace(VENDORED_WHEELS_PLACEHOLDER, json.dumps(vendored))
    (pyodide_out / "boot.js").write_text(boot_js, encoding="utf-8")
    write_service_worker(web_dir / "sw.js", dist_dir / "sw.js", mirror_hosts(sdk_dir))
    inject_index(app_out / "index.html", web_dir / "sw-gate.js", [*vendored, wheel.name])

    inject_landing.run(app_dir, dist_dir, web_dir)
    size = (app_out / "app.tar.gz").stat().st_size / 1e6
    print(f"           app.tar.gz {size:.1f} MB, wheels {wheel.name} + {', '.join(vendored)}")
