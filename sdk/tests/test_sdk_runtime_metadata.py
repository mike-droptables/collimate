"""P4F22: copied SDK source carries its exact canonical distribution metadata."""
import email.parser
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from tests._workspace import HOST_BASE_IMAGE, host_base_image


SDK = Path(__file__).resolve().parents[1]
ROOT = SDK.parent
RESOURCE = ROOT / 'sdk/src/collimate/_sdk_metadata/METADATA'


def source_only_packages(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    for name in ('collimate', 'gen'):
        shutil.copytree(SDK / 'src' / name, source / name, ignore=ignore)
    # Only the SDK packages' own requirement (SDK_REQUIREMENTS) is copied.
    for name in ('yaml',):
        spec = importlib.util.find_spec(name)
        assert spec and spec.origin
        origin = Path(spec.origin)
        if spec.submodule_search_locations:
            shutil.copytree(origin.parent, source / name, ignore=ignore)
        else:
            shutil.copyfile(origin, source / origin.name)
    return source


def test_sdk_runtime_metadata_matches_canonical_project_P4F22():
    from packaging.requirements import Requirement
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    metadata = email.parser.Parser().parsestr(RESOURCE.read_text())
    assert metadata['Name'] == project['name']
    assert metadata['Version'] == project['version']
    assert metadata['Requires-Python'] == project['requires-python']
    requirements = [Requirement(value) for value in metadata.get_all('Requires-Dist', [])]
    core = [value for value in requirements if value.marker is None or value.marker.evaluate({'extra': ''})]
    assert {str(value) for value in core} == {str(Requirement(value)) for value in project['dependencies']}
    assert not metadata.get_all('Provides-Extra', [])
    assert 'optional-dependencies' not in project
    assert metadata.get_payload() == (ROOT / project['readme']).read_text()


def test_source_only_worker_stages_exact_sdk_metadata_and_dependencies_P4F22(tmp_path):
    """Real isolated Python; source/deps copied, no installed SDK distribution."""
    # Runtime imports need these installed dependencies; copying only their
    # packages leaves the child independent of the parent's SDK dist-info/.pth.
    source = source_only_packages(tmp_path)
    worker = r'''
import email.parser, importlib.metadata, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    importlib.metadata.distribution('collimate')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('The isolated source-only worker unexpectedly has installed SDK metadata')
import importlib.util
import gen
spec = importlib.util.spec_from_file_location('host_base_image', sys.argv[3])
base = importlib.util.module_from_spec(spec); sys.modules['host_base_image'] = base; spec.loader.exec_module(base)
staged = Path(base.stage(sys.argv[2]).context)
assert callable(gen.main)
metadata_files = list((staged / 'sdk').glob('collimate-*.dist-info/METADATA'))
assert len(metadata_files) == 1
metadata = metadata_files[0].read_bytes()
assert metadata == (Path(sys.argv[1]) / 'collimate/_sdk_metadata/METADATA').read_bytes()
headers = email.parser.Parser().parsestr(metadata.decode())
from collimate.shared import SDK_REQUIREMENTS
requirements = list(SDK_REQUIREMENTS)
assert (staged / 'sdk-requirements.txt').read_text() == '\n'.join(requirements) + '\n'
assert not [p for p in staged.rglob('*') if 'collimate_server' in p.parts]
for package in ('collimate', 'gen'):
    original = Path(sys.argv[1]) / package
    for path in original.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            assert (staged / 'sdk' / package / path.relative_to(original)).read_bytes() == path.read_bytes()
print(json.dumps({'version': headers['Version'], 'requires': requirements, 'source_exact': True}))
'''
    (tmp_path / 'context').mkdir()
    result = subprocess.run([sys.executable, '-I', '-S', '-c', worker,
                             str(source), str(tmp_path / 'context'), str(HOST_BASE_IMAGE)],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    evidence = json.loads(result.stdout)
    assert evidence['version'] == project['version'] and evidence['source_exact']
    from collimate.shared import SDK_REQUIREMENTS
    assert evidence['requires'] == list(SDK_REQUIREMENTS)
    # Each SDK requirement is verbatim a dependency of the one distribution.
    assert set(SDK_REQUIREMENTS) <= set(project['dependencies'])


def test_public_runtime_source_only_worker_has_same_canonical_metadata_P4F33(tmp_path):
    source = source_only_packages(tmp_path)
    worker = r'''
import importlib.metadata, importlib.resources, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    importlib.metadata.distribution('collimate')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('Source-only worker has ambient SDK metadata')
import collimate
import gen
from collimate import handlers
from collimate.shared import sdk_distribution
assert 'handlers' in collimate.__all__
assert callable(handlers.validate_handler)
assert callable(gen.main)
assert isinstance(importlib.resources.files('collimate'), Path)
assert isinstance(importlib.resources.files('gen'), Path)
distribution = sdk_distribution()
canonical = Path(sys.argv[1]) / 'collimate/_sdk_metadata/METADATA'
assert distribution.read_text('METADATA').encode() == canonical.read_bytes()
assert distribution.requires
assert not (Path(sys.argv[1]) / 'collimate/_distribution.py').exists()
print(json.dumps({'public_source_only': True, 'metadata_exact': True}))
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', worker, str(source)],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'public_source_only': True, 'metadata_exact': True}


def test_sdk_runtime_metadata_missing_requirements_never_default_empty_P4F22(tmp_path, monkeypatch):
    import pytest
    from collimate import shared as runtime
    root = tmp_path / '_sdk_metadata'
    root.mkdir()
    (root / 'METADATA').write_text('Metadata-Version: 2.4\nName: collimate\nVersion: 0.2.0\nRequires-Python: >=3.12\n')
    monkeypatch.setattr(runtime, '__file__', str(tmp_path / 'shared.py'))
    with pytest.raises(RuntimeError, match='runtime requirements'):
        runtime.sdk_distribution()


@pytest.mark.parametrize('package', ['collimate', 'gen'])
def test_image_staging_refuses_nonfilesystem_package_resources_P4F33(
        tmp_path, monkeypatch, package):
    """Real zip Traversable: staging must not invent temporary package roots."""
    import importlib.resources
    base = host_base_image()
    context = tmp_path / 'context'
    context.mkdir()
    with zipfile.ZipFile(tmp_path / 'package.zip', 'w') as archive:
        archive.writestr(package + '/__init__.py', '')
        resource = zipfile.Path(archive, package + '/')
        actual = importlib.resources.files
        monkeypatch.setattr(importlib.resources, 'files',
                            lambda name: resource if name == package else actual(name))
        with pytest.raises(RuntimeError, match='filesystem-backed collimate and gen'):
            base.stage(context)
    assert not (context / 'sdk').exists(), 'Unsupported resource must not be copied as an SDK'


def test_image_staging_copies_the_python_package_without_its_package_data(tmp_path, monkeypatch):
    """An installed wheel's collimate/ also holds every tree the wheel
    force-includes, and ``npm ci`` into its tool dir adds node_modules/.bin
    symlinks; the staged context holds the Python packages and the native
    verifier only, and no symlink."""
    import importlib.resources
    base = host_base_image()
    # The build hook places these under collimate/ (in the SDK's home) in the wheel; the content
    # trees go beside it as collimate_content/ etc., never inside it.
    hook = (ROOT / 'build/hatch_build.py').read_text()
    package_data = {name for name in ('shared', 'renderer_compilers', 'renderer_tools')
                    if f'{{home}}/collimate/{name}' in hook}
    assert package_data == {'shared', 'renderer_compilers', 'renderer_tools'}
    assert package_data == set(base._SDK_PACKAGE_DATA)
    assert not {'generators', 'renderers', 'builtin', 'suggested', 'collimate_server'} & package_data
    site = tmp_path / 'site-packages'
    kept = ['collimate/__init__.py', 'collimate/testing/forward.py',
            'collimate/native_verifier/linux-x64/manifest.json', 'gen/__init__.py']
    for rel in kept + [f'collimate/{tree}/present' for tree in package_data]:
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text(rel)
    tools = site / 'collimate/renderer_tools/node_modules'
    (tools / 'esbuild/bin').mkdir(parents=True)
    (tools / 'esbuild/bin/esbuild').write_text('#!/bin/sh\n')
    (tools / '.bin').mkdir()
    (tools / '.bin/esbuild').symlink_to('../esbuild/bin/esbuild')
    actual = importlib.resources.files
    monkeypatch.setattr(importlib.resources, 'files',
                        lambda name: site / name if name in ('collimate', 'gen') else actual(name))
    (tmp_path / 'context').mkdir()
    context = Path(base.stage(tmp_path / 'context').context)
    staged = {path.relative_to(context / 'sdk').as_posix() for path in (context / 'sdk').rglob('*')
              if path.is_file() and path.relative_to(context / 'sdk').parts[0] in ('collimate', 'gen')}
    assert staged == set(kept)
    assert not [path for path in context.rglob('*') if path.is_symlink()]
