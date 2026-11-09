# SPDX-License-Identifier: MIT
"""``scripts/generate.py``: the repository's one generate-and-check command.

The checkout passes ``--check``; the declared steps cover every ``sync_*.py``
generator, so a new one cannot be left out; and the runner itself checks
without writing, stops at the first failure with the commands to run, writes
in order before it checks, and names the preparation when tooling is absent.
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
SCRIPT = WORKSPACE / "scripts" / "generate.py"
GENERATOR_DIRS = ("sdk/scripts", "build", "frontend/scripts")
# Read before the suite's autouse fixture points XDG_CACHE_HOME at an empty
# directory: the real check runs on the machine's uv cache, as CI's does.
CACHE = os.environ.get("XDG_CACHE_HOME")

_spec = importlib.util.spec_from_file_location("repo_generate", SCRIPT)
generate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = generate  # the dataclass resolves its annotations through it
_spec.loader.exec_module(generate)


def scripts_named(command: str) -> list[str]:
    """The repository scripts ``command`` runs, as root-relative paths."""
    return [word for word in generate.split(command)[1] if word.endswith((".py", ".mjs"))]


def test_the_checkout_passes_every_check():
    env = {key: value for key, value in os.environ.items() if key != "XDG_CACHE_HOME"}
    if CACHE is not None:
        env["XDG_CACHE_HOME"] = CACHE
    result = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True,
                            text=True, timeout=600, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "generate: writing" not in result.stdout
    for step in generate.STEPS:
        assert f"ok: {step.name}" in result.stdout, step.name
        for command in step.check:
            assert f"$ {command}\n" in result.stdout, command


def test_every_generator_script_is_a_step():
    written = {path for step in generate.STEPS for command in step.write
               for path in scripts_named(command)}
    generators = {path.relative_to(WORKSPACE).as_posix()
                  for folder in GENERATOR_DIRS for path in (WORKSPACE / folder).glob("sync_*.py")}
    assert generators, GENERATOR_DIRS
    assert generators <= written, f"no generate.py step writes with {sorted(generators - written)}"


def test_every_step_names_scripts_that_exist_and_says_how_to_fix_it():
    names = [step.name for step in generate.STEPS]
    assert len(names) == len(set(names))
    for step in generate.STEPS:
        assert step.check, step.name
        assert step.write or step.fix, f"{step.name} only checks, so it must say how to fix it"
        for command in step.write + step.check:
            for path in scripts_named(command):
                assert (WORKSPACE / path).is_file(), (step.name, path)


def test_the_order_writes_each_input_before_what_reads_it():
    names = [step.name for step in generate.STEPS]
    for before, after in (("skill-references", "builtins-list"),  # refs are written under builtins/
                          ("toolchain", "renderer-runtime"),  # sdk/scripts' manifests are hashed inputs
                          ("mirror-hosts", "renderer-runtime"),  # mirror.ts is a hashed input
                          ("renderer-runtime", "module-mirror")):  # react.js, session.js are freeze roots
        assert names.index(before) < names.index(after), (before, after)
    assert names[-1] == "retired-tokens"


def test_preparation_writes_no_generated_file():
    """CI checks right after ``build/prepare_sdk_checkout.sh``; if it ran a
    generator, the check would read what it had just written."""
    prepare = (WORKSPACE / "build" / "prepare_sdk_checkout.sh").read_text()
    commands = [line for line in prepare.splitlines() if line.strip() and not line.startswith("#")]
    for step in generate.STEPS:
        for command in step.write:
            for path in scripts_named(command):
                assert not any(Path(path).name in line for line in commands), (step.name, path)


# The runner, on steps of its own in a scratch root.

def py(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def log(entry: str) -> str:
    return py(f"open('log', 'a').write({entry!r} + '\\n')")


def steps(fail: str = ""):
    def check(name):
        return py(f"import sys; open('log', 'a').write('check {name}\\n'); sys.exit(3)") if name == fail \
            else log(f"check {name}")
    return (
        generate.Step("one", "first", (log("write one"),), (check("one"),)),
        generate.Step("two", "second", (log("write two"),), (check("two"),)),
        generate.Step("three", "check only", (), (check("three"),), fix="do the thing"),
    )


def entries(root: Path) -> list[str]:
    path = root / "log"
    return path.read_text().splitlines() if path.exists() else []


def test_check_writes_nothing_and_stops_at_the_first_failure(tmp_path, capfd):
    declared = steps(fail="two")
    assert generate.main(["--check"], root=tmp_path, steps=declared) == 1
    out = capfd.readouterr().out
    assert entries(tmp_path) == ["check one", "check two"]
    assert "FAILED at two (exit 3)" in out
    assert f"    {declared[1].check[0]}\n" in out
    assert f"To regenerate: {declared[1].write[0]}" in out
    assert "python3 scripts/generate.py --only two" in out
    assert "[1/3] one: first" in out and "[3/3]" not in out


def test_a_failing_check_only_step_says_how_to_fix_it(tmp_path, capfd):
    assert generate.main(["--check", "--only", "three"], root=tmp_path, steps=steps(fail="three")) == 1
    assert "To fix: do the thing" in capfd.readouterr().out


def test_without_check_every_write_runs_in_order_then_every_check(tmp_path, capfd):
    assert generate.main([], root=tmp_path, steps=steps()) == 0
    assert entries(tmp_path) == ["write one", "write two", "check one", "check two", "check three"]
    assert "every check passes (3 step(s))" in capfd.readouterr().out


def test_only_runs_one_step(tmp_path):
    assert generate.main(["--only", "two"], root=tmp_path, steps=steps()) == 0
    assert entries(tmp_path) == ["write two", "check two"]


def test_missing_tooling_names_the_preparation_and_runs_nothing(tmp_path, capfd, monkeypatch):
    needy = (generate.Step("runtime", "needs npm packages", (log("write"),), (log("check"),),
                           needs=("sdk/scripts/node_modules",)),)
    assert generate.main(["--check"], root=tmp_path, steps=needy) == 1
    out = capfd.readouterr().out
    assert "sdk/scripts/node_modules is missing; run bash build/prepare_sdk_checkout.sh" in out
    assert entries(tmp_path) == []

    monkeypatch.setattr(generate.shutil, "which", lambda tool: None)
    uv = (generate.Step("uv", "a uv step", (), ("uv run --locked python -c pass",), fix="x"),)
    assert generate.main(["--check"], root=tmp_path, steps=uv) == 1
    assert "uv is not on PATH" in capfd.readouterr().out
