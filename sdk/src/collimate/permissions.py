# SPDX-License-Identifier: MIT
"""Grant axes, their ordered ladders, and the typed :class:`Grant` a run
reads with ``gen.grant()`` — the one SDK definition. The linter validates
runner permissions against these tuples and the host's ``runner_permissions``
imports them.

A grant has two layers. A saved runner's ``permissions`` block is the
manifest layer: the host fills every axis the block omits with that axis's
default (``commands: deny``, ``internet: full``, ``update_packages: true``,
every other axis at its lowest level). What reaches the run is the
environment layer, ``COLLIMATE_PERMISSIONS``: the host always sends every
axis, ``{}`` there is an unrestricted dev run (every axis at its top), and
``Grant.from_dict`` floors an omitted axis or an unknown value to the lowest
level. A host run with no grant, a value that is not a JSON object, or a
grant naming an axis the SDK does not know stops the run. The host
enforces ``docker`` at placement
and at the environment-plane ops, and ``update_packages`` for everything it
builds or pulls. It enforces ``internet`` and ``internet_allow`` only for a
``docker: limited`` run, whose container sits behind its egress proxy: a
``docker: none`` or ``full`` run is a native process on the host network,
and its IPC gates bind what it asks the host for, not what the process can
reach on its own. ``commands`` has no host enforcement point, because the
shell commands an agent chooses run inside the harness or sandbox its
generator drives: a generator that honours the axis declares
``enforces: [commands]`` in its manifest and enforces it itself. A generator
also honours ``update_packages`` for the registry lookups it makes itself.

The paragraph above is mirrored in ``shared/docs/api-reference.md``
(``tests/test_permissions.py`` keeps the two equal).
"""
from __future__ import annotations

from dataclasses import dataclass

# Restrictive → permissive; index is the level rank.
DOCKER_LEVELS: tuple = ("none", "limited", "full")
DOCKER_DESCRIPTIONS: dict[str, str] = {
    "none": "Runs on the host without permission to launch Docker containers.",
    "limited": "Runs the generator in one container, without permission to launch more containers.",
    "full": "Runs the generator on the host and allows it to launch Docker containers, including agent harnesses.",
}
INTERNET_MODES: tuple = ("none", "proxy", "full")
COMMAND_LEVELS: tuple = ("deny", "ask", "allow")

# Leveled axes; update_packages is boolean and internet_allow an allowlist.
AXIS_LEVELS: dict = {
    "docker": DOCKER_LEVELS,
    "internet": INTERNET_MODES,
    "commands": COMMAND_LEVELS,
}

# Every axis a grant carries, in the order the host serialises them.
AXES: tuple = (
    "update_packages", "docker", "internet", "internet_allow", "commands",
)

# The axes with no host enforcement point: a generator that honours one
# declares it in its manifest's ``enforces`` list, and the runner editor and
# linter treat the axis as meaningful only for such a generator.
GENERATOR_AXES: tuple = ("commands",)


@dataclass(frozen=True)
class Grant:
    """A run's effective grant, one attribute per axis.

    ``gen.grant()`` returns the running run's; :meth:`from_dict` builds one
    from the environment-layer dict (tests, and host code that holds the raw
    JSON). :data:`UNRESTRICTED` (from ``{}``) carries every axis at its most
    permissive value, so a field reads the same whether or not the run is
    restricted. Every field is checked on construction: a leveled axis off
    its ladder, a non-bool flag or a non-string allowlist entry raises
    :class:`ValueError`, so no grant can carry a level the gates do not
    know."""

    update_packages: bool = False
    docker: str = DOCKER_LEVELS[0]
    internet: str = INTERNET_MODES[0]
    internet_allow: tuple = ()
    commands: str = COMMAND_LEVELS[0]

    def __post_init__(self) -> None:
        for axis, ladder in AXIS_LEVELS.items():
            value = getattr(self, axis)
            if not isinstance(value, str) or value not in ladder:
                raise ValueError(
                    f"grant {axis}={value!r} is not one of {', '.join(ladder)}")
        for axis in ("update_packages",):
            if not isinstance(getattr(self, axis), bool):
                raise ValueError(f"grant {axis} must be a bool, got {getattr(self, axis)!r}")
        allow = self.internet_allow
        if not isinstance(allow, tuple) or not all(isinstance(x, str) for x in allow):
            raise ValueError(f"grant internet_allow must be a tuple of str, got {allow!r}")

    @classmethod
    def from_dict(cls, data: dict) -> "Grant":
        """The grant an environment-layer dict describes. ``{}`` is
        :data:`UNRESTRICTED`. In a restricted grant an omitted axis, or a
        value of the wrong type or off the ladder, floors to the lowest level
        (``False`` for a boolean axis). An axis name the SDK does not know
        raises :class:`ValueError`: the host and the SDK disagree about what a
        grant is."""
        if not isinstance(data, dict):
            raise TypeError(f"a grant is a dict, got {type(data).__name__}")
        if not data:
            return UNRESTRICTED
        unknown = sorted(set(data) - set(AXES))
        if unknown:
            raise ValueError(f"unknown grant axes {unknown} (known: {', '.join(AXES)})")

        def level(axis: str) -> str:
            value, ladder = data.get(axis), AXIS_LEVELS[axis]
            return value if isinstance(value, str) and value in ladder else ladder[0]

        allow = data.get("internet_allow")
        return cls(
            update_packages=data.get("update_packages") is True,
            docker=level("docker"),
            internet=level("internet"),
            internet_allow=(tuple(allow) if isinstance(allow, list)
                            and all(isinstance(x, str) for x in allow) else ()),
            commands=level("commands"),
        )

    def at_least(self, axis: str, level: str) -> bool:
        """Whether the grant meets ``level`` on the leveled ``axis``. An axis
        that is not leveled, or a level off its ladder, raises (a caller bug,
        not a denial)."""
        ladder = AXIS_LEVELS[axis]
        return ladder.index(getattr(self, axis)) >= ladder.index(level)


#: The unrestricted grant (a dev run's ``{}``): every axis at its top.
UNRESTRICTED = Grant(
    update_packages=True,
    docker=DOCKER_LEVELS[-1],
    internet=INTERNET_MODES[-1],
    commands=COMMAND_LEVELS[-1],
)


def effective_commands(grant: Grant, can_ask: bool) -> "tuple[str, str | None]":
    """The ``commands`` level a generator enforces for ``grant``, and the
    warning to show when it differs from the granted one. ``ask`` needs
    someone to answer each approval card; where nobody can (``can_ask`` —
    ``gen.can_ask()`` for the running run: a run with no
    host) it resolves to ``deny``. A level off the ladder raises: a caller
    bug, never read as a grant."""
    if grant.commands not in COMMAND_LEVELS:
        raise ValueError(f"commands level {grant.commands!r} is not one of "
                         f"{', '.join(COMMAND_LEVELS)}")
    if grant.commands == "ask" and not can_ask:
        return "deny", (
            "commands: ask needs someone to approve each command, and nobody "
            "can answer an approval card in this run ("
            "a run with no host) — commands resolve to deny")
    return grant.commands, None


__all__ = [
    "AXES",
    "AXIS_LEVELS",
    "COMMAND_LEVELS",
    "DOCKER_LEVELS",
    "DOCKER_DESCRIPTIONS",
    "GENERATOR_AXES",
    "Grant",
    "INTERNET_MODES",
    "UNRESTRICTED",
    "effective_commands",
]
