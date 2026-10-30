# SPDX-License-Identifier: MIT
"""Every shipped generator keeps to the public SDK surface.

Generator cells are frozen per channel while the SDK library is injected
fresh, so a generator that reaches past the public line (an internal module,
a name ``gen`` does not export, the environment the host hands the SDK)
breaks the moment the SDK's internals move. The public line is an allowlist:

* ``gen`` — exactly ``gen.__all__`` plus the per-run ``gen.prompt`` /
  ``gen.cancelled``, however it is spelled: ``gen.x``, an alias
  (``import gen as g``), ``from gen import x``, or ``getattr(gen, "x")``
  (a name that is not a literal is refused), and the Agent generator's
  ``gen.session`` namespace, exactly its ``__all__``;
* ``collimate`` — only the modules in ``collimate.__all__``, imported or
  named in a string (``importlib.import_module("collimate._host")``), and no
  ``_``-prefixed name from one of them;
* the host's run environment — no string naming one of the ``COLLIMATE_*``
  variables the SDK reads (the SDK is the one reader: ``gen.grant()``,
  ``gen.run_info()``, ``gen.can_ask()`` …), whether it is read through
  ``os.environ``, ``getenv``, a copy, a wrapper or a shell; no name built
  from a ``"COLLIMATE_"`` prefix; no ``/proc/self/environ``.

Docstrings are prose and are not scanned.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import collimate
import gen as _gen
import gen.session as _gen_session
from collimate.native import DYNAMIC_ATTRS

from tests._workspace import GENERATORS


_REPO = Path(__file__).resolve().parents[1]
_GENERATORS = GENERATORS

_GEN_PUBLIC = frozenset(_gen.__all__) | frozenset(DYNAMIC_ATTRS) | {"session"}
_SESSION_PUBLIC = frozenset(_gen_session.__all__)
_COLLIMATE_PUBLIC = frozenset(collimate.__all__)


def _host_env_names() -> frozenset:
    """Every ``COLLIMATE_*`` variable the SDK itself reads or names: the
    host's run environment, derived from the SDK source so a new one is
    covered the day it is added."""
    names: set = set()
    for path in (_REPO / "src").rglob("*.py"):
        names |= set(re.findall(r"COLLIMATE_[A-Z0-9_]*[A-Z0-9]", path.read_text(encoding="utf-8")))
    return frozenset(names)


# A variable the host no longer sets (the run's cell id is ``COLLIMATE_CELL_ID``,
# read through ``gen.run_info()``): a generator reading it is stale, so it
# stays forbidden although the SDK no longer names it.
_RETIRED_HOST_ENV = frozenset({"COLLIMATE_IN_PLACE_CELL_ID"})

_HOST_ENV = _host_env_names() | _RETIRED_HOST_ENV
_HOST_ENV_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _HOST_ENV))) + r")\b")


def _generator_files() -> list:
    return sorted(
        p for p in _GENERATORS.rglob("*.py")
        if "__pycache__" not in p.parts and p.relative_to(_GENERATORS).parts[0] != "tmp"
    )


def _docstring_ids(tree: ast.AST) -> set:
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _collimate_module_problem(dotted: str) -> "str | None":
    """Why the ``collimate.…`` module path ``dotted`` is off limits, or None."""
    parts = dotted.split(".")
    if parts[0] != "collimate" or len(parts) == 1:
        return None
    if parts[1] not in _COLLIMATE_PUBLIC:
        return f"non-public module collimate.{parts[1]}"
    if any(p.startswith("_") for p in parts[2:]):
        return f"private module {dotted}"
    return None


def violations(source: str) -> list:
    """``(line, what)`` for every rule ``source`` breaks."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    found: list = []
    gen_aliases = {"gen"}          # names bound to the gen package
    session_aliases: set = set()   # names bound to gen.session
    collimate_aliases: set = set()  # names bound to the collimate package
    public_aliases: set = set()    # names bound to a public collimate module

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "gen":
                    gen_aliases.add(alias.asname or "gen")
                elif alias.name == "gen.session":
                    if alias.asname:
                        session_aliases.add(alias.asname)
                    else:
                        gen_aliases.add("gen")
                elif alias.name.startswith("gen."):
                    found.append((node.lineno, f"{alias.name} is not a gen namespace"))
                elif alias.name == "collimate":
                    collimate_aliases.add(alias.asname or "collimate")
                elif alias.name.startswith("collimate."):
                    problem = _collimate_module_problem(alias.name)
                    if problem:
                        found.append((node.lineno, problem))
                    elif alias.asname:
                        public_aliases.add(alias.asname)
                    else:
                        collimate_aliases.add("collimate")
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            if node.module == "gen":
                for alias in node.names:
                    if alias.name != "*" and alias.name not in _GEN_PUBLIC:
                        found.append((node.lineno, f"gen.{alias.name} is not exported"))
                    elif alias.name == "session":
                        session_aliases.add(alias.asname or "session")
            elif node.module == "gen.session":
                for alias in node.names:
                    if alias.name not in _SESSION_PUBLIC:
                        found.append((node.lineno, f"gen.session.{alias.name} is not exported"))
            elif node.module.startswith("gen."):
                found.append((node.lineno, f"{node.module} is not a gen namespace"))
            elif node.module == "collimate":
                for alias in node.names:
                    if alias.name not in _COLLIMATE_PUBLIC:
                        found.append((node.lineno, f"non-public module collimate.{alias.name}"))
                    else:
                        public_aliases.add(alias.asname or alias.name)
            elif node.module.startswith("collimate."):
                problem = _collimate_module_problem(node.module)
                if problem:
                    found.append((node.lineno, problem))
                for alias in node.names:
                    if alias.name.startswith("_"):
                        found.append((node.lineno, f"private name {node.module}.{alias.name}"))

    def attr_problem(base: str, attr: str) -> "str | None":
        if base in gen_aliases and attr not in _GEN_PUBLIC:
            return f"gen.{attr} is not exported"
        if base in session_aliases and attr not in _SESSION_PUBLIC:
            return f"gen.session.{attr} is not exported"
        if base in collimate_aliases and attr not in _COLLIMATE_PUBLIC:
            return f"non-public module collimate.{attr}"
        if base in public_aliases and attr.startswith("_"):
            return f"private name {base}.{attr}"
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            problem = attr_problem(node.value.id, node.attr)
            if problem:
                found.append((node.lineno, problem))
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
              and isinstance(node.value.value, ast.Name) and node.value.value.id in gen_aliases
              and node.value.attr == "session" and node.attr not in _SESSION_PUBLIC):
            found.append((node.lineno, f"gen.session.{node.attr} is not exported"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("getattr", "hasattr", "setattr", "delattr")
              and len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
              and node.args[0].id in gen_aliases | collimate_aliases | public_aliases):
            name = node.args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                problem = attr_problem(node.args[0].id, name.value)
                if problem:
                    found.append((node.lineno, problem))
            else:
                found.append((node.lineno, f"{node.func.id} on {node.args[0].id} with a computed name"))
        elif isinstance(node, ast.Constant) and id(node) not in docs \
                and isinstance(node.value, (str, bytes)):
            text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
            m = _HOST_ENV_RE.search(text)
            if m:
                found.append((node.lineno, f"host variable {m.group(1)}"))
            if text == "COLLIMATE_":
                found.append((node.lineno, "COLLIMATE_ prefix"))
            if "/proc/self/environ" in text:
                found.append((node.lineno, "the process environment file"))
            if re.fullmatch(r"collimate(\.\w+)+", text):
                problem = _collimate_module_problem(text)
                if problem:
                    found.append((node.lineno, problem))
        elif isinstance(node, ast.JoinedStr) and node.values \
                and isinstance(node.values[0], ast.Constant) \
                and str(node.values[0].value).startswith("COLLIMATE_"):
            found.append((node.lineno, "a COLLIMATE_ name built at run time"))
        elif isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant) \
                and isinstance(node.left.value, str) and node.left.value.startswith("COLLIMATE_"):
            found.append((node.lineno, "a COLLIMATE_ name built at run time"))
    return sorted(set(found))


