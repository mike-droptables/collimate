"""Shipped definitions validate through the real native compiler and engine."""
from pathlib import Path
import time

import pytest
import yaml

from collimate._host.renderer_bundle import compile_verifier
from collimate._host.verification import run_verifier

from tests._workspace import BUILTINS, GENERATORS


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def handlers():
    return {name: compile_verifier((BUILTINS / f'definitions/{name}/derived/verify.ts').read_bytes(),
            prelude=(ROOT/'shared/verify/prelude.js').read_bytes()).code
            for name in ('generator', 'runner', 'handler', 'options')}


def verify(handlers, name, value, *, package=None, path=None, paths=None):
    return run_verifier(handlers[name], {'path': path or f'pkg/sample.{name}',
        'text': yaml.safe_dump(value), 'package': package or {}, 'cell_paths': paths or []},
        deadline=time.monotonic()+5)


def test_generator_definition_checks_declared_package_files__D12(handlers):
    value = {'id': 'test', 'name': 'Test', 'summary': 'A generator', 'entrypoint': 'main.py',
             'main_config': 'settings.set_yaml', 'configs': [{'id':'default','name':'Default','path':'configs/default/manifest.config_yaml'}]}
    files = {'main.py': 'print(1)', 'settings.set_yaml': 'fields: []', 'configs/default/manifest.config_yaml':'settings: {}'}
    assert verify(handlers, 'generator', value, package=files) == {'valid': True}
    assert verify(handlers, 'generator', value, package={k:v for k,v in files.items() if k!='main.py'})['valid'] is False
    assert verify(handlers, 'generator', {**value, 'entrypoint':'../outside.py'}, package=files)['valid'] is False
    assert verify(handlers, 'generator', {**value, 'retired_composition':True}, package=files)['valid'] is False


def test_runner_schema_checks_grants_without_cross_package_lookup__D12(handlers):
    value = {'id':'test','name':'Test','generator':'other-generator','settings':{'settings.set_yaml':{}},
             'permissions':{'docker':'none','internet':'full','commands':'deny'},'accounts':{'role':'local-id'}}
    assert verify(handlers,'runner',value) == {'valid':True}
    assert verify(handlers,'runner',{**value,'permissions':{'commands':'execute'}})['valid'] is False
    assert verify(handlers,'runner',{**value,'accounts':{'role':{'secret':'unsafe'}}})['valid'] is False


def test_handler_definition_requires_covered_examples__D12(handlers):
    value = {'id':'note','name':'Note','description':'A note','guidance':'Use a title',
             'types':[{'ext':'.note'}], 'examples':[{'folder':'basic','description':'Simple','expect':'valid','feedback':'Good'}]}
    assert verify(handlers,'handler',value,package={'examples/basic/sample.note':'# Title'}) == {'valid':True}
    assert verify(handlers,'handler',value,package={'examples/basic/sample.txt':'# Title'})['valid'] is False
    assert verify(handlers,'handler',{**value,'types':[{'ext':'.note'},{'ext':'.NOTE'}]},package={'examples/basic/sample.note':'# Title'})['valid'] is False


@pytest.mark.parametrize('path,body', [
    ('skills/author/SKILL.md', '# Override'), ('skills/local/derived/hidden.md', '# Hidden'),
    ('skills/scripts/SKILL.md', '# Hidden scripts band'), ('skills/derived/SKILL.md', '# Hidden derived band'),
    ('skills/local/verify.ts', 'export default ()=>({valid:true})'),
    ('skills/local/scripts/helper.txt', 'helper'),
    ('skills/local/helper.py', 'print(1)'), ('skills/local/helper.txt', '#!/bin/sh\nexit 0'),
    ('skills/local/blob.bin', {'size': 4, 'hash': 'opaque'}),
])
def test_handler_verifier_refuses_behavior_in_added_guidance_P4F01(handlers, path, body):
    value = {'id':'note','name':'Note','description':'A note','guidance':'Use a title', 'types':[{'ext':'.note'}], 'examples':[]}
    package = {'derived/skills/author/SKILL.md': '# Accepted', 'skills/local/SKILL.md': '# Local', path: body}
    result = verify(handlers, 'handler', value, package=package)
    assert result['valid'] is False, (path, result)


