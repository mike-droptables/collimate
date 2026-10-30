"""P4F20: expected local validation errors remain correctable tool results."""
import os

import pytest

from _agent_shared import workspace_tools  # noqa: E402


def test_missing_local_verifier_allows_corrected_model_call_P4F20(native_bridge):
    """The bridge's validate_file row: a wrong verifier is a correctable
    result, and the corrected call validates."""
    import gen

    gen.update_file('status.note', 'completed')
    row = next(row for row in workspace_tools.tool_entries(enable_file_tools=False, websearch=False)
               if row[0] == 'validate_file')
    first = row[4]('status.note', 'handler-id-is-not-a-path')
    assert first['valid'] is False
    assert 'handler-id-is-not-a-path' in first['error']
    assert row[4]('status.note') == {'valid': True}


@pytest.mark.parametrize('path,verifier', [
    ('absent.note', None), ('status.note', '../outside.ts'),
    ('status.note', 'directory'),
])
def test_bad_local_validation_paths_return_invalid_P4F20(native_bridge, path, verifier):
    import gen
    from pathlib import Path
    gen.update_file('status.note', 'completed')
    (Path(gen.workspace_path()) / 'directory').mkdir()
    result = workspace_tools.validate_file(path, verifier)
    assert result['valid'] is False and result['error']


@pytest.mark.parametrize('failure', [RuntimeError('host unavailable'), TypeError('bug')])
def test_validation_unexpected_failures_still_propagate_P4F20(native_bridge, monkeypatch, failure):
    import gen
    gen.update_file('status.note', 'completed')
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(gen, 'validate', fail)
    with pytest.raises(type(failure), match=str(failure)):
        workspace_tools.validate_file('status.note')


@pytest.mark.parametrize('failure', [OSError('transport I/O failed'),
                                    PermissionError('transport access denied')])
def test_real_host_bridge_transport_errors_propagate_P4F20(tmp_path, monkeypatch, failure):
    """Exercise HostBridge framing/send; inject failure only at transport write."""
    from collimate import native
    from collimate._host.bridge import HostBridge
    (tmp_path / 'status.note').write_text('completed')
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge.workspace = str(tmp_path)
    bridge._sock_rfile = None
    class FailingTransport:
        def write(self, line):
            raise failure
    bridge._sock_wfile = FailingTransport()
    monkeypatch.setattr(native, '_bridge', bridge)
    with pytest.raises(type(failure), match=str(failure)):
        workspace_tools.validate_file('status.note')


def test_real_host_bridge_closed_pipe_propagates_P4F20(tmp_path, monkeypatch):
    """The real HostBridge writes a real pipe whose reader has closed."""
    from collimate import native
    from collimate._host.bridge import HostBridge
    (tmp_path / 'status.note').write_text('completed')
    bridge = HostBridge.__new__(HostBridge)
    bridge._initialize_ipc()
    bridge.workspace = str(tmp_path)
    bridge._sock_rfile = None
    reader, writer = os.pipe()
    os.close(reader)
    class PipeTransport:
        def write(self, line):
            return os.write(writer, line.encode())
        def flush(self):
            pass
    try:
        bridge._sock_wfile = PipeTransport()
        monkeypatch.setattr(native, '_bridge', bridge)
        with pytest.raises(BrokenPipeError):
            workspace_tools.validate_file('status.note')
    finally:
        os.close(writer)
