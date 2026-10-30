"""F1931: real discovery and cleanup in both sibling test suites."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


@pytest.mark.parametrize('suite', ['tests', 'tests_live'])
@pytest.mark.parametrize('fails', [False, True], ids=['success', 'test-failure'])
def test_discovered_native_bridge_binds_and_cleans_up__F1931(tmp_path, suite, fails):
    root = Path(__file__).resolve().parents[1]
    marker = tmp_path / 'lifecycle.json'
    source = '''
import errno, json, os
from pathlib import Path
import pytest
from collimate import native
from collimate.testing import RecordingBridge

@pytest.fixture(autouse=True)
def observe_cleanup(monkeypatch):
    closed = []
    original = RecordingBridge.close
    def close(bridge):
        assert native.current_bridge() is None
        descriptors = [bridge._stop_pipe_r, bridge._stop_pipe_w]
        original(bridge)
        for descriptor in descriptors:
            with pytest.raises(OSError) as error:
                os.fstat(descriptor)
            assert error.value.errno == errno.EBADF
        closed.append(True)
    monkeypatch.setattr(RecordingBridge, 'close', close)
    yield
    assert native.current_bridge() is None
    assert closed == [True]
    Path(MARKER).write_text(json.dumps({'cleared': True, 'closed': True}))

def test_real_fixture(native_bridge, tmp_path):
    assert native.current_bridge() is native_bridge
    assert Path(native_bridge.workspace) == tmp_path
    assert all(os.fstat(fd) for fd in (native_bridge._stop_pipe_r, native_bridge._stop_pipe_w))
    native.update_file('fixture-proof.txt', 'real bridge write')
    assert native.read_file('fixture-proof.txt') == 'real bridge write'
    if FAILS:
        pytest.fail('F1931 deliberate body failure')
'''.replace('MARKER', repr(str(marker))).replace('FAILS', repr(fails))
    home = tmp_path / 'home'
    home.mkdir()
    with tempfile.NamedTemporaryFile(mode='w', prefix='_f1931_', suffix='.py',
                                     dir=root / suite, delete=False) as probe:
        probe.write(source)
        path = Path(probe.name)
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(path),
             '--basetemp', str(tmp_path / 'child')], cwd=root,
            env={'PATH': str(Path(sys.executable).parent) + os.pathsep + '/usr/bin:/bin',
                 'HOME': str(home), 'LANG': 'C.UTF-8', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
            capture_output=True, text=True, timeout=30,
        )
    finally:
        path.unlink(missing_ok=True)
    assert result.returncode == (1 if fails else 0), result.stdout + result.stderr
    assert ('1 failed' if fails else '1 passed') in result.stdout
    if fails:
        assert 'F1931 deliberate body failure' in result.stdout
    assert json.loads(marker.read_text()) == {'cleared': True, 'closed': True}
