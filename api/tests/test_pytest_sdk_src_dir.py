"""A generator run's PYTHONPATH (``backends.sdk_src_dir``) holds the SDK's two
packages, ``collimate`` and ``gen``, and nothing else, in every install
layout, so nothing the server imports shadows a run's own dependencies:

  * a checkout: ``sdk/src`` (the editable install puts it and ``api`` on
    ``sys.path``);
  * the wheel (pip, ``uvx collimate``, the desktop package's server
    environment): ``<site-packages>/collimate-run-sdk``, put on ``sys.path``
    by the wheel's ``collimate-run-sdk.pth``, beside the server's packages
    and dependencies in ``site-packages`` itself.

The wheel's own contents are pinned by ``release_policy.py check-wheel``
(test_pytest_release_policy.py); a built server environment's run proves the
whole path (build/check_server_env.py, test_pytest_server_env.py).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from collimate.shared import WHEEL_SDK_HOME
from collimate_server.process_manager.backends import SDK_PACKAGES, sdk_src_dir

ROOT = Path(__file__).resolve().parents[2]


def test_a_checkout_runs_see_sdk_src_which_holds_only_the_sdk():
    assert Path(sdk_src_dir()) == ROOT / "sdk" / "src"
    assert {name for name in os.listdir(sdk_src_dir()) if not name.startswith(".")} == SDK_PACKAGES


def _wheel_layout(site: Path, *, beside_sdk=()) -> Path:
    """``site`` laid out as an installed collimate wheel leaves site-packages:
    the server package and a dependency of the server's at its top, the SDK
    packages in their home, the .pth naming it (as build/hatch_build.py
    writes it)."""
    for package in ("collimate_server", "pydantic"):
        (site / package).mkdir(parents=True)
        (site / package / "__init__.py").write_text("")
    home = site / WHEEL_SDK_HOME
    for package in ("collimate", "gen", *beside_sdk):
        (home / package).mkdir(parents=True)
        (home / package / "__init__.py").write_text("")
    (site / f"{WHEEL_SDK_HOME}.pth").write_text(WHEEL_SDK_HOME + "\n")
    return home


# The server's side: site-packages processed as the interpreter's site does
# (the .pth included), and backends.sdk_src_dir from its source file (its
# imports are the standard library's) finding the SDK the .pth put there.
_SERVER = """
import importlib.util, site, sys
site.addsitedir(sys.argv[1])
spec = importlib.util.spec_from_file_location("backends", sys.argv[2])
backends = sys.modules["backends"] = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backends)
print(backends.sdk_src_dir())
"""
BACKENDS = ROOT / "api" / "collimate_server" / "process_manager" / "backends.py"


def _server_says(site: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "-S", "-c", _SERVER, str(site), str(BACKENDS)],
                          capture_output=True, text=True, timeout=60)


def test_a_wheel_installs_runs_see_the_sdk_home_and_none_of_the_servers_packages(tmp_path):
    site = tmp_path / "site-packages"
    home = _wheel_layout(site)
    said = _server_says(site)
    assert said.returncode == 0, said.stderr
    assert said.stdout.strip() == str(home)
    # A run with that PYTHONPATH imports the SDK and cannot import the
    # server's dependency, which a run's own environment provides instead.
    run = subprocess.run(
        [sys.executable, "-S", "-c", "import collimate, gen\ntry:\n import pydantic\nexcept ImportError:\n"
                                     " print('own dependencies only')\nelse:\n print(pydantic.__file__)"],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": said.stdout.strip()},
        capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "own dependencies only"


def test_an_sdk_home_holding_anything_else_refuses_the_run(tmp_path):
    site = tmp_path / "site-packages"
    home = _wheel_layout(site, beside_sdk=("pydantic",))
    (home / "stray.py").write_text("")
    (home / ".DS_Store").write_text("")  # never importable, so never in the way
    said = _server_says(site)
    assert said.returncode != 0
    assert f"the SDK directory {home} holds pydantic, stray.py beside collimate and gen" in said.stderr


@pytest.mark.parametrize("name", ["collimate", "gen"])
def test_the_sdk_packages_are_the_two_a_run_imports(name):
    assert name in SDK_PACKAGES and len(SDK_PACKAGES) == 2
