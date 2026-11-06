"""Every shipped named preset is a complete current-schema snapshot."""
from pathlib import Path

import pytest
import yaml

from collimate.config_values import resolve_form_values

from tests._workspace import GENERATORS


ROOT = Path(__file__).resolve().parents[1]
CASES = [(manifest, config)
         for manifest in sorted((GENERATORS).glob('*/*/*.generator'))
         for config in yaml.safe_load(manifest.read_text()).get('configs', [])]


@pytest.mark.parametrize('manifest,config', CASES,
                         ids=[f'{path.stem}/{config["id"]}' for path, config in CASES])
def test_shipped_named_preset_contains_every_current_field_P4F18(manifest, config):
    definition = yaml.safe_load(manifest.read_text())
    main = definition['main_config']
    fields, _ = resolve_form_values(yaml.safe_load((manifest.parent / main).read_text()))
    preset = yaml.safe_load((manifest.parent / config['path']).read_text())
    assert set(preset['settings']) == {main}
    assert set(preset['settings'][main]) == {field['key'] for field in fields}
