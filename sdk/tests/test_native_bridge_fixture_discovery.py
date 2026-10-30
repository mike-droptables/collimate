"""F1931: both sibling suites resolve one common bridge fixture."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


@pytest.mark.parametrize('suite', ['tests', 'tests_live'])
def test_native_bridge_has_one_common_discovered_definition__F1931(tmp_path, suite):
    root = Path(__file__).resolve().parents[1]
    source = '''
from pathlib import Path
from collimate import native

def test_common_discovery(native_bridge, request):
    assert native.current_bridge() is native_bridge
    native.update_file('discovery.txt', 'shared fixture')
    assert native.read_file('discovery.txt') == 'shared fixture'
    definitions = request._fixturemanager.getfixturedefs('native_bridge', request.node)
    assert len(definitions) == 1, 'multiple native_bridge definitions remain visible'
    assert Path(definitions[0].func.__code__.co_filename).resolve() == Path(ROOT) / 'conftest.py', \
        'native_bridge still belongs to a sibling suite rather than the common ancestor'
'''.replace('ROOT', repr(str(root)))
    home = tmp_path / 'home'
    home.mkdir()
    with tempfile.NamedTemporaryFile(mode='w', prefix='_f1931_discovery_', suffix='.py',
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
    assert result.returncode == 0, result.stdout + result.stderr
    assert '1 passed' in result.stdout


def test_restricted_live_consumers_keep_subscription_binding_and_gates__F1931(
        native_bridge, monkeypatch):
    """Bind the real account offline; retained consumers select the same leg."""
    import ast
    import inspect
    import importlib

    # Load the live tree's canonical shared-module setup before its consumer.
    importlib.import_module('tests_live.conftest')
    from tests_live import test_live_restricted_grants as consumers

    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'f1931-offline-test-token')
    consumers._bind_claude_account('claude_code')
    account = native_bridge._rec_accounts['model']
    assert account['service'] == 'anthropic'
    assert account['auth_type'] == 'subscription'
    assert account['protocol'] == 'cli'
    assert account['model'] == consumers._CLAUDE_MODEL
    assert account['metadata'] == {'harness': 'claude-code', 'plan': 'claude_code'}
    assert account['secrets'] == {'oauth_token': 'f1931-offline-test-token'}
    tree = ast.parse(inspect.getsource(consumers))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == '_bind_claude_account']
    assert len(calls) == 3
    assert all(len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
               and call.args[0].value == 'claude_code' for call in calls)
    assert consumers.requires_claude_code in consumers.pytestmark
    assert consumers.requires_host_node in consumers.pytestmark
    assert consumers.requires_claude_token in consumers.pytestmark
    token_gate = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'requires_claude_token'
                              for target in node.targets))
    assert ast.unparse(token_gate.args[0]) == "not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')"
