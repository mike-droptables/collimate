"""Public SDK contracts consumed directly by host and generator runtimes."""
from pathlib import Path
import json

import pytest


@pytest.mark.parametrize('row', json.loads((Path(__file__).parents[1] / 'shared/set-yaml-resolution-contract.json').read_text()), ids=lambda row: row['name'])
def test_set_yaml_has_explicit_generator_and_form_resolvers__F1225(row):
    from collimate.config_values import parse_set_yaml, resolve_generator_values, resolve_form_values
    parsed = parse_set_yaml(row['text'])
    assert resolve_generator_values(parsed) == row['generator']
    fields, values = resolve_form_values(parsed)
    assert all(isinstance(field.get('key'), str) and field['key'] for field in fields)
    assert values == row['form']


def test_file_codec_preserves_text_and_binary_wire_values__F1228():
    from collimate.wire import encode_files, decode_files
    files = {'text.txt': 'a\r\nb\n', 'binary.bin': b'\x00\xff', 'view': memoryview(b'abc')}
    encoded = encode_files(files)
    assert encoded == {'text.txt': {'text': 'a\r\nb\n'}, 'binary.bin': {'b64': 'AP8='}, 'view': {'b64': 'YWJj'}}
    assert decode_files(encoded) == {'text.txt': 'a\r\nb\n', 'binary.bin': b'\x00\xff', 'view': b'abc'}
    assert decode_files({'bad': {'b64': 'a'}, 'empty': None}) == {'bad': b'', 'empty': ''}


def test_run_paths_follow_the_cross_language_contract__F1238_F1884():
    from collimate import pointers
    fixture = json.loads((Path(__file__).parents[1] / 'shared/run-path-contract.json').read_text())
    for row in fixture['paths']:
        assert pointers.norm_path(row['path']) == row['normalized'], row
        assert pointers.is_run_path(row['path']) is row['is_run_path'], row
        assert (pointers.run_label(row['path']) or None) == row['label'], row
    for row in fixture['labels']:
        assert bool(pointers.LABEL_RE.fullmatch(row['label'])) is row['valid']
        assert pointers.run_card_path(row['label']) == row['card']


def test_effective_grants_follow_the_host_policy_fixture__F1239():
    from dataclasses import asdict
    from collimate.permissions import AXES, AXIS_LEVELS, Grant
    fixture = json.loads((Path(__file__).parents[1] / 'shared/permissions-contract.json').read_text())
    assert list(AXES) == fixture['axes']
    assert {name:list(levels) for name, levels in AXIS_LEVELS.items()} == fixture['levels']
    for row in fixture['definitions']:
        parsed = asdict(Grant.from_dict(row['grant']))
        parsed['internet_allow'] = list(parsed['internet_allow'])
        assert parsed == row['grant']


def test_generator_ref_builds_all_target_facts_from_manifest__F0205_F1282_F1268_F1311():
    from collimate.lint import GeneratorRef
    manifest = {'main_config':'settings.set_yaml', 'config_folders':[{'name':'_skills'}],
                'required_files':['input.txt'], 'account_requirements':{'model':{'kind':'model'}},
                'requires_docker':True, 'runs_on':'local-machine', 'enforces':['commands']}
    ref = GeneratorRef.from_manifest(manifest, read_file=lambda path:'fields:\n- {key: x, type: text}\n')
    assert ref.config_folders == {'_skills'}
    assert ref.main_config == 'settings.set_yaml'
    assert ref.set_yaml_fields == {'settings.set_yaml':{'x'}}
    assert ref.required_files == {'input.txt'}
    assert ref.account_requirements == manifest['account_requirements']
    assert ref.requires_docker and ref.runs_on == 'local-machine'
    assert ref.enforces == {'commands'} and ref.main_fields == [{'key':'x','type':'text'}]
    assert GeneratorRef.from_manifest(manifest).set_yaml_fields == {'settings.set_yaml':None}
    assert GeneratorRef.from_manifest(manifest, read_file=lambda path:'[').main_fields == []
    assert ref.requires_docker_if is None
    conditional = GeneratorRef.from_manifest(
        {**manifest, 'requires_docker': False, 'requires_docker_if': {'x': 'on'}},
        read_file=lambda path:'fields:\n- {key: x, type: dropdown, options: [on, off]}\n')
    assert conditional.requires_docker_if == {'x': 'on'}
    assert conditional.docker_required({'x': 'on'}) and not conditional.docker_required({'x': 'off'})


def test_surfaces_is_the_public_owner_of_resolve_opens_GN03():
    import collimate
    from collimate import surfaces
    assert 'surfaces' in collimate.__all__
    assert {'resolve_opens', 'surface_path', 'surface_declaration'} <= set(surfaces.__all__)
    assert 'public' in (surfaces.__doc__ or '').lower()


def test_lint_exports_the_single_manifest_vocabulary__F1283_F1305_F1888():
    from collimate import lint
    assert {'settings','accounts','generator'} <= lint.RUNNER_FIELDS
    assert {'main_config','timeout_seconds','enforces'} <= lint.GENERATOR_FIELDS
    assert {'entrypoint','extensions'} <= lint.RENDERER_FIELDS
    assert lint.GENERATOR_RUNS_ON == {'anywhere','local-machine'}
    assert lint.CONFIG_FOLDER_FIELDS == {'name','description','sample'}
    assert {'kinds','auth_types','required'} <= lint.ACCOUNT_REQUIREMENT_FIELDS
    assert 'metadata' in lint.ACCOUNT_PROPERTY_ROOTS
    assert {'secrets','__proto__','prototype','constructor'} == lint.ACCOUNT_PROPERTY_FORBIDDEN


def test_account_kinds_are_the_four_kinds():
    from collimate import lint
    assert lint.ACCOUNT_KINDS == {"model", "git", "account", "custom"}


def test_main_config_is_required_and_timeout_is_official__F1267_F2019():
    from collimate.lint import lint_generator_manifest_data
    manifest = {'id':'g','name':'G','summary':'S','entrypoint':'main.py','timeout_seconds':1}
    issues = lint_generator_manifest_data(manifest, files_present={'main.py'})
    assert any(i.code == 'missing-required' and i.field == 'main_config' for i in issues)
    assert not any(i.field == 'timeout_seconds' for i in issues)


def test_unknown_target_is_error_and_no_resolver_means_partial_visibility__F1262():
    from collimate.lint import lint_runner_manifest_data, Level
    manifest = {'id':'r','name':'R','generator':'missing','settings':{}}
    assert any(i.code == 'runner-generator' and i.level == Level.ERROR
               for i in lint_runner_manifest_data(manifest, resolve=lambda gid:None))
    assert not any(i.code == 'runner-generator' for i in lint_runner_manifest_data(manifest))
