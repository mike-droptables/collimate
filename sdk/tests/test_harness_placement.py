# SPDX-License-Identifier: MIT
"""One placement answer for every caller: where a harness runs under a grant,
and the least grant an Annotation run adopts for it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from collimate.harness_placement import (
    adopt, availability, placement, runtime_refusal, sandbox_notes, terminal_availability, terminal_locus,
)
from collimate.permissions import Grant


def grant(**axes) -> Grant:
    return Grant(**{"docker": "none", "internet": "full", "commands": "deny", **axes})


def absent(_harness):
    return None


def usable(harness):
    return SimpleNamespace(path=f"/usr/bin/{harness}", version="9.9.9", binary=harness, blocker="")


@pytest.mark.parametrize("harness", ["qwen-code", "cline", "pi", "antigravity", "hermes", "vibe", "copilot"])
def test_an_annotation_run_adopts_docker_full_for_a_container_harness(harness):
    placed = adopt(harness, grant(), "annotation", probe=absent)
    assert placed.ok and placed.adopted and placed.runtime == "docker"
    assert placed.grant.docker == "full" and placed.raised == (("docker", "none", "full"),)
    assert placed.describe_raise() == "docker: full"
    # The other axes are the runner's own.
    assert (placed.grant.internet, placed.grant.commands) == ("full", "deny")


@pytest.mark.parametrize("surface", ["chat", "terminal"])
def test_only_the_annotation_surface_adopts(surface):
    placed = adopt("qwen-code", grant(), surface, probe=absent)
    assert not placed.ok and not placed.adopted
    assert "docker: full" in placed.refusal


def test_a_runner_that_already_grants_enough_is_kept_as_it_is():
    full = grant(docker="full", commands="allow")
    placed = adopt("codex", full, "annotation", probe=absent)
    assert placed.ok and not placed.adopted and placed.grant is full


def test_a_host_twin_runs_the_machines_cli_before_anything_is_adopted():
    placed = adopt("codex", grant(), "annotation", probe=usable)
    assert placed.ok and not placed.adopted and placed.runtime == "standard"
    assert placed.host == {"path": "/usr/bin/codex", "version": "9.9.9", "binary": "codex"}
    missing = adopt("codex", grant(), "annotation", probe=absent)
    assert missing.adopted and missing.runtime == "docker"


def test_a_restricted_network_cannot_adopt_docker_full():
    placed = adopt("qwen-code", grant(docker="limited", internet="proxy"), "annotation", probe=absent)
    assert not placed.ok and "internet: proxy" in placed.refusal


def test_claude_runs_on_this_machine_and_is_never_raised_to_docker_full():
    assert adopt("claude-code", grant(), "annotation", probe=absent).runtime == "host"
    # A host adapter needs no container: limited is refused, never adopted.
    placed = adopt("claude-code", grant(docker="limited"), "annotation", probe=absent)
    assert not placed.ok and not placed.adopted and "cannot run with docker: limited" in placed.refusal
    noted = placement("claude-code", grant(), "chat", probe=usable)
    assert noted.notes == ("Claude Code uses this machine's claude-code 9.9.9 at /usr/bin/claude-code",)


def test_pydantic_ai_is_a_host_adapter_with_no_cli_to_probe():
    def never_probed(harness):
        raise AssertionError("pydantic-ai has no host binary to probe")

    for level in ("deny", "ask", "allow"):
        for docker in ("none", "full"):
            placed = placement("pydantic-ai", grant(docker=docker, commands=level), "chat", probe=never_probed)
            assert placed.ok and placed.runtime == "host" and placed.host is None
            # Its commands run here under docker: none (noted, with docker:
            # full recommended) and in its sandbox under docker: full.
            if docker == "none" and level != "deny":
                assert placed.notes == (sandbox_notes("pydantic-ai", grant(docker=docker, commands=level)))
                assert "runs commands on this machine" in placed.notes[0]
                assert "recommended for commands: allow" in placed.notes[0]
            else:
                assert placed.notes == ()
    # limited is refused on every surface: a host adapter is never raised.
    refused = placement("pydantic-ai", grant(docker="limited"), "chat")
    assert "cannot run with docker: limited" in refused.refusal
    placed = adopt("pydantic-ai", grant(docker="limited"), "annotation")
    assert not placed.ok and not placed.adopted and "cannot run with docker: limited" in placed.refusal
    assert "no TTY" in placement("pydantic-ai", grant(docker="full"), "terminal").refusal


def test_the_turn_time_check_never_probes_and_agrees_with_placement():
    assert runtime_refusal("codex", grant(docker="full"), "docker") == ""
    assert runtime_refusal("codex", grant(), "standard", "/usr/bin/codex") == ""
    assert "needs docker: full" in runtime_refusal("codex", grant(), "docker")
    assert "docker: limited" in runtime_refusal("claude-code", grant(docker="limited"), "docker")
    assert runtime_refusal("pydantic-ai", grant(commands="allow"), "host") == ""
    assert "docker: limited" in runtime_refusal("pydantic-ai", grant(docker="limited"), "host")


def test_availability_is_the_structured_answer_the_key_manager_renders():
    answer = availability("opencode", probe=absent)
    assert set(answer) == {"none", "limited", "full"}
    assert answer["full"].ok and not answer["full"].adopted
    assert answer["none"].adopted and answer["limited"].adopted
    assert availability("opencode", probe=usable)["none"].runtime == "standard"


def test_an_unknown_harness_names_the_roster():
    with pytest.raises(ValueError, match="expected one of pydantic-ai"):
        placement("no-such", grant())


# ── the CLI surface: where its terminal locus puts it ─────────────────


def test_the_terminal_locus_is_the_catalogs_per_auth():
    assert terminal_locus("claude-code", "api_key") == terminal_locus("claude-code", "claude_code") == "host"
    assert terminal_locus("antigravity", "google") == "host"
    assert terminal_locus("antigravity", "api_key") == terminal_locus("antigravity") == "container"
    assert terminal_locus("codex", "chatgpt") == "container"
    with pytest.raises(ValueError, match="no TTY"):
        terminal_locus("pydantic-ai")
    with pytest.raises(ValueError, match="isn't honored"):
        terminal_locus("antigravity", "chatgpt")


@pytest.mark.parametrize("harness,auth", [("claude-code", "claude_code"), ("claude-code", "api_key"),
                                          ("antigravity", "google")])
@pytest.mark.parametrize("docker", ["none", "full"])
@pytest.mark.parametrize("commands", ["ask", "allow"])
def test_a_cli_on_this_machine_runs_under_none_or_full_with_ask_or_allow(harness, auth, docker, commands):
    placed = placement(harness, grant(docker=docker, commands=commands), "terminal", auth=auth, probe=usable)
    assert placed.ok and placed.runtime == "host" and not placed.adopted
    assert placed.host == {"path": f"/usr/bin/{harness}", "version": "9.9.9", "binary": harness}
    assert placed.notes[0] == f"its CLI runs on this machine: {harness} 9.9.9 at /usr/bin/{harness}"
    assert "outside any sandbox" in placed.notes[1]


@pytest.mark.parametrize("harness,auth", [("claude-code", "claude_code"), ("antigravity", "google")])
def test_a_cli_on_this_machine_is_refused_under_limited_under_deny_and_without_the_cli(harness, auth):
    limited = placement(harness, grant(docker="limited", commands="ask"), "terminal", auth=auth, probe=usable)
    assert not limited.ok and "cannot run its CLI with docker: limited" in limited.refusal
    assert "use docker: none or full" in limited.refusal
    deny = placement(harness, grant(commands="deny"), "terminal", auth=auth, probe=usable)
    assert not deny.ok and "cannot run its CLI with commands: deny" in deny.refusal
    assert "nothing can hold a deny on an interactive TTY" in deny.refusal
    missing = placement(harness, grant(commands="ask"), "terminal", auth=auth, probe=absent)
    assert not missing.ok and "was not found on this machine" in missing.refusal
    # Never adopted: adopt() raises no grant for the CLI surface.
    for g in (grant(docker="limited", commands="ask"), grant(commands="deny")):
        adopted = adopt(harness, g, "terminal", auth=auth, probe=usable)
        assert not adopted.ok and not adopted.adopted and adopted.grant is g


def test_a_container_cli_needs_docker_full_and_is_never_adopted():
    for harness, auth in (("antigravity", "api_key"), ("codex", "api_key"), ("codex", "chatgpt")):
        placed = placement(harness, grant(docker="full"), "terminal", auth=auth, probe=usable)
        assert placed.ok and placed.runtime == "docker" and placed.notes == ("its CLI runs in its container",)
        refused = adopt(harness, grant(), "terminal", auth=auth, probe=usable)
        assert not refused.ok and not refused.adopted and "needs docker: full" in refused.refusal


def test_sandbox_notes_say_a_cli_on_this_machine_runs_outside_the_sandbox():
    assert "outside any sandbox" in sandbox_notes("claude-code", grant(commands="ask"), "terminal", "api_key")[0]
    assert sandbox_notes("antigravity", grant(docker="full"), "terminal", "api_key") == ()


def test_terminal_availability_is_what_verify_says_the_cli_runs_on():
    answer = terminal_availability("antigravity", "google", probe=usable)
    assert set(answer) == {"none", "limited", "full"}
    assert answer["none"].runtime == answer["full"].runtime == "host" and not answer["limited"].ok
    keyed = terminal_availability("antigravity", "api_key", probe=usable)
    assert keyed["full"].runtime == "docker" and not keyed["none"].ok
    assert terminal_availability("pydantic-ai") == {}
