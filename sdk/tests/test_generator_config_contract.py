"""Generator-owned complete configs validate files and malformed metadata safely."""

import pytest
import yaml

from collimate.lint import Level, lint_generator_manifest_data, lint_path


def content():
    definition = {
        'id': 'g', 'name': 'G', 'summary': 'Example', 'entrypoint': 'g.py',
        'main_config': 'settings.set_yaml',
        'config_folders': [{'name': 'skills', 'description': 'Skills'}],
        'required_files': ['notes.md'],
        'configs': [{'id': 'default', 'name': 'Default', 'path': 'manifest.config_yaml'}],
    }
    files = {
        'g.py': 'pass',
        'settings.set_yaml': 'fields: [{key: text, type: text, default: hello}]',
        'manifest.config_yaml': yaml.safe_dump({
            'name': 'Default', 'prompt': 'Saved task', 'settings': {'settings.set_yaml': {'text': 'hello'}},
            'folders': ['skills'], 'accounts': {}, 'permissions': {}, 'packages': {}, 'tags': [],
        }),
        'files/skills/SKILL.md': 'instructions',
        'files/notes.md': 'required',
    }
    return definition, files


def issues(definition, files):
    return lint_generator_manifest_data(definition, files_present=set(files), file_contents=files)


def test_root_level_config_resolves_its_supplemental_files():
    definition, files = content()
    assert issues(definition, files) == []


def test_named_config_folder_cannot_claim_missing_contents():
    definition, files = content()
    del files['files/skills/SKILL.md']
    assert 'runner-folder-empty' in {i.code for i in issues(definition, files)}


def test_account_taxonomy_requirements_accept_git_and_custom_fields():
    definition, files = content()
    definition['account_requirements'] = {
        'model': {'kind': 'model', 'protocols': ['openai-responses'],
                  'auth_types': ['api_key'], 'harnesses': ['codex'], 'required': True},
        'git': {'label': 'Git account', 'kind': 'git', 'capabilities': ['git']},
        'custom': {'kinds': ['custom'], 'custom_type': 'issue-tracker'},
    }
    assert issues(definition, files) == []


@pytest.mark.parametrize('requirement', [
    {'kind': []}, {'kind': 'unrecognized'}, {'kinds': ['unrecognized']},
    {'kind': 'repository'}, {'kinds': ['repository']},
    {'kind': 'model', 'harnesses': 'codex'}, {'kind': 'custom', 'custom_type': ''},
    {'kind': 'custom', 'required_fields': ['secret']},
])
def test_invalid_taxonomy_requirements_are_reported(requirement):
    definition, files = content()
    definition['account_requirements'] = {'connection': requirement}
    assert any(issue.code.startswith('account-') for issue in issues(definition, files))


@pytest.mark.parametrize('role,prop', [
    ('missing', 'metadata.harness'), ('model', 'secrets.api_key'),
    ('model', 'metadata.__proto__.value'), ('model', 'metadata..harness'), ('model', None),
])
def test_account_derived_fields_require_declared_role_and_public_property(role, prop):
    definition, files = content()
    definition['account_requirements'] = {'model': {'kind': 'model'}}
    files['settings.set_yaml'] = yaml.safe_dump({'fields': [{
        'key': 'text', 'type': 'text', 'default': 'hello',
        'account_role': role, 'account_property': prop,
    }]})
    assert 'config-field-account' in {issue.code for issue in issues(definition, files)}


@pytest.mark.parametrize('field', ['main_config', 'config_folders', 'account_requirements', 'configs', 'required_files'])
@pytest.mark.parametrize('value', [None, True, 7, 'unexpected', {}, [None], [{}]])
def test_malformed_generator_structures_never_crash_lint(tmp_path, field, value):
    definition, files = content()
    definition[field] = value
    # main_config is one of the mutated fields: a malformed or missing value
    # must surface as lint issues (or be skipped), never as a crash.
    issues(definition, files)
    for path, body in {'example.generator': yaml.safe_dump(definition), **files}.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    lint_path(tmp_path)


@pytest.mark.parametrize('fields', [[None], ['bad'], [{'key': 'x', 'type': 'text'}, None]])
def test_malformed_main_fields_return_issues(fields):
    definition, files = content()
    files['settings.set_yaml'] = yaml.safe_dump({'fields': fields})
    assert any(i.level == Level.ERROR for i in issues(definition, files))


@pytest.mark.parametrize('path', ['C:/settings.set_yaml', 'C:settings.set_yaml', 'c:/settings.set_yaml'])
def test_generator_paths_reject_windows_drive_prefixes(path):
    definition, files = content()
    definition['main_config'] = path
    files[path] = files['settings.set_yaml']
    assert 'main-config' in {i.code for i in issues(definition, files)}


def test_config_contract_errors_name_their_field():
    definition, files = content()
    files['settings.set_yaml'] = 'fields: [{key: text, type: text}, {key: text, type: text}]'
    definition['account_requirements'] = {'model': {'kind': 'bogus'}}
    got = {(i.code, i.field) for i in issues(definition, files)}
    assert ('config-field-duplicate', 'settings.set_yaml:text') in got
    assert ('account-kind', 'account_requirements.model.kind') in got
