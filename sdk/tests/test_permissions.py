# SPDX-License-Identifier: MIT
"""Tests for :mod:`collimate.permissions` — the ONE SDK-wide definition of the
grant axes, their ladders, and the typed :class:`Grant`.

Pinned here: (1) the ORDER of each leveled axis, because every downstream
capability derivation is a rank comparison over these tuples — most notably
the Docker permission level; (2) the environment-layer
semantics :meth:`Grant.from_dict` gives a dict: ``{}`` is the unrestricted
grant with every axis at its top, a non-empty grant is restricted and floors
every axis it omits (or names a bad value for) to the bottom rung, and an
unknown axis name raises; (3) ``effective_commands``. A drift-guard at the
end asserts the lint's membership sets are still built from these very
tuples."""
from __future__ import annotations

import dataclasses

import pytest

import collimate
from collimate import permissions as perms
from collimate.lint import core as lint_core
from collimate.permissions import (
    AXES,
    AXIS_LEVELS,
    COMMAND_LEVELS,
    DOCKER_LEVELS,
    GENERATOR_AXES,
    INTERNET_MODES,
    UNRESTRICTED,
    Grant,
    effective_commands,
)


# ---------------------------------------------------------------------------
# The ladders themselves
# ---------------------------------------------------------------------------

def test_ladders_are_exact_restrictive_to_permissive_tuples():
    """Literal order pins: rank == index, so a reorder is a breaking change."""
    assert DOCKER_LEVELS == ("none", "limited", "full")
    assert INTERNET_MODES == ("none", "proxy", "full")
    assert COMMAND_LEVELS == ("deny", "ask", "allow")


def test_every_ladder_is_a_tuple_of_unique_lowercase_levels():
    for axis, levels in AXIS_LEVELS.items():
        assert isinstance(levels, tuple), axis
        assert len(levels) == len(set(levels)), axis
        assert all(lv == lv.lower() and lv for lv in levels), axis


def test_axis_levels_covers_exactly_the_leveled_axes():
    """``AXIS_LEVELS`` is the complete leveled-axis registry — the non-leveled
    axes (the ``update_packages`` boolean and the
    ``internet_allow`` list) stay out of it, and each entry is the
    module-level tuple itself, not a copy."""
    assert set(AXIS_LEVELS) == {
        "docker", "internet", "commands"}
    assert AXIS_LEVELS["docker"] is DOCKER_LEVELS
    assert AXIS_LEVELS["internet"] is INTERNET_MODES
    assert AXIS_LEVELS["commands"] is COMMAND_LEVELS


def test_the_grant_has_one_attribute_per_axis():
    assert set(AXIS_LEVELS) <= set(AXES)
    assert set(GENERATOR_AXES) <= set(AXES)
    assert {f.name for f in dataclasses.fields(Grant)} == set(AXES)


def test_commands_is_the_one_axis_a_generator_enforces():
    assert GENERATOR_AXES == ("commands",)


def test_module_is_part_of_the_public_collimate_surface():
    assert "permissions" in collimate.__all__
    assert collimate.permissions is perms
    for name in perms.__all__:
        assert hasattr(perms, name), name


# ---------------------------------------------------------------------------
# Grant.from_dict — the environment layer
# ---------------------------------------------------------------------------


def test_the_empty_grant_is_unrestricted_with_every_axis_at_its_top():
    g = Grant.from_dict({})
    assert g is UNRESTRICTED
    assert g.update_packages is True
    for axis, ladder in AXIS_LEVELS.items():
        assert getattr(g, axis) == ladder[-1], axis
        for level in ladder:
            assert g.at_least(axis, level), (axis, level)
    assert g.commands == "allow"


def test_a_restricted_grant_floors_every_omitted_axis():
    g = Grant.from_dict({"docker": "limited"})
    assert g.docker == "limited"
    assert g.update_packages is False
    assert g.internet_allow == ()
    for axis, ladder in AXIS_LEVELS.items():
        if axis != "docker":
            assert getattr(g, axis) == ladder[0], axis
            assert g.at_least(axis, ladder[0]) and not g.at_least(axis, ladder[1]), axis


@pytest.mark.parametrize("axis", sorted(AXIS_LEVELS))
def test_every_ladder_compares_by_rank(axis):
    ladder = AXIS_LEVELS[axis]
    for have_i, have in enumerate(ladder):
        g = Grant.from_dict({axis: have})
        assert getattr(g, axis) == have
        for need_i, need in enumerate(ladder):
            assert g.at_least(axis, need) is (need_i <= have_i), (axis, have, need)


def test_a_value_off_the_ladder_or_of_the_wrong_type_denies():
    """Default-deny: an unknown level never guesses upward, and a boolean
    axis holds only a real True."""
    assert Grant.from_dict({"docker": "everything"}).docker == "none"
    assert Grant.from_dict({"docker": True}).docker == "none"
    assert Grant.from_dict({"commands": "yolo"}).commands == "deny"
    assert Grant.from_dict({"update_packages": None}).update_packages is False
    assert Grant.from_dict({"internet_allow": "example.com"}).internet_allow == ()
    assert Grant.from_dict({"internet_allow": ["a.com", 3]}).internet_allow == ()


