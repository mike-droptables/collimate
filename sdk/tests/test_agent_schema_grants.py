"""D06 shipped Agent configuration has one grant-owned security contract."""

import pytest
import yaml

from collimate.config_values import parse_set_yaml

from tests._workspace import GENERATORS


PACKAGE = GENERATORS / 'agent/agent'
# The main-config fields: the surface, an annotation run's session, the
# harness, and the settings every surface or the annotation and chat
# surfaces read.
FIELDS = ['surface', 'session', 'backend', 'system_prompt', 'mcp', 'approvals',
          'selected_file_first']
PRESETS = sorted(PACKAGE.glob('configs/*/manifest.config_yaml'))


def test_agent_schema_uses_grant_owned_security__D06():
    schema = parse_set_yaml((PACKAGE / 'agent-config.set_yaml').read_text())
    fields = {field['key']: field for field in schema['fields']}
    assert list(fields) == FIELDS
    assert fields['backend']['account_role'] == 'model'
    assert fields['backend']['account_property'] == 'metadata.harness'
    assert fields['approvals']['options'] == ['auto', 'ask']


def test_the_generator_ships_its_presets_GN02():
    assert len(PRESETS) == 22


@pytest.mark.parametrize('path', PRESETS, ids=lambda path: path.parent.name)
def test_agent_presets_use_only_current_schema_settings__D06(path):
    preset = yaml.safe_load(path.read_text())
    settings = preset['settings']['agent-config.set_yaml']
    assert list(settings) == FIELDS, path
    assert settings['surface'] in {'annotation', 'chat'}
    assert settings['session'] in {'oneshot', 'interactive'}
    assert settings['approvals'] in {'auto', 'ask'}
