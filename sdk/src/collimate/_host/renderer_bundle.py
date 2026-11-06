# SPDX-License-Identifier: MIT
"""Authoritative renderer compilation identity and build-time tooling.

Paths are relative to one discovered package. Digests describe exact bytes:
UTF-8 text is not newline-normalized. Generated bundles live outside packages.
Node is needed by the build CLI, never by the source-serving route.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import copy
import time
import hashlib
import json
import math
import os
import tempfile
import re
import subprocess
from pathlib import Path
from typing import Mapping

CONTRACT = json.loads((Path(__file__).parent.parent / "renderer_bundle_contract.json").read_text())


def validate_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or re.match(r"^[A-Za-z]:", value)
            or any(p in ("", ".", "..", "_runs") for p in value.split("/"))):
        raise ValueError(f"Invalid renderer package path: {value!r}")
    return value


def renderer_source_hash(files: Mapping[str, str], *, entrypoint: str) -> str:
    """Hash a package-relative map of SHA-256 digests under this compiler contract."""
    validate_path(entrypoint)
    if entrypoint not in files:
        raise ValueError("Renderer entrypoint is absent from the package")
    for name, digest in files.items():
        validate_path(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"Invalid content digest for {name!r}")
    h = hashlib.sha256()

    def add(value: bytes) -> None:
        h.update(len(value).to_bytes(4, "big"))
        h.update(value)

    add(b"collimate-renderer-bundle-v1")
    add(json.dumps(CONTRACT, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    add(entrypoint.encode())
    for name in sorted(files, key=lambda p: p.encode()):
        add(name.encode())
        add(bytes.fromhex(files[name]))
    return h.hexdigest()



@dataclass(frozen=True)
class RendererBundle:
    source_hash: str
    code: str
    bundle_hash: str
    entrypoint_js: str


@dataclass(frozen=True)
class VerifierBundle:
    source_hash: str
    code: str
    bundle_hash: str


class RendererCompileError(ValueError):
    """Native compilation refused, failed or exceeded its bounded budget."""


def _native(files, entrypoint, policy, deadline, own_process_group, *, verifier=False):
    from .._esbuild_service import compile_map, MAX_BYTES, MAX_ITEMS, ServiceError
    from .._renderer_compilers import compiler_path
    end = time.monotonic() + 5
    if deadline is not None:
        if not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
            raise RendererCompileError('Compiler deadline must be finite')
        end = min(end, deadline)
    try:
        if len(files) > MAX_ITEMS or any(not isinstance(raw, bytes) for raw in files.values()) or sum(len(raw) for raw in files.values()) > MAX_BYTES:
            raise RendererCompileError('Renderer source bytes exceed compiler input limits or have invalid types')
        snapshot = dict(files)
        for path in snapshot:
            validate_path(path).encode('utf-8')
        if entrypoint not in snapshot:
            raise RendererCompileError('Renderer entrypoint is absent from the package')
        if time.monotonic() >= end:
            raise RendererCompileError('Native compiler deadline exceeded')
        try:
            executable = compiler_path(policy['esbuild_version'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RendererCompileError('Native compiler resources unavailable or invalid: ' + type(exc).__name__) from exc
        return compile_map(executable, policy['esbuild_version'], snapshot, entrypoint, policy, end, own_process_group, verifier=verifier)
    except (ServiceError, OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, RendererCompileError):
            raise
        message = ('Native compiler transport failed: ' + type(exc).__name__) if isinstance(exc, (OSError, TypeError, KeyError)) else str(exc)[:4096]
        raise RendererCompileError(message) from exc


def bundle_renderer(files: Mapping[str, bytes], *, entrypoint: str,
                    deadline: float | None = None, _own_process_group: bool = True) -> RendererBundle:
    """Compile a virtual renderer package with the bundled native executable."""
    from .._esbuild_service import MAX_BYTES, MAX_ITEMS
    started = time.monotonic()
    if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise RendererCompileError('Compiler deadline must be finite')
    try:
        if not isinstance(files, Mapping) or len(files) > MAX_ITEMS or any(not isinstance(b, bytes) for b in files.values()) or sum(map(len, files.values())) > MAX_BYTES:
            raise RendererCompileError('Renderer source bytes exceed compiler input limits or have invalid types')
        snapshot = dict(files)
        source = renderer_source_hash({p: hashlib.sha256(b).hexdigest() for p, b in snapshot.items()}, entrypoint=entrypoint)
    except (ValueError, TypeError) as exc:
        raise RendererCompileError(str(exc)) from exc
    code, transformed = _native(snapshot, entrypoint, copy.deepcopy(CONTRACT), min(started + 5, deadline) if deadline is not None else started + 5, _own_process_group)
    return RendererBundle(source, code, hashlib.sha256(code.encode()).hexdigest(), transformed)


def _verifier_policy():
    policy = copy.deepcopy(CONTRACT)
    policy['options']['format'] = 'iife'
    policy['global_name'] = '__collimate_verifier'
    policy['external_prefixes'] = []
    policy['imports'] = ['collimate/verify']
    return policy


def verifier_source_hash(source: bytes, *, prelude: bytes) -> str:
    """Identity includes exact verifier/prelude bytes and the separate profile."""
    if not isinstance(source, bytes) or not isinstance(prelude, bytes):
        raise RendererCompileError('Verifier and prelude must be bytes')
    h = hashlib.sha256()
    for value in (b'collimate-verifier-bundle-v1', json.dumps(_verifier_policy(), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode(),
                  b'verify.ts', hashlib.sha256(source).digest(), b'prelude.js', hashlib.sha256(prelude).digest()):
        h.update(len(value).to_bytes(4, 'big'))
        h.update(value)
    return h.hexdigest()


def compile_verifier(source: bytes, *, prelude: bytes, deadline: float | None = None,
                     _own_process_group: bool = True) -> VerifierBundle:
    """Compile a verifier importing only the supplied canonical prelude module."""
    from .._esbuild_service import MAX_BYTES
    started = time.monotonic()
    if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
        raise RendererCompileError('Compiler deadline must be finite')
    if not isinstance(source, bytes) or not isinstance(prelude, bytes) or len(source) + len(prelude) > MAX_BYTES:
        raise RendererCompileError('Verifier source bytes exceed compiler input limits or have invalid types')
    identity = verifier_source_hash(source, prelude=prelude)
    code, _ = _native({'verify.ts': source, 'prelude.js': prelude}, 'verify.ts', _verifier_policy(), min(started + 5, deadline) if deadline is not None else started + 5, _own_process_group, verifier=True)
    return VerifierBundle(identity, code, hashlib.sha256(code.encode()).hexdigest())


def tool_dir() -> Path:
    """Installed SDK tools or the editable checkout's scripts directory."""
    installed = Path(__file__).parent.parent / "renderer_tools"
    if installed.is_dir():
        return installed
    checkout = Path(__file__).resolve().parents[3] / "scripts"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("SDK renderer build tools are missing")


