"""P4F29: draft commands resolve dependencies from their actual SDK source."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys


def test_prepare_child_source_only_exact_metadata_and_no_ambient_fallback_P4F29(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    sdk = Path(__file__).resolve().parents[1]
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    for name in ('collimate', 'gen'):
        shutil.copytree(sdk / 'src' / name, source / name, ignore=ignore)
    # Copy dependency packages only: -I -S excludes installed SDK metadata,
    # editable .pth files and the caller's PYTHONPATH from this real worker.
    for name in ('yaml', 'pydantic', 'pydantic_core', 'annotated_types',
                 'typing_extensions', 'typing_inspection'):
        spec = importlib.util.find_spec(name)
        assert spec and spec.origin
        origin = Path(spec.origin)
        if spec.submodule_search_locations:
            shutil.copytree(origin.parent, source / name, ignore=ignore)
        else:
            shutil.copyfile(origin, source / origin.name)
    worker = r'''
import email.parser, importlib.metadata, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
try:
    importlib.metadata.distribution('collimate-sdk')
except importlib.metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('Source-only worker unexpectedly has installed SDK metadata')
from collimate.testing import make_test_bridge
from collimate.testing.forward import Forwarder, prepare_child
workspace = Path(sys.argv[2]); workspace.mkdir()
draft = workspace / 'draft'; draft.mkdir()
(draft / 'probe.generator').write_text('entrypoint: main.py\n')
(draft / 'main.py').write_text('raise AssertionError("parent must never execute draft")\n')
resource = Path(sys.argv[1]) / 'collimate/_sdk_metadata/METADATA'
headers = email.parser.Parser().parsestr(resource.read_text())
expected = [value for value in headers.get_all('Requires-Dist', []) if 'extra ==' not in value]
assert expected
bridge = make_test_bridge(workspace)
relay = Forwarder(bridge)
relay.port = 1234
def prepare():
    child = prepare_child(relay, 'draft/probe.generator', prompt='private fixture prompt')
    try:
        assert child.argv[:3] == ['uv', 'run', '--no-project']
        assert child.argv[3:-1] == [item for value in dict.fromkeys(expected) for item in ('--with', value)]
        assert child.argv[-1] == str(draft / 'main.py')
        assert Path(child.host_run_dir, 'prompt.md').read_text() == 'private fixture prompt'
    finally:
        child.close()
    assert not Path(child.host_run_dir).exists()
try:
    prepare()
    # A different installed distribution must never replace this source's
    # canonical metadata, even when importlib can find the ambient version.
    ambient = Path(sys.argv[1]) / 'collimate_sdk-0.0.1.dist-info'; ambient.mkdir()
    (ambient / 'METADATA').write_text('Metadata-Version: 2.4\nName: collimate-sdk\nVersion: 0.0.1\nRequires-Dist: unexpected-ambient-package==999\n')
    assert importlib.metadata.version('collimate-sdk') == '0.0.1'
    prepare()
    resource.unlink()
    try:
        prepare_child(relay, 'draft/probe.generator')
    except RuntimeError as exc:
        assert 'Canonical SDK runtime metadata is missing' in str(exc)
    else:
        raise AssertionError('Missing canonical metadata fell back to ambient distribution')
    assert not list((workspace / '.collrun').glob('forward-*'))
    print(json.dumps({'source_only': True, 'requirements': expected, 'ambient_ignored': True}))
finally:
    relay.close(); bridge.close()
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', worker,
                             str(source), str(tmp_path / 'workspace')],
                            cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence['source_only'] and evidence['ambient_ignored']
    from collimate.shared import sdk_distribution
    assert evidence['requirements'] == [value for value in sdk_distribution().requires or ()
                                        if 'extra ==' not in value]