def test_options_definition_checks_literal_roots_and_real_matches__D12(handlers):
    value = {'options':[{'name':'One','root':'a[1]','files':['**','../brief.md']}], 'rest':'keep'}
    paths = ['nested/choices.options_stack','nested/a[1]/x.txt','nested/brief.md']
    assert verify(handlers,'options',value,path=paths[0],paths=paths) == {'valid':True}
    assert verify(handlers,'options',value,path=paths[0],paths=[paths[0],'nested/a1/x.txt'])['valid'] is False
    assert verify(handlers,'options',{'options':[{'name':'None','files':['absent*']}]},path=paths[0],paths=paths)['valid'] is False
    assert verify(handlers,'options',{'options':[{'name':'Escape','files':['../../bad']}]},path=paths[0],paths=paths)['valid'] is False


def test_definition_schema_fields_and_grants_match_sdk_authority__D12():
    import json
    from collimate.lint import GENERATOR_FIELDS, RUNNER_FIELDS
    from collimate.permissions import AXIS_LEVELS, AXES
    schemas = json.loads((ROOT/'shared/verify/definition-schemas.json').read_text())
    assert set(schemas['generator']['properties']) == GENERATOR_FIELDS
    assert set(schemas['runner']['properties']) == RUNNER_FIELDS
    permissions = schemas['runner']['properties']['permissions']['properties']
    assert set(permissions) == set(AXES)
    assert {key: tuple(permissions[key]['enum']) for key in AXIS_LEVELS} == AXIS_LEVELS


def test_generator_verifier_takes_the_selected_file_and_opens_keys_GN01_GN03(handlers):
    value = {'id':'demo','name':'Demo','summary':'Useful','entrypoint':'main.py','main_config':'settings.set_yaml'}
    files = {'main.py':'pass','settings.set_yaml':'fields: []'}
    declared = {**value, 'takes_selected_file': True, 'takes_selected_file_if': {'first': True},
                'requires_docker_if': {'surface': 'terminal'},
                'opens': [{'path': '{run}/chat/conversation.chat', 'if': {'surface': 'chat'}},
                          {'path': '{tab}'}]}
    assert verify(handlers, 'generator', declared, package=files) == {'valid': True}
    for bad in ({'takes_selected_file': 'yes'}, {'takes_selected_file_if': {}},
                {'requires_docker_if': {}}, {'requires_docker_if': 'terminal'},
                {'opens': {'path': 'x'}}, {'opens': [{'if': {'a': 'b'}}]},
                {'opens': [{'path': ''}]}, {'opens': [{'path': 'x', 'when': {'a': 'b'}}]},
                {'opens': [{'path': 'x', 'if': {}}]}):
        assert verify(handlers, 'generator', {**value, **bad}, package=files)['valid'] is False, bad


def test_all_shipped_generators_pass_their_actual_handler__D12(handlers):
    checked = []
    for definition in sorted((GENERATORS).glob('*/*/*.generator')):
        files = {p.relative_to(definition.parent).as_posix(): p.read_text()
                 for p in definition.parent.rglob('*') if p.is_file() and p.suffix not in ('.pyc',)}
        verdict = verify(handlers, 'generator', yaml.safe_load(definition.read_text()), package=files)
        assert verdict == {'valid':True}, (definition, verdict)
        checked.append(definition)
    assert len(checked) >= 3


