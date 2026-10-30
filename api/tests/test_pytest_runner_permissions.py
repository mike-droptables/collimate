"""Unit coverage for ``api/runner_permissions.py``.

Pure-module tests (no app fixture): defaults, ladder ordering, the allowlist's one spelling, the ``COLLIMATE_PERMISSIONS`` env
round-trip, and the ``allows`` capability gate for every OP_CAPABILITY axis.
"""
from __future__ import annotations

import json

import pytest
from collimate import permissions as sdk

import runner_permissions as rp
from conftest import sdk_grant
from runner_permissions import (
    RunnerPermissions,
    RunnerPermissionsError,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_omitted_block_is_plain_host_subprocess():
    p = RunnerPermissions.from_definition(None)
    assert p.to_dict() == {
        # Default OPEN, like ``internet`` — a plain host subprocess is free to
        # fetch what it installs; a runner opts OUT for reproducibility.
        "update_packages": True,
        "docker": "none",
        "internet": "full",
        "internet_allow": [],
        # Omitted → deny: no shell commands unless the runner says so.
        "commands": "deny",
    }


def test_empty_block_matches_omitted():
    assert RunnerPermissions.from_definition({}) == RunnerPermissions.from_definition(None)


def test_partial_block_fills_defaults():
    p = RunnerPermissions.from_definition({"docker": "limited"})
    assert p.docker == "limited"
    assert p.internet == "full"  # default
    assert p.update_packages is True  # default


def test_update_packages_defaults_on_and_opts_out_explicitly():
    """The freshness axis is one of the two default-OPEN axes: a runner that says
    nothing gets fresh upstream artifacts on a build, and pinning is an
    explicit ``false``. UNRESTRICTED holds it open too."""
    assert RunnerPermissions.from_definition({}).update_packages is True
    assert RunnerPermissions.from_definition({"update_packages": True}).update_packages is True
    assert RunnerPermissions.from_definition({"update_packages": False}).update_packages is False
    assert rp.UNRESTRICTED.update_packages is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block", [
    {"docker": "bogus"},
    {"internet": "sometimes"},
    {"commands": "yolo"},
    {"commands": True},
    {"update_packages": "yes"},
    {"update_packages": 1},
    {"api_keys": "not-a-list"},
    {"api_keys": [1, 2]},
])
def test_bad_axis_raises(block):
    with pytest.raises(RunnerPermissionsError):
        RunnerPermissions.from_definition(block)


def test_invariant_internet_needs_docker():
    assert RunnerPermissions.from_definition({"internet": "none"}).invariant_error()
    assert RunnerPermissions.from_definition({"internet": "proxy"}).invariant_error()
    # satisfied once docker is raised
    assert RunnerPermissions.from_definition(
        {"internet": "none", "docker": "limited"}
    ).invariant_error() is None
    # full runs natively on the host network: no proxy, so no restriction
    for internet in ("none", "proxy"):
        assert RunnerPermissions.from_definition(
            {"internet": internet, "docker": "full"}
        ).invariant_error()
    # full internet never needs docker
    assert RunnerPermissions.from_definition({"internet": "full"}).invariant_error() is None


# ---------------------------------------------------------------------------
# Ladders
# ---------------------------------------------------------------------------


def test_ladder_orderings():
    assert rp.DOCKER_LEVELS == ("none", "limited", "full")
    assert rp.INTERNET_MODES == ("none", "proxy", "full")
    assert rp.COMMAND_LEVELS == ("deny", "ask", "allow")
    assert rp.CAPABILITY_ENUMS["commands"] is rp.COMMAND_LEVELS
    assert rp.CAPABILITY_ENUMS is sdk.AXIS_LEVELS


def test_at_least_helpers():
    p = RunnerPermissions.from_definition({
        "docker": "limited", "internet": "proxy",
        })
    assert p.at_least("docker", "none") and p.at_least("docker", "limited")
    assert not p.at_least("docker", "full")
    assert p.at_least("internet", "proxy") and not p.at_least("internet", "full")
    ask = RunnerPermissions.from_definition({"commands": "ask"})
    assert ask.at_least("commands", "deny") and ask.at_least("commands", "ask")
    assert not ask.at_least("commands", "allow")
    assert not p.at_least("commands", "ask")               # omitted → deny
    assert rp.UNRESTRICTED.at_least("commands", "allow")   # every axis at its top
    # An unknown axis or an off-ladder level is a caller bug, not a denial.
    with pytest.raises(KeyError):
        p.at_least("update_packages", "none")
    with pytest.raises(ValueError):
        p.at_least("docker", "bogus")


# ---------------------------------------------------------------------------
# Intersection
# ---------------------------------------------------------------------------


def test_internet_allow_entries_have_one_spelling__F2041():
    """Entries are normalized the way the proxy reads them: case, surrounding
    dots and whitespace go, blanks and entries another entry covers drop out.
    A spelling the grammar does not know is kept (it never matches; the
    linter warns about it) rather than failing the runner."""
    proxy = {"docker": "limited", "internet": "proxy"}
    p = RunnerPermissions.from_definition({**proxy, "internet_allow": [
        " .GitHub.COM. ", "api.github.com", "", "..", "B.com", "*.example.com"]})
    assert p.internet_allow == ("*.example.com", "b.com", "github.com")
    same = RunnerPermissions.from_definition({**proxy, "internet_allow": [
        "github.com", "b.com", "*.example.com"]})
    assert p == same
    assert rp.normalize_internet_allow(["com", "example.com", "x.org"]) == ("com", "x.org")
    assert rp.normalize_internet_allow(["notexample.com", "example.com"]) == (
        "example.com", "notexample.com")


