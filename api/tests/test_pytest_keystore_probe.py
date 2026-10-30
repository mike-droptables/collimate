"""Keyring availability probe in the keystore: ``available()`` really runs
the probe against the ``keyring`` backend, answers a real bool (a missing
or broken OS keychain is False, never a truthy placeholder), and caches
the answer for the rest of the process.
"""
from __future__ import annotations

import sys
import types

import keystore


def _reset_cache():
    keystore._keychain_ok = None


def test_probe_returns_bool_not_function(monkeypatch):
    _reset_cache()
    fake_keyring = types.ModuleType("keyring")
    fake_keyring.get_password = lambda service, name: None
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    result = keystore.available()
    assert result is True
    assert keystore._keychain_ok is True


def test_probe_detects_broken_keychain(monkeypatch):
    _reset_cache()
    fake_keyring = types.ModuleType("keyring")

    def _boom(service, name):
        raise RuntimeError("no keychain backend")

    fake_keyring.get_password = _boom
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    result = keystore.available()
    assert result is False


def test_probe_result_is_cached(monkeypatch):
    _reset_cache()
    calls = []
    fake_keyring = types.ModuleType("keyring")

    def _probe(service, name):
        calls.append(1)
        return None

    fake_keyring.get_password = _probe
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    assert keystore.available() is True
    assert keystore.available() is True
    assert len(calls) == 1

    _reset_cache()
