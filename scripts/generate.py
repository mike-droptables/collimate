#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run every generator in the repository in dependency order, or check them all.

Every generated file is committed beside its source, but the SDK's renderer
runtime and verifier prelude: those are built and git-ignored, and the
manifest committed beside each (sdk/shared/renderer-runtime/index.json,
sdk/shared/verify/prelude.json) pins their sources and bytes, so their steps
write the outputs with the manifest and check by rebuilding and comparing.
Each step below pairs the command that writes a set of them with the command
that checks them; a few steps only check (the native payloads, the module
snapshot's bodies and its graph, the retired-token scan), and their failure
says what to do instead.

    python3 scripts/generate.py --check             # every check, in order; writes nothing
    python3 scripts/generate.py                     # every write, in order, then every check
    python3 scripts/generate.py --only contracts    # one step (with --check: its check only)
    python3 scripts/generate.py --list              # the steps and their commands

``--check`` stops at the first failing step, prints the command that failed
and the one that fixes it, and exits 1. Every step and every command is
printed, and each command's output (stderr too) goes to stdout, so a log that
keeps one stream still shows why a step failed.

The steps run the scripts the way each documents: through ``uv run`` where
the output depends on the workspace's packages or its Python (the Unicode
table, for one), with ``python3`` or ``node`` otherwise. This script is
stdlib only and installs nothing. ``bash build/prepare_sdk_checkout.sh``
installs what the steps use (the npm packages in ``sdk/scripts`` and
``sdk/packages/renderer-hooks``, the native compiler and verifier payloads,
the workspace venv); a step whose tooling is missing says to run it.

A new generator is a new step: ``sdk/tests/test_generate.py`` fails while a
``sync_*.py`` in ``sdk/scripts``, ``build/`` or ``frontend/scripts`` has none.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF = "python3 scripts/generate.py"
PREPARE = "bash build/prepare_sdk_checkout.sh"
UV = "uv run --locked"
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Step:
    name: str
    description: str
    write: tuple[str, ...]  # empty for a step that only checks
    check: tuple[str, ...]
    needs: tuple[str, ...] = ()  # paths (from the root) that preparation installs
    fix: str = ""  # a check-only step: what makes its check pass


def generator(name: str, description: str, command: str, *, needs: tuple[str, ...] = ()) -> Step:
    """A step whose script writes without arguments and checks with ``--check``."""
    return Step(name, description, (command,), (command + " --check",), needs)


NODE_SCRIPTS = "sdk/scripts/node_modules"
NODE_HOOKS = "sdk/packages/renderer-hooks/node_modules"

STEPS: tuple[Step, ...] = (
    generator("harness-catalog",
              "the Agent's harness adapters projected into the SDK's harness catalog and the "
              "Agent generator's settings schema, manifest and presets",
              f"{UV} python sdk/scripts/sync_harness_catalog.py"),
    generator("distribution-metadata",
              "the SDK's runtime METADATA, built from sdk/pyproject.toml by Hatch's wheel builder",
              f"{UV} --with hatchling python sdk/scripts/sync_distribution_metadata.py"),
    generator("skill-references",
              "the guides in sdk/shared/docs mirrored into the built-in handlers' derived skills "
              "and the Guide preset, with the handlers' accepted derived hashes",
              f"{UV} python sdk/scripts/sync_skill_references.py"),
    generator("builtins-list",
              "api/builtins.json: each built-in package's id and content hash (after "
              "skill-references, which writes under builtins/)",
              f"PYTHONPATH=api {UV} python build/sync_builtins.py"),
    generator("contracts",
              "the channel SSE records and IPC ops projected into the JSON Schema and the "
              "frontend's generated TypeScript",
              f"{UV} python sdk/scripts/sync_contracts.py"),
    generator("toolchain",
              "toolchain.json's Node, React and esbuild written into the npm manifests, lockfiles "
              "and workflows (before renderer-runtime, which hashes sdk/scripts' manifests)",
              "python3 sdk/scripts/sync_toolchain.py"),
    generator("mirror-hosts",
              "the module mirror's hosts written into the runtime's, the app's and the test "
              "harness's import maps (before renderer-runtime, which hashes mirror.ts)",
              "python3 sdk/scripts/sync_mirror_hosts.py"),
    generator("app-constants",
              "the api's tag limits and run-folder prefix written into the frontend's copies",
              "python3 frontend/scripts/sync_app_constants.py"),
    generator("casefold",
              "the frontend's case-fold exceptions from the workspace Python's Unicode table",
              f"{UV} python frontend/scripts/sync_casefold.py"),
    generator("renderer-runtime",
              "sdk/shared/renderer-runtime (React, the session runtime, the snapshot codecs: built, "
              "git-ignored) and its committed manifest, built from sdk/packages/renderer-hooks",
              "node sdk/scripts/build_renderer_runtime.mjs", needs=(NODE_SCRIPTS, NODE_HOOKS)),
    generator("verify-prelude",
              "sdk/shared/verify/prelude.js (built, git-ignored), its dependency licenses and its "
              "committed manifest prelude.json, bundled from prelude.ts",
              "node sdk/scripts/build_verify_prelude.mjs", needs=(NODE_SCRIPTS,)),
    Step("native-resources",
         "the prepared native compiler and verifier payloads match their pinned hashes",
         (), ("python3 sdk/scripts/prepare_renderer_compilers.py --verify-only",
              "python3 sdk/scripts/prepare_verifier_engine.py --verify-only"),
         needs=("sdk/build/renderer_compilers", "sdk/src/collimate/native_verifier"),
         fix=f"{PREPARE} (it downloads, verifies and stages the pinned payloads)"),
    Step("module-snapshot",
         "module-snapshot/modules/ (git-ignored; the orphan branch module-snapshot stores it) holds "
         "every body module-snapshot/index.json names, each matching its sha256",
         (), ("python3 build/snapshot_store.py check",),
         fix="python3 build/snapshot_store.py fetch"),
    Step("module-mirror",
         "module-snapshot/ holds every module the shipped renderers and the runtime import "
         "(after renderer-runtime, whose react.js and session.js are freeze roots)",
         (), (f"{UV} python -m collimate._host.module_mirror check",),
         fix=f"{UV} python -m collimate._host.module_mirror freeze (needs the network), then python3 "
             "build/snapshot_store.py publish and commit module-snapshot/'s index.json, LICENSES.json "
             "and builders.json"),
    Step("retired-tokens",
         "no retired name in the tree, generated files included (last, after every write)",
         (), ("python3 sdk/scripts/scan_retired_tokens.py",),
         fix="rename each hit it prints"),
)