def test_shipped_definition_examples_and_accepted_hashes__D12(handlers):
    from collimate.handlers import derived_hash
    for name in handlers:
        root = BUILTINS / f'definitions/{name}'
        manifest = yaml.safe_load((root/f'{name}.handler').read_text())
        derived = {p.relative_to(root/'derived').as_posix(): p.read_bytes()
                   for p in (root/'derived').rglob('*') if p.is_file()}
        assert manifest['derived'] == derived_hash(derived)
        extensions = [item['ext'] for item in manifest['types']]
        for example in manifest['examples']:
            folder = root/'examples'/example['folder']
            files = {p.relative_to(folder).as_posix(): p.read_text() for p in folder.rglob('*') if p.is_file()}
            targets = [p for p in files if any(p.endswith(ext) for ext in extensions)]
            assert targets
            for path in targets:
                prefix = path.rpartition('/')[0]
                package = {p[len(prefix)+1:] if prefix else p: text for p,text in files.items()
                           if p.startswith(prefix+'/') if prefix} if prefix else files
                verdict = run_verifier(handlers[name], {'path':'fixture/'+path,'text':files[path],
                    'package':package,'cell_paths':['fixture/'+p for p in files]}, deadline=time.monotonic()+5)
                assert verdict['valid'] == (example['expect']=='valid'), (name,path,verdict)


def test_generator_verifier_refuses_parameters(handlers):
    value={'id':'demo','name':'Demo','summary':'Useful','entrypoint':'main.py','main_config':'settings.set_yaml'}
    files={'main.py':'pass','settings.set_yaml':'fields: []'}
    assert verify(handlers,'generator',value,package=files) == {'valid':True}
    declared={**value,'parameters':{'mode':{'options':['one','two'],'default':'one'}}}
    assert verify(handlers,'generator',declared,package=files)['valid'] is False


@pytest.mark.parametrize('folder',[
 {'name':'nested/slot','description':'Slot'}, {'name':'.collrun','description':'Slot'},
 {'name':'slot','description':' '}, {'name':'slot','description':'Slot','sample':'elsewhere/a.txt'},
 {'name':'slot','description':'Slot','sample':'samples/missing'},
])
def test_generator_config_folder_contract_matches_existing_schema_rules__D12(handlers,folder):
    value={'id':'demo','name':'Demo','summary':'Useful','entrypoint':'main.py','main_config':'settings.set_yaml','config_folders':[folder]}
    assert verify(handlers,'generator',value,package={'main.py':'pass','settings.set_yaml':'fields: []'})['valid'] is False


def test_generator_sample_folder_control__D12(handlers):
    value={'id':'demo','name':'Demo','summary':'Useful','entrypoint':'main.py','main_config':'settings.set_yaml',
           'config_folders':[{'name':'_skills','description':'Skills','sample':'samples/skills'}]}
    files={'main.py':'pass','settings.set_yaml':'fields: []','samples/skills/SKILL.md':'Skill'}
    assert verify(handlers,'generator',value,package=files) == {'valid':True}


def test_handler_metadata_nonempty_name_matches_catalog_contract__D12(handlers):
    value={'id':'note','name':' ','description':'Note','guidance':'Title','types':[{'ext':'.note'}],'examples':[]}
    assert verify(handlers,'handler',value)['valid'] is False


def test_runner_package_version_shape_is_text__D12(handlers):
    value={'id':'demo','name':'Demo','generator':'g','settings':{},'packages':{'image':{'version':'1'}}}
    assert verify(handlers,'runner',value)['valid'] is False


@pytest.mark.parametrize('requirement', [
    {}, {'kind': 'invalid'}, {'kind': ['model']}, {'kinds': 'model'},
    {'kinds': ['model', 'invalid']}, {'kind': 'model', 'required': 'yes'},
    {'kind': 'model', 'label': ' '}, {'kind': 'model', 'capabilities': 'text'},
    {'kind': 'model', 'protocols': ['']}, {'kind': 'model', 'unknown': True},
])
def test_generator_account_requirement_contract_matches_canonical_lint__D12(handlers, requirement):
    value = {'id': 'demo', 'name': 'Demo', 'summary': 'Useful', 'entrypoint': 'main.py',
             'main_config': 'settings.set_yaml', 'account_requirements': {'model': requirement}}
    assert verify(handlers, 'generator', value, package={'main.py': 'pass', 'settings.set_yaml': 'fields: []'})['valid'] is False


