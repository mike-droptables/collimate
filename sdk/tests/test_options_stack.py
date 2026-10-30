# SPDX-License-Identifier: MIT
"""D07 pure declaration parsing and logical path mapping."""
import importlib

import pytest
import yaml


def module():
    return importlib.import_module('collimate.options_stack')


def document(options, rest='keep', path='.options_stack'):
    return module().parse_options_stack(path, yaml.safe_dump({'options': options, 'rest': rest}))


def test_options_selection_and_literal_mapping__D07():
    m = module()
    paths = ['deep/a.options_stack', 'z.options_stack', '.options_stack', 'a.options_stack']
    assert m.select_options_path(paths) == '.options_stack'
    assert m.select_options_path(paths[:1] + ['b/c.options_stack', 'b.options_stack']) == 'b.options_stack'
    doc = document([{'name': 'Literal', 'root': 'copy[1]', 'files': ['**', '../brief.md']}])
    plan = m.map_options_stack(doc, ['.options_stack', 'copy[1]/a.bin', 'copy[1]/deep/b.txt', 'brief.md', 'extra.md'])
    assert plan.options == [{'name': 'Literal', 'files': {'a.bin': 'copy[1]/a.bin', 'deep/b.txt': 'copy[1]/deep/b.txt', 'brief.md': 'brief.md'}}]
    assert plan.kept == {'extra.md'} and plan.retain_source


def test_native_options_reads_actual_declaration__D07(native_bridge, tmp_path):
    from collimate import native
    text = 'options:\n- name: Choice\n  files: ["**"]\n'
    (tmp_path / '.options_stack').write_text(text)
    (tmp_path / 'a.bin').write_bytes(b'\xff\x00')
    assert native.options() == {'path': '.options_stack', 'options': [{'name': 'Choice', 'files': ['**']}], 'rest': 'keep'}
    assert native.read_file('a.bin') == b'\xff\x00'


def test_testing_options_fixture_seeds_definition_and_bytes__D07():
    from collimate.testing import run_generator_callable
    async def run():
        import gen
        gen.update_file('summary.txt', gen.options()['options'][0]['name'])
        gen.update_file('copy.bin', gen.read_file('source/data.bin'))
    result = run_generator_callable(run, options={'options': [{'name': 'Actual', 'root': 'source', 'files': ['**']}]},
                                    inputs={'source/data.bin': b'\xff\x00'})
    assert result.ok, result.error
    assert result.files['summary.txt'] == 'Actual'
    assert result.files['copy.bin'] == b'\xff\x00'
    assert '.options_stack' in result.files


@pytest.mark.parametrize('glob,expected', [
    ('*.txt', {'a.txt'}), ('?.txt', {'a.txt'}), ('**/*.txt', {'a.txt', 'sub/b.txt'}),
    ('**', {'a.txt', 'sub/b.txt', 'file[1].md', 'file1.md', 'x{a}.md'}),
    ('file[1].md', {'file[1].md'}), ('x{a}.md', {'x{a}.md'}),
])
def test_glob_dialect_is_literal_except_three_tokens__D07(glob, expected):
    doc = document([{'name': 'Dialect', 'files': [glob]}])
    plan = module().map_options_stack(doc, ['.options_stack', 'a.txt', 'sub/b.txt', 'file[1].md', 'file1.md', 'x{a}.md'])
    assert set(plan.options[0]['files']) == expected


@pytest.mark.parametrize('rest,expected,retained', [('keep', {'extra.txt', 'protected.txt'}, True),
                                                   ('drop', {'protected.txt'}, True),
                                                   ('each', {'protected.txt'}, True)])
def test_remainder_and_generic_protection__D07(rest, expected, retained):
    doc = document([{'name': 'A', 'root': 'a', 'files': ['**']},
                    {'name': 'Overlap', 'root': 'a', 'files': ['**']}], rest)
    plan = module().map_options_stack(doc, ['.options_stack', 'a/file.txt', 'extra.txt', 'protected.txt'],
                                     protected_paths=['protected.txt'])
    assert plan.kept == expected and plan.retain_source == retained
    for option in plan.options:
        assert option['files']['file.txt'] == 'a/file.txt'
        assert ('extra.txt' in option['files']) == (rest == 'each')
        assert 'protected.txt' not in option['files']


