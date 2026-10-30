# SPDX-License-Identifier: MIT
"""The runtime of the Agent cell's generator.

``agent`` is a thin entrypoint over :func:`run`, passing its package folder;
the run's surface (``annotation``, ``chat`` or ``terminal``) is its
``surface`` setting. The modules: ``entry`` (the run and its turns),
``state`` (the session and the one-shot's pass), ``consts`` (the shared
vocabulary), ``annotation`` (the annotation surface), ``corrections`` (the
host-refusal corrections), ``terminal`` (the harness's TTY), ``selected``
(the selected file as the first message), ``media`` and ``run_end``. The
generator's own skills (``annotations``, ``llm-summary``) ship in
``skills/`` and are staged per surface (:func:`surface_skills`).
"""
from __future__ import annotations

import asyncio
import os

import gen
import yaml

from _agent_shared import parse_agent_config, stage_builtin_skills
from _agent_shared.logging import safe_log as _secret_log

from . import entry

# The generator-owned skills a run stages beside the runner's.
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def surface_skills(surface: str) -> tuple:
    """The generator-owned skills a run on ``surface`` stages: every surface
    gets ``llm-summary``; the annotation surface also ``annotations``."""
    if surface == "annotation":
        return ("annotations", "llm-summary")
    return ("llm-summary",)


# The generator's manifest and main config, beside its entrypoint.
MANIFEST = "agent.generator"
MAIN_CONFIG = "agent-config.set_yaml"


def config_fields(here: str) -> list:
    """The ``fields:`` block of the generator's own main config, so
    parse_agent_config enforces the schema's cross-field rules (the same
    evaluator the UI and the server gate use) rather than re-encoding them.

    It is read off the on-disk sibling of the entrypoint (``here``),
    materialised into the cell at run time like the shared ``_*`` folders —
    NOT via gen.read_file() (the output stage is not seeded yet) and NOT from
    the seeded workspace copy: runner settings only rewrite the ``values:``
    block, so the ``fields:`` schema is override-invariant and the static
    sibling is the faithful source. A missing sibling (a synthetic fixture
    with no schema file) degrades to coercion-only validation."""
    path = os.path.join(here, MAIN_CONFIG)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return (yaml.safe_load(fh.read()) or {}).get("fields", [])


def manifest(here: str) -> dict:
    """The generator's own manifest, read off the on-disk sibling of the
    entrypoint like :func:`config_fields` (``{}`` when a synthetic fixture
    has none)."""
    path = os.path.join(here, MANIFEST)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh.read()) or {}


def surface_opens(here: str, values: dict) -> list:
    """The surface files this run serves: the manifest's ``opens`` resolved
    for the run's main-config ``values`` (over the schema's defaults) by the
    host's own resolver, so the file the host created as the starting
    placeholder is the one the run serves (``consts.SessionPaths.for_run``
    takes it from here)."""
    from collimate.config_values import resolve_fields
    from collimate.surfaces import resolve_opens
    fields = config_fields(here)
    return resolve_opens(manifest(here), fields, resolve_fields(fields, values),
                         folder=gen.run_info().folder)


async def run(*, here: str) -> None:
    """One run of the generator at ``here`` on the surface its config
    picks."""
    from collimate.host_harness import probe_scope
    from _agent_shared.accounts import model_account, model_config

    raw = model_config(gen.config(MAIN_CONFIG), model_account())
    fields = config_fields(here)
    with probe_scope():
        cfg = parse_agent_config(raw, fields=fields)
    cfg["opens"] = surface_opens(here, raw)
    for w in cfg["warnings"]:
        _secret_log(f"config: {w}")
    # The generator's own skills, staged before the first turn discovers the
    # folder.
    staged = stage_builtin_skills(SKILLS_DIR, names=surface_skills(cfg["surface"]))
    if staged:
        _secret_log(f"built-in skills staged: {', '.join(staged)}")

    from _agent_shared.accounts import forget_git_session, git_session
    try:
        git_session()      # the git role: redaction registered, account kept for the harness spawns; a broken bound entry refuses (ValueError)
        await entry.run_session(cfg)
    finally:
        forget_git_session()
        # The ACP sessions close first, on this run's own loop: each adapter
        # child exits and its subprocess transport closes while the loop still
        # runs. Then the containerized CLI backends' cached containers (and
        # the bridges) go with the run so nothing outlives the generator
        # process (a oneshot run ends here too; atexit is only
        # the defensive twin).
        from _agent_shared.acp_run import clear_run_state, close_acp_sessions
        from _agent_shared.cli_container import close_all
        from _agent_shared.login_store import close_account_credentials
        from _agent_shared.harness_homes import close_harness_homes
        from _agent_shared import reset_run_state
        from _agent_shared import annotation_tools
        try:
            await close_acp_sessions()
        finally:
            try:
                await asyncio.to_thread(close_all)
            finally:
                try:
                    close_account_credentials()
                finally:
                    try:
                        close_harness_homes()
                    finally:
                        # The annotation threads the run posted in are the
                        # run's too: a host that runs several generators in
                        # one process must not hand one run's to the next.
                        reset_run_state()
                        annotation_tools.clear()
                        clear_run_state()
