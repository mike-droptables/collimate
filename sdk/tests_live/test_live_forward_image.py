"""Opt-in unpaid actual-image SDK access proof; no provider request."""
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def test_public_sdk_readable_not_writable_by_command_uid_P4F30():
    """Opt-in actual image proof, including the same non-root command identity."""
    import hashlib
    import json
    import os
    import pytest

    image = os.environ.get('COLLIMATE_TEST_COMMAND_IMAGE')
    if not image:
        pytest.skip('Set COLLIMATE_TEST_COMMAND_IMAGE to the built command image')
    assert os.getuid() != 0, 'The real host command UID must be non-root for this proof'
    import docker
    client = docker.from_env()
    container = None
    script = '''import gen, collimate.native, hashlib, json, os
from pathlib import Path
assert callable(getattr(gen, 'main', None)), 'public gen facade is unreadable'
root = Path('/opt/collimate-sdk')
for path in [root, *root.rglob('*')]:
    assert path.stat().st_uid == 0, str(path)
    assert not os.access(path, os.W_OK), str(path)
    assert os.access(path, os.R_OK), str(path)
    if path.is_dir():
        assert os.access(path, os.X_OK), str(path)
print(json.dumps({name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                  for name,module in [('gen',gen),('native',collimate.native)]}))
'''
    try:
        container = client.containers.run(image, ['sleep', '60'], detach=True, labels={})
        result = container.exec_run(['python', '-c', script], user=f'{os.getuid()}:{os.getgid()}')
        assert result.exit_code == 0, result.output.decode()
        source = Path(__file__).parents[1] / 'src'
        assert json.loads(result.output) == {
            'gen': hashlib.sha256((source / 'gen/__init__.py').read_bytes()).hexdigest(),
            'native': hashlib.sha256((source / 'collimate/native.py').read_bytes()).hexdigest(),
        }
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
