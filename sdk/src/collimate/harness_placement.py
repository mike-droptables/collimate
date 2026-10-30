# SPDX-License-Identifier: MIT
"""Where a harness runs under a grant, and the least grant it needs.

One function answers it for every caller: the Agent's config parsing (which
refuses a run its grant cannot place), the linter (which warns about a
runner that cannot serve its harness), the host's Key manager (which says
where an entry can run) and the host's run start (which raises an Annotation
run's grant to its harness's minimum — :func:`adopt`). The answer is
structured (:class:`Placement`); each caller renders its own sentences.

Metadata only: the per-harness facts are :mod:`collimate.harness_catalog`
(generated from the harness adapters), and the one machine fact,
whether a harness's CLI is installed here, is :func:`collimate.host_harness.probe`.

The rules, by where a harness's turn runs:

* a host-locus adapter (pydantic-ai, claude-code) runs under ``docker:
  none`` or ``full``, never ``limited`` (its adapter is a host process), and
  its commands, where the grant allows them, run on this machine — except a
  sandboxed harness's (pydantic-ai) under ``docker: full``, which run in its
  Docker sandbox; the placement says so where they would run here;
* a container harness needs ``docker: full``, or ``docker: none`` with its
  host twin's CLI installed and usable on this machine;
* the CLI surface, on a harness that has one, runs where its terminal locus
  puts it for the account's auth (the catalog's ``terminal_locus``): in the
  harness's container under ``docker: full``, or as the vendor's own CLI on
  this machine (Claude Code; Antigravity on the Google plan) under ``docker:
  none`` or ``full``, with ``commands`` not ``deny`` — nothing can hold a
  deny on an interactive TTY on the host — and the CLI installed and usable
  here. A CLI on this machine runs outside any sandbox.

Only a need for a container is adopted, and only on the Annotation surface:
a container harness's (its own container, or a host twin's absent CLI). A
host-locus adapter needs no container, so its grant is never raised — under
``docker: limited`` it is refused — and neither is a CLI surface's. Raising ``docker`` to ``full`` needs ``internet: full`` too (a
restricted network is the egress proxy of ``docker: limited``), so a grant on
a restricted network cannot be raised.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from collimate.harness_catalog import HARNESSES
from collimate.permissions import DOCKER_LEVELS, Grant

SURFACES = ("annotation", "chat", "terminal")


@dataclass(frozen=True)
class Placement:
    """Where ``harness`` runs on ``surface`` under ``grant``.

    ``runtime`` is ``host`` (an adapter process here), ``docker``
    (its container) or ``standard`` (this machine's own CLI), or ``""`` when
    the grant cannot place it — ``refusal`` then says why and what grant
    would. ``grant`` is the grant the run runs under: the one given, or, for
    an adopting caller, that grant with ``raised`` axes lifted to the
    harness's minimum (``(axis, from, to)``). ``host`` is the machine's CLI
    the run uses (``{path, version, binary}``) or ``None``; ``notes`` are
    facts worth showing (which CLI runs, why a standard run is impossible)."""

    harness: str
    surface: str
    runtime: str
    grant: Grant
    raised: tuple = ()
    refusal: str = ""
    host: "dict | None" = None
    notes: tuple = ()

    @property
    def ok(self) -> bool:
        return not self.refusal

    @property
    def adopted(self) -> bool:
        return bool(self.raised)

    def describe_raise(self) -> str:
        """``"docker: full"`` for the axes this placement raised."""
        return ", ".join(f"{axis}: {to}" for axis, _from, to in self.raised)


def row(harness: str) -> dict:
    """The catalog row for ``harness``; ``ValueError`` names the roster."""
    found = HARNESSES.get(harness)
    if found is None:
        raise ValueError(f"unknown backend {harness!r} — expected one of {', '.join(HARNESSES)}")
    return found


def container_refusal(harness: str) -> str:
    """Actionable refusal for a container harness the grant cannot place."""
    facts = row(harness)
    msg = f"backend {harness!r} needs docker: full for its harness container"
    if facts["host_twin"]:
        msg += f", or docker: none with a usable {facts['host_binary']} installed on PATH outside Snap"
    return msg


def describe_host(host: dict) -> str:
    """``"codex 0.164.0 at /usr/local/bin/codex"`` for a placement's ``host``."""
    return f"{host['binary']} {host['version']} at {host['path']}" if host["version"] else host["path"]


def sandbox_notes(harness: str, grant: Grant, surface: str = "annotation", auth: str = "") -> tuple:
    """Where commands run outside a sandbox, the placement says so. A
    sandboxed harness runs its commands in its Docker sandbox under
    ``docker: full`` and on this machine otherwise, and ``docker: full`` is
    recommended; a CLI surface on this machine runs outside any sandbox,
    under the CLI's own prompting."""
    facts = row(harness)
    if surface == "terminal":
        if facts["terminal"] and terminal_locus(harness, auth) == "host":
            return (f"the {facts['host_binary']} CLI runs on this machine, outside any sandbox: under "
                    "commands: ask its own prompt asks before each command, under allow it runs them "
                    "unattended",)
        return ()
    if facts.get("commands_sandboxed") and grant.commands != "deny" and grant.docker != "full":
        return (f"{facts['label']} runs commands on this machine under docker: {grant.docker}; "
                "docker: full sandboxes them in a container and is recommended for commands: allow",)
    return ()


def terminal_locus(harness: str, auth: str = "") -> str:
    """Where ``harness``'s CLI surface runs for an account billed under
    ``auth`` (``api_key`` or a plan; blank is the harness's first auth):
    ``host`` (the vendor's CLI on this machine) or ``container``. A harness
    with no CLI surface, or an auth it does not offer, raises
    :class:`ValueError`."""
    facts = row(harness)
    loci = facts["terminal_locus"]
    if not loci:
        raise ValueError(f"backend {harness!r} has no TTY; the terminal surface runs a harness's own TTY")
    auth = auth or facts["auths"][0]
    if auth not in loci:
        raise ValueError(f"auth {auth!r} isn't honored by backend {harness!r} — it offers "
                         f"{', '.join(loci)}")
    return loci[auth]


def _host_terminal(harness: str, grant: Grant, auth: str, look: Callable) -> Placement:
    """The CLI surface as the vendor's own CLI on this machine."""
    facts = row(harness)
    binary = facts["host_binary"]

    def refused(why: str, **kw) -> Placement:
        return Placement(harness, "terminal", "", grant, refusal=why, **kw)

    if grant.docker == "limited":
        return refused(f"backend {harness!r} cannot run its CLI with docker: limited (the CLI is {binary} on "
                       "this machine); use docker: none or full")
    if grant.commands == "deny":
        return refused(f"backend {harness!r} cannot run its CLI with commands: deny: the CLI is {binary} on "
                       "this machine, where nothing can hold a deny on an interactive TTY; use commands: ask "
                       "(its own prompt asks before each command) or allow")
    found = look(harness)
    if found is None or found.blocker:
        why = found.blocker if found is not None else f"{binary} was not found on this machine"
        return refused(f"{why}; backend {harness!r}'s CLI is this machine's own {binary}: install it on PATH "
                       "outside Snap", notes=(why,))
    host = {"path": found.path, "version": found.version, "binary": found.binary}
    return Placement(harness, "terminal", "host", grant, host=host,
                     notes=(f"its CLI runs on this machine: {describe_host(host)}",
                            *sandbox_notes(harness, grant, "terminal", auth)))


def _default_probe(harness: str):
    from collimate.host_harness import probe
    return probe(harness)


def placement(harness: str, grant: Grant, surface: str = "annotation", *, auth: str = "",
              adopt: bool = False, probe: "Callable | None" = None) -> Placement:
    """Where ``harness`` runs on ``surface`` under ``grant`` (see the module
    docstring), for an account billed under ``auth`` (``api_key`` or a plan;
    it decides where a CLI surface runs, :func:`terminal_locus`). With
    ``adopt``, a ``docker`` axis the harness needs more of is raised to its
    minimum instead of refused, where the network axis allows it; ``probe``
    (``harness -> HostHarness | None``) is
    :func:`collimate.host_harness.probe` by default.

    On the terminal surface ``runtime`` is where the CLI runs: ``host`` (this
    machine; ``host`` is the CLI found here) or ``docker`` (its
    container)."""
    facts = row(harness)
    surface = (surface or "annotation").strip().lower()
    if surface not in SURFACES:
        raise ValueError(f"unknown surface {surface!r} — expected one of {', '.join(SURFACES)}")
    look = probe or _default_probe

    def raise_docker(why: str, runtime: str, **kw) -> Placement:
        if not adopt:
            return Placement(harness, surface, "", grant, refusal=why, **kw)
        if grant.internet != "full":
            return Placement(harness, surface, "", grant, refusal=(
                f"{why}; this runner's internet: {grant.internet} keeps it on docker: limited, so "
                "docker: full cannot be adopted for it — give the runner internet: full"), **kw)
        lifted = replace(grant, docker="full")
        return Placement(harness, surface, runtime, lifted,
                         raised=(("docker", grant.docker, "full"),), **kw)

    if surface == "terminal":
        if not facts["terminal"]:
            return Placement(harness, surface, "", grant, refusal=(
                f"backend {harness!r} has no TTY; the terminal surface runs a harness's own TTY"))
        if terminal_locus(harness, auth) == "host":
            return _host_terminal(harness, grant, auth or facts["auths"][0], look)
        if grant.docker != "full":
            return raise_docker(f"backend {harness!r} runs its CLI in its container: the terminal "
                                "surface needs docker: full", "docker")
        return Placement(harness, surface, "docker", grant, notes=("its CLI runs in its container",))

    locus = facts["locus"]
    if locus == "host":
        if grant.docker == "limited":
            # Never adopted: a host adapter needs no container, so docker:
            # full would grant what the harness does not need.
            return Placement(harness, surface, "", grant, refusal=(
                f"backend {harness!r} cannot run with docker: limited (its adapter is a process on this "
                "machine); use docker: none or full"))
        binary = facts["host_binary"]
        if not binary:
            # An adapter Collimate installs whole (pydantic-ai): no machine CLI to look for.
            return Placement(harness, surface, "host", grant, notes=sandbox_notes(harness, grant))
        found = look(harness)
        if found is not None and not found.blocker:
            host = {"path": found.path, "version": found.version, "binary": found.binary}
            return Placement(harness, surface, "host", grant, host=host,
                             notes=(f"{facts['label']} uses this machine's {describe_host(host)}",))
        why = found.blocker if found is not None else f"{binary} was not found on this machine"
        return Placement(harness, surface, "host", grant,
                         notes=(f"{why}; running the {facts['label']} bundled in Collimate's adapter instead",))

    # A container harness.
    if grant.docker == "full":
        return Placement(harness, surface, "docker", grant)
    if grant.docker == "limited":
        return raise_docker(
            f"backend {harness!r} cannot run with docker: limited; use docker: full"
            + (" or docker: none with its host CLI" if facts["host_twin"] else ""), "docker")
    if not facts["host_twin"]:
        return raise_docker(container_refusal(harness), "docker")
    found = look(harness)
    if found is not None and not found.blocker:
        host = {"path": found.path, "version": found.version, "binary": found.binary}
        return Placement(harness, surface, "standard", grant, host=host)
    why = found.blocker if found is not None else f"{facts['host_binary']} was not found on this machine"
    return raise_docker(f"{why}; {container_refusal(harness)}", "docker", notes=(why,))


def adopt(harness: str, grant: Grant, surface: str = "annotation", *, auth: str = "",
          probe: "Callable | None" = None) -> Placement:
    """The grant an Annotation run on ``harness`` runs under: ``grant``
    itself where it places the harness, else ``grant`` with ``docker``
    raised to the harness's minimum (the run's config records it). Only the
    Annotation surface adopts — a chat or CLI runner's grant is the user's
    explicit choice, so there the answer is :func:`placement`'s refusal, and
    a CLI surface's grant is never raised."""
    return placement(harness, grant, surface, auth=auth,
                     adopt=(surface or "annotation") == "annotation", probe=probe)


def runtime_refusal(harness: str, grant: Grant, runtime: str, host_path: str = "") -> str:
    """Why a turn on ``harness`` must not run at ``runtime`` under ``grant``
    ("" when it may): the turn-time twin of :func:`placement`, for trusted
    callers that pass a runtime themselves. It never probes the machine."""
    facts = row(harness)
    locus = facts["locus"]
    if locus == "host" and grant.docker == "limited":
        return f"{harness} cannot run with docker: limited; use docker: none or full"
    if locus == "container":
        container = grant.docker == "full" and runtime == "docker"
        twin = (grant.docker == "none" and facts["host_twin"] and runtime == "standard"
                and bool(host_path))
        if not (container or twin):
            return container_refusal(harness)
    return ""


def availability(harness: str, *, probe: "Callable | None" = None) -> dict:
    """``{docker level: Placement}`` for the Annotation surface at commands
    deny on a full network — the structured answer the Key manager renders
    its "where can this entry run" sentence from. Each placement is
    :func:`adopt`'s, so a level the harness needs more than says so."""
    look = probe or _default_probe
    cached: dict = {}

    def once(name):
        if name not in cached:
            cached[name] = look(name)
        return cached[name]

    return {level: adopt(harness, Grant(docker=level, internet="full", commands="deny"),
                         "annotation", probe=once)
            for level in DOCKER_LEVELS}


def terminal_availability(harness: str, auth: str = "", *, probe: "Callable | None" = None) -> dict:
    """``{docker level: Placement}`` for the CLI surface of an account
    billed under ``auth``, at commands ask on a full network (a CLI on this
    machine refuses deny), or ``{}`` for a harness with no CLI surface —
    what the Key manager's Verify says the CLI runs on."""
    if not row(harness)["terminal"]:
        return {}
    look = probe or _default_probe
    cached: dict = {}

    def once(name):
        if name not in cached:
            cached[name] = look(name)
        return cached[name]

    return {level: placement(harness, Grant(docker=level, internet="full", commands="ask"),
                             "terminal", auth=auth, probe=once)
            for level in DOCKER_LEVELS}


__all__ = [
    "Placement",
    "SURFACES",
    "adopt",
    "availability",
    "container_refusal",
    "describe_host",
    "sandbox_notes",
    "placement",
    "row",
    "runtime_refusal",
    "terminal_availability",
    "terminal_locus",
]
