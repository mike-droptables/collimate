"""Exercise every packaged preset through the actual runner-template route."""
from pathlib import Path

import pytest
import yaml

import genesis
from test_pytest_runners import runner_app as runner_app

ROOT = Path(genesis.GENERATORS_DIR)
CASES = [(manifest, config)
         for manifest in sorted(ROOT.glob('*/*/*.generator'))
         for config in yaml.safe_load(manifest.read_text()).get('configs', [])]


@pytest.mark.parametrize('manifest,config', CASES,
                         ids=[f'{path.stem}/{config["id"]}' for path, config in CASES])
def test_every_packaged_config_opens_with_complete_settings_P4F18(runner_app, manifest, config):
    from collimate.config_values import resolve_form_values
    from models import VirtualFile

    client, _, source, _ = runner_app
    folder = manifest.parent
    # The package's files as the asset-tree reader takes them: a checkout's
    # bundled tree may hold interpreter bytecode, which is never cell content.
    source.files = [VirtualFile(name=path.relative_to(folder.parent).as_posix(),
                               content=path.read_text(), language='text')
                    for path in sorted(folder.rglob('*'))
                    if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc']
    definition = yaml.safe_load(manifest.read_text())
    main = definition['main_config']
    fields, defaults = resolve_form_values(yaml.safe_load((folder / main).read_text()))
    response = client.get(f'/api/runner_generators/{definition["id"]}/configs/{config["id"]}')
    assert response.status_code == 200, response.text
    result = response.json()['runner']['settings']
    assert set(result) == {main}
    assert set(result[main]) == {field['key'] for field in fields}
    expected = defaults if config['id'] == 'default' else yaml.safe_load(
        (folder / config['path']).read_text())['settings'][main]
    assert result[main] == expected
