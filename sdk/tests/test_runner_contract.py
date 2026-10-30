"""Saved-runner lint, asset reading and account contract."""
import hashlib

import pytest
import yaml

from collimate import native
from collimate.lint import GeneratorRef, Level, lint_runner_manifest_data
from collimate._host.packaging import read_asset_cells


def test_accounts_never_fall_back_to_ambient_keys(native_bridge,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','ambient')
    with pytest.raises(LookupError):native.account('model')
    native_bridge._rec_accounts={'model':{'revision':1,'model':'chosen','secrets':{'api_key':'one'}}}
    snapshot=native.account('model')
    snapshot['secrets']['api_key']='mutated'
    assert native.account('model')['secrets']['api_key']=='one'
    native.commit_account('model',expected_revision=1,secrets={'api_key':'two'})
    with pytest.raises(RuntimeError,match='revision'):
        native.commit_account('model',expected_revision=1,secrets={'api_key':'stale'})


def test_native_exposes_no_removed_run_or_secret_apis():
    for name in ('list_relays','relay_info','create_relay','set_relay_tag','delete_relay_tag','relay','api_key','secret','validate_config'):
        assert not hasattr(native,name),name


def recipe():
    return {'id':'r','name':'R','description':'Example','generator':'g','tags':['search'],
            'prompt':'saved','settings':{'settings.set_yaml':{'count':1}},'folders':[],
            'accounts':{},'permissions':{'docker':'none','internet':'full'},'packages':{}}


def target():
    return GeneratorRef(main_config='settings.set_yaml',set_yaml_fields={'settings.set_yaml':{'count'}},account_requirements={'model':{'kind':'model'}},enforces=frozenset())


def errors(data, **kw):
    return [i.code for i in lint_runner_manifest_data(data,resolve=lambda gid:target() if gid=='g' else None,**kw) if i.level==Level.ERROR]


def test_runner_lint_requires_full_main_settings():
    assert errors(recipe())==[]
    data=recipe()
    data['settings']['settings.set_yaml']={}
    assert 'runner-settings-complete' in errors(data)
    data=recipe()
    data['settings']['other.set_yaml']={}
    assert 'runner-main-config' in errors(data)


def test_runner_lint_rejects_legacy_tag_recipes_and_secret_values():
    data=recipe()
    data['tags']={'default':{'generator':'g'}}
    assert 'bad-type' in errors(data)
    data=recipe()
    data['accounts']={'model':{'api_key':'secret'}}
    assert 'runner-accounts' in errors(data)
    data=recipe()
    data['accounts']={'unknown':'option'}
    assert 'runner-account-role' in errors(data)
    data=recipe()
    data['exposed_config']=[]
    assert 'unknown-field' in errors(data)


def test_main_config_never_appears_in_files():
    assert 'runner-main-file' in errors(recipe(),files_present={'files/settings.set_yaml'})


def test_generator_cell_reads_complete_runner(tmp_path):
    cell=tmp_path/'generators'/'example'
    runner=cell/'saved-package'
    runner.mkdir(parents=True)
    generator=cell/'g'
    generator.mkdir()
    (generator/'g.generator').write_text('id: g\n')
    saved=recipe()
    saved['accounts']={'model':'local-option'}
    (runner/'invocation.runner').write_text(yaml.safe_dump(saved))
    files=runner/'files'
    files.mkdir()
    binary=b'\x00\xff\x89saved input'
    (files/'input.bin').write_bytes(binary)
    (files/'notes.md').write_text('saved notes')
    (cell_spec,)=read_asset_cells(tmp_path/'generators','generator')
    packed={f.name:f for f in cell_spec.files}
    assert yaml.safe_load(packed['saved-package/invocation.runner'].content)==saved
    assert packed['saved-package/files/notes.md'].content=='saved notes'
    assert packed['saved-package/files/input.bin'].data==binary
    assert packed['saved-package/files/input.bin'].blob_hash==hashlib.sha256(binary).hexdigest()
