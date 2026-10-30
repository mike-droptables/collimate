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

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / 'src/collimate/_sdk_metadata/METADATA'


def source_only_packages(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    for name in ('collimate', 'gen'):
        shutil.copytree(ROOT / 'src' / name, source / name, ignore=ignore)
    shutil.copytree(GENERATORS / '_env_shared', source / '_env_shared', ignore=ignore)
    for name in ('yaml', 'pydantic', 'pydantic_core', 'annotated_types',
                 'typing_extensions', 'typing_inspection'):
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
    importlib.metadata.distribution('collimate-sdk')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('The isolated source-only worker unexpectedly has installed SDK metadata')
from collimate import native
import gen
from collimate.testing import make_test_bridge
from _env_shared import base_context
workspace = Path(sys.argv[2]); workspace.mkdir()
bridge = make_test_bridge(workspace)
native.set_bridge(bridge)
try:
    staged = Path(base_context())
    assert callable(gen.main)
    metadata_files = list((staged / 'sdk').glob('collimate_sdk-*.dist-info/METADATA'))
    assert len(metadata_files) == 1
    metadata = metadata_files[0].read_bytes()
    assert metadata == (Path(sys.argv[1]) / 'collimate/_sdk_metadata/METADATA').read_bytes()
    headers = email.parser.Parser().parsestr(metadata.decode())
    requirements = [value for value in headers.get_all('Requires-Dist', []) if 'extra ==' not in value]
    assert (staged / 'sdk-requirements.txt').read_text() == '\n'.join(requirements) + '\n'
    for package in ('collimate', 'gen'):
        original = Path(sys.argv[1]) / package
        for path in original.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                assert (staged / 'sdk' / package / path.relative_to(original)).read_bytes() == path.read_bytes()
    print(json.dumps({'version': headers['Version'], 'requires': requirements, 'source_exact': True}))
finally:
    native._clear_bridge(); bridge.close()
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', worker,
                             str(source), str(tmp_path / 'workspace')],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    evidence = json.loads(result.stdout)
    assert evidence['version'] == project['version'] and evidence['source_exact']
    from packaging.requirements import Requirement
    assert {str(Requirement(value)) for value in evidence['requires']} == {
        str(Requirement(value)) for value in project['dependencies']}


def test_public_runtime_source_only_worker_has_same_canonical_metadata_P4F33(tmp_path):
    source = source_only_packages(tmp_path)
    worker = r'''
import importlib.metadata, importlib.resources, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    importlib.metadata.distribution('collimate-sdk')
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
    (root / 'METADATA').write_text('Metadata-Version: 2.4\nName: collimate-sdk\nVersion: 0.2.0\nRequires-Python: >=3.12\n')
    monkeypatch.setattr(runtime, '__file__', str(tmp_path / 'shared.py'))
    with pytest.raises(RuntimeError, match='runtime requirements'):
        runtime.sdk_distribution()


@pytest.mark.parametrize('package', ['collimate', 'gen'])
def test_image_staging_refuses_nonfilesystem_package_resources_P4F33(
        native_bridge, tmp_path, monkeypatch, package):
    """Real zip Traversable: staging must not invent temporary package roots."""
    import importlib.resources
    sys.path.insert(0, str(GENERATORS))
    import _env_shared
    with zipfile.ZipFile(tmp_path / 'package.zip', 'w') as archive:
        archive.writestr(package + '/__init__.py', '')
        resource = zipfile.Path(archive, package + '/')
        actual = importlib.resources.files
        monkeypatch.setattr(importlib.resources, 'files',
                            lambda name: resource if name == package else actual(name))
        with pytest.raises(RuntimeError, match='filesystem-backed collimate and gen'):
            _env_shared.base_context()
    staged = Path(native_bridge.workspace) / '.collrun/container-base/sdk'
    assert not staged.exists(), 'Unsupported resource must not be copied as an SDK'


def test_image_staging_copies_the_python_package_without_its_package_data(native_bridge, tmp_path, monkeypatch):
    """An installed wheel's collimate/ also holds every tree the wheel
    force-includes, and ``npm ci`` into its tool dir adds node_modules/.bin
    symlinks; the staged context holds the Python packages and the native
    verifier only, and no symlink."""
    import importlib.resources
    sys.path.insert(0, str(GENERATORS))
    import _env_shared
    force_include = tomllib.loads((ROOT / 'pyproject.toml').read_text())['tool']['hatch']['build']['targets']['wheel']['force-include']
    package_data = {target.split('/')[1] for target in force_include.values() if target.startswith('collimate/')}
    assert {'shared', 'renderer_compilers', 'renderer_tools'} <= package_data
    # The first-party content trees ship in the app's wheel, not the SDK's.
    assert not {'generators', 'renderers', 'builtin', 'suggested'} & package_data
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
    context = Path(_env_shared.base_context())
    staged = {path.relative_to(context / 'sdk').as_posix() for path in (context / 'sdk').rglob('*')
              if path.is_file() and path.relative_to(context / 'sdk').parts[0] in ('collimate', 'gen')}
    assert staged == set(kept)
    assert not [path for path in context.rglob('*') if path.is_symlink()]