def split(command: str) -> tuple[dict[str, str], list[str]]:
    """``command``'s leading ``NAME=value`` words as an environment, and its argv."""
    words = shlex.split(command)
    env = {}
    while words and ASSIGNMENT.match(words[0]):
        key, _, value = words.pop(0).partition("=")
        env[key] = value
    return env, words


def node_range(root: Path) -> str:
    try:
        return json.loads((root / "sdk/shared/contracts/toolchain.json").read_text())["node"]
    except (OSError, ValueError, KeyError):
        return "24"


def missing(steps, root: Path, check_only: bool) -> list[str]:
    """What the selected commands need and the checkout lacks, each with its remedy."""
    found: dict[str, str] = {}
    for step in steps:
        for command in (step.check if check_only else step.write + step.check):
            tool = split(command)[1][0]
            if tool in found or shutil.which(tool):
                continue
            if tool == "uv":
                found[tool] = f"{step.name}: uv is not on PATH; install it (python3 -m pip install uv), then run {PREPARE}"
            elif tool == "node":
                found[tool] = (f"{step.name}: node is not on PATH; install Node {node_range(root)} "
                               f"(sdk/shared/contracts/toolchain.json), then run {PREPARE}")
            else:
                found[tool] = f"{step.name}: {tool} is not on PATH"
        for need in step.needs:
            if need not in found and not (root / need).exists():
                found[need] = f"{step.name}: {need} is missing; run {PREPARE}"
    return list(found.values())


def run(command: str, root: Path) -> int:
    env, argv = split(command)
    sys.stdout.flush()
    return subprocess.run(argv, cwd=root, env={**os.environ, **env}, stderr=subprocess.STDOUT,
                          check=False).returncode


def remedy(step: Step) -> list[str]:
    if step.write:
        return [f"To regenerate: {' && '.join(step.write)}",
                f"          (or: {SELF} --only {step.name}), then rerun {SELF} --check"]
    return [f"To fix: {step.fix}", f"        then rerun {SELF} --check"]


def phase(label: str, steps, root: Path, writing: bool) -> int:
    chosen = [step for step in steps if step.write] if writing else list(steps)
    if not chosen:
        return 0
    print(f"generate: {label} {len(chosen)} step(s) in {root}", flush=True)
    for number, step in enumerate(chosen, 1):
        print(f"[{number}/{len(chosen)}] {step.name}: {step.description}")
        for command in step.write if writing else step.check:
            print(f"    $ {command}", flush=True)
            code = run(command, root)
            if code:
                print(f"generate: FAILED at {step.name} (exit {code}); the command was:")
                print(f"    {command}")
                if not writing:
                    print("\n".join(remedy(step)))
                return 1
        print(f"    ok: {step.name}")
    return 0


def describe(steps) -> None:
    for step in steps:
        print(f"{step.name}: {step.description}")
        for command in step.write:
            print(f"    write: {command}")
        for command in step.check:
            print(f"    check: {command}")
        if step.fix:
            print(f"    fix:   {step.fix}")


def main(argv: list[str] | None = None, *, root: Path = ROOT, steps: tuple[Step, ...] = STEPS) -> int:
    parser = argparse.ArgumentParser(prog=SELF, description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="run the checks only; write nothing")
    parser.add_argument("--only", metavar="STEP", choices=[step.name for step in steps],
                        help="run one step: " + ", ".join(step.name for step in steps))
    parser.add_argument("--list", action="store_true", help="print the steps and their commands")
    args = parser.parse_args(argv)
    selected = tuple(step for step in steps if args.only in (None, step.name))
    if args.list:
        describe(selected)
        return 0
    absent = missing(selected, root, args.check)
    if absent:
        print("generate: the tooling these steps use is not prepared:")
        for line in absent:
            print(f"    {line}")
        return 1
    if not args.check and phase("writing", selected, root, writing=True):
        return 1
    if phase("checking", selected, root, writing=False):
        return 1
    print(f"generate: every check passes ({len(selected)} step(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