def _build_requests(root: Path) -> list[dict]:
    """The renderer packages of one asset tree."""
    import yaml
    from ..definitions import discover_definitions
    from .packaging.layout import scan_cells

    # Layout discovery reads cell.yaml before yielding cells. Validate the
    # root and every descendant first, without following directory symlinks,
    # so even a symlinked cell/manifest cannot cause an outside read.
    if root.is_symlink():
        raise ValueError(f"Symlink renderer root is not allowed: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink outside the renderer's folder is not allowed: {path}")
    requests = []
    for cell in scan_cells(root):
        paths = []
        # Never traverse a symlink while discovering a package.
        for p in cell.path.rglob("*"):
            if p.is_symlink():
                raise ValueError(f"Symlink outside the renderer's folder is not allowed: {p}")
            rel = p.relative_to(cell.path)
            if p.is_file() and "__pycache__" not in rel.parts and p.suffix not in (".pyc", ".pyo"):
                paths.append(rel.as_posix())
        found = discover_definitions(paths)
        if found.problems:
            raise ValueError("; ".join(p.message for p in found.problems))
        for package in found.packages:
            if package.kind != "renderer":
                continue
            definition = yaml.safe_load((cell.path / package.definition_path).read_bytes())
            if not isinstance(definition, dict) or not isinstance(definition.get("entrypoint"), str):
                raise ValueError(f"Renderer has no entrypoint: {package.definition_path}")
            requests.append({"directory": str((cell.path / package.prefix).resolve()),
                             "entrypoint": validate_path(definition["entrypoint"])})
    return requests


