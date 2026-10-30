# SPDX-License-Identifier: MIT
"""Actual repeated imports distinguish generator-owned modules from libraries."""
import importlib
import os
import subprocess
import sys
import types

import pytest

from collimate.testing import run_generator


def test_repeated_generator_imports_preserve_transitive_library_identity__F1255(tmp_path, monkeypatch):
    owned = tmp_path / 'owned'
    library = tmp_path / 'owned-library'
    added = tmp_path / 'added'
    for path in (owned, library / 'f1255_dependency', added):
        path.mkdir(parents=True)
    (library / 'f1255_dependency/__init__.py').write_text('from .model import Token\n')
    (library / 'f1255_dependency/model.py').write_text('class Token: pass\n')
    sibling = owned / 'f1255_sibling.py'
    helper = added / 'f1255_helper.py'
    sibling.write_text('VALUE = 1\n')
    helper.write_text('VALUE = 2\n')
    observed = types.ModuleType('f1255_observed')
    observed.rows = []
    monkeypatch.setitem(sys.modules, 'f1255_observed', observed)
    monkeypatch.syspath_prepend(str(library))
    before = list(sys.path)
    entry = owned / 'entry.py'
    entry.write_text(f'''
import sys, types
import f1255_dependency, f1255_sibling, f1255_observed
sys.path.insert(0, {str(added)!r})
import f1255_helper
sys.modules['f1255_injected'] = types.ModuleType('f1255_injected')
def run():
    f1255_observed.rows.append((f1255_dependency, f1255_dependency.Token,
                               f1255_dependency.Token(), f1255_sibling.VALUE, f1255_helper.VALUE))
''')
    try:
        first = run_generator(entry)
        assert first.ok, first.error
        sibling.write_text('VALUE = 1111\n')
        helper.write_text('VALUE = 2222\n')
        importlib.invalidate_caches()
        second = run_generator(entry)
        assert second.ok, second.error
        one, two = observed.rows
        assert one[3:] == (1, 2) and two[3:] == (1111, 2222)
        assert one[0] is two[0] and one[1] is two[1]
        assert isinstance(one[2], two[1])
        assert sys.modules['f1255_dependency.model'].Token is one[1]
        assert not {'f1255_sibling', 'f1255_helper', 'f1255_injected'} & sys.modules.keys()
        assert sys.path == before
    finally:
        for name in ('f1255_dependency', 'f1255_dependency.model', 'f1255_sibling',
                     'f1255_helper', 'f1255_injected'):
            sys.modules.pop(name, None)
        sys.path[:] = before


@pytest.mark.parametrize('failure', ['import', 'run'])
def test_owned_modules_cleaned_on_failure_without_evicting_prior_modules__F1255(tmp_path, monkeypatch, failure):
    prior = types.ModuleType('f1255_prior')
    prior.__file__ = str(tmp_path / 'prior.py')
    monkeypatch.setitem(sys.modules, 'f1255_prior', prior)
    (tmp_path / 'f1255_failure_helper.py').write_text('VALUE = 1\n')
    entry = tmp_path / 'entry.py'
    raising = "raise ValueError('owned failure')"
    source = 'import f1255_failure_helper, f1255_prior\n'
    source += raising if failure == 'import' else 'def run():\n    ' + raising
    entry.write_text(source)
    result = run_generator(entry)
    assert not result.ok and 'owned failure' in result.error
    assert 'f1255_failure_helper' not in sys.modules
    assert sys.modules['f1255_prior'] is prior


