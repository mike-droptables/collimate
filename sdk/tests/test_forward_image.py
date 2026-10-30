"""D23 command images carry the current SDK and uv, with stable staged bytes."""
import importlib
import sys
from pathlib import Path

from tests._workspace import GENERATORS


def test_command_base_stages_current_sdk_inside_private_workspace_D23(native_bridge):
    sys.path.insert(0, str(GENERATORS))
    shared = importlib.import_module('_env_shared')
    context = Path(shared.base_context())
    assert context.is_relative_to(Path(native_bridge.workspace) / '.collrun')
    assert (context / 'sdk/collimate/testing/forward.py').read_bytes() == (
        Path(__file__).parents[1] / 'src/collimate/testing/forward.py').read_bytes()
    assert (context / 'sdk/gen/__init__.py').read_bytes() == (
        Path(__file__).parents[1] / 'src/gen/__init__.py').read_bytes()
    dockerfile = (context / 'Dockerfile').read_text()
    assert 'uv python install' in dockerfile
    assert 'PYTHONPATH=/opt/collimate-sdk' in dockerfile
    assert shared.base_context() == str(context)
    assert not native_bridge.fs_list()
