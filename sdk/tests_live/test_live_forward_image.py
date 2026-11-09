"""Opt-in unpaid actual-image SDK proofs; no provider request."""
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _command_image() -> str:
    import os
    image = os.environ.get('COLLIMATE_TEST_COMMAND_IMAGE')
    if not image:
        pytest.skip('Set COLLIMATE_TEST_COMMAND_IMAGE to the built command image')
    assert os.getuid() != 0, 'The real host command UID must be non-root for this proof'
    return image


def _in_container(image: str, steps: list) -> list:
    """Run each argv in one disposable container as the host command UID."""
    import os
    import docker
    from .conftest import live_container_labels
    client = docker.from_env()
    container = None
    try:
        container = client.containers.run(image, ['sleep', '120'], detach=True,
                                          labels=live_container_labels())
        outputs = []
        for argv in steps:
            result = container.exec_run(argv, user=f'{os.getuid()}:{os.getgid()}',
                                        environment={'HOME': '/tmp/home'})
            assert result.exit_code == 0, (argv, result.output.decode())
            outputs.append(result.output.decode())
        return outputs
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()


def test_public_sdk_readable_not_writable_by_command_uid_P4F30():
    """Opt-in actual image proof, including the same non-root command identity."""
    import hashlib
    import json

    script = '''import gen, collimate.native, hashlib, json, os
from pathlib import Path
assert callable(getattr(gen, 'main', None)), 'public gen facade is unreadable'
root = Path('/opt/collimate')
for path in [root, *root.rglob('*')]:
    assert path.stat().st_uid == 0, str(path)
    assert not os.access(path, os.W_OK), str(path)
    assert os.access(path, os.R_OK), str(path)
    if path.is_dir():
        assert os.access(path, os.X_OK), str(path)
print(json.dumps({name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                  for name,module in [('gen',gen),('native',collimate.native)]}))
'''
    [output] = _in_container(_command_image(), [['python', '-c', script]])
    source = Path(__file__).parents[1] / 'src'
    assert json.loads(output) == {
        'gen': hashlib.sha256((source / 'gen/__init__.py').read_bytes()).hexdigest(),
        'native': hashlib.sha256((source / 'collimate/native.py').read_bytes()).hexdigest(),
    }


def test_image_python_imports_sdk_without_pythonpath_and_project_venv_wins():
    """The SDK is installed in the image's Python, not put on PYTHONPATH.

    The image's `python` (plain and in a login shell) and uv's no-project
    runs import it with its metadata; a project venv imports its own
    ``collimate`` instead."""
    import json
    import shlex

    probe = ('import collimate, gen, importlib.metadata as m, json, os; '
             'from pathlib import Path; from collimate.shared import sdk_distribution; '
             'print(json.dumps({"pythonpath": os.environ.get("PYTHONPATH"), '
             '"sdk": str(Path(collimate.__file__).resolve()), '
             '"gen": str(Path(gen.__file__).resolve()), '
             '"version": m.version("collimate"), "metadata": sdk_distribution().version}))')
    project = ('uv venv -q /tmp/project && site="$(/tmp/project/bin/python -c '
               '\'import sysconfig; print(sysconfig.get_path("purelib"))\')" && '
               'mkdir "$site/collimate" && echo "OWNER = \'project\'" > "$site/collimate/__init__.py" && '
               '/tmp/project/bin/python -c \'import collimate; print(collimate.OWNER)\'')
    plain, login, uv_run, owner = _in_container(_command_image(), [
        ['python', '-c', probe],
        ['bash', '-lc', f'python -c {shlex.quote(probe)}'],
        ['uv', 'run', '-q', '--no-project', 'python', '-c', probe],
        ['bash', '-c', project],
    ])
    for output in (plain, login, uv_run):
        report = json.loads(output)
        assert report['pythonpath'] is None
        assert report['sdk'] == '/opt/collimate/collimate/__init__.py'
        assert report['gen'] == '/opt/collimate/gen/__init__.py'
        assert report['version'] == report['metadata']
    assert owner.strip() == 'project'
