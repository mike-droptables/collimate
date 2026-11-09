#!/usr/bin/env python3
"""Build the server environment a desktop package carries: a standalone
CPython with the ``collimate`` wheel and its locked dependencies installed
into it, which the shell runs as ``<server>/bin/python3 -I -B -m
collimate_server``.

    python3 build/server_env.py --wheel dist/collimate-<v>-<tags>.whl --out dist/desktop/server [--target T] [--release]

The tree it writes (``--out``, shipped as the package's ``resources/server/``):

- the python-build-standalone ``install_only_stripped`` build of
  :data:`PYTHON_VERSION` for the target (:data:`STANDALONE`, each archive
  pinned by sha256 and checked before it is unpacked), flattened so the
  interpreter is ``bin/python3``. It is relocatable as shipped (its binaries
  find ``lib/`` relative to themselves), so the tree runs from wherever the
  package puts it, a read-only mount included.
- the dependencies exactly as ``uv.lock`` pins them for the runtime (``uv
  export --no-dev``, hashes required, binary wheels only, no deps resolved
  at install), then the wheel itself with ``--no-deps``, installed into the
  interpreter's own ``site-packages`` with ``uv pip install --python``. The
  build-time wheelhouse is uv's cache; nothing is installed at runtime.
- every module compiled to bytecode with ``--invalidation-mode
  unchecked-hash`` (valid whatever the files' timestamps become in an
  installer), except the bundled content trees, which the server reads as
  data and never imports.
- pruned: the console scripts the installs wrote (their shebangs name this
  build's absolute path; the shell only ever runs ``python3 -m``), pip,
  Tcl/Tk and its modules, the headers and ``share/``.
- ``bin/uv``: the uv release binary of :data:`UV_VERSION` for the target
  (:data:`UV_ARCHIVES`, each archive pinned by sha256 and checked before it
  is unpacked). The server runs every native generator run with it (``uv
  run`` of the generator's entrypoint, its inline dependencies fetched on the
  first run and cached by uv after), on ``bin/python3`` where the
  generator's ``requires-python`` admits it; the shell names the tree to the
  server with ``--server-env``. Only ``uv`` is carried, not ``uvx``.
- the SDK packages, ``collimate`` and ``gen``, alone in
  ``site-packages/collimate-run-sdk/`` (the wheel's layout, put on
  ``sys.path`` by its ``.pth``): a generator run's PYTHONPATH, which shows it
  none of the server's dependencies. The smoke test checks it.
- on macOS, every Mach-O file signed (build/mac_codesign.py): ad-hoc, or
  with the release's Developer ID when ``--release`` is given and
  ``MAC_SIGNING`` is ``true`` (``CSC_LINK`` / ``CSC_KEY_PASSWORD``, as
  electron-builder reads them), the hardened runtime and a timestamp then.
  The app's signature must not rewrite them after the hash names them, so
  the packaging config has electron-builder skip the tree and seal it as it
  is. Linux trees are not signed. The SDK's pinned native resources (its
  esbuild binaries and QuickJS extension, collimate._host.native_resources)
  are checked against their pins before signing; codesign rewrites the
  Mach-O ones, so beside each the build writes ``codesign.json``, the pin
  and the sha256 it was signed to, which the SDK admits on macOS in place of
  the pin. The smoke run checks every resource as the SDK will at runtime.
- ``collimate-server.json``: the target, the interpreter, the wheel (file,
  version, sha256), the uv (version, archive, sha256), on macOS ``codesign``
  (the identity, ``-`` for ad-hoc, its name and whether the hardened runtime
  is on) and ``hash``, the :func:`tree_hash` of everything else in the tree,
  signatures included. The hash names the environment: the build stamp
  carries it, the shell names its AppImage copy by it, and a kept-alive
  server is adopted only by a shell carrying the same one.

The tree is built beside ``--out`` and renamed into place only once it has
(signed and) started its own interpreter and imported the server, so ``--out`` is either
the previous complete tree or this one. It is built for the host by default;
``--target`` names one of :data:`STANDALONE`, and a target other than the
host's is refused, since the environment is proven by running it (the
release builds each target on a runner of that platform).

Needs ``uv`` on PATH (or ``$UV``) to install the tree, and the network for
the first download of each archive and of the locked wheels; all are cached
(``$COLLIMATE_BUILD_CACHE``, default ``~/.cache/collimate-build``, and uv's
own cache).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mac_codesign  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "collimate-server.json"
SCHEMA = 2
PYTHON_VERSION = "3.13.18"
STANDALONE_RELEASE = "20261026"
STANDALONE_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
                  "{release}/cpython-{version}%2B{release}-{triple}-install_only_stripped.tar.gz")
# target: (the archive's platform triple, its sha256 from the release's SHA256SUMS)
STANDALONE = {
    "linux-x64": ("x86_64-unknown-linux-gnu",
                  "df15fe60d3ff55bf9dcf25824b5572756bb8816ff93865a273d3301d7066a8b7"),
    "linux-arm64": ("aarch64-unknown-linux-gnu",
                    "578d9da1983463ffb0c8c98ab0da2f18177f44069328eb0af847860c1eb05c3e"),
    "darwin-x64": ("x86_64-apple-darwin",
                   "7412c19d896c99431b74a18a5643e110449a7f9590fbab179c3e5ab27b9aa9e3"),
    "darwin-arm64": ("aarch64-apple-darwin",
                     "8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405"),
}
# The uv the environment carries for generator runs, one release for every
# target: target: (the release's platform triple, the archive's sha256 from the
# release's sha256.sum).
UV_VERSION = "0.13.6"
UV_URL = "https://github.com/astral-sh/uv/releases/download/{version}/uv-{triple}.tar.gz"
UV_ARCHIVES = {
    "linux-x64": ("x86_64-unknown-linux-gnu",
                  "f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645"),
    "linux-arm64": ("aarch64-unknown-linux-gnu",
                    "229e194255c7958d86d56e8790d969a6f74736592133f0dfd0076f754c642c1d"),
    "darwin-x64": ("x86_64-apple-darwin",
                   "63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02"),
    "darwin-arm64": ("aarch64-apple-darwin",
                     "60fbd5c8a1ed681845eea28d9d622fcde03c51e91a43465b64a1e17db1ed3472"),
}
# Where it lands in the tree; the server started with --server-env <tree>
# (the shell passes the tree, or its AppImage copy) runs generators with it.
UV_IN_TREE = "bin/uv"
# The SDK's pinned native resources: `pinned` lists them with their pins (each
# checked against it), `check` checks each as the SDK does before using it.
NATIVE_RESOURCES = "collimate._host.native_resources"
SIGNING_RECORD = "codesign.json"  # collimate._host.native_resources.RECORD
MACHINES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
PYTHON_LIB = "lib/python" + ".".join(PYTHON_VERSION.split(".")[:2])
# What the shell runs and its aliases; everything else the archive and the
# installs put in bin/ goes (uv is placed after the prune).
KEEP_BIN = ("python3", "python" + ".".join(PYTHON_VERSION.split(".")[:2]), "python")
PRUNE = ("include", "share", "lib/pkgconfig", "lib/itcl4", "lib/tcl9", "lib/tk9", "lib/thread",
         "lib/libtcl", "lib/libtk", f"{PYTHON_LIB}/tkinter", f"{PYTHON_LIB}/idlelib",
         f"{PYTHON_LIB}/turtledemo", f"{PYTHON_LIB}/turtle.py", f"{PYTHON_LIB}/ensurepip",
         f"{PYTHON_LIB}/lib-dynload/_tkinter", f"{PYTHON_LIB}/site-packages/pip")
# The bundled trees the wheel installs as package data, read and never imported.
DATA_TREES = ("collimate_content", "collimate_builtins", "collimate_module_snapshot")
WHEEL = re.compile(r"collimate-(?P<version>[^-]+)-[^-]+-[^-]+-[^-]+\.whl")


class Refused(Exception):
    """The inputs do not describe one server environment."""


def host_target(sys_platform: str = sys.platform, machine: str = platform.machine()) -> str:
    """The :data:`STANDALONE` target of this machine."""
    system = {"linux": "linux", "darwin": "darwin"}.get(sys_platform)
    arch = MACHINES.get(machine.lower())
    if system is None or arch is None:
        raise Refused(f"no server environment for {sys_platform}/{machine} (Windows is connect-only)")
    return f"{system}-{arch}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    """The sha256 of the tree at ``root`` but its manifest: one line per entry,
    in sorted path order, ``<path>\\0<kind>\\0<content>\\n``, where a file's
    kind is ``x`` (executable) or ``f`` and its content its sha256, a
    symlink's kind is ``l`` and its content its target, and a directory is
    ``d`` with no content. Timestamps and owners do not count."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if rel == MANIFEST:
                continue
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries.append((rel, "l", os.readlink(path)))
            elif stat.S_ISDIR(mode):
                entries.append((rel, "d", ""))
            else:
                entries.append((rel, "x" if mode & stat.S_IXUSR else "f", sha256(path)))
    digest = hashlib.sha256()
    for rel, kind, content in sorted(entries):
        digest.update(f"{rel}\0{kind}\0{content}\n".encode())
    return digest.hexdigest()