@pytest.mark.parametrize('rest,retained', [('keep', True), ('drop', False), ('each', False)])
def test_empty_remainder_source_retention__D07(rest, retained):
    plan = module().map_options_stack(document([{'name': 'All', 'files': ['**']}], rest), ['.options_stack', 'x.txt'])
    assert plan.kept == set() and plan.retain_source == retained


@pytest.mark.parametrize('option,paths,reason', [
    ({'name': 'Missing', 'root': 'missing', 'files': ['**']}, ['x.txt'], 'root'),
    ({'name': 'Empty', 'files': ['missing*']}, ['x.txt'], 'no exportable'),
    ({'name': 'Escape', 'root': '../outside', 'files': ['**']}, ['x.txt'], 'escapes'),
    ({'name': 'Absolute', 'files': ['/etc/**']}, ['x.txt'], 'invalid relative'),
    ({'name': 'Drive', 'root': 'C:/outside', 'files': ['**']}, ['x.txt'], 'invalid relative'),
    ({'name': 'Collision', 'root': 'a', 'files': ['**', '../x.txt']}, ['a/x.txt', 'x.txt'], 'collision'),
])
def test_mapping_refusal_names_option_before_return__D07(option, paths, reason):
    with pytest.raises(ValueError, match=option['name'] + '.*' + reason):
        module().map_options_stack(document([option]), ['.options_stack'] + paths)


def test_each_preserves_original_paths_and_refuses_collision__D07():
    with pytest.raises(ValueError, match='Choice.*remainder collision'):
        module().map_options_stack(document([{'name': 'Choice', 'root': 'a', 'files': ['**']}], 'each'),
                                  ['.options_stack', 'a/x.txt', 'x.txt'])


def test_nested_definition_mapping_and_escape__D07():
    m = module()
    doc = document([{'name': 'Nested', 'root': 'a', 'files': ['**', '../brief.md']}], path='nested/custom.options_stack')
    plan = m.map_options_stack(doc, ['nested/custom.options_stack', 'nested/a/file.txt', 'nested/brief.md', 'root.txt'])
    assert plan.options[0]['files'] == {'file.txt': 'nested/a/file.txt', 'brief.md': 'nested/brief.md'}
    with pytest.raises(ValueError, match='escapes the options folder'):
        m.map_options_stack(document([{'name': 'Outside', 'files': ['../root.txt']}], path='nested/custom.options_stack'),
                            ['nested/custom.options_stack', 'root.txt'])


def test_selected_protected_definition_refuses_without_fallback__D07():
    files = {'.options_stack': 'options: []', 'nested/a.options_stack': 'options: []'}
    with pytest.raises(ValueError, match='selected options definition is protected'):
        module().read_options_stack(files, files.__getitem__, protected_paths=['.options_stack'])
    doc = document([{'name': 'A', 'files': ['**']}])
    with pytest.raises(ValueError, match='selected options definition is protected'):
        module().map_options_stack(doc, ['.options_stack', 'x'], protected_paths=['.options_stack'])


@pytest.mark.parametrize('text', ['[broken', 'null', 'options: []', 'options: [a]',
                                  'options: [{name: A, files: []}]',
                                  'options: [{name: A, files: ["**"], root: []}]',
                                  'options: [{name: A, files: ["**"]}]\nrest: invalid',
                                  'options: [{name: A, files: ["**"]}]\nextra: bad'])
def test_schema_refusal__D07(text):
    with pytest.raises(ValueError):
        module().parse_options_stack('.options_stack', text)


def test_only_consumed_definition_is_excluded__D07():
    doc = document([{'name': 'All', 'files': ['**']}])
    plan = module().map_options_stack(doc, ['.options_stack', 'nested/other.options_stack', 'x.txt'])
    assert set(plan.options[0]['files']) == {'nested/other.options_stack', 'x.txt'}


