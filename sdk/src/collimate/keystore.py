# SPDX-License-Identifier: MIT
"""The keystore's structures, never its values. Public: a generator may
import this beside ``gen``.

A Key manager entry is a package of settings coupled with its secrets: the
user picks it as a unit for a runner's account role, and it always holds at
least one private value. Besides the shipped kinds (``model``, ``git``,
``account``, each over one of the app's services), the user builds ``custom``
entries by hand: a nested structure of keys, each one secret or public.

A generator never writes the keystore. Its manifest says what it needs: a
role naming a shipped kind with its filters, or a ``custom`` role with the
exact ``structure`` it reads::

    account_requirements:
      tracker:
        label: Issue tracker
        kind: custom
        structure:
          url: public
          auth:
            user: public
            token: secret

An entry fills that role only when its structure is the same: the same keys,
the same nesting, the same secret flags, nothing more (:func:`same_structure`).
The entry's name is the user's own label and never part of the match. When
no entry has the structure, the runner picker shows it so the user can build
it in the Key manager.

:func:`check_structure` is the one reading of a structure (a role's and an
entry's alike). ``gen.keystore_structures()`` answers the structures the
keystore accepts, as :class:`KeystoreStructures`: the shipped kinds and the
structures of the user's custom entries, keys and flags only. It exists for
manifest authoring (a generator writing a generator matches an existing
structure before declaring a new one); a run's own values come from
``gen.account(role)``, the snapshot taken when the run started.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Union

__all__ = [
    "KEY_PATTERN", "MAX_DEPTH", "MAX_KEYS", "CustomStructure", "Group", "Key", "KeystoreStructures",
    "ShippedService", "check_structure", "leaves", "same_structure", "structure_from_leaves",
]

#: A key's name: a letter, then letters, digits or underscores (at most 64).
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
#: The deepest nesting a structure takes (a key under this many groups).
MAX_DEPTH = 6
#: The most keys (leaves) one structure holds.
MAX_KEYS = 64
_FLAGS = ("secret", "public")


def check_structure(raw: Any, *, where: str = "structure") -> dict:
    """``raw`` checked and in canonical form (keys sorted at every level): a
    mapping from key names to ``"secret"``, ``"public"`` or a nested mapping
    (a group). At least one key is secret: an entry is the place private
    values live. ValueError says what is wrong."""
    count = 0

    def walk(node: Any, path: str, depth: int) -> dict:
        nonlocal count
        if not isinstance(node, Mapping) or not node:
            raise ValueError(f"{path} must map key names to secret, public or a nested group")
        if depth > MAX_DEPTH:
            raise ValueError(f"{path} nests deeper than {MAX_DEPTH} groups")
        result: dict = {}
        for name in sorted(node, key=str):
            if not isinstance(name, str) or not KEY_PATTERN.fullmatch(name):
                raise ValueError(f"{path}: {name!r} is not a key name (a letter, then letters, digits or underscores)")
            value = node[name]
            if isinstance(value, Mapping):
                result[name] = walk(value, f"{path}.{name}", depth + 1)
            elif value in _FLAGS:
                count += 1
                result[name] = value
            else:
                raise ValueError(f"{path}.{name} must be secret, public or a nested group")
        return result

    result = walk(raw, where, 0)
    if count > MAX_KEYS:
        raise ValueError(f"{where} holds more than {MAX_KEYS} keys")
    if not any(secret for _path, secret in leaves(result)):
        raise ValueError(f"{where} needs at least one secret key: a keystore entry holds private values")
    return result


def leaves(structure: Mapping) -> tuple[tuple[str, bool], ...]:
    """Every key of a checked structure as ``(dotted path, secret)``, in
    canonical order."""
    found: list[tuple[str, bool]] = []

    def walk(node: Mapping, prefix: str) -> None:
        for name in sorted(node):
            value = node[name]
            path = f"{prefix}{name}"
            if isinstance(value, Mapping):
                walk(value, path + ".")
            else:
                found.append((path, value == "secret"))
    walk(structure, "")
    return tuple(found)


def structure_from_leaves(pairs) -> dict:
    """The structure whose keys are ``pairs`` of ``(dotted path, secret)``,
    checked (:func:`check_structure`); ValueError for a path that is both a
    key and a group, or a repeated one."""
    tree: dict = {}
    for path, secret in pairs:
        parts = str(path).split(".")
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"{path!r}: {part!r} is a key and cannot hold others")
            node = child
        if parts[-1] in node:
            raise ValueError(f"{path!r} is named twice, or is both a key and a group")
        node[parts[-1]] = "secret" if secret else "public"
    return check_structure(tree)


def same_structure(a: Any, b: Any) -> bool:
    """Whether ``a`` and ``b`` are one structure: the same keys, nesting and
    secret flags. Either malformed is never the same as anything."""
    try:
        return check_structure(a) == check_structure(b)
    except ValueError:
        return False


# -- the introspection answer ---------------------------------------------------
# What gen.keystore_structures() returns. Every object is frozen and holds
# names and flags only: a key's name and whether it is secret, a group's name
# and its keys, a shipped service's ids and enums. No field can hold a value,
# and the reader below refuses an answer with anything else in it.

@dataclass(frozen=True)
class Key:
    """One key of a structure: its name and whether it is secret."""
    name: str
    secret: bool


@dataclass(frozen=True)
class Group:
    """A nested label and the keys under it."""
    name: str
    keys: tuple[Union["Key", "Group"], ...]


@dataclass(frozen=True)
class CustomStructure:
    """A structure the user built in the Key manager (one of possibly many
    entries with it; the answer never says which, or how many)."""
    keys: tuple[Union[Key, Group], ...]

    def as_manifest(self) -> dict:
        """The mapping a ``custom`` role's ``structure:`` takes to match it."""
        def node(items) -> dict:
            return {item.name: ("secret" if item.secret else "public") if isinstance(item, Key) else node(item.keys)
                    for item in items}
        return node(self.keys)

    def leaves(self) -> tuple[tuple[str, bool], ...]:
        """Its keys as ``(dotted path, secret)``."""
        return leaves(self.as_manifest())


