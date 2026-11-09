# SPDX-License-Identifier: MIT
"""Characterize existing renderer subprocess boundaries before F1927 extraction."""
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._node_renderer import BUNDLER, LOADER
from tests._workspace import WORKSPACE

_CASES = [
    ('assistant_transcript', 'test_assistant_transcript_roundtrip', 'ASSISTANT_BUNDLE', 'message-model.ts'),
    ('html_renderer_dark', 'test_html_renderer_dark_injection', 'HTML_RENDERER_BUNDLE', 'html_entry.ts'),
    ('html_renderer_links', 'test_html_renderer_links', 'HTML_RENDERER_BUNDLE', 'html_links_entry.ts'),
    ('renderer_conditions', 'test_renderer_conditions_match_contract__F1817', 'SCHEMA_FORM_BUNDLE', 'main.tsx'),
    ('run_renderer_model', 'test_run_renderer_model', 'RUN_RENDERER_BUNDLE', 'main.tsx'),
    ('terminal_renderer_model', 'test_terminal_renderer_model', 'TERMINAL_RENDERER_BUNDLE', 'main.tsx'),
    ('webview_artifact', 'test_webview_artifact_and_rpc_contract', 'WEBVIEW_BUNDLE', 'main.tsx'),
    ('website_frame', 'test_website_frame_keeps_its_origin_only_for_another_site_or_a_live_proxy__F1739', 'WEBSITE_BUNDLE', 'main.tsx'),
]


@pytest.mark.parametrize('module_name,function_name,env_key,entry_name', _CASES)
@pytest.mark.parametrize('failure', [None, 'build', 'test'])
def test_actual_renderer_shim_preserves_commands_and_failure_propagation__F1927(
        tmp_path, monkeypatch, module_name, function_name, env_key, entry_name, failure):
    module = importlib.import_module('tests.test_' + module_name)
    calls = []
    monkeypatch.setenv('F1927_INHERITED', 'preserve')

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        build = len(calls) == 1
        if build:
            Path(argv[3]).write_text('export {};\n')
        return SimpleNamespace(returncode=7 if failure == ('build' if build else 'test') else 0,
                               stdout='fixture stdout', stderr='fixture stderr')

    monkeypatch.setattr(subprocess, 'run', run)
    invoke = getattr(module, function_name)
    if failure:
        with pytest.raises(AssertionError, match='fixture stderr'):
            invoke(tmp_path)
    else:
        invoke(tmp_path)
    build_argv, build_options = calls[0]
    assert build_argv[:2] == ['node', str(BUNDLER)]
    assert Path(build_argv[2]).name == entry_name
    bundle = Path(build_argv[3])
    assert bundle.parent == tmp_path
    assert build_options == {'capture_output': True, 'text': True, 'cwd': str(WORKSPACE)}
    if failure == 'build':
        assert len(calls) == 1
        return
    test_argv, test_options = calls[1]
    assert len(calls) == 2
    assert test_argv == ['node', '--import', LOADER.as_uri(), '--test', str(module._TEST_FILE)]
    assert test_options['cwd'] == str(WORKSPACE)
    assert test_options['capture_output'] and test_options['text']
    assert test_options['env'][env_key] == str(bundle)
    assert test_options['env']['F1927_INHERITED'] == 'preserve'
    if env_key == 'RUN_RENDERER_BUNDLE':
        assert test_options['env']['TZ'] == 'UTC'