def test_real_sdk_first_import_keeps_identity_across_generator_runs__F1255(tmp_path):
    # A child interpreter makes first-import behavior independent of suite order.
    entry = tmp_path / 'entry.py'
    entry.write_text("import importlib, f1255_observed, _symtable, __hello__\nmodule = importlib.import_module('collimate._host.package_hash')\ndef run():\n    f1255_observed.rows.append(module)\n")
    code = """
import sys, types
from collimate.testing import run_generator
sys.modules.pop('collimate._host.package_hash', None)
sys.modules.pop('_symtable', None)
sys.modules.pop('__hello__', None)
assert '_symtable' in sys.builtin_module_names
observed = types.ModuleType('f1255_observed'); observed.rows = []
sys.modules['f1255_observed'] = observed
for repeat in range(2):
    result = run_generator(sys.argv[1])
    assert result.ok, result.error
assert '_symtable' in sys.modules
assert sys.modules['__hello__'].__spec__.origin == 'frozen'
assert observed.rows[0] is observed.rows[1]
assert sys.modules['collimate._host.package_hash'] is observed.rows[0]
assert observed.rows[0].content_hash({'entry.py': b'pass'}) == observed.rows[1].content_hash({'entry.py': b'pass'})
"""
    result = subprocess.run([sys.executable, '-c', code, str(entry)],
                            env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('mixed', [False, True])
def test_external_namespace_parent_and_child_survive_actual_repeated_imports__F1255(tmp_path, monkeypatch, mixed):
    owned = tmp_path / 'entry'
    library = tmp_path / 'library'
    owned.mkdir()
    namespace = library / 'f1255_external_namespace'
    namespace.mkdir(parents=True)
    if mixed:
        (owned / 'f1255_external_namespace').mkdir()
    (namespace / 'model.py').write_text('class Token: pass\n')
    monkeypatch.syspath_prepend(str(library))
    observed = types.ModuleType('f1255_observed')
    observed.rows = []
    monkeypatch.setitem(sys.modules, 'f1255_observed', observed)
    entry = owned / 'entry.py'
    entry.write_text('''
import f1255_external_namespace.model, f1255_observed
def run():
    f1255_observed.rows.append((f1255_external_namespace,
        f1255_external_namespace.model, f1255_external_namespace.model.Token()))
''')
    try:
        for _ in range(2):
            result = run_generator(entry)
            assert result.ok, result.error
        one, two = observed.rows
        assert one[0] is two[0] is sys.modules['f1255_external_namespace']
        assert one[1] is two[1] is sys.modules['f1255_external_namespace.model']
        assert isinstance(one[2], two[1].Token)
    finally:
        sys.modules.pop('f1255_external_namespace', None)
        sys.modules.pop('f1255_external_namespace.model', None)


def test_owned_namespace_parent_and_helpers_reload_across_actual_runs__F1255(tmp_path, monkeypatch):
    namespace = tmp_path / 'f1255_owned_namespace'
    namespace.mkdir()
    helper = namespace / 'helper.py'
    helper.write_text('VALUE = 1\n')
    observed = types.ModuleType('f1255_observed')
    observed.rows = []
    monkeypatch.setitem(sys.modules, 'f1255_observed', observed)
    entry = tmp_path / 'entry.py'
    entry.write_text('''
import f1255_owned_namespace.helper, f1255_observed
def run():
    f1255_observed.rows.append((f1255_owned_namespace,
        f1255_owned_namespace.helper.VALUE))
''')
    for value in (1, 2222):
        helper.write_text(f'VALUE = {value}\n')
        result = run_generator(entry)
        assert result.ok, result.error
        assert not {'f1255_owned_namespace', 'f1255_owned_namespace.helper'} & sys.modules.keys()
    assert [row[1] for row in observed.rows] == [1, 2222]
    assert observed.rows[0][0] is not observed.rows[1][0]


def test_mixed_namespace_from_import_reloads_owned_child_and_retains_external_child__F1255(tmp_path, monkeypatch):
    owned, external = tmp_path / 'owned', tmp_path / 'external'
    name = 'f1255_mixed_namespace'
    for root in (owned, external):
        (root / name).mkdir(parents=True)
    helper = owned / name / 'helper.py'
    helper.write_text('VALUE = 1\n')
    (external / name / 'model.py').write_text('class Token: pass\n')
    monkeypatch.syspath_prepend(str(external))
    observed = types.ModuleType('f1255_observed')
    observed.rows = []
    monkeypatch.setitem(sys.modules, 'f1255_observed', observed)
    entry = owned / 'entry.py'
    entry.write_text('''
import f1255_mixed_namespace.model, f1255_observed
from f1255_mixed_namespace import helper
def run():
    f1255_observed.rows.append((f1255_mixed_namespace,
        f1255_mixed_namespace.model, helper.VALUE))
''')
    try:
        for value in (1, 2222):
            helper.write_text(f'VALUE = {value}\n')
            result = run_generator(entry)
            assert result.ok, result.error
        one, two = observed.rows
        assert [one[2], two[2]] == [1, 2222]
        assert one[0] is two[0] is sys.modules[name]
        assert one[1] is two[1] is sys.modules[name + '.model']
        assert not hasattr(two[0], 'helper')
        assert name + '.helper' not in sys.modules
    finally:
        for module in (name, name + '.helper', name + '.model'):
            sys.modules.pop(module, None)


def test_owned_child_eviction_preserves_a_replaced_parent_attribute__F1255(tmp_path, monkeypatch):
    owned, external = tmp_path / 'owned', tmp_path / 'external'
    name = 'f1255_replaced_namespace'
    for root in (owned, external):
        (root / name).mkdir(parents=True)
    (owned / name / 'helper.py').write_text('VALUE = 1\n')
    monkeypatch.syspath_prepend(str(external))
    observed = types.ModuleType('f1255_observed')
    observed.replacement = object()
    monkeypatch.setitem(sys.modules, 'f1255_observed', observed)
    entry = owned / 'entry.py'
    entry.write_text('''
import f1255_replaced_namespace.helper, f1255_observed
def run():
    f1255_replaced_namespace.helper = f1255_observed.replacement
''')
    try:
        result = run_generator(entry)
        assert result.ok, result.error
        assert name + '.helper' not in sys.modules
        assert sys.modules[name].helper is observed.replacement
    finally:
        sys.modules.pop(name, None)
        sys.modules.pop(name + '.helper', None)