def test_folders_preserve_previous_naming_rule__D07():
    m = module()
    assert m.sanitize_folder(' ../Name\\part ', 'fallback') == '..-Name-part'
    assert m.sanitize_folder('...', 'fallback') == 'fallback'
    used = set()
    assert [m.dedupe_folder('A', used) for _ in range(3)] == ['A', 'A-2', 'A-3']


@pytest.mark.parametrize('root', ['a*', 'a?', 'a[1]', 'a{1}'])
def test_option_root_is_literal_even_with_glob_characters__D07(root):
    plan = module().map_options_stack(document([{'name': 'Literal root', 'root': root, 'files': ['**']}]),
                                     ['.options_stack', root + '/x.txt', 'abc/y.txt', 'ab/y.txt', 'a1/y.txt'])
    assert plan.options[0]['files'] == {'x.txt': root + '/x.txt'}


def test_shared_options_mapping_contract__D07():
    import json
    from pathlib import Path
    cases = json.loads((Path(__file__).resolve().parents[1] / 'shared/options-stack-contract.json').read_text())
    for case in cases:
        doc = module().parse_options_stack(case['path'], yaml.safe_dump(case['document']))
        plan = module().map_options_stack(doc, case['paths'], protected_paths=case['protected'])
        assert plan.options == case['options'], case['name']
        assert plan.kept == set(case['kept']), case['name']
        assert plan.retain_source == case['retain_source'], case['name']


def test_entrypoint_options_fixture_seeds_same_document__D07(tmp_path, monkeypatch):
    from collimate.testing import run_generator
    # Entrypoint import and dev-mode writes must stay in this test's directory.
    monkeypatch.chdir(tmp_path)
    entrypoint = tmp_path / 'run.py'
    entrypoint.write_text('import gen\nasync def run():\n    gen.update_file("seen", gen.options()["options"][0]["name"])\n    gen.update_file("copy.bin", gen.read_file("data.bin"))\nif __name__ == "__main__":\n    gen.main(run)\n')
    result = run_generator(entrypoint, options={'options': [{'name': 'Actual', 'files': ['**']}]}, inputs={'data.bin': b'\xff\x00'})
    assert result.ok, result.error
    assert result.files['seen'] == 'Actual' and result.files['copy.bin'] == b'\xff\x00'


@pytest.mark.parametrize('base', ['odd*', 'odd?'])
def test_options_parent_folder_is_literal__D07(base):
    plan = module().map_options_stack(document([{'name': 'Literal parent', 'files': ['**']}], path=base + '/choices.options_stack'),
                                     [base + '/choices.options_stack', base + '/x.txt', 'oddity/y.txt', 'oddy/z.txt'])
    assert plan.options[0]['files'] == {'x.txt': base + '/x.txt'}


def test_parent_traversal_keeps_literal_prefix_components__D07():
    doc = document([{'name': 'Literal traversal', 'root': 'a*', 'files': ['**', '../brief.md']}],
                   path='odd?/choices.options_stack')
    plan = module().map_options_stack(doc, ['odd?/choices.options_stack', 'odd?/a*/x.txt', 'odd?/brief.md', 'oddy/brief.md'])
    assert plan.options[0]['files'] == {'x.txt': 'odd?/a*/x.txt', 'brief.md': 'odd?/brief.md'}
    with pytest.raises(ValueError, match='traverses a glob'):
        module().map_options_stack(document([{'name': 'Refused', 'files': ['a*/../x.txt']}]), ['.options_stack', 'x.txt'])


def test_plain_generator_import_needs_no_yaml_parser__D07():
    import subprocess
    import sys
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / 'src'
    result = subprocess.run(
        [sys.executable, '-S', '-c',
         'import sys; sys.path.insert(0, sys.argv[1]); import gen; '
         'from collimate.options_stack import read_options_stack; '
         'assert read_options_stack([], lambda _: "") is None; '
         'assert callable(gen.update_file)', str(source)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
