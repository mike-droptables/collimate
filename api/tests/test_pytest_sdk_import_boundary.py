"""The app's side of the SDK import boundary (what P6R01 kept, Phase 5).

The app imports the SDK's host plumbing from ``collimate._host.<module>``
(the IPC and channel-event contracts, renderer compilation and verification,
the module snapshot, the asset-tree reader), and the public modules. It never
imports the generator process's own runtime: the bridge
(``collimate._host.bridge``) and the result types' module
(``collimate._results``); a public type comes through ``collimate.native``.
The SDK's own test, ``sdk/tests/test_import_boundary.py``, proves the other
direction: the SDK and content never import the app.
"""
import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
GENERATOR_RUNTIME = ("collimate._host.bridge", "collimate._results")


def references(source: str) -> list[str]:
    """Every module ``source`` reaches by name: imports (nested ones too),
    literal dynamic imports and attribute reach through a name bound to
    ``collimate`` or to ``collimate._host``."""
    tree = ast.parse(source)
    found, bound = [], {}
    loaders = {"import_module", "__import__", "find_spec"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "importlib.util"):
            loaders |= {a.asname for a in node.names if a.asname and a.name in loaders}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
                if alias.name in ("collimate", "collimate._host"):
                    bound[alias.asname or alias.name] = alias.name
                elif alias.name.startswith("collimate.") and not alias.asname:
                    bound["collimate"] = "collimate"
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append(node.module)
            found += [f"{node.module}.{a.name}" for a in node.names if node.module in ("collimate", "collimate._host")]
            for a in node.names:
                if node.module == "collimate" and a.name == "_host":
                    bound[a.asname or a.name] = "collimate._host"
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in loaders:
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "name"), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain, value = [node.attr], node.value
            while isinstance(value, ast.Attribute):
                chain.insert(0, value.attr)
                value = value.value
            if isinstance(value, ast.Name) and value.id in bound:
                found.append(".".join([bound[value.id], *chain]))
    return found


def runtime_imports(source: str) -> list[str]:
    return [name for name in references(source)
            if any(name == mod or name.startswith(mod + ".") for mod in GENERATOR_RUNTIME)]


def _app_sources() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py")
                  if not {"__pycache__", "node_modules", ".venv", "static", ".collimate"} & set(p.relative_to(APP).parts))


@pytest.mark.parametrize("path", _app_sources(), ids=lambda p: str(p.relative_to(APP)))
def test_the_app_never_imports_the_generator_runtime(path):
    assert runtime_imports(path.read_text(encoding="utf-8")) == []


def test_the_scan_reads_the_app_and_its_tests():
    names = {p.relative_to(APP).as_posix() for p in _app_sources()}
    assert {"main.py", "process_manager/ipc.py", "routes/runs/create.py", "tests/test_pytest_log_flush.py",
            "tests/test_pytest_sdk_import_boundary.py"} <= names


@pytest.mark.parametrize("source", [
    "from collimate._host.bridge import HostBridge as Bridge\n",
    "from collimate._results import GeneratorResult as Result\n",
    "import collimate._host.bridge as bridge\n",
    "from collimate import _results as results\n",
    "from collimate._host import bridge\n",
    "def nested():\n    from collimate._host.bridge import HostBridge\n",
    "import importlib as loader\nloader.import_module('collimate._results')\n",
    "from importlib import import_module as load\nload('collimate._host.bridge')\n",
    "__import__('collimate._results')\n",
    "import collimate as sdk\nBridge = sdk._host.bridge.HostBridge\n",
    "from collimate import _host as plumbing\nBridge = plumbing.bridge.HostBridge\n",
    "import importlib\nimportlib.import_module(name='collimate._results')\n",
])
def test_the_scan_refuses_each_way_to_the_generator_runtime(source):
    assert runtime_imports(source), source


@pytest.mark.parametrize("source", [
    "from collimate.native import HostBridge as Bridge, GeneratorResult as Result\n",
    "from collimate import native as runtime\nBridge = runtime.HostBridge\n",
    "import importlib\nimportlib.import_module('collimate.native')\n",
    "from collimate._host.ipc import OPS\nfrom collimate._host import channel_events\n",
    "import collimate._host.renderer_bundle as bundles\n",
])
def test_the_scan_allows_the_public_types_and_the_host_plumbing(source):
    assert runtime_imports(source) == []