def _handler_bundles(root: Path, *, builtin: bool = False) -> list[VerifierBundle]:
    """Validate one tree's handlers' accepted derived folders before
    publishing any index. Only the ``builtin`` tree's handlers may declare
    the reserved types. A handler with neither a ``derived:`` line nor a
    ``derived/`` folder is metadata only (types and render_as rules, no
    checks) and ships no verifier bundle; one with only one of the two is
    refused."""
    import yaml
    from ..definitions import discover_definitions
    from ..handlers import derived_hash, validate_handler
    from .packaging.layout import scan_cells
    from ..shared import shared_path

    bundles = []
    prelude = None
    for cell in scan_cells(root):
        paths = [p.relative_to(cell.path).as_posix() for p in cell.path.rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')]
        discovered = discover_definitions(paths)
        if discovered.problems:
            raise ValueError('; '.join(problem.message for problem in discovered.problems))
        for package in discovered.packages:
            if package.kind != 'handler':
                continue
            manifest = validate_handler(yaml.safe_load((cell.path / package.definition_path).read_bytes()), allow_reserved=builtin)
            prefix = package.prefix + '/derived/'
            files = {path[len(prefix):]: (cell.path / path).read_bytes() for path in paths
                     if path.startswith(prefix) and '_runs' not in path.split('/')}
            if not files and manifest.get('derived') is None:
                continue
            if not files or manifest.get('derived') != derived_hash(files):
                raise ValueError(f'Handler {manifest["id"]!r} has no matching accepted derived folder')
            source = files.get('verify.ts')
            if source is None:
                raise ValueError(f'Handler {manifest["id"]!r} has no derived/verify.ts')
            if prelude is None:
                prelude = shared_path('verify', 'prelude.js').read_bytes()
            bundles.append(compile_verifier(source, prelude=prelude))
    return bundles


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.bundle-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_cache(renderers: Path, builtin: Path, output: Path) -> None:
    """One index for both trees: their renderer bundles and their handlers'
    verifier bundles."""
    # Preflights all roots/paths before any reads.
    requests = _build_requests(renderers) + _build_requests(builtin)
    if not requests:
        raise ValueError("No renderer packages found")
    handlers = _handler_bundles(renderers) + _handler_bundles(builtin, builtin=True)
    # Keep a previous complete cache index intact if any graph cannot compile.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.renderer-build-') as folder:
        staged = Path(folder)
        subprocess.run(['node', str(tool_dir() / 'renderer_bundle.mjs'), '--out', str(staged)],
                       input=json.dumps(requests), text=True, check=True, timeout=120)
        index = json.loads((staged / 'index.json').read_text())
        entries = {entry['source_hash']: entry for entry in index['entries']}
        for bundle in handlers:
            filename = bundle.bundle_hash + '.js'
            (staged / filename).write_bytes(bundle.code.encode('utf-8'))
            entries[bundle.source_hash] = {'source_hash': bundle.source_hash, 'bundle_hash': bundle.bundle_hash, 'file': filename}
        index['entries'] = sorted(entries.values(), key=lambda entry: entry['source_hash'])
        output.mkdir(parents=True, exist_ok=True)
        for entry in index['entries']:
            _atomic_write(output / entry['file'], (staged / entry['file']).read_bytes())
        _atomic_write(output / 'index.json', (json.dumps(index, indent=2) + '\n').encode('utf-8'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-dir", action="store_true")
    parser.add_argument("--build", type=Path, help="the renderers tree")
    parser.add_argument("--builtin", type=Path, help="the built-in tree (renderers and handlers)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.tool_dir:
        print(tool_dir())
        return
    if not args.build or not args.builtin or not args.out:
        parser.error("--build, --builtin and --out are required")
    _build_cache(args.build, args.builtin, args.out.resolve())


if __name__ == "__main__":
    main()