def test_the_full_host_grant_round_trips():
    g = Grant.from_dict({
        "update_packages": False, "docker": "limited",
        "internet": "proxy", "internet_allow": ["api.example.com"],
        "commands": "ask",
    })
    assert (g.update_packages, g.docker, g.internet) == (False, "limited", "proxy")
    assert g.internet_allow == ("api.example.com",)
    assert g.commands == "ask"


def test_an_unknown_axis_is_a_host_sdk_disagreement_and_raises():
    with pytest.raises(ValueError, match="api_keys"):
        Grant.from_dict({"docker": "limited", "api_keys": ["anthropic"]})
    with pytest.raises(TypeError):
        Grant.from_dict(None)  # type: ignore[arg-type]


def test_at_least_rejects_a_caller_bug():
    g = Grant.from_dict({"docker": "limited"})
    with pytest.raises(KeyError):
        g.at_least("update_packages", "none")          # not a leveled axis
    with pytest.raises(KeyError):
        g.at_least("bogus_axis", "read")
    with pytest.raises(ValueError):
        g.at_least("docker", "everything")
    with pytest.raises(ValueError):
        g.at_least("docker", "see")               # another ladder's level


def test_a_grant_is_frozen():
    g = Grant.from_dict({"commands": "ask"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.commands = "allow"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# effective_commands
# ---------------------------------------------------------------------------


def test_effective_commands_resolves_ask_to_deny_when_nobody_can_answer():
    ask = Grant.from_dict({"commands": "ask"})
    assert effective_commands(ask, True) == ("ask", None)
    level, warning = effective_commands(ask, False)
    assert level == "deny"
    assert "nobody can answer" in warning


@pytest.mark.parametrize("level", ["deny", "allow"])
def test_effective_commands_keeps_levels_that_need_no_answer(level):
    g = Grant.from_dict({"commands": level})
    assert effective_commands(g, False) == (level, None)
    assert effective_commands(g, True) == (level, None)


def test_effective_commands_of_the_unrestricted_grant_is_allow():
    assert effective_commands(UNRESTRICTED, False) == ("allow", None)


@pytest.mark.parametrize("fields", [
    {"commands": "Allow"},                       # off the ladder: never read as allow
    {"docker": "root"},
    {"update_packages": 1},
    {"internet_allow": ["api.example.com"]},     # a list, not the frozen tuple
    {"internet_allow": ("ok", 3)},
])
def test_a_grant_built_directly_refuses_a_value_off_its_axis(fields):
    with pytest.raises(ValueError):
        Grant(**fields)


def test_effective_commands_refuses_a_level_off_the_ladder():
    """A grant whose commands level was forced past the constructor check is
    a caller bug, never enforced as some level."""
    g = Grant()
    object.__setattr__(g, "commands", "Allow")
    with pytest.raises(ValueError, match="Allow"):
        effective_commands(g, True)


def test_the_lint_knows_exactly_the_grant_axes():
    """The runner lint's known permission keys are the grant's axes, taken
    from this module rather than listed again."""
    assert lint_core._RUNNER_PERM_KNOWN == set(AXES)


# ---------------------------------------------------------------------------
# Drift guard: the lint validates membership against THESE tuples
# ---------------------------------------------------------------------------

# leveled axis → the lint's membership-set attribute name.
_LINT_SET_ATTRS = {
    "docker": "_RUNNER_DOCKER_LEVELS",
    "internet": "_RUNNER_INTERNET_MODES",
    "commands": "_RUNNER_COMMAND_LEVELS",
}


def test_lint_membership_sets_do_not_drift_from_the_ladders():
    """The runner lint accepts exactly the levels these ladders define — order
    lives here, membership is the lint's only concern."""
    assert set(_LINT_SET_ATTRS) == set(AXIS_LEVELS)  # every axis has a lint set
    for axis, attr in _LINT_SET_ATTRS.items():
        lint_set = getattr(lint_core, attr)
        assert isinstance(lint_set, set), attr
        assert lint_set == set(AXIS_LEVELS[axis]), attr


def test_the_grant_paragraph_is_mirrored_in_the_api_reference():
    """permissions.py's two-layer paragraph and the API reference's say the
    same thing, word for word (rst ``code`` → markdown `code`)."""
    import re
    from pathlib import Path

    doc = perms.__doc__
    start = doc.index("A grant has two layers.")
    para = doc[start:doc.index("\n\n", start)]
    para = " ".join(para.split()).replace("``", "`")
    reference = (Path(__file__).resolve().parents[1] / "shared" / "docs"
                 / "api-reference.md").read_text(encoding="utf-8")
    mirrored = re.search(r"^A grant has two layers\..*$", reference, re.M).group(0)
    assert mirrored == para
