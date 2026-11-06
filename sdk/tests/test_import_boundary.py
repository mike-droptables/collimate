# SPDX-License-Identifier: MIT
"""The import boundary the repo boundary used to provide (plan Phase 5.3).

Two rules, each proved twice:

* ``sdk/src`` and the first-party content (``content/`` and the built-ins'
  Python under ``builtins/``) never import the server (``collimate_server``,
  in ``api/collimate_server/``);
* content imports only the documented SDK surface: ``gen`` (with the Agent
  generator's ``gen.session``), the modules in ``collimate.__all__``, and its
  own helper packages (``_agent_shared``, ``_env_shared``, ``_web_shared``).
  ``collimate._host`` (the host plumbing), ``collimate._results`` and
  ``collimate.native`` are the SDK's own.

The static scan reads every import statement, the literal dynamic forms
(``importlib.import_module``, ``__import__``, ``find_spec``,
``sys.modules[...]``) and attribute reach through a name bound to
``collimate``, function bodies included. The walk builds the one wheel into a
scratch directory, installs it alone into a fresh venv (only its declared
dependencies come with it), and imports every ``collimate`` module and every
generator module there with ``python -I -B`` (writing no bytecode into the
trees it reads), so the checkout's sources are off ``sys.path`` and every SDK
import resolves from the wheel. The wheel carries the server too, so the walk
refuses ``collimate_server`` ahead of every other finder: an import of it
fails the walk although it is installed. A third-party package a generator
declares in its PEP 723 header is not in that venv; the walk stubs it and
reports its name (``tests/_import_walk.py``).
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import collimate
import gen.session

from tests._workspace import BUILTINS, CONTENT, GENERATORS, SDK, WORKSPACE

API = WORKSPACE / "api"
HELPERS = ("_agent_shared", "_env_shared", "_web_shared")
_SKIP_DIRS = {"__pycache__", "node_modules", "tests", "tests_live", "static", "dist"}


def _api_names() -> frozenset:
    """The server's importable top-level names: the packages under ``api/``
    (``collimate_server``), not its tests."""
    return frozenset(d.name for d in API.iterdir()
                     if d.is_dir() and d.name not in _SKIP_DIRS and (d / "__init__.py").is_file())


API_NAMES = _api_names()


def _python_files(*roots: Path) -> list[Path]:
    return sorted(p for root in roots for p in root.rglob("*.py")
                  if not {"__pycache__", "node_modules"} & set(p.parts))


def _content_files() -> list[Path]:
    return _python_files(CONTENT, BUILTINS)


def references(source: str) -> list[tuple[int, str]]:
    """``(line, dotted module)`` for every module ``source`` reaches by name.
    Relative imports are the importing package's own and yield nothing."""
    tree = ast.parse(source)
    found: list = []
    bound: set = set()   # names bound to the collimate package
    loaders = {"import_module", "__import__", "find_spec"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "importlib.util"):
            loaders |= {a.asname for a in node.names if a.asname and a.name in loaders}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
                if alias.name == "collimate" or (alias.name.startswith("collimate.") and not alias.asname):
                    bound.add(alias.asname or "collimate")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.lineno, node.module))
            # A name imported from a package may be its submodule (``from
            # collimate import fs``) and one from gen is an export; past
            # those, only a private name says anything.
            whole = node.module in ("collimate", "gen", "gen.session")
            if whole or node.module.startswith(("collimate.", "gen.")):
                found += [(node.lineno, f"{node.module}.{a.name}") for a in node.names
                          if a.name != "*" and (whole or a.name.startswith("_"))]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in loaders:
                arg = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg in ("name", "fullname")), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((node.lineno, arg.value))
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
              and node.value.attr == "modules" and isinstance(node.slice, ast.Constant)
              and isinstance(node.slice.value, str)):
            found.append((node.lineno, node.slice.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
            found.append((node.lineno, f"collimate.{node.attr}"))
    return found


def app_imports(source: str, *, local: frozenset = frozenset()) -> list[tuple[int, str]]:
    """The references of ``source`` to a module the app defines. ``local``:
    top-level names that resolve beside the file instead (its own folder, the
    content helpers), and the standard library, never count."""
    return [(line, name) for line, name in references(source)
            if name.split(".")[0] in API_NAMES
            and name.split(".")[0] not in local | set(sys.stdlib_module_names)]


def surface_problem(name: str) -> "str | None":
    """Why content may not reach ``name``, or None."""
    parts = name.split(".")
    if parts[0] == "gen":
        if len(parts) == 1 or parts[1] in gen.__all__ or (parts[1] == "session" and (
                len(parts) == 2 or parts[2] in gen.session.__all__)):
            return None
        return f"{name} is not gen's surface"
    if parts[0] != "collimate" or len(parts) == 1:
        return None
    if parts[1] not in collimate.__all__:
        return f"{name}: collimate.{parts[1]} is not a documented module"
    if any(p.startswith("_") for p in parts[2:]):
        return f"{name} is private"
    return None


def _local_names(path: Path) -> frozenset:
    """Top-level names a content file imports from beside itself."""
    names = {p.stem for p in path.parent.glob("*.py")} | {d.name for d in path.parent.iterdir() if d.is_dir()}
    if GENERATORS in path.parents:
        names |= set(HELPERS)
    return frozenset(names)


# ── the static scan ────────────────────────────────────────────────────


def test_the_app_names_are_its_modules_and_packages():
    assert API_NAMES == {"collimate_server"}


@pytest.mark.parametrize("path", _python_files(SDK / "src"), ids=lambda p: str(p.relative_to(SDK)))
def test_the_sdk_never_imports_the_app(path):
    assert app_imports(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", _content_files(), ids=lambda p: str(p.relative_to(WORKSPACE)))
def test_content_never_imports_the_app_and_keeps_to_the_documented_surface(path):
    source = path.read_text(encoding="utf-8")
    assert app_imports(source, local=_local_names(path)) == []
    problems = [(line, surface_problem(name)) for line, name in references(source)]
    assert [p for p in problems if p[1]] == []


def test_the_scan_covers_the_sdk_the_content_and_the_built_ins():
    content = {p.relative_to(WORKSPACE).as_posix() for p in _content_files()}
    assert {"content/generators/agent/agent/agent.py", "content/generators/launch/launch/run.py",
            "content/generators/_agent_shared/session/entry.py",
            "builtins/definitions/generator/examples/basic/main.py"} <= content
    assert len(_python_files(SDK / "src")) > 40


@pytest.mark.parametrize("source", [
    "from collimate_server.process_manager.ipc import IPCMixin\n",
    "import collimate_server.routes.runs.create\n",
    "def f():\n    from collimate_server.persistence import save\n",
    "from collimate_server import main\n",
    "import importlib\nm = importlib.import_module('collimate_server.main')\n",
    "from importlib import import_module as load\nm = load('collimate_server.routes')\n",
    "m = __import__('collimate_server.events')\n",
    "import sys\nm = sys.modules['collimate_server']\n",
])
def test_the_scan_catches_each_way_to_the_app(source):
    assert app_imports(source)


@pytest.mark.parametrize("source", [
    # the host plumbing and the SDK's other internals, however reached
    "from collimate._host import ipc\n",
    "import collimate._host.channel_events\n",
    "from collimate._host.bridge import HostBridge\n",
    "from collimate import _host\n",
    "from collimate import _results\n",
    "import collimate._results as r\n",
    "from collimate.native import HostBridge\n",
    "def nested():\n    from collimate._host.bridge import HostBridge\n",
    "import importlib\nh = importlib.import_module('collimate._host.ipc')\n",
    "import importlib\nh = importlib.import_module(name='collimate._results')\n",
    "h = __import__('collimate.native')\n",
    "import sys\nh = sys.modules['collimate._host']\n",
    "import collimate as sdk\nb = sdk._host.bridge\n",
    "import collimate\nb = collimate.native\n",
    "from collimate.fs import _open_ws_dir\n",
    "import gen.session as s\nfrom gen.session import SessionState\n",
])
def test_the_scan_refuses_each_way_past_the_documented_surface(source):
    assert [name for _, name in references(source) if surface_problem(name)], source


@pytest.mark.parametrize("source", [
    "import gen\nfrom gen import Message\nimport gen.session\nfrom gen.session import submit_turn\n",
    "from collimate import fs, pointers\nimport collimate.permissions\n",
    "from collimate.shared import sdk_distribution\nfrom collimate.testing.forward import Forwarder\n",
    "from collimate.lint import lint_path\nimport collimate\nx = collimate.providers\n",
    "from _agent_shared import harnesses\nimport yaml, json\n",
    "from .config import load\n",
])
def test_the_scan_allows_the_documented_surface(source):
    assert [name for _, name in references(source) if surface_problem(name)] == []
    assert app_imports(source, local=frozenset(HELPERS)) == []


# ── the walk in a venv holding only the one wheel ──────────────────────


# Run in the walk venv: the distributions the wheel needs, transitively. A
# platform marker is not evaluated (a superset is the bound); an extra is skipped.
CLOSURE = """
import importlib.metadata as m, json, re
seen, todo = set(), ['collimate']
while todo:
    name = re.sub(r'[-_.]+', '-', todo.pop()).lower()
    if name in seen:
        continue
    seen.add(name)
    try:
        requires = m.distribution(name).requires or []
    except m.PackageNotFoundError:
        continue
    todo += [re.match(r'[A-Za-z0-9._-]+', r).group() for r in requires if 'extra ==' not in r]
print(json.dumps(sorted(seen)))
"""


@pytest.fixture(scope="module")
def walk(tmp_path_factory):
    if sys.version_info < (3, 13):
        pytest.skip("the walk installs the collimate wheel into a venv of this Python, "
                    "and the distribution requires Python 3.13")
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("the import walk needs uv on PATH (python3 -m pip install uv)")
    scratch = tmp_path_factory.mktemp("import-boundary")
    wheels = scratch / "wheel"
    built = subprocess.run([uv, "build", "--wheel", "--out-dir", str(wheels)],
                           cwd=WORKSPACE, capture_output=True, text=True, timeout=900)
    assert built.returncode == 0, built.stdout + built.stderr
    [wheel] = wheels.glob("collimate-*.whl")
    venv = scratch / "venv"
    made = subprocess.run([uv, "venv", "--python", sys.executable, str(venv)],
                          capture_output=True, text=True, timeout=300)
    assert made.returncode == 0, made.stdout + made.stderr
    python = venv / "bin" / "python"
    install = [uv, "pip", "install", "--python", str(python), str(wheel)]
    installed = subprocess.run([*install, "--offline"], capture_output=True, text=True, timeout=600)
    if installed.returncode != 0:  # an empty cache: fetch the declared dependencies once
        installed = subprocess.run(install, capture_output=True, text=True, timeout=600)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    frozen = subprocess.run([uv, "pip", "freeze", "--python", str(python)],
                            capture_output=True, text=True, timeout=120)
    files = [str(p) for p in _content_files()
             if not {"_agent_shared", "_env_shared", "_web_shared"} & set(p.relative_to(WORKSPACE).parts)]
    argument = json.dumps({"api_names": sorted(API_NAMES), "generators": str(GENERATORS), "files": files})
    ran = subprocess.run([str(python), "-I", "-B", str(Path(__file__).with_name("_import_walk.py")), argument],
                         cwd=scratch, capture_output=True, text=True, timeout=600)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    report = json.loads(ran.stdout.strip().splitlines()[-1])
    closure = subprocess.run([str(python), "-I", "-c", CLOSURE], capture_output=True, text=True, timeout=120)
    assert closure.returncode == 0, closure.stderr
    report["closure"] = json.loads(closure.stdout)
    report["installed"] = sorted(line.split("==")[0].lower() for line in frozen.stdout.splitlines() if "==" in line)
    report["venv"] = str(venv)
    return report


def test_the_walk_venv_holds_only_the_wheel_and_its_declared_dependencies(walk):
    # The one wheel and what it declares, transitively; pydantic and the rest
    # of the server's requirements are installed, and the SDK still imports
    # without them (the walk stubs those, see _import_walk.SERVER_ONLY).
    installed = {name.replace("_", "-") for name in walk["installed"]}
    assert installed <= set(walk["closure"]) | {"collimate"}, sorted(installed - set(walk["closure"]))
    assert walk["collimate_origin"].startswith(walk["venv"]), walk["collimate_origin"]
    # Installed beside the SDK, and refused all the same.
    assert walk["api_found"] == sorted(API_NAMES)


def test_the_walk_imports_every_sdk_module_from_the_wheel(walk):
    assert walk["failures"] == []
    assert {"collimate.native", "collimate._host.bridge", "collimate._host.ipc", "collimate.lint.core",
            "collimate.testing", "gen", "gen.session"} <= set(walk["collimate"])
    assert len(walk["collimate"]) > 40


def test_the_walk_imports_every_generator_module_without_the_app(walk):
    assert walk["api_imports"] == []
    assert walk["failures"] == []
    walked = set(walk["content"])
    assert len(walked) == len(_content_files())
    assert {"_agent_shared.session.entry", "_env_shared", "_web_shared.daemon"} <= walked
    # What the venv lacks is third-party: each a package a generator declares,
    # never the SDK, a content helper or the app.
    assert not {"collimate", "gen", *HELPERS} & set(walk["stubbed"])
    assert not API_NAMES & set(walk["stubbed"])
    assert not set(sys.stdlib_module_names) & set(walk["stubbed"])


def test_the_walk_refuses_an_app_import_and_resolves_no_sdk_name_it_lacks(walk, tmp_path):
    """The walk's own mechanism: a generator module that imports the app, or
    an SDK module the wheel does not have, fails it."""
    (tmp_path / "reaches_the_app.py").write_text("import gen\nfrom collimate_server.process_manager.ipc import IPCMixin\n")
    (tmp_path / "reaches_past_the_sdk.py").write_text("from collimate import retired_module\n")
    argument = json.dumps({"api_names": sorted(API_NAMES), "generators": str(tmp_path / "none"),
                           "files": [str(tmp_path / "reaches_the_app.py"), str(tmp_path / "reaches_past_the_sdk.py")]})
    ran = subprocess.run([str(Path(walk["venv"]) / "bin" / "python"), "-I", "-B",
                          str(Path(__file__).with_name("_import_walk.py")), argument],
                         cwd=tmp_path, capture_output=True, text=True, timeout=300)
    report = json.loads(ran.stdout.strip().splitlines()[-1])
    assert report["api_imports"] == ["collimate_server"]
    assert sorted(Path(f["module"]).name for f in report["failures"]) == [
        "reaches_past_the_sdk.py", "reaches_the_app.py"]
    assert report["stubbed"] == []
