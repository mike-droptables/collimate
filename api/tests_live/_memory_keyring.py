# SPDX-License-Identifier: MIT
"""A keyring backend that lives in the server process.

The live suite's test server always runs on this backend (the conftest
selects it through ``PYTHON_KEYRING_BACKEND``). The null backend stores
nothing, so saving an account with a secret fails with 503 and the Key
manager scenarios could not run at all; the real OS keychain stores, but the
secrets a session saves are the developer's own (an OpenRouter key, the
Claude login's access token), and a server the suite SIGKILLs — or a pytest
that crashes before deleting its accounts — would leave them there as orphan
slots. This backend keeps secrets in a dict for the life of the server:
nothing reaches the developer's keychain, and nothing survives the session.
"""
from __future__ import annotations

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from jaraco.classes.properties import classproperty


class MemoryKeyring(KeyringBackend):
    @classproperty
    def priority(cls) -> float:
        return 1.0

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self._store[(service, username)]
        except KeyError:
            raise PasswordDeleteError(f"{service}/{username} is not stored") from None