@pytest.mark.parametrize(
    "path", _generator_files(), ids=lambda p: str(p.relative_to(_GENERATORS)))
def test_generator_uses_only_the_public_sdk(path):
    found = violations(path.read_text(encoding="utf-8"))
    assert not found, "\n".join(f"{path}:{line}: {what}" for line, what in found)


def test_the_scan_covers_every_generator():
    files = {p.relative_to(_GENERATORS).parts[0] for p in _generator_files()}
    assert {"agent", "git", "launch", "web", "_agent_shared", "_env_shared", "_web_shared"} <= files


def test_existing_handler_and_runtime_owners_are_public_P4F33():
    """Existing contracts must be declared, not exempted from the scanner."""
    assert 'handlers' in collimate.__all__
    assert not violations('from collimate import handlers\n'
                          'from collimate.shared import sdk_distribution\n')
    from collimate import handlers
    from collimate.shared import sdk_distribution
    assert callable(handlers.validate_handler)
    assert sdk_distribution().metadata['Name'] == 'collimate-sdk'


def test_the_host_environment_is_derived_from_the_sdk():
    assert {"COLLIMATE_PERMISSIONS", "COLLIMATE_RUN_ID", "COLLIMATE_CHANNEL",
            "COLLIMATE_CELL_ID", "COLLIMATE_IPC_PORT",
            "COLLIMATE_PACKAGES", "COLLIMATE_RUNNER", "COLLIMATE_UI_FILES"} <= _host_env_names()


def test_the_retired_cell_variable_stays_forbidden():
    assert "COLLIMATE_IN_PLACE_CELL_ID" not in _host_env_names()
    assert "COLLIMATE_IN_PLACE_CELL_ID" in _HOST_ENV


