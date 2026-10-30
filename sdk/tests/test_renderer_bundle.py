"""The production renderer package hash and pinned build pipeline."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from collimate._host.renderer_bundle import CONTRACT
from tests._workspace import BUILTINS, RENDERERS


ROOT = Path(__file__).resolve().parents[1]


def digest_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def run_build(root: Path, output: Path, builtin: Path | None = None):
    """Build one index; a synthetic renderers root pairs with an empty
    built-in root beside it."""
    if builtin is None:
        builtin = root.parent / 'builtin'
        builtin.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, '-m', 'collimate._host.renderer_bundle',
         '--build', str(root), '--builtin', str(builtin), '--out', str(output)],
        capture_output=True, text=True, timeout=60,
    )


def package(root: Path, name='example') -> Path:
    folder = root / 'cell' / name
    folder.mkdir(parents=True)
    (folder / 'example.renderer').write_text('id: example\nentrypoint: main.tsx\n')
    (folder / 'main.tsx').write_text(
        "import {defineRenderer} from '/api/sdk/react.js';\n"
        "import {text} from './inner';\nexport default defineRenderer({component:()=>text});\n"
    )
    (folder / 'inner.ts').write_text("export const text = 'first';\n")
    return folder


def test_exact_compiler_pin_and_committable_lock__F1868_F1975():
    pkg = json.loads((ROOT / 'scripts/package.json').read_text())
    lock = json.loads((ROOT / 'scripts/package-lock.json').read_text())
    assert pkg['dependencies']['esbuild'] == CONTRACT['esbuild_version']
    assert lock['packages']['']['dependencies']['esbuild'] == CONTRACT['esbuild_version']
    assert 'scripts/package-lock.json' not in (ROOT / '.gitignore').read_text().splitlines()


def test_source_hash_is_full_relative_content_identity__F0251_F0295_F1868():
    from collimate._host.renderer_bundle import renderer_source_hash

    files = {'main.tsx': digest('hello\r\n'), '\U00010000.ts': digest('one'), '\ue000.ts': digest('two')}
    value = renderer_source_hash(files, entrypoint='main.tsx')
    assert len(value) == 64
    assert value == renderer_source_hash(dict(reversed(list(files.items()))), entrypoint='main.tsx')
    assert value != renderer_source_hash(files | {'main.tsx': digest('hello\n')}, entrypoint='main.tsx')
    assert value != renderer_source_hash(files | {'asset.bin': digest('binary')}, entrypoint='main.tsx')
    for path in ('../escape', '/root', 'a//b', 'a/./b', 'a\\b', 'a\x00b', '_runs/file', 'nested/_runs/file'):
        with pytest.raises(ValueError):
            renderer_source_hash(files | {path: digest('x')}, entrypoint='main.tsx')


def test_build_bundles_siblings_preserves_authored_js_and_python_hash__F0251_F1868(tmp_path):
    from collimate._host.renderer_bundle import renderer_source_hash

    root, output = tmp_path / 'renderers', tmp_path / 'renderer_bundles'
    folder = package(root)
    (folder / 'main.js').write_text('// authored companion\n')
    (folder / 'inner.ts').write_bytes(b"export const text = 'first';\r\n")
    for name in ('\U00010000.txt', '\ue000.txt'):
        (folder / name).write_text(name)
    (folder / 'asset.bin').write_bytes(b'\xff\x00')
    result = run_build(root, output)
    assert result.returncode == 0, result.stderr
    index = json.loads((output / 'index.json').read_text())
    assert index['schema'] == 1 and index['compiler'] == CONTRACT['esbuild_version']
    [entry] = index['entries']
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    assert entry['source_hash'] == renderer_source_hash(hashes, entrypoint='main.tsx')
    js = (output / entry['file']).read_text()
    assert "./inner" not in js and 'first' in js and '/api/sdk/react.js' in js
    assert hashlib.sha256(js.encode()).hexdigest() == entry['bundle_hash']
    assert (folder / 'main.js').read_text() == '// authored companion\n'
    (folder / 'inner.ts').write_text("export const text = 'changed';\n")
    assert run_build(root, output).returncode == 0
    [changed] = json.loads((output / 'index.json').read_text())['entries']
    assert changed['source_hash'] != entry['source_hash']
    assert changed['bundle_hash'] != entry['bundle_hash']


def test_failed_build_does_not_publish_partial_index__F1868(tmp_path):
    root, output = tmp_path / 'renderers', tmp_path / 'renderer_bundles'
    folder = package(root)
    result = run_build(root, output)
    assert result.returncode == 0, result.stderr
    before = (output / 'index.json').read_bytes()
    (folder / 'inner.ts').write_text('export const text = <broken')
    assert run_build(root, output).returncode != 0
    assert (output / 'index.json').read_bytes() == before


def test_runs_import_is_refused_by_actual_native_bundle__P0F21(tmp_path):
    folder = package(tmp_path)
    (folder / '_runs').mkdir()
    (folder / '_runs/private.ts').write_text("export const text = 'not package code';")
    (folder / 'main.tsx').write_text(
        "import {defineRenderer} from '/api/sdk/react.js';\n"
        "import {text} from './_runs/private';\nexport default defineRenderer({component:()=>text});"
    )
    from collimate._host.renderer_bundle import bundle_renderer, RendererCompileError
    with pytest.raises(RendererCompileError, match='_runs'):
        bundle_renderer({'main.tsx': (folder/'main.tsx').read_bytes()}, entrypoint='main.tsx', deadline=time.monotonic()+5)


@pytest.mark.parametrize('specifier', ['../other/secret', '/tmp/private', 'react', './_runs/private', 'bad/api/sdk/react.js'])
def test_real_build_cannot_resolve_outside_package__P0F21(tmp_path, specifier):
    root, output = tmp_path / 'renderers', tmp_path / 'bundles'
    folder = package(root)
    other = root / 'cell/other'
    other.mkdir()
    (other / 'secret.ts').write_text("export const text='SECRET_NOT_IN_OUTPUT';")
    (folder / 'main.tsx').write_text(f"import {{text}} from {specifier!r}; export default text;")
    result = run_build(root, output)
    assert result.returncode != 0
    assert not (output / 'index.json').exists()
    assert 'SECRET_NOT_IN_OUTPUT' not in result.stderr


def test_build_ignores_host_config_and_is_root_independent__F1868_P0F21(tmp_path):
    import shutil

    root, output = tmp_path / 'first', tmp_path / 'first-output'
    folder = package(root)
    (folder / 'settings.json').write_text('{"value": "kept"}')
    (folder / 'inner.ts').write_text("import config from './settings.json'; export const text = config.value;")
    (folder / 'tsconfig.json').write_text('{"extends":"../../../secret"}')
    (tmp_path / 'secret').write_text('HOST_SECRET_NOT_READ')
    (folder / '_runs').mkdir()
    (folder / '_runs/log.txt').write_text('not source')
    result = run_build(root, output)
    assert result.returncode == 0, result.stderr
    copied, copied_output = tmp_path / 'elsewhere', tmp_path / 'second-output'
    shutil.copytree(root, copied)
    (copied / 'cell/example/_runs/log.txt').write_text('a later log')
    assert run_build(copied, copied_output).returncode == 0
    assert (output / 'index.json').read_bytes() == (copied_output / 'index.json').read_bytes()
    [entry] = json.loads((output / 'index.json').read_text())['entries']
    assert (output / entry['file']).read_bytes() == (copied_output / entry['file']).read_bytes()
    assert 'HOST_SECRET_NOT_READ' not in result.stderr


def test_symlink_collection_does_not_follow_host_file__P0F21(tmp_path):
    root, output = tmp_path / 'renderers', tmp_path / 'bundles'
    folder = package(root)
    secret = tmp_path / 'outside.ts'
    secret.write_text('SECRET_NOT_IN_OUTPUT')
    (folder / 'inner.ts').unlink()
    (folder / 'inner.ts').symlink_to(secret)
    result = run_build(root, output)
    assert result.returncode != 0
    assert 'SECRET_NOT_IN_OUTPUT' not in result.stderr
    assert not output.exists()


def test_all_19_shipped_renderers_produce_prefill_artifacts__F1868__D14(tmp_path):
    result = run_build(RENDERERS, tmp_path / 'bundles', BUILTINS)
    assert result.returncode == 0, result.stderr
    index = json.loads((tmp_path / 'bundles/index.json').read_text())
    from collimate._host.renderer_bundle import _build_requests, renderer_source_hash
    renderer_hashes = set()
    requests = _build_requests(RENDERERS) + _build_requests(BUILTINS)
    for request in requests:
        directory = Path(request['directory'])
        bodies = {p.relative_to(directory).as_posix():digest_bytes(p.read_bytes()) for p in directory.rglob('*') if p.is_file() and '_runs' not in p.relative_to(directory).parts}
        renderer_hashes.add(renderer_source_hash(bodies, entrypoint=request['entrypoint']))
    entries = [entry for entry in index['entries'] if entry['source_hash'] in renderer_hashes]
    assert len(entries) == 19
    assert len({entry['source_hash'] for entry in entries}) == 19
    # One index: the 19 renderer bundles and the six built-in handlers'
    # verifier bundles (the metadata-only llm-summary handler has none).
    assert len(index['entries']) == 25
    for entry in entries:
        code = (tmp_path / 'bundles' / entry['file']).read_bytes()
        assert hashlib.sha256(code).hexdigest() == entry['bundle_hash']
        assert b'/api/sdk/' in code


def test_theme_contract_ships_in_the_runtime_and_the_roster_stays_19_R06():
    """The renderer theme contract (plan 1006, W2) changed no roster: the SDK
    still ships 19 renderers, and the served runtime module exports every
    theme member the renderers import."""
    from collimate._host.renderer_bundle import _build_requests
    requests = _build_requests(RENDERERS) + _build_requests(BUILTINS)
    assert len(requests) == 19
    assert len({Path(r['directory']).resolve() for r in requests}) == 19
    runtime = (ROOT / 'shared/renderer-runtime/react.js').read_text()
    exported = runtime.rsplit('export {', 1)[1].split('};', 1)[0]
    names = {name.strip() for name in exported.split(',') if name.strip()}
    assert {'useTheme', 'useEditing', 'tokens', 'THEME_VAR_NAMES', 'themeCss', 'resolveVar', 'toHex8'} <= names


@pytest.mark.parametrize('link_root', [False, True])
def test_discovery_refuses_symlink_before_reading_outside_manifest__P0F21(tmp_path, monkeypatch, link_root):
    from collimate._host.renderer_bundle import _build_requests

    outside = tmp_path / 'outside'
    package(outside)
    manifest = outside / 'cell/cell.yaml'
    manifest.write_text('name: Outside\n')
    root = tmp_path / 'renderers'
    if link_root:
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / 'linked-cell').symlink_to(outside / 'cell', target_is_directory=True)
    reads = []
    original = Path.read_text

    def observe(path, *args, **kwargs):
        if path.resolve() == manifest:
            reads.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', observe)
    refused = False
    try:
        _build_requests(root)
    except ValueError:
        refused = True
    assert reads == [], f'discovery read an outside cell manifest: {reads}'
    assert refused, 'symlink root/cell must fail before layout discovery'


@pytest.mark.parametrize('entrypoint', [False, True])
def test_typescript_angle_assertions_compile_as_ts_in_build_and_native__F1868_F1280(tmp_path, entrypoint):
    root, output = tmp_path / 'renderers', tmp_path / 'bundles'
    folder = package(root)
    if entrypoint:
        (folder / 'example.renderer').write_text('id: example\nentrypoint: main.ts\n')
        (folder / 'main.tsx').unlink()
        (folder / 'main.ts').write_text(
            "import {defineRenderer} from '/api/sdk/react.js';\n"
            "const value: unknown = 7; const typed = <number>value;\n"
            "export default defineRenderer({component:()=>typed});\n"
        )
    else:
        (folder / 'inner.ts').write_text("const value: unknown = 'typed'; export const text = <string>value;\n")
    result = run_build(root, output)
    assert result.returncode == 0, result.stderr
    from collimate._host.renderer_bundle import bundle_renderer
    entry = 'main.ts' if entrypoint else 'main.tsx'
    native = bundle_renderer({p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}, entrypoint=entry, deadline=time.monotonic()+5)
    assert native.code and native.entrypoint_js


def test_cli_requires_the_builtin_root(tmp_path):
    root = tmp_path / 'renderers'
    package(root)
    result = subprocess.run(
        [sys.executable, '-m', 'collimate._host.renderer_bundle', '--build', str(root), '--out', str(tmp_path / 'out')],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0 and '--builtin' in result.stderr
    assert not (tmp_path / 'out').exists()


def test_renderers_root_refuses_a_handler_on_a_reserved_type(tmp_path):
    """A seedable handler may never take a type the app's built-in handlers
    reserve; only the built-in root may."""
    root, output = tmp_path / 'renderers', tmp_path / 'bundles'
    package(root)
    handler = root / 'cell' / 'note'
    handler.mkdir()
    (handler / 'note.handler').write_text(
        'id: note\nname: Note\ndescription: x\nguidance: x\ntypes: [{ext: .generator}]\nexamples: []\nderived: x\n')
    result = run_build(root, output)
    assert result.returncode != 0 and 'reserved for the app' in result.stderr
    assert not output.exists()


@pytest.mark.parametrize('derived_line, builds', [('', True), ('derived: ' + 'a' * 64 + '\n', False)])
def test_renderers_root_skips_a_metadata_only_handler(tmp_path, derived_line, builds):
    """A handler with neither a ``derived:`` line nor a ``derived/`` folder
    is metadata only (types and render_as rules): it ships no verifier
    bundle and the build goes on. One with a ``derived:`` line but no
    folder is still refused."""
    root, output = tmp_path / 'renderers', tmp_path / 'bundles'
    package(root)
    handler = root / 'cell' / 'note'
    handler.mkdir()
    (handler / 'note.handler').write_text(
        'id: note\nname: Note\ndescription: x\nguidance: x\n'
        'types: [{ext: .note, render_as: .md}]\nexamples: []\n' + derived_line)
    result = run_build(root, output)
    if builds:
        assert result.returncode == 0, result.stderr
        assert len(json.loads((output / 'index.json').read_text())['entries']) == 1
    else:
        assert result.returncode != 0
        assert "has no matching accepted derived folder" in result.stderr
        assert not output.exists()


def test_both_roots_build_into_one_index(tmp_path):
    from collimate._host.renderer_bundle import _build_requests
    root, builtin, output = tmp_path / 'renderers', tmp_path / 'builtin', tmp_path / 'bundles'
    package(root)
    (package(builtin, 'core') / 'inner.ts').write_text("export const text = 'built in';\n")
    result = run_build(root, output, builtin)
    assert result.returncode == 0, result.stderr
    assert len(json.loads((output / 'index.json').read_text())['entries']) == 2
    assert len(_build_requests(builtin)) == 1