@pytest.mark.parametrize('extra', [{'main_config': 'main.py'}, {'required_files': ['settings.set_yaml']}])
def test_generator_main_schema_and_supplemental_files_contract__D12(handlers, extra):
    value = {'id': 'demo', 'name': 'Demo', 'summary': 'Useful', 'entrypoint': 'main.py',
             'main_config': 'settings.set_yaml', **extra}
    assert verify(handlers, 'generator', value, package={'main.py': 'pass', 'settings.set_yaml': 'fields: []'})['valid'] is False


def test_generator_account_requirement_fields_and_positive_controls__D12(handlers):
    import json
    from collimate.lint.core import ACCOUNT_REQUIREMENT_FIELDS
    schema = json.loads((ROOT/'shared/verify/definition-schemas.json').read_text())
    requirement = schema['generator']['properties']['account_requirements']['additionalProperties']
    assert set(requirement['properties']) == ACCOUNT_REQUIREMENT_FIELDS
    value = {'id': 'demo', 'name': 'Demo', 'summary': 'Useful', 'entrypoint': 'main.py',
             'main_config': 'settings.set_yaml', 'required_files': ['supplement.txt'],
             'account_requirements': {
                 'model': {'kind': 'model', 'required': True, 'capabilities': ['text'], 'label': 'Writer'},
                 'repo': {'kinds': ['git'], 'required': False, 'services': ['github', 'gitlab', 'git']},
             }}
    assert verify(handlers, 'generator', value, package={'main.py': 'pass', 'settings.set_yaml': 'fields: []'}) == {'valid': True}


def test_definition_schema_account_kinds_equal_lint_account_kinds():
    import json
    from collimate.lint import ACCOUNT_KINDS
    schema = json.loads((ROOT/'shared/verify/definition-schemas.json').read_text())
    requirement = schema['generator']['properties']['account_requirements']['additionalProperties']['properties']
    assert set(requirement['kind']['enum']) == ACCOUNT_KINDS
    assert set(requirement['kinds']['items']['enum']) == ACCOUNT_KINDS
    assert len(requirement['kind']['enum']) == len(requirement['kinds']['items']['enum']) == len(ACCOUNT_KINDS)


def test_handler_verifier_accepts_text_guidance_but_refuses_aliases_and_bounds_P4F01(handlers):
    value = {'id':'note','name':'Note','description':'A note','guidance':'Use a title',
             'types':[{'ext':'.note'}], 'examples':[]}
    package = {'derived/skills/author/SKILL.md': '# Accepted',
               'skills/local/SKILL.md': '---\nname: local\n---\nExtra guidance',
               'skills/local/references/example.md': '```python\nprint(1)\n```',
               'skills/local/data.json': '{"guide":"text"}'}
    assert verify(handlers, 'handler', value, package=package) == {'valid': True}
    for extra in ({'skills/other/SKILL.md': '---\nname: local\n---\nAlias'},
                  {'skills/local/SKILL.md': '---\nname: false\n---\nInvalid name'},
                  {'skills/local/data.txt': 'x' * (1024 * 1024)},
                  {f'skills/local/data-{i}.txt': 'x' for i in range(128)}):
        assert verify(handlers, 'handler', value, package={**package, **extra})['valid'] is False


@pytest.mark.parametrize('body', ['---\r\nname: document-authoring\r\n---\r\nAdded', '---\nname: " document-authoring "\n---\nAdded'])
def test_compiled_verifier_reserves_actual_runtime_skill_alias_P4F01(handlers, body, monkeypatch):
    monkeypatch.syspath_prepend(str(GENERATORS))
    from _agent_shared.cell_caps import _skill_from
    assert _skill_from('local/SKILL.md', body, []).name == 'document-authoring'
    value = {'id':'note','name':'Note','description':'A note','guidance':'Title','types':[{'ext':'.note'}],'examples':[]}
    for accepted in ['---\r\nname: document-authoring\r\n---\r\nAccepted', '---\nname: " document-authoring "\n---\nAccepted']:
        package = {'derived/skills/author/SKILL.md': accepted, 'skills/local/SKILL.md': body}
        assert verify(handlers, 'handler', value, package=package)['valid'] is False