@pytest.mark.parametrize("source", [
    # private SDK modules and names, however imported
    "from collimate._host.bridge import Message\n",
    "from collimate import _results\n",
    "import collimate._host.bridge as h\n",
    "from collimate._host import bridge as h\n",
    "from collimate.native import _require_bridge\n",
    "from collimate.native import HostBridge\n",
    "import collimate.native as n\nn._require_bridge()\n",
    "from collimate import native\nb = native._bridge\n",
    "import collimate\nx = collimate._host.bridge.Message\n",
    "from collimate.lint.core import _check_enforces\n",
    "from collimate import fs as f\nf._check_parts([])\n",
    "import importlib\nh = importlib.import_module('collimate._host')\n",
    "h = __import__('collimate.native')\n",
    "import sys\nh = sys.modules['collimate._host']\n",
    # gen past its exports
    "import gen\nb = gen._require_bridge()\n",
    "import gen\nb = getattr(gen, '_require_bridge')()\n",
    "import gen\nname = '_bridge'\nb = getattr(gen, name)\n",
    "from gen import _require_bridge\n",
    "import gen as g\nb = g._bridge\n",
    "import gen\nh = gen.HostBridge\n",
    "import gen\nm = gen.messages\n",
    # gen.session past its exports
    "import gen\nx = gen.session.SessionState\n",
    "import gen.session as s\nx = s._session\n",
    "from gen.session import SessionState\n",
    "from gen import session\nx = session.serve_chat\n",
    "import gen.native\n",
    "from collimate._host import ipc\n",
    "import collimate._host.channel_events\n",
    # the host's environment, however it is read
    "import os\nx = os.environ.get('COLLIMATE_RUN_ID', '')\n",
    "import os\nx = os.environ['COLLIMATE_CHANNEL']\n",
    "import os\nx = os.getenv('COLLIMATE_CELL_ID')\n",
    "import os\nK = 'COLLIMATE_IN_PLACE_CELL_ID'\nx = os.environ.get(K)\n",
    "import os\nx = os.environ.get('COLLIMATE_CELL_ID', '')\n",
    "import json, os\nx = json.loads(os.environ['COLLIMATE_RUNNER'])['folder']\n",
    "import os\nK: str = 'COLLIMATE_RUN_ID'\nx = os.environ.get(K)\n",
    "import os\ndef f():\n    k = 'COLLIMATE_RUN_ID'\n    return os.environ.get(k)\n",
    "import os\nclass C:\n    K = 'COLLIMATE_RUN_ID'\n",
    "from os import environ\nx = environ.get('COLLIMATE_RUN_ID')\n",
    "from os import getenv\nx = getenv('COLLIMATE_RUN_ID')\n",
    "import os as o\nx = o.environ.get('COLLIMATE_RUN_ID')\n",
    "import os\nx = 'COLLIMATE_CELL_ID' in os.environ\n",
    "import os\nx = os.environ.copy()['COLLIMATE_RUN_ID']\n",
    "import os\nx = dict(os.environ).get('COLLIMATE_RUN_ID')\n",
    "import os\nx = os.environ.pop('COLLIMATE_RUN_ID', '')\n",
    "import os\nx = os.environb.get(b'COLLIMATE_RUN_ID')\n",
    "import os\nx = os.environ.get(key='COLLIMATE_RUN_ID')\n",
    "def _env(n):\n    return n\nx = _env('COLLIMATE_RUN_ID')\n",
    "import os\nx = os.environ.get('COLLIMATE_' + 'RUN_ID')\n",
    "import os\nk = 'RUN_ID'\nx = os.environ.get(f'COLLIMATE_{k}')\n",
    "import os\nx = {k: v for k, v in os.environ.items() if k.startswith('COLLIMATE_')}\n",
    "import subprocess\nx = subprocess.check_output(['sh', '-c', 'echo $COLLIMATE_RUN_ID'])\n",
    "x = open('/proc/self/environ').read()\n",
])
def test_the_scan_catches_each_route_past_the_public_line(source):
    assert violations(source), source


@pytest.mark.parametrize("source", [
    "import gen\nfrom gen import Message\nfrom collimate import fs\n",
    "import gen\np = gen.prompt\nc = gen.cancelled\nh = gen.history.append\n",
    "import gen\nf = getattr(gen, 'grant')\n",
    "import gen\ngen.session.submit_turn('x', source='@opening')\n",
    "import gen.session\nbusy = gen.session.chat_busy()\n",
    "from gen.session import request_finish\n",
    "from gen import session\nsession.interrupt_turn()\n",
    "from collimate.pointers import terminal_pointer\n",
    "import collimate.permissions\ng = collimate.permissions.Grant\n",
    "from collimate.lint import declared_enforces\n",
    "import os\nos.environ['COLLIMATE_PROMPT'] = 'x'\n",           # a variable for a child
    "env = {'COLLIMATE_GIT_CRED': 'x'}\n",                            # a child's env
    "import os\nx = os.environ.get('HOME')\n",
    '"""Reads COLLIMATE_RUN_ID through gen.run_info()."""\n',        # prose
])
def test_the_scan_allows_the_public_surface(source):
    assert violations(source) == []