def read_manifest(server: Path) -> dict:
    """``server``'s manifest, refused unless the tree still hashes to it."""
    try:
        manifest = json.loads((server / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Refused(f"{server} holds no readable {MANIFEST}: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise Refused(f"{server / MANIFEST} is not a schema {SCHEMA} manifest")
    actual = tree_hash(server)
    if manifest.get("hash") != actual:
        raise Refused(f"{server} hashes to {actual}, not the {manifest.get('hash')} its manifest names")
    return manifest


def cache_dir() -> Path:
    return Path(os.environ.get("COLLIMATE_BUILD_CACHE") or Path.home() / ".cache" / "collimate-build")


# A pinned download is retried this many times over a passing failure of the
# release CDN (a 5xx or 429 answer, a dropped or timed-out connection) with
# a doubling pause from DOWNLOAD_BACKOFF seconds: GitHub's release downloads
# answered 504 on two desktop legs of the v0.1.2 release, twice.
DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_BACKOFF = 2.0


def _passing(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


GITHUB_RELEASE_ASSET = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_asset_location(url: str, token: str) -> str:
    """Where the GitHub release asset at ``url`` (a ``releases/download``
    link) is served, found through the API with ``token``: GitHub throttles
    anonymous downloads first (its runners' anonymous fetches answered 504
    through the 2026-11-09 incident while authenticated requests did not),
    so a build with a token asks as itself. The release's assets are listed
    by tag, the asset's API link answers a redirect to the signed body, and
    that body is fetched without the token (a signed URL refuses a second
    credential). A URL that is no such link, or an asset the release lacks,
    is ``Refused``."""
    match = GITHUB_RELEASE_ASSET.match(url)
    if match is None:
        raise Refused(f"{url} is not a GitHub release asset link")
    owner, repo, tag, name = (urllib.parse.unquote(part) for part in match.groups())
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{urllib.parse.quote(tag)}"
    with urllib.request.urlopen(urllib.request.Request(api, headers={**headers, "Accept": "application/vnd.github+json"}),
                                timeout=60) as response:
        release = json.load(response)
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise Refused(f"release {tag} of {owner}/{repo} has no asset {name}")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(asset["url"], headers={**headers, "Accept": "application/octet-stream"}),
                         timeout=60) as response:
            # GitHub answers a redirect; a body here is the asset itself,
            # which only a GitHub change would serve.
            raise Refused(f"{asset['url']} answered {response.status} instead of a redirect to the asset")
    except urllib.error.HTTPError as error:
        if error.code not in (301, 302, 303, 307, 308) or not error.headers.get("Location"):
            raise
        return error.headers["Location"]


def download(url: str, into: Path) -> None:
    """``url``'s body into ``into``, retried over passing failures; a GitHub
    release asset is fetched as the ``GITHUB_TOKEN`` holder when that
    variable is set (:func:`github_asset_location`)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            source = github_asset_location(url, token) if token and GITHUB_RELEASE_ASSET.match(url) else url
            with urllib.request.urlopen(source, timeout=120) as response, into.open("wb") as out:
                shutil.copyfileobj(response, out)
            return
        except Exception as error:  # noqa: BLE001 - classified below
            if attempt == DOWNLOAD_ATTEMPTS or not _passing(error):
                raise
            pause = DOWNLOAD_BACKOFF * 2 ** (attempt - 1)
            print(f"server_env: {url}: {error}; retrying in {pause:g}s ({attempt}/{DOWNLOAD_ATTEMPTS})", flush=True)
            time.sleep(pause)


def fetch_pinned(url: str, expected: str, folder: str) -> Path:
    """The archive at ``url``, downloaded into the cache's ``folder`` once
    (:func:`download`) and checked against its pinned sha256 every time."""
    archive = cache_dir() / folder / url.rsplit("/", 1)[-1].replace("%2B", "+")
    if not archive.is_file() or sha256(archive) != expected:
        archive.parent.mkdir(parents=True, exist_ok=True)
        partial = archive.with_name(archive.name + ".partial")
        print(f"server_env: downloading {url}", flush=True)
        try:
            download(url, partial)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        actual = sha256(partial)
        if actual != expected:
            partial.unlink()
            raise Refused(f"{url} has sha256 {actual}, not the pinned {expected}")
        partial.replace(archive)
    return archive


def fetch_standalone(target: str) -> Path:
    """The pinned python-build-standalone archive for ``target``."""
    triple, expected = STANDALONE[target]
    url = STANDALONE_URL.format(release=STANDALONE_RELEASE, version=PYTHON_VERSION, triple=triple)
    return fetch_pinned(url, expected, "python-build-standalone")


def fetch_uv(target: str) -> Path:
    """The pinned uv release archive for ``target``."""
    triple, expected = UV_ARCHIVES[target]
    return fetch_pinned(UV_URL.format(version=UV_VERSION, triple=triple), expected, "uv")


def place_uv(archive: Path, tree: Path) -> None:
    """The archive's ``uv-<triple>/uv`` as ``tree``'s :data:`UV_IN_TREE`, an
    executable regular file; refused when the archive holds anything outside
    one ``uv-<triple>/`` folder, or no such file."""
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        folder = Path(members[0].name).parts[0] if members and Path(members[0].name).parts else ""
        for member in members:
            parts = Path(member.name).parts
            if (not folder.startswith("uv-") or not parts or parts[0] != folder
                    or member.name.startswith("/") or ".." in parts):
                raise Refused(f"{archive.name} holds {member.name!r} outside one uv-<triple>/ folder")
        binary = next((m for m in members if m.name == f"{folder}/uv" and m.isfile()), None)
        stream = bundle.extractfile(binary) if binary is not None else None
        if stream is None:
            raise Refused(f"{archive.name} holds no {folder}/uv file")
        placed = tree / UV_IN_TREE
        with stream, placed.open("wb") as out:
            shutil.copyfileobj(stream, out)
    placed.chmod(0o755)


def unpack(archive: Path, into: Path) -> None:
    """The archive's ``python/`` folder as ``into`` itself."""
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "python" or member.name.startswith("/") or ".." in parts:
                raise Refused(f"{archive.name} holds {member.name!r} outside python/")
        bundle.extractall(into.parent / (into.name + ".unpack"), filter="tar")
    (into.parent / (into.name + ".unpack") / "python").rename(into)
    (into.parent / (into.name + ".unpack")).rmdir()


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("server_env: " + " ".join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def install(tree: Path, wheel: Path, uv: str) -> None:
    python = str(tree / "bin" / "python3")
    with tempfile.TemporaryDirectory(prefix="server-env-") as scratch:
        requirements = Path(scratch) / "requirements.txt"
        run([uv, "export", "--locked", "--no-dev", "--no-emit-project", "--no-header",
             "--format", "requirements.txt", "--output-file", str(requirements)], cwd=ROOT,
            stdout=subprocess.DEVNULL)
        common = [uv, "pip", "install", "--python", python, "--no-config", "--link-mode", "copy",
                  "--no-deps", "--only-binary", ":all:"]
        run([*common, "--require-hashes", "-r", str(requirements)], cwd=scratch)
        run([*common, str(wheel)], cwd=scratch)
        run([uv, "pip", "check", "--python", python], cwd=scratch)


def prune(tree: Path) -> None:
    for entry in sorted((tree / "bin").iterdir()):
        if entry.name not in KEEP_BIN:
            entry.unlink()
    for prefix in PRUNE:
        parent, _, stem = (tree / prefix).as_posix().rpartition("/")
        for entry in Path(parent).glob(stem + "*") if Path(parent).is_dir() else ():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    for cache in tree.rglob("__pycache__"):
        shutil.rmtree(cache)


def compile_bytecode(tree: Path) -> None:
    exclude = "/(" + "|".join(DATA_TREES) + ")/"
    run([str(tree / "bin" / "python3"), "-I", "-m", "compileall", "-q", "-j", "0", "-f",
         "--invalidation-mode", "unchecked-hash", "-x", exclude, str(tree / PYTHON_LIB)],
        stdout=subprocess.DEVNULL)


def tree_python(tree: Path, args: list[str], scratch: str) -> subprocess.CompletedProcess:
    """The tree's own interpreter, isolated (``-I -B``), run with ``args``;
    Refused with what it said when it fails."""
    try:
        return run([str(tree / "bin" / "python3"), "-I", "-B", *args], cwd=scratch, capture_output=True,
                   text=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": scratch,
                                   "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"})
    except subprocess.CalledProcessError as error:
        raise Refused(f"{tree / 'bin' / 'python3'} {' '.join(args[:2])} failed ({error.returncode}):\n"
                      f"{(error.stderr or '').strip()}") from error


def pinned_resources(tree: Path) -> dict[str, str]:
    """The SDK's pinned native resources in ``tree`` (relative) with their
    pins, as the tree's SDK lists them, refusing any whose bytes are not its
    pin: run before anything is signed."""
    with tempfile.TemporaryDirectory(prefix="server-env-pinned-") as scratch:
        listing = json.loads(tree_python(tree, ["-m", NATIVE_RESOURCES, "pinned"], scratch).stdout)
    root = tree.resolve()
    return {Path(path).resolve().relative_to(root).as_posix(): pin for path, pin in listing.items()}


def record_signatures(tree: Path, pinned: dict[str, str], signed: list[str]) -> list[str]:
    """Beside each pinned resource the signing rewrote, in its folder's
    ``codesign.json``, its pin and the sha256 it was signed to, which the SDK
    admits on macOS in place of the pin (collimate._host.native_resources);
    the resources recorded. Refused for a resource that changed but was not
    signed, or a record already there."""
    records: dict[str, dict[str, dict[str, str]]] = {}
    for rel, pin in sorted(pinned.items()):
        now = sha256(tree / rel)
        if now == pin:
            continue
        if rel not in signed:
            raise Refused(f"{rel} no longer hashes to its pin {pin} and was not signed")
        folder, _, name = rel.rpartition("/")
        records.setdefault(folder, {})[name] = {"pinned": pin, "signed": now}
    for folder, files in sorted(records.items()):
        record = tree / folder / SIGNING_RECORD
        if record.exists() or record.is_symlink():
            raise Refused(f"{record} exists before the build wrote it")
        record.write_text(json.dumps({"schema": 1, "files": files}, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    return sorted(f"{folder}/{name}" for folder, files in records.items() for name in files)


def smoke(tree: Path) -> None:
    """The tree's own interpreter, isolated, imports the server and the SDK
    from the tree and nowhere else, the directory a generator run gets as its
    PYTHONPATH is in the tree and holds the SDK alone (``sdk_src_dir``
    refuses anything else), the SDK's pinned native resources pass the checks
    the SDK makes before running or loading them (on macOS the signed bytes
    their records name), and the tree's uv is the pinned one."""
    probe = ("import sys, collimate, collimate_server.main, gen; from pathlib import Path; "
             "from collimate_server.process_manager.backends import sdk_src_dir; "
             "root = Path(sys.argv[1]).resolve(); "
             "assert all(Path(m.__file__).resolve().is_relative_to(root) "
             "for m in (collimate, collimate_server.main, gen)), sys.path; "
             "assert Path(sdk_src_dir()).resolve().is_relative_to(root), sdk_src_dir()")
    with tempfile.TemporaryDirectory(prefix="server-env-smoke-") as scratch:
        tree_python(tree, ["-c", probe, str(tree)], scratch)
        print("server_env: " + tree_python(tree, ["-m", NATIVE_RESOURCES, "check"], scratch).stdout.strip(),
              flush=True)
        said = run([str(tree / UV_IN_TREE), "--version"], cwd=scratch, capture_output=True, text=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": scratch}).stdout.split()
        if said[:2] != ["uv", UV_VERSION]:
            raise Refused(f"{tree / UV_IN_TREE} says {' '.join(said)!r}, not uv {UV_VERSION}")


def build(wheel: Path, out: Path, target: str, uv: str, *, release: bool = False, env: dict | None = None) -> dict:
    """The environment for ``target`` at ``out``; on macOS signed by the
    signer :func:`mac_codesign.signer_for` names for ``env`` (default: this
    process's) and ``release``."""
    env = dict(os.environ) if env is None else env
    match = WHEEL.fullmatch(wheel.name)
    if match is None or not wheel.is_file():
        raise Refused(f"{wheel} is not a collimate wheel")
    if target not in STANDALONE or target not in UV_ARCHIVES:
        raise Refused(f"target must be one of {', '.join(STANDALONE)}, not {target!r}")
    if target != host_target():
        raise Refused(f"a {target} environment is built on a {target} machine, not on {host_target()}")
    mac = target.startswith("darwin-")
    if mac:
        mac_codesign.release_signs(env, release)  # a malformed MAC_SIGNING stops here, before any download
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_name(out.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        unpack(fetch_standalone(target), staging)
        install(staging, wheel.resolve(), uv)
        prune(staging)
        compile_bytecode(staging)
        uv_archive = fetch_uv(target)
        place_uv(uv_archive, staging)
        codesign = None
        if mac:
            # Checked against their pins while still unsigned: the signing
            # rewrites the Mach-O ones, and their records name the result.
            pinned = pinned_resources(staging)
            # Before the smoke run, so it runs the signed binaries; before the
            # hash, so the hash names the bytes the app's signature seals.
            with mac_codesign.signer_for(env, release) as signer:
                signed = mac_codesign.sign_tree(staging, signer)
            codesign = signer.manifest()
            print(f"server_env: signed {len(signed)} Mach-O files "
                  f"{'with ' + str(signer.name) if signer.developer_id else 'ad-hoc'}", flush=True)
            recorded = record_signatures(staging, pinned, signed)
            print(f"server_env: recorded the signed sha256 of {len(recorded)} of {len(pinned)} pinned native "
                  f"resources: {', '.join(recorded)}", flush=True)
        smoke(staging)
        for cache in staging.rglob("__pycache__"):
            # The smoke run's -B keeps it from writing; this is the proof.
            if cache.parent.name in DATA_TREES:
                raise Refused(f"{cache} holds bytecode in a data tree")
        manifest = {
            "schema": SCHEMA,
            "target": target,
            "python": {"version": PYTHON_VERSION, "standalone": STANDALONE_RELEASE},
            "collimate": {"version": match["version"], "wheel": wheel.name, "sha256": sha256(wheel)},
            "uv": {"version": UV_VERSION, "archive": uv_archive.name, "sha256": UV_ARCHIVES[target][1]},
            **({"codesign": codesign} if codesign is not None else {}),
            "hash": tree_hash(staging),
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(out, ignore_errors=True)
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--wheel", required=True, type=Path, help="the collimate wheel to carry")
    parser.add_argument("--out", required=True, type=Path, help="the environment to write")
    parser.add_argument("--target", help="the platform it is for (default: this machine's)")
    parser.add_argument("--release", action="store_true",
                        help="a release build: on macOS signed with the Developer ID when MAC_SIGNING is true")
    args = parser.parse_args(argv)
    uv = os.environ.get("UV") or shutil.which("uv")
    try:
        if not uv:
            raise Refused("uv is not on PATH (or $UV)")
        manifest = build(args.wheel, args.out, args.target or host_target(), uv, release=args.release)
    except (Refused, mac_codesign.Refused, subprocess.CalledProcessError, OSError) as error:
        print(f"server_env: {error}", file=sys.stderr)
        return 1
    size = sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file() and not p.is_symlink())
    print(f"server_env: {manifest['target']} collimate {manifest['collimate']['version']} on CPython "
          f"{PYTHON_VERSION} with uv {UV_VERSION}, {size / 1e6:.0f} MB, hash {manifest['hash'][:16]} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
