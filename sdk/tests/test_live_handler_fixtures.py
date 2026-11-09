"""Live recording fixtures carry accepted handler bytes without faking publication."""
import shutil
from pathlib import Path

import pytest
from collimate.testing import make_test_bridge
from tests_live import conftest as live
from tests._workspace import BUILTINS

ROOT = Path(__file__).parents[1]


def test_live_bridge_seeds_accepted_handler_metadata_skills_and_real_verifier_D21(tmp_path):
    bridge = make_test_bridge(str(tmp_path))
    try:
        live._seed_accepted_handlers(bridge)
        handler = next(value for value in bridge.handlers() if value['id'] == 'options')
        assert handler['derived']
        skills = bridge.handler_skills('.options_stack')
        prefix = 'options--options-authoring/'
        assert prefix + 'SKILL.md' in skills
        assert skills[prefix + 'references/how-to-write-options.md'] == (
            ROOT / 'shared/docs/how-to-write-options.md').read_text()
        bridge.fs_write_any('choices.options_stack', 'options: [unterminated')
        verdict = bridge.validate('choices.options_stack')
        assert verdict['valid'] is False and verdict['error']
    finally:
        bridge.finish(success=True)


def test_live_handler_seed_refuses_stale_accepted_hash_D21(tmp_path):
    source = BUILTINS / 'definitions/options'
    package = tmp_path / 'options'
    shutil.copytree(source, package)
    (package / 'derived/skills/options-authoring/SKILL.md').write_text('stale')
    with pytest.raises(ValueError, match='accepted'):
        live._accepted_handler_records(tmp_path)


def test_live_generator_bridge_factory_preserves_existing_chat_gate_D21(tmp_path, monkeypatch):
    import collimate.testing as ct
    calls = []
    original = ct.make_test_bridge
    def previous_factory(workspace, **kwargs):
        bridge = original(workspace, **kwargs)
        bridge.chat_poll = lambda: {'kind': 'user_msg', 'content': 'gate-preserved'}
        calls.append('previous')
        return bridge
    monkeypatch.setattr(ct, 'make_test_bridge', previous_factory)
    def recording_run(*args, **kwargs):
        bridge = ct.make_test_bridge(str(tmp_path))
        assert bridge.chat_poll()['content'] == 'gate-preserved'
        assert bridge.handler_skills('.options_stack')
        bridge.finish(success=True)
        return 'recorded'
    monkeypatch.setattr(live, 'run_generator', recording_run)
    assert live.run_agent_generator(account={}, config={}) == 'recorded'
    assert ct.make_test_bridge is previous_factory
    assert calls == ['previous']


def test_live_verifier_has_external_process_deadline_D21():
    code = 'globalThis.__collimate_verifier={default:()=>{while(true){}}};'
    result = live._watched_verdict(code, {'path': 'choices.options_stack'}, timeout=.2)
    assert result['valid'] is False and 'deadline' in result['error']