def test_internet_allow_has_one_spelling():
    # Sorted, without duplicates, and empty outside proxy mode (where it has no
    # effect): two grants that reach the same hosts are the same grant.
    proxy = {"docker": "limited", "internet": "proxy"}
    messy = RunnerPermissions.from_definition({**proxy, "internet_allow": ["z.com", "a.com", "z.com"]})
    tidy = RunnerPermissions.from_definition({**proxy, "internet_allow": ["a.com", "z.com"]})
    assert messy == tidy and messy.internet_allow == ("a.com", "z.com")
    assert RunnerPermissions.from_definition({"internet_allow": ["a.com"]}) == RunnerPermissions.from_definition({})
    assert RunnerPermissions.from_definition(
        {"docker": "limited", "internet": "none", "internet_allow": ["a.com"]}).internet_allow == ()


def test_a_grant_names_every_axis():
    # No axis has a field default: a grant is built by from_definition (which
    # applies the definition defaults) or UNRESTRICTED.
    with pytest.raises(TypeError):
        RunnerPermissions(docker="full")


_GRANTS = [
    {"docker": "limited"},
    {"docker": "limited", "internet": "proxy", "internet_allow": ["z.com", "a.com"]},
    {"docker": "limited", "internet": "proxy", "internet_allow": ["a.com", "a.com"]},
    {"internet_allow": ["a.com"]},
    {"docker": "limited", "internet": "none", "internet_allow": ["a.com"]},
    {"update_packages": False, "commands": "ask", },
]


# ---------------------------------------------------------------------------
# Env round-trip
# ---------------------------------------------------------------------------


def test_env_roundtrip_concrete_grant():
    p = RunnerPermissions.from_definition({
        "docker": "limited", "internet": "proxy", "internet_allow": ["a.com"],
        "update_packages": False, "commands": "ask",
    })
    sent = json.loads(p.to_env())
    assert set(sent) == set(sdk.AXES)
    assert RunnerPermissions.from_definition(sent) == p
    # What the run reads with gen.grant(). A default-OPEN axis is the one that
    # silently widens if it drops out of the JSON on the way to the child, so
    # assert the value, not just equality.
    grant = sdk.Grant.from_dict(sent)
    assert grant.update_packages is False
    assert grant.commands == "ask"
    assert grant.internet_allow == ("a.com",)


@pytest.mark.parametrize("perms", [
    rp.UNRESTRICTED,
    RunnerPermissions.from_definition(None),
    RunnerPermissions.from_definition({
        "docker": "limited", "internet": "proxy", "internet_allow": ["b.com", "a.com"],
        "update_packages": False, "commands": "ask"}),
], ids=["unrestricted", "defaults", "proxy"])
def test_env_always_names_exactly_every_sdk_axis__F1239(perms):
    # The SDK stops a host run whose grant is missing, not a JSON object, or
    # names an axis it does not know; ``{}`` is a dev run's spelling, never
    # the host's. The host sends every axis, for every grant.
    sent = json.loads(perms.to_env())
    assert set(sent) == set(sdk.AXES)
    assert sdk.Grant.from_dict(sent) == sdk_grant(perms)


def test_host_permissions_follow_shared_sdk_fixture__F1239():
    from pathlib import Path
    import collimate
    fixture = json.loads((Path(collimate.__file__).resolve().parents[2] / "shared/permissions-contract.json").read_text())
    assert list(sdk.AXES) == fixture["axes"]
    assert {axis: list(levels) for axis, levels in rp.CAPABILITY_ENUMS.items()} == fixture["levels"]
    for row in fixture["definitions"]:
        assert json.loads(RunnerPermissions.from_definition(row["definition"]).to_env()) == row["grant"]


def test_unrestricted_holds_every_axis_at_its_top():
    # Every check reads UNRESTRICTED through its fields, like any grant; it is
    # the same grant the SDK calls UNRESTRICTED.
    u = rp.UNRESTRICTED
    for axis, ladder in sdk.AXIS_LEVELS.items():
        assert getattr(u, axis) == ladder[-1], axis
    assert u.docker == "full" and u.internet == "full" and u.commands == "allow"
    assert sdk.Grant.from_dict(json.loads(u.to_env())) == sdk.UNRESTRICTED


# ---------------------------------------------------------------------------
# allows() — the OP_CAPABILITY gate
# ---------------------------------------------------------------------------


def test_allows_env_plane_needs_full_docker():
    full = RunnerPermissions.from_definition({"docker": "full"})
    limited = RunnerPermissions.from_definition({"docker": "limited"})
    assert full.allows(rp.CAP_ENV_PLANE)
    assert not limited.allows(rp.CAP_ENV_PLANE)


def test_allows_unknown_capability_raises():
    p = RunnerPermissions.from_definition({})
    with pytest.raises(KeyError):
        p.allows("not_a_capability")


def test_unrestricted_allows_everything():
    for cap in rp.CAPABILITIES:
        assert rp.UNRESTRICTED.allows(cap)


@pytest.mark.parametrize("value", [[], ["openai"], None])
def test_api_keys_axis_is_not_part_of_runner_security(value):
    with pytest.raises(RunnerPermissionsError, match="api_keys"):
        RunnerPermissions.from_definition({"api_keys": value})
