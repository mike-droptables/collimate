"""Native caps and container memory read one manifest policy without sharing defaults."""
from unittest.mock import patch

import pytest


@pytest.mark.parametrize('raw,expected', [(123,123), (123.9,123), (None,None), (0,None), (-1,None), ('123',None)])
def test_actual_native_and_container_limit_inputs__F0210(raw, expected):
    from manifest_limits import manifest_rlimit
    from process_manager.manager import ProcessManager
    from process_manager.docker_runner import RunnerSpec, build_run_argv
    manifest = {'rlimits': {'memory_bytes': raw}}
    seen = []
    resource = type('R', (), {'RLIMIT_DATA': 2, 'setrlimit': staticmethod(lambda key, value: seen.append(value))})
    with patch.dict('sys.modules', {'resource': resource}):
        ProcessManager._build_preexec_fn(manifest)()
    assert seen == [((expected if expected is not None else ProcessManager._DEFAULT_RLIMIT_MEMORY_BYTES),) * 2]
    value = manifest_rlimit(manifest, 'memory_bytes')
    assert value == expected
    argv = build_run_argv(RunnerSpec(image='test', container_name='test', run_id='test', app_dir='/app', workspace_dir='/workspace', sdk_src='/sdk', entrypoint='run.py', env={}, network='bridge', memory_bytes=value))
    if expected is None:
        assert '--memory' not in argv
    else:
        assert argv[argv.index('--memory') + 1] == str(expected)


@pytest.mark.parametrize('manifest', [{}, {'rlimits': None}, {'rlimits': []}])
def test_missing_limit_block_keeps_caller_default__F0210(manifest):
    from manifest_limits import manifest_rlimit
    assert manifest_rlimit(manifest, 'memory_bytes') is None
    assert manifest_rlimit(manifest, 'open_files', 1024) == 1024