@dataclass(frozen=True)
class ShippedService:
    """A shipped kind over one of the app's services: what a role names
    (``kind`` and ``services``, and the ``auth_types``, ``protocols`` and
    ``capabilities`` it may filter on) to take such entries."""
    kind: str
    service: str
    name: str
    protocol: str
    auth_types: tuple[str, ...]
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class KeystoreStructures:
    """The structures the keystore accepts: the shipped kinds, then the
    distinct structures of the user's custom entries."""
    shipped: tuple[ShippedService, ...]
    custom: tuple[CustomStructure, ...]

    def matching(self, structure: Any) -> CustomStructure | None:
        """The custom structure ``structure`` (a role's mapping) is, if the
        user has built it."""
        try:
            wanted = check_structure(structure)
        except ValueError:
            return None
        return next((item for item in self.custom if item.as_manifest() == wanted), None)


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHIPPED_FIELDS = frozenset({"kind", "service", "name", "protocol", "auth_types", "capabilities"})


def _ids(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in value):
        raise ValueError(f"{what} must be a list of ids")
    return tuple(value)


def _keys(structure: Mapping) -> tuple[Union[Key, Group], ...]:
    return tuple(Group(name, _keys(value)) if isinstance(value, Mapping) else Key(name, value == "secret")
                 for name, value in structure.items())


def structures_from_answer(answer: Any) -> KeystoreStructures:
    """The host's answer to ``keystore_structures`` read into the frozen,
    value-free objects above; ValueError (nothing returned) for any field or
    shape the answer may not carry."""
    if not isinstance(answer, Mapping) or set(answer) != {"shipped", "custom"}:
        raise ValueError("keystore structures: expected shipped and custom")
    if not isinstance(answer["shipped"], list) or not isinstance(answer["custom"], list):
        raise ValueError("keystore structures: shipped and custom are lists")
    shipped = []
    for row in answer["shipped"]:
        if not isinstance(row, Mapping) or set(row) != _SHIPPED_FIELDS:
            raise ValueError("keystore structures: a shipped service has exactly kind, service, name, protocol, "
                             "auth_types and capabilities")
        name = row["name"]
        if not isinstance(name, str) or not name or len(name) > 80 or "\n" in name:
            raise ValueError("keystore structures: a shipped service's name is one short line")
        kind, service, protocol = (_ids([row[key]], key)[0] for key in ("kind", "service", "protocol"))
        shipped.append(ShippedService(kind, service, name, protocol, _ids(row["auth_types"], "auth_types"),
                                      _ids(row["capabilities"], "capabilities")))
    custom = []
    for raw in answer["custom"]:
        structure = check_structure(raw, where="keystore structures: a custom structure")
        custom.append(CustomStructure(_keys(structure)))
    canonical = [json.dumps(item.as_manifest(), sort_keys=True) for item in custom]
    if len(set(canonical)) != len(canonical):
        raise ValueError("keystore structures: a custom structure is listed twice")
    return KeystoreStructures(tuple(shipped), tuple(custom))
