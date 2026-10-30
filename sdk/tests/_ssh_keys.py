# SPDX-License-Identifier: MIT
"""Throwaway SSH keys made by ``ssh-keygen`` at test time, so no private key is
committed (the public mirror's secret scan refuses one).

:func:`throwaway_keys` makes, once per process, an Ed25519, an ECDSA, an RSA
2048 key, an RSA 2048 key in the old PEM format (``rsa_pem``) and an Ed25519
key protected by the passphrase ``x`` (``ed25519_encrypted``), all with the
comment ``collimate-test``, reads them and removes the files. When
``ssh-keygen`` is missing it skips what asked for a key, as the git tests
skip without git: the test, or the whole module when the module builds a
key-bearing value at import.

The SDK tests reach it through the session-scoped ``ssh_keys`` fixture in
``_git_fixtures.py``; the app's tests, which cannot import ``sdk/tests``, load
this module by path. It imports only the standard library and pytest.
"""
from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

COMMENT = "collimate-test"
# name -> the ssh-keygen arguments that make it.
_SPECS = {
    "ed25519": ("-t", "ed25519", "-N", ""),
    "ecdsa": ("-t", "ecdsa", "-b", "256", "-N", ""),
    "rsa": ("-t", "rsa", "-b", "2048", "-N", ""),
    "rsa_pem": ("-t", "rsa", "-b", "2048", "-m", "PEM", "-N", ""),
    "ed25519_encrypted": ("-t", "ed25519", "-N", "x"),
}
# The kinds whose ``ssh-keygen -lf`` facts tests compare against.
_FINGERPRINTED = ("ed25519", "ecdsa", "rsa")
# Found at import, as GIT_OK is, so a test that empties PATH still gets keys.
SSH_KEYGEN = shutil.which("ssh-keygen")
# A synthetic PuTTY file; its format, not its key, is what tests need.
_PUTTY = Path(__file__).parent / "data" / "ssh_keys" / "ed25519_putty.txt"


@dataclass(frozen=True)
class SshKeys:
    """``text`` maps ``<name>`` to a private key and ``<name>.pub`` to its
    public key line; ``fingerprints`` maps ed25519, ecdsa and rsa to
    ``{"bits", "fingerprint", "type"}`` as ``ssh-keygen -lf`` prints them."""
    text: dict
    fingerprints: dict

    def __getitem__(self, name: str) -> str:
        if name == _PUTTY.name:
            return _PUTTY.read_text(encoding="utf-8")
        return self.text[name]

    def blob(self, name: str = "ed25519") -> str:
        """The base64 public key of ``name``."""
        return self.text[f"{name}.pub"].split()[1]

    def public(self, name: str = "ed25519") -> str:
        """``<keytype> <base64>`` of ``name``, without the comment."""
        return " ".join(self.text[f"{name}.pub"].split()[:2])


def throwaway_keys() -> SshKeys:
    """The process's test keys (made on first use); skips the caller (a test,
    or a module at import) when ``ssh-keygen`` is missing."""
    if SSH_KEYGEN is None:
        pytest.skip("needs ssh-keygen", allow_module_level=True)
    return _generate(SSH_KEYGEN)


def _run(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          timeout=60).stdout


@functools.cache
def _generate(keygen: str) -> SshKeys:
    text: dict = {}
    fingerprints: dict = {}
    with tempfile.TemporaryDirectory(prefix="collimate-test-keys-") as tmp:
        for name, args in _SPECS.items():
            path = Path(tmp) / name
            _run(keygen, "-q", *args, "-C", COMMENT, "-f", str(path))
            text[name] = path.read_text(encoding="utf-8")
            text[f"{name}.pub"] = path.with_suffix(".pub").read_text(encoding="utf-8")
            if name in _FINGERPRINTED:
                # "256 SHA256:... collimate-test (ED25519)"
                words = _run(keygen, "-lf", str(path.with_suffix(".pub"))).split()
                fingerprints[name] = {"bits": int(words[0]), "fingerprint": words[1],
                                      "type": words[-1].strip("()")}
    return SshKeys(text, fingerprints)
