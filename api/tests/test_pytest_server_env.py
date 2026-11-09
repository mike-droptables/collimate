"""The server environment a desktop package carries (build/server_env.py) and
its boot check (build/check_server_env.py).

The builder's rules run on synthetic trees here. The real environment is
built by build/build_desktop.sh into ``dist/desktop/server`` (or a package's
``resources/server``, named by ``$COLLIMATE_SERVER_ENV``); when one exists,
``test_the_built_environment_boots_and_runs_a_generator_with_no_network``
boots it in a network namespace of its own, reaches /health and /ready and
runs the run probe with the environment's own uv, and skips with the reason
otherwise (no environment built, or no unprivileged network namespace on
this machine).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "build" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server_env = _load("server_env")


def _tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python3.13").write_bytes(b"\x7fELF interpreter")
    (root / "bin" / "python3.13").chmod(0o755)
    (root / "bin" / "python3").symlink_to("python3.13")
    (root / "lib" / "python3.13" / "site-packages" / "collimate_server").mkdir(parents=True)
    (root / "lib" / "python3.13" / "site-packages" / "collimate_server" / "main.py").write_text("app = 1\n")
    return root


def _manifest(root: Path, **extra) -> dict:
    manifest = {"schema": server_env.SCHEMA, "target": "linux-x64", "hash": server_env.tree_hash(root), **extra}
    (root / server_env.MANIFEST).write_text(json.dumps(manifest))
    return manifest


def test_the_tree_hash_names_paths_kinds_and_bytes_but_not_times_or_the_manifest(tmp_path):
    root = _tree(tmp_path / "server")
    first = server_env.tree_hash(root)
    os.utime(root / "bin" / "python3.13", (0, 0))
    (root / server_env.MANIFEST).write_text("{}")
    assert server_env.tree_hash(root) == first
    for change in (lambda: (root / "lib" / "extra.txt").write_text("x"),
                   lambda: (root / "bin" / "python3.13").chmod(0o644),
                   lambda: (root / "bin" / "python3").unlink() or (root / "bin" / "python3").symlink_to("python3.14"),
                   lambda: (root / "lib" / "python3.13" / "site-packages" / "collimate_server" / "main.py").write_text("app = 2\n")):
        before = server_env.tree_hash(root)
        change()
        assert server_env.tree_hash(root) != before


def test_a_manifest_is_read_only_while_the_tree_still_hashes_to_it(tmp_path):
    root = _tree(tmp_path / "server")
    manifest = _manifest(root, collimate={"version": "0.1.0"})
    assert server_env.read_manifest(root) == manifest
    (root / "lib" / "stray.py").write_text("")
    with pytest.raises(server_env.Refused, match="hashes to"):
        server_env.read_manifest(root)
    (root / server_env.MANIFEST).write_text("not json")
    with pytest.raises(server_env.Refused, match="no readable"):
        server_env.read_manifest(root)
    (root / server_env.MANIFEST).write_text(json.dumps({"schema": 99}))
    with pytest.raises(server_env.Refused, match="schema"):
        server_env.read_manifest(root)


@pytest.mark.parametrize(("sys_platform", "machine", "target"), [
    ("linux", "x86_64", "linux-x64"), ("linux", "aarch64", "linux-arm64"),
    ("darwin", "arm64", "darwin-arm64"), ("darwin", "x86_64", "darwin-x64")])
def test_each_host_names_its_pinned_target(sys_platform, machine, target):
    assert server_env.host_target(sys_platform, machine) == target
    triple, digest = server_env.STANDALONE[target]
    assert len(digest) == 64 and triple in server_env.STANDALONE_URL.format(
        release=server_env.STANDALONE_RELEASE, version=server_env.PYTHON_VERSION, triple=triple)
    uv_triple, uv_digest = server_env.UV_ARCHIVES[target]
    assert len(uv_digest) == 64 and uv_triple == triple
    assert server_env.UV_URL.format(version=server_env.UV_VERSION, triple=uv_triple) == (
        f"https://github.com/astral-sh/uv/releases/download/{server_env.UV_VERSION}/uv-{triple}.tar.gz")


def test_every_target_with_an_interpreter_carries_a_uv():
    assert set(server_env.UV_ARCHIVES) == set(server_env.STANDALONE)
    assert len({digest for _, digest in server_env.UV_ARCHIVES.values()}) == len(server_env.UV_ARCHIVES)


@pytest.mark.parametrize(("sys_platform", "machine"), [("win32", "AMD64"), ("linux", "riscv64"), ("freebsd", "x86_64")])
def test_a_host_without_a_server_environment_is_refused(sys_platform, machine):
    with pytest.raises(server_env.Refused):
        server_env.host_target(sys_platform, machine)


def test_another_targets_environment_is_refused_before_anything_downloads(tmp_path, monkeypatch):
    wheel = tmp_path / "collimate-0.1.0-cp312-abi3-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(server_env, "host_target", lambda: "linux-x64")
    monkeypatch.setattr(server_env, "fetch_standalone", lambda target: pytest.fail("downloaded"))
    with pytest.raises(server_env.Refused, match="on a darwin-arm64 machine"):
        server_env.build(wheel, tmp_path / "out", "darwin-arm64", "uv")
    with pytest.raises(server_env.Refused, match="not a collimate wheel"):
        server_env.build(tmp_path / "other-1.0-py3-none-any.whl", tmp_path / "out", "linux-x64", "uv")
    assert not (tmp_path / "out").exists()


def test_the_archive_is_unpacked_as_the_tree_and_refused_with_anything_outside_python(tmp_path):
    def archive(names):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
        path = tmp_path / f"archive-{len(names)}.tar.gz"
        path.write_bytes(buffer.getvalue())
        return path

    server_env.unpack(archive(["python/bin/python3.13", "python/lib/libpython3.13.so"]), tmp_path / "tree")
    assert sorted(p.relative_to(tmp_path / "tree").as_posix() for p in (tmp_path / "tree").rglob("*") if p.is_file()) == [
        "bin/python3.13", "lib/libpython3.13.so"]
    with pytest.raises(server_env.Refused, match="outside python/"):
        server_env.unpack(archive(["python/bin/python3", "etc/passwd", "x"]), tmp_path / "other")


def _uv_archive(tmp_path: Path, members: dict[str, bytes], name: str = "uv-x86_64-unknown-linux-gnu.tar.gz") -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for member, body in members.items():
            info = tarfile.TarInfo(member)
            info.size, info.mode = len(body), 0o755
            bundle.addfile(info, io.BytesIO(body))
    path = tmp_path / name
    path.write_bytes(buffer.getvalue())
    return path


def test_the_uv_archive_gives_the_tree_its_uv_alone_executable(tmp_path):
    root = _tree(tmp_path / "server")
    server_env.place_uv(_uv_archive(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": b"\x7fELF uv",
                                               "uv-x86_64-unknown-linux-gnu/uvx": b"\x7fELF uvx"}), root)
    placed = root / server_env.UV_IN_TREE
    assert placed.read_bytes() == b"\x7fELF uv" and os.access(placed, os.X_OK) and not placed.is_symlink()
    assert sorted(p.name for p in (root / "bin").iterdir()) == ["python3", "python3.13", "uv"]


@pytest.mark.parametrize(("members", "says"), [
    ({"uv-x86_64-unknown-linux-gnu/uv": b"x", "uv-x86_64-unknown-linux-gnu/../../bin/sh": b"x"}, "outside"),
    ({"uv-x86_64-unknown-linux-gnu/uv": b"x", "elsewhere/uv": b"x"}, "outside"),
    ({"python/bin/uv": b"x"}, "outside"),
    ({"uv-x86_64-unknown-linux-gnu/uvx": b"x"}, "holds no uv-x86_64-unknown-linux-gnu/uv"),
])
def test_a_uv_archive_of_another_shape_is_refused_placing_nothing(tmp_path, members, says):
    root = _tree(tmp_path / "server")
    with pytest.raises(server_env.Refused, match=says):
        server_env.place_uv(_uv_archive(tmp_path, members), root)
    assert not (root / server_env.UV_IN_TREE).exists()


def test_a_uv_archive_is_fetched_once_against_its_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIMATE_BUILD_CACHE", str(tmp_path / "cache"))
    body = _uv_archive(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": b"x"}).read_bytes()
    fetched = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(url, timeout):
        fetched.append(url)
        return Response(body)

    import hashlib
    monkeypatch.setattr(server_env.urllib.request, "urlopen", urlopen)
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64",
                        ("x86_64-unknown-linux-gnu", hashlib.sha256(body).hexdigest()))
    archive = server_env.fetch_uv("linux-x64")
    assert archive == tmp_path / "cache" / "uv" / "uv-x86_64-unknown-linux-gnu.tar.gz"
    assert server_env.fetch_uv("linux-x64") == archive and fetched == [
        f"https://github.com/astral-sh/uv/releases/download/{server_env.UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz"]
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64", ("x86_64-unknown-linux-gnu", "0" * 64))
    with pytest.raises(server_env.Refused, match="not the pinned"):
        server_env.fetch_uv("linux-x64")
    assert not archive.with_name(archive.name + ".partial").exists()


def _cdn(monkeypatch, answers):
    """``urlopen`` answering from ``answers`` in turn (an exception is raised,
    bytes are the body), and the pauses the retry slept."""
    import email.message
    import urllib.error

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls, pauses = [], []

    def urlopen(url, timeout):
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "Gateway Time-out", email.message.Message(), None)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    monkeypatch.setattr(server_env.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(server_env.time, "sleep", pauses.append)
    return calls, pauses


def test_a_pinned_download_retries_a_passing_failure_of_the_release_cdn_with_a_doubling_pause(tmp_path, monkeypatch):
    import hashlib
    import urllib.error
    monkeypatch.setenv("COLLIMATE_BUILD_CACHE", str(tmp_path / "cache"))
    body = _uv_archive(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": b"x"}).read_bytes()
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64",
                        ("x86_64-unknown-linux-gnu", hashlib.sha256(body).hexdigest()))
    # Two 504s, a dropped connection, then the body: four requests, three pauses.
    calls, pauses = _cdn(monkeypatch, [504, 504, urllib.error.URLError("reset"), body])
    archive = server_env.fetch_uv("linux-x64")
    assert archive.read_bytes() == body
    assert len(calls) == 4 and pauses == [2.0, 4.0, 8.0]


def test_a_pinned_download_gives_up_after_its_attempts_and_never_retries_a_final_answer(tmp_path, monkeypatch):
    import urllib.error
    monkeypatch.setenv("COLLIMATE_BUILD_CACHE", str(tmp_path / "cache"))
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64", ("x86_64-unknown-linux-gnu", "0" * 64))
    calls, pauses = _cdn(monkeypatch, [504] * server_env.DOWNLOAD_ATTEMPTS)
    with pytest.raises(urllib.error.HTTPError):
        server_env.fetch_uv("linux-x64")
    assert len(calls) == server_env.DOWNLOAD_ATTEMPTS and pauses == [2.0, 4.0, 8.0, 16.0]
    # A 404 is the CDN's answer, not a passing failure: one request, no pause.
    calls, pauses = _cdn(monkeypatch, [404])
    with pytest.raises(urllib.error.HTTPError):
        server_env.fetch_uv("linux-x64")
    assert len(calls) == 1 and pauses == []
    assert not list((tmp_path / "cache" / "uv").glob("*.partial"))


def test_with_a_github_token_a_release_asset_is_fetched_through_the_api_as_the_token_holder(tmp_path, monkeypatch):
    import email.message
    import hashlib
    import urllib.error
    monkeypatch.setenv("COLLIMATE_BUILD_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    body = _uv_archive(tmp_path, {"uv-x86_64-unknown-linux-gnu/uv": b"x"}).read_bytes()
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64",
                        ("x86_64-unknown-linux-gnu", hashlib.sha256(body).hexdigest()))
    tag = f"https://api.github.com/repos/astral-sh/uv/releases/tags/{server_env.UV_VERSION}"
    asset = "https://api.github.com/repos/astral-sh/uv/releases/assets/77"
    signed = "https://objects.githubusercontent.com/signed/uv.tar.gz?token=abc"
    direct = server_env.UV_URL.format(version=server_env.UV_VERSION, triple="x86_64-unknown-linux-gnu")
    requests = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def answer(request):
        url = request.full_url if hasattr(request, "full_url") else request
        headers = dict(getattr(request, "headers", {}) or {})
        requests.append((url, headers))
        if url == tag:
            assert headers["Authorization"] == "Bearer ghs_test"
            return Response(json.dumps({"assets": [
                {"name": "uv-aarch64-apple-darwin.tar.gz", "url": "https://api.github.com/x/1"},
                {"name": "uv-x86_64-unknown-linux-gnu.tar.gz", "url": asset}]}).encode())
        if url == asset:
            assert headers["Authorization"] == "Bearer ghs_test" and headers["Accept"] == "application/octet-stream"
            location = email.message.Message()
            location["Location"] = signed
            raise urllib.error.HTTPError(url, 302, "Found", location, None)
        if url in (signed, direct):
            assert "Authorization" not in headers
            return Response(body)
        raise AssertionError(url)

    class Opener:
        def open(self, request, timeout):
            return answer(request)

    monkeypatch.setattr(server_env.urllib.request, "urlopen", lambda request, timeout: answer(request))
    monkeypatch.setattr(server_env.urllib.request, "build_opener", lambda *handlers: Opener())
    archive = server_env.fetch_uv("linux-x64")
    assert archive.read_bytes() == body
    assert [url for url, _ in requests] == [tag, asset, signed]
    # Without the token the link itself is fetched, anonymously.
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setitem(server_env.UV_ARCHIVES, "linux-x64", ("x86_64-unknown-linux-gnu", "0" * 64))
    requests.clear()
    with pytest.raises(server_env.Refused, match="not the pinned"):
        server_env.fetch_uv("linux-x64")
    assert [url for url, _ in requests] == [direct]


def test_a_release_without_the_asset_is_refused_by_name(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(server_env.urllib.request, "urlopen",
                        lambda request, timeout: Response(json.dumps({"assets": []}).encode()))
    with pytest.raises(server_env.Refused, match="has no asset uv-x86_64-unknown-linux-gnu.tar.gz"):
        server_env.github_asset_location(
            server_env.UV_URL.format(version=server_env.UV_VERSION, triple="x86_64-unknown-linux-gnu"), "ghs_test")
    with pytest.raises(server_env.Refused, match="not a GitHub release asset link"):
        server_env.github_asset_location("https://example.com/uv.tar.gz", "ghs_test")


def test_pruning_keeps_the_interpreter_and_drops_scripts_tk_pip_and_bytecode(tmp_path):
    root = _tree(tmp_path / "server")
    lib = root / "lib" / "python3.13"
    for name in ("uvicorn", "pip3", "idle3", "python3-config"):
        (root / "bin" / name).write_text("#!/build/path/bin/python3\n")
    (root / "bin" / "python").symlink_to("python3.13")
    for folder in ("include/python3.13", "share/terminfo", "lib/tcl9.0", "lib/tk9.0", f"{lib.relative_to(root)}/tkinter",
                   f"{lib.relative_to(root)}/site-packages/pip", f"{lib.relative_to(root)}/site-packages/pip-26.dist-info",
                   f"{lib.relative_to(root)}/json/__pycache__"):
        (root / folder).mkdir(parents=True)
        (root / folder / "file").write_text("x")
    (root / "lib" / "libtcl9.0.so").write_text("x")
    (lib / "json" / "__init__.py").write_text("")
    server_env.prune(root)
    assert sorted(p.name for p in (root / "bin").iterdir()) == ["python", "python3", "python3.13"]
    assert not any((root / gone).exists() for gone in ("include", "share", "lib/tcl9.0", "lib/tk9.0", "lib/libtcl9.0.so"))
    assert not (lib / "tkinter").exists() and not (lib / "site-packages" / "pip").exists()
    assert not (lib / "site-packages" / "pip-26.dist-info").exists()
    assert (lib / "json" / "__init__.py").exists() and not (lib / "json" / "__pycache__").exists()


# ---------------------------------------------------------------------------
# build(): on macOS the Mach-O files are signed before the tree is hashed
# ---------------------------------------------------------------------------

SHA = "0123456789ABCDEF0123456789ABCDEF01234567"
NAME = "Developer ID Application: Collimate Contributors (TEAM123456)"


SDK_HOME = "lib/python3.13/site-packages/collimate-run-sdk/collimate"
# The SDK's pinned native resources in a simulated tree: a compiler and a
# verifier payload of the target's and of linux-x64 (which every wheel carries).
PINNED = ("renderer_compilers/{target}/esbuild", "renderer_compilers/linux-x64/esbuild",
          "native_verifier/{target}/_quickjs.abi3.so", "native_verifier/{target}/LICENSE.quickjs-ng",
          "native_verifier/linux-x64/_quickjs.abi3.so")


def _simulated_build(monkeypatch, tmp_path, target):
    """build()'s steps replaced with fakes that make a tree of ``target``'s
    shape (Mach-O files on darwin, ELF on linux, and the SDK's pinned native
    resources, :data:`PINNED`), the SDK's listing of those resources replaced
    by their hashes as installed, and codesign replaced by one that rewrites
    each file it signs, as the real one does; the steps in the order they
    ran."""
    steps = []
    magic = b"\xcf\xfa\xed\xfe" if target.startswith("darwin-") else b"\x7fELF"
    pinned = [f"{SDK_HOME}/" + rel.format(target=target) for rel in PINNED]

    def install(tree, wheel, uv):
        steps.append("install")
        for rel in pinned:
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            body = b"license text" if "LICENSE" in rel else (b"\x7fELF" if "linux-x64" in rel else magic)
            (tree / rel).write_bytes(body + b" " + rel.encode())

    def pinned_resources(tree):
        steps.append("pinned")
        assert not any(b" signed by " in (tree / rel).read_bytes() for rel in pinned)
        return {rel: server_env.sha256(tree / rel) for rel in pinned}

    def unpack(archive, into):
        steps.append("unpack")
        for rel in ("bin/python3.13", "lib/libpython3.13.dylib", "lib/python3.13/lib-dynload/_ssl.so"):
            (into / rel).parent.mkdir(parents=True, exist_ok=True)
            (into / rel).write_bytes(magic + b" binary")
        (into / "bin" / "python3").symlink_to("python3.13")
        (into / "lib" / "python3.13" / "os.py").write_text("sep = '/'\n")

    def place_uv(archive, tree):
        steps.append("place_uv")
        (tree / server_env.UV_IN_TREE).write_bytes(magic + b" uv")

    def codesign(command, check=False, capture_output=False, text=False):
        if command[1] == "--sign":
            steps.append("codesign")
            path = Path(command[-1])
            path.write_bytes(path.read_bytes() + f" signed by {command[2]}".encode())
        return subprocess.CompletedProcess(command, 0, "", "")

    def smoke(tree):
        steps.append("smoke")
        # The smoke run runs the signed binaries.
        if target.startswith("darwin-"):
            assert b" signed by " in (tree / "bin" / "python3.13").read_bytes()

    real_sign_tree = server_env.mac_codesign.sign_tree
    monkeypatch.setattr(server_env, "host_target", lambda: target)
    monkeypatch.setattr(server_env, "fetch_standalone", lambda target: tmp_path / "python.tar.gz")
    monkeypatch.setattr(server_env, "unpack", unpack)
    monkeypatch.setattr(server_env, "install", install)
    monkeypatch.setattr(server_env, "pinned_resources", pinned_resources)
    monkeypatch.setattr(server_env, "prune", lambda tree: steps.append("prune"))
    monkeypatch.setattr(server_env, "compile_bytecode", lambda tree: steps.append("compile"))
    monkeypatch.setattr(server_env, "fetch_uv", lambda target: tmp_path / f"uv-{target}.tar.gz")
    monkeypatch.setattr(server_env, "place_uv", place_uv)
    monkeypatch.setattr(server_env, "smoke", smoke)
    monkeypatch.setattr(server_env.mac_codesign, "sign_tree",
                        lambda tree, signer: real_sign_tree(tree, signer, codesign))
    wheel = tmp_path / "collimate-0.1.1-cp312-abi3-macosx_11_0_arm64.whl"
    wheel.write_bytes(b"wheel")
    return wheel, steps


def test_a_mac_environment_is_signed_before_its_smoke_run_and_hashed_after(tmp_path, monkeypatch):
    """The manifest's hash names the signed bytes, so a copy of the tree (the
    app's Resources/server, which electron-builder leaves alone) re-hashes to
    it; re-signing it afterwards, as electron-builder did on the first mac
    release legs, would not."""
    wheel, steps = _simulated_build(monkeypatch, tmp_path, "darwin-arm64")
    out = tmp_path / "server"
    manifest = server_env.build(wheel, out, "darwin-arm64", "uv", env={})
    assert steps == ["unpack", "install", "prune", "compile", "place_uv", "pinned", *["codesign"] * 6, "smoke"]
    assert manifest["codesign"] == {"identity": "-", "name": None, "hardened_runtime": False}
    assert list(manifest)[-2:] == ["codesign", "hash"]
    assert manifest["hash"] == server_env.tree_hash(out) and server_env.read_manifest(out) == manifest
    for rel in ("bin/python3.13", "bin/uv", "lib/libpython3.13.dylib", "lib/python3.13/lib-dynload/_ssl.so"):
        assert (out / rel).read_bytes().endswith(b" signed by -"), rel
    assert (out / "lib" / "python3.13" / "os.py").read_text() == "sep = '/'\n"
    bundled = tmp_path / "Collimate.app" / "Contents" / "Resources" / "server"
    shutil.copytree(out, bundled, symlinks=True)
    assert server_env.read_manifest(bundled) == manifest
    (bundled / "bin" / "python3.13").write_bytes((bundled / "bin" / "python3.13").read_bytes() + b" re-signed")
    with pytest.raises(server_env.Refused, match="hashes to"):
        server_env.read_manifest(bundled)


def test_a_mac_environment_records_the_signed_bytes_of_each_pinned_native_resource(tmp_path, monkeypatch):
    """codesign rewrites the SDK's Mach-O resources (esbuild, the QuickJS
    extension) after the build checked them against their pins (v0.1.1's mac
    legs: "Native compiler resource hash mismatch" at the packaged server's
    startup); beside each the build records its pin and its signed sha256,
    which the SDK admits on macOS in place of the pin, and the tree's hash
    covers the records."""
    from collimate._host import native_resources

    wheel, steps = _simulated_build(monkeypatch, tmp_path, "darwin-arm64")
    out = tmp_path / "server"
    manifest = server_env.build(wheel, out, "darwin-arm64", "uv", env={})
    sdk = out / SDK_HOME
    compiler = json.loads((sdk / "renderer_compilers/darwin-arm64/codesign.json").read_text())
    payload = json.loads((sdk / "native_verifier/darwin-arm64/codesign.json").read_text())
    # Only the Mach-O files the signing rewrote: no linux file, no license.
    assert sorted(compiler["files"]) == ["esbuild"] and sorted(payload["files"]) == ["_quickjs.abi3.so"]
    assert not (sdk / "renderer_compilers/linux-x64/codesign.json").exists()
    assert not (sdk / "native_verifier/linux-x64/codesign.json").exists()
    for record, rel in ((compiler, "renderer_compilers/darwin-arm64/esbuild"),
                        (payload, "native_verifier/darwin-arm64/_quickjs.abi3.so")):
        assert record["schema"] == 1
        entry = record["files"][Path(rel).name]
        path = sdk / rel
        unsigned = path.read_bytes().removesuffix(b" signed by -")
        assert entry == {"pinned": server_env.hashlib.sha256(unsigned).hexdigest(), "signed": server_env.sha256(path)}
        # The SDK admits the signed bytes on macOS and nowhere else.
        assert native_resources.admitted(path, entry["pinned"], platform="darwin")
        assert not native_resources.admitted(path, entry["pinned"], platform="linux")
    linux = sdk / "renderer_compilers/linux-x64/esbuild"
    assert native_resources.admitted(linux, server_env.sha256(linux), platform="darwin")
    assert manifest["hash"] == server_env.tree_hash(out)
    copy = tmp_path / "copy"
    shutil.copytree(out, copy, symlinks=True)
    (copy / SDK_HOME / "native_verifier/darwin-arm64/codesign.json").unlink()
    with pytest.raises(server_env.Refused, match="hashes to"):
        server_env.read_manifest(copy)


def test_a_pinned_resource_the_signing_did_not_rewrite_is_not_recorded_and_one_it_did_not_sign_is_refused(tmp_path):
    tree = tmp_path / "tree"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "esbuild").write_bytes(b"\xcf\xfa\xed\xfe pinned")
    (tree / "a" / "LICENSE").write_bytes(b"text")
    pins = {rel: server_env.sha256(tree / rel) for rel in ("a/esbuild", "a/LICENSE")}
    assert server_env.record_signatures(tree, pins, ["a/esbuild"]) == []
    assert not (tree / "a" / "codesign.json").exists()
    (tree / "a" / "LICENSE").write_bytes(b"changed")
    with pytest.raises(server_env.Refused, match="a/LICENSE no longer hashes to its pin"):
        server_env.record_signatures(tree, pins, ["a/esbuild"])
    (tree / "a" / "LICENSE").write_bytes(b"text")
    (tree / "a" / "esbuild").write_bytes(b"\xcf\xfa\xed\xfe pinned signed")
    (tree / "a" / "codesign.json").write_text("{}")
    with pytest.raises(server_env.Refused, match="exists before the build wrote it"):
        server_env.record_signatures(tree, pins, ["a/esbuild"])
    (tree / "a" / "codesign.json").unlink()
    assert server_env.record_signatures(tree, pins, ["a/esbuild"]) == ["a/esbuild"]


def test_the_pinned_resources_are_the_trees_sdk_listing_relative_to_the_tree(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    (tree / "sdk").mkdir(parents=True)
    calls = []

    def tree_python(root, args, scratch):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({str(tree / "sdk" / "esbuild"): "ab" * 32}), "")

    monkeypatch.setattr(server_env, "tree_python", tree_python)
    assert server_env.pinned_resources(tree) == {"sdk/esbuild": "ab" * 32}
    assert calls == [["-m", "collimate._host.native_resources", "pinned"]]


def test_a_failing_run_of_the_trees_interpreter_says_what_it_said(tmp_path, monkeypatch):
    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, "", "ValueError: Native compiler resource hash mismatch\n")

    monkeypatch.setattr(server_env.subprocess, "run", failing)
    with pytest.raises(server_env.Refused, match="-m collimate._host.native_resources failed \\(1\\):\n"
                                                 "ValueError: Native compiler resource hash mismatch"):
        server_env.tree_python(tmp_path, ["-m", server_env.NATIVE_RESOURCES, "check"], str(tmp_path))


def test_a_signed_mac_release_signs_its_environment_with_the_developer_id(tmp_path, monkeypatch):
    wheel, steps = _simulated_build(monkeypatch, tmp_path, "darwin-x64")
    used = []

    @contextlib.contextmanager
    def developer_id(env):
        used.append(env)
        yield server_env.mac_codesign.Signer(SHA, NAME)

    monkeypatch.setattr(server_env.mac_codesign, "developer_id", developer_id)
    env = {"MAC_SIGNING": "true", "CSC_LINK": "x"}
    manifest = server_env.build(wheel, tmp_path / "server", "darwin-x64", "uv", release=True, env=env)
    assert used == [env]
    assert manifest["codesign"] == {"identity": SHA, "name": NAME, "hardened_runtime": True}
    assert (tmp_path / "server" / "bin" / "uv").read_bytes().endswith(f" signed by {SHA}".encode())
    # Not a release: ad-hoc, whatever MAC_SIGNING says.
    manifest = server_env.build(wheel, tmp_path / "server", "darwin-x64", "uv", env=env)
    assert manifest["codesign"]["identity"] == "-" and used == [env]


def test_a_linux_environment_is_not_signed_and_its_manifest_names_no_signature(tmp_path, monkeypatch):
    wheel, steps = _simulated_build(monkeypatch, tmp_path, "linux-x64")
    monkeypatch.setattr(server_env.mac_codesign, "sign_tree", lambda tree, signer: pytest.fail("signed"))
    monkeypatch.setattr(server_env, "pinned_resources", lambda tree: pytest.fail("listed for signing"))
    manifest = server_env.build(wheel, tmp_path / "server", "linux-x64", "uv", release=True,
                                env={"MAC_SIGNING": "true"})
    assert steps == ["unpack", "install", "prune", "compile", "place_uv", "smoke"]
    assert "codesign" not in manifest and list(manifest) == [
        "schema", "target", "python", "collimate", "uv", "hash"]
    assert not list((tmp_path / "server").rglob("codesign.json"))


def test_a_malformed_mac_signing_flag_stops_a_mac_build_before_anything_downloads(tmp_path, monkeypatch):
    wheel, steps = _simulated_build(monkeypatch, tmp_path, "darwin-arm64")
    monkeypatch.setattr(server_env, "fetch_standalone", lambda target: pytest.fail("downloaded"))
    with pytest.raises(server_env.mac_codesign.Refused, match="MAC_SIGNING"):
        server_env.build(wheel, tmp_path / "server", "darwin-arm64", "uv", release=True, env={"MAC_SIGNING": "1"})
    assert steps == [] and not (tmp_path / "server").exists()


def _built() -> Path | None:
    named = os.environ.get("COLLIMATE_SERVER_ENV")
    candidate = Path(named) if named else ROOT / "dist" / "desktop" / "server"
    return candidate if (candidate / server_env.MANIFEST).is_file() else None


def test_the_built_environment_boots_and_runs_generators_on_its_own_python_with_no_network(tmp_path):
    """The offline check (one fetch before it: the dependency probe's
    dependencies, cached for the environment's interpreter) on a built
    environment: both probes run by its uv on its Python, the SDK alone on
    their PYTHONPATH, the dependency probe's pydantic its own."""
    server = _built()
    if server is None:
        pytest.skip("no server environment built here (bash build/build_desktop.sh writes dist/desktop/server, "
                    "or name one with COLLIMATE_SERVER_ENV)")
    out = tmp_path / "result.json"
    result = subprocess.run([sys.executable, str(ROOT / "build" / "check_server_env.py"), str(server),
                             "--offline", "--json", str(out)], capture_output=True, text=True, timeout=600)
    if result.returncode == 3:
        pytest.skip(f"no network namespace here: {result.stderr.strip()}")
    assert result.returncode == 0, result.stdout + result.stderr
    checked = json.loads(out.read_text())
    assert checked["offline"] is True and checked["outside"] in {"ENETUNREACH", "EHOSTUNREACH", "ECONNREFUSED"}
    root = server.resolve()
    uv = root / server_env.UV_IN_TREE
    assert checked["command"][0] == str(root / "bin" / "python3")
    assert checked["command"][1:5] == ["-I", "-B", "-m", "collimate_server"]
    assert checked["command"][-2:] == ["--server-env", str(root)]
    assert checked["ready"]["status"] == "ok" and checked["ready"]["checks"]["bundled"] == "ok"
    assert checked["health"]["version"] == checked["manifest"]["collimate"]["version"]
    assert checked["manifest"]["uv"]["version"] == server_env.UV_VERSION
    for key, output in (("run", "ran-with.json"), ("own_dependencies", "own-deps.json")):
        run = checked[key]
        assert run["status"] == "completed" and Path(run["uv"]).resolve() == uv.resolve()
        assert run["file"].rsplit("/", 1)[-1] == output
        assert Path(run["python_home"]) == root.resolve() and run["python"] == checked["manifest"]["python"]["version"]
        [entry] = run["pythonpath"]
        assert entry["holds"] == ["collimate", "gen"] and Path(entry["path"]).resolve().is_relative_to(root)
    own = checked["own_dependencies"]
    assert own["pydantic"]["version"] != own["server_pydantic"]
    for module in ("pydantic", "pydantic_core"):
        assert not Path(own[module]["file"]).is_relative_to(root)


FAILING_SERVER = '''
import json, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])


class Handler(BaseHTTPRequestHandler):
    def _answer(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        self._answer(200, json.dumps({"status": "ok", "version": "0"}).encode(), "application/json")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._answer(500, b"Internal Server Error", "text/plain")
        # As uvicorn: the route's traceback is logged after the 500 went out.
        time.sleep(0.5)
        sys.stderr.write("Traceback (most recent call last):\\nValueError: the route failed\\n")
        sys.stderr.flush()


server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
# Request threads are joined at close, as uvicorn's shutdown waits for its
# tasks: ThreadingHTTPServer's default daemon threads are not tracked, so
# server_close would return at once and the process exit could cut the
# handler off before it logged (it did, once, on a hosted runner).
server.daemon_threads = False
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
server.serve_forever()
server.server_close()
'''


def test_a_failing_call_reports_its_answer_and_the_whole_server_log_written_after_it(tmp_path):
    """v0.1.1's mac legs: the probe read the server log on the 500, before
    uvicorn had logged the route's traceback, so the log showed none. The
    log is read once the server has stopped, and the answer's body is named."""
    check = _load("check_server_env")
    server = tmp_path / "server"
    (server / "bin").mkdir(parents=True)
    (tmp_path / "failing_server.py").write_text(FAILING_SERVER)
    (server / "bin" / "python3").write_text(f"#!/bin/sh\nexec {sys.executable} {tmp_path / 'failing_server.py'} \"$@\"\n")
    (server / "bin" / "python3").chmod(0o755)
    root = tmp_path / "scratch"
    root.mkdir()
    with pytest.raises(AssertionError) as failed:
        check.boot(server, {}, root)
    said = str(failed.value)
    assert said.startswith("POST /api/channels?name=Run%20probe: 500 Internal Server Error, "
                           "answering 'Internal Server Error'")
    log = said.split("--- server log (all of it, read after the server stopped) ---\n", 1)[1]
    assert "ValueError: the route failed" in log
