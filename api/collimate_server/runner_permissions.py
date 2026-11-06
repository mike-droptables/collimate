"""Immutable runner grants and the host environment capability gate."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# The SDK owns the capability names its OPS table gates ops on.
from collimate._host.ipc import CAP_ENV_PLANE
# The SDK owns the permission ladders.
from collimate.permissions import (
    AXIS_LEVELS,
    COMMAND_LEVELS,
    DOCKER_DESCRIPTIONS,
    DOCKER_LEVELS,
    INTERNET_MODES,
)

# Public editor vocabulary uses the SDK ladders.
CAPABILITY_ENUMS: dict[str, tuple[str, ...]] = AXIS_LEVELS

PERMISSION_LABELS = {
    "docker": "Docker", "internet": "Internet access", "commands": "Commands",
    "update_packages": "Allow package updates", "internet_allow": "Allowed internet hosts",
}
PERMISSION_DESCRIPTIONS = {"docker": DOCKER_DESCRIPTIONS}

_DEFAULTS: dict[str, Any] = {
    # Default OPEN (the internet axis is the other one): the artifacts a
    # runner names should be the artifacts that run, so a runner opts OUT of
    # freshness for reproducibility/air-gap, never in.
    "update_packages": True,
    "docker": "none",
    "internet": "full",
    "internet_allow": (),
    # Omitted → deny: an agent runs no shell commands unless its runner says so.
    "commands": "deny",
}


def _rank(ladder: tuple[str, ...], value: str) -> int:
    return ladder.index(value)


CAPABILITIES = (CAP_ENV_PLANE,)


class RunnerPermissionsError(ValueError):
    """A permissions block that can't be parsed into a valid grant (bad enum,
    wrong type). Callers (catalog validation) catch this to mark a runner invalid
    rather than crash."""


# ---------------------------------------------------------------------------
# The internet_allow grammar — one definition, read by the grant, by
# the egress proxy (``egress_proxy.host_allowed``), so an
# entry means the same thing at every layer. An entry is a host name that
# reaches itself and every name below it at a label boundary: ``example.com``
# covers ``api.example.com`` but not ``notexample.com``. Wildcards, schemes,
# paths and ports are not part of the grammar; such an entry never matches
# (the linter warns ``runner-internet-allow-entry``).
# ---------------------------------------------------------------------------


def normalize_host_entry(entry: str) -> str:
    """One allowlist entry in its one spelling: surrounding whitespace and
    dots stripped, lowercased (``" .API.Example.com. "`` → ``api.example.com``).
    An entry that was only dots or whitespace comes back empty."""
    return entry.strip().strip(".").strip().lower()


def entry_covers(entry: str, name: str) -> bool:
    """Whether the normalized allowlist ``entry`` reaches the normalized host
    ``name``: equal, or ``name`` ends with ``"." + entry``. The same rule
    orders entries: an entry covers every entry it would match as a host."""
    return name == entry or name.endswith("." + entry)


def normalize_internet_allow(entries) -> tuple[str, ...]:
    """An allowlist in its one spelling: every entry normalized, blanks and
    entries another entry covers dropped, sorted. Two lists that reach the
    same hosts spell the same tuple."""
    names = {normalize_host_entry(e) for e in entries} - {""}
    return tuple(sorted(
        n for n in names if not any(o != n and entry_covers(o, n) for o in names)
    ))


@dataclass(frozen=True)
class RunnerPermissions:
    """A resolved, immutable runner capability grant carrying every axis.
    Build one with :meth:`from_definition` (defaults, validation and the one
    spelling of the allowlist)."""

    update_packages: bool
    docker: str
    internet: str
    # The proxy allowlist in its one spelling (:func:`normalize_internet_allow`),
    # and empty unless ``internet`` is ``proxy`` (the only mode that reads it),
    # so two grants that reach the same hosts compare equal.
    internet_allow: tuple[str, ...]
    # deny < ask < allow. Enforced by a generator that declares
    # ``enforces: [commands]``, not by any host gate.
    commands: str

    # -- construction --------------------------------------------------------

    @classmethod
    def from_definition(cls, perms: dict[str, Any] | None) -> "RunnerPermissions":
        """Resolve a definition's ``permissions`` mapping into a grant, applying the
        per-axis defaults (:data:`_DEFAULTS`) for omitted keys. Raises
        :class:`RunnerPermissionsError` on a bad enum value or wrong type — the
        block is malformed, not merely sparse. An omitted (``None``) block is
        the plain-host-subprocess baseline. An allowlist outside ``proxy``
        mode has no effect and resolves to empty (the linter warns
        ``runner-internet-allow-unused``)."""
        perms = perms or {}
        if not isinstance(perms, dict):
            raise RunnerPermissionsError(
                f"permissions must be a mapping, got {type(perms).__name__}"
            )

        unknown = set(perms) - set(_DEFAULTS)
        if unknown:
            raise RunnerPermissionsError(
                f"Delete unsupported permission axes: {', '.join(sorted(unknown))}"
            )

        def _enum(key: str, ladder: tuple[str, ...]) -> str:
            v = perms.get(key, _DEFAULTS[key])
            if not isinstance(v, str) or v not in ladder:
                raise RunnerPermissionsError(
                    f"permissions.{key}={v!r} is not one of {list(ladder)}"
                )
            return v

        def _bool(key: str) -> bool:
            v = perms.get(key, _DEFAULTS[key])
            if not isinstance(v, bool):
                raise RunnerPermissionsError(f"permissions.{key} must be a bool")
            return v

        allow = perms.get("internet_allow", [])
        if not isinstance(allow, list) or not all(isinstance(x, str) for x in allow):
            raise RunnerPermissionsError(
                "permissions.internet_allow must be a list of strings"
            )
        internet = _enum("internet", INTERNET_MODES)

        return cls(
            update_packages=_bool("update_packages"),
            docker=_enum("docker", DOCKER_LEVELS),
            internet=internet,
            internet_allow=normalize_internet_allow(allow) if internet == "proxy" else (),
            commands=_enum("commands", COMMAND_LEVELS),
        )

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain-JSON axis mapping: what a run's records store as its
        ``permissions`` and what :meth:`to_env` sends. Lists are lists, not
        tuples."""
        return {
            "update_packages": self.update_packages,
            "docker": self.docker,
            "internet": self.internet,
            "internet_allow": list(self.internet_allow),
            "commands": self.commands,
        }

    def to_env(self) -> str:
        """The ``COLLIMATE_PERMISSIONS`` env value: a JSON object carrying
        every axis (``collimate.permissions.AXES``), which is what the SDK
        requires of a host run — :data:`UNRESTRICTED` included."""
        return json.dumps(self.to_dict(), sort_keys=True)

    # -- combination ---------------------------------------------------------

    def invariant_error(self) -> str | None:
        """The reason string when the one hard cross-axis invariant is violated
        (``internet != full`` without ``docker == limited``), else ``None``. Not
        applied automatically — the catalog surfaces it as a marker.

        ``limited`` exactly, as ``runners.validate_definition`` says: the egress
        proxy and per-run network exist only for the docker:limited container;
        ``full`` runs natively on the host network, as does ``none``."""
        if self.internet != "full" and self.docker != "limited":
            return (
                f"internet={self.internet!r} requires docker: limited — a "
                "restricted network is only enforced for the docker:limited container"
            )
        return None

    def allows(self, capability: str) -> bool:
        """Whether this grant permits ``capability`` (an OP_CAPABILITY name from
        the :data:`CAPABILITIES` vocabulary). Unknown capability names raise
        ``KeyError`` (a gate bug, not a denial)."""
        check = _CAPABILITY_CHECKS.get(capability)
        if check is None:
            raise KeyError(f"unknown capability {capability!r}")
        return check(self)

    def at_least(self, axis: str, level: str) -> bool:
        """Whether the grant meets ``level`` on the leveled ``axis`` (the SDK's
        ``Grant.at_least``). An axis that is not leveled, or a level off its
        ladder, raises (a caller bug, not a denial)."""
        ladder = AXIS_LEVELS[axis]
        return _rank(ladder, getattr(self, axis)) >= _rank(ladder, level)


# Tests may request the most permissive grant explicitly.
UNRESTRICTED = RunnerPermissions(
    update_packages=True,
    docker=DOCKER_LEVELS[-1],
    internet=INTERNET_MODES[-1],
    internet_allow=(),
    commands=COMMAND_LEVELS[-1],
)


# ---------------------------------------------------------------------------
# Capability predicate table (consulted by ``allows``).
# ---------------------------------------------------------------------------

_CAPABILITY_CHECKS = {CAP_ENV_PLANE: lambda p: p.at_least("docker", "full")}


__all__ = [
    "RunnerPermissions", "RunnerPermissionsError", "UNRESTRICTED",
    "normalize_host_entry", "entry_covers", "normalize_internet_allow",
    "DOCKER_LEVELS", "INTERNET_MODES", "COMMAND_LEVELS", "CAPABILITY_ENUMS",
    "CAPABILITIES", "CAP_ENV_PLANE",
]
