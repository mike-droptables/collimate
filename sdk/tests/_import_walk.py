# SPDX-License-Identifier: MIT
"""Import every SDK module and every first-party generator module, then report.

Run by ``tests/test_import_boundary.py`` with ``python -I -B`` in a venv that holds
only the one ``collimate`` wheel and its declared dependencies: no checkout
source on ``sys.path``. Standard library only. Arguments: a JSON object
``{"api_names": [...], "generators": <content/generators>, "files": [...]}``,
where ``files`` are the generator modules to load by path (entrypoints,
capture scripts, the built-in example), and the helper packages
(``_agent_shared``, ``_env_shared``, ``_web_shared``) are imported by name from
``generators``.

A module the server defines (``api_names``) is refused and recorded, by a
finder ahead of every other, since the wheel installs it beside the SDK. A
third-party package a generator declares in its PEP 723 header is not in the
venv; it is stubbed so the walk reaches the rest of the module, and its name
is reported. ``collimate`` and ``gen`` are never stubbed: every SDK import
resolves from the wheel or the walk fails.
"""
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import pkgutil
import sys
import sysconfig
import traceback
import types
from pathlib import Path

sys.dont_write_bytecode = True  # never a __pycache__ in the trees it reads
ARGS = json.loads(sys.argv[1])
API_NAMES = frozenset(ARGS["api_names"])
REPORT = {"api_imports": [], "stubbed": [], "failures": [], "collimate": [], "content": [],
          "api_found": [], "collimate_origin": ""}


class _Stub(types.ModuleType):
    """A missing third-party module: any attribute is a further stub, a class
    that can be subclassed, called, used as a decorator, caught or passed
    as a warning category."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = type(name, (Warning,), {"__getattr__": lambda self, attr: _Stub(attr),
                                "__call__": lambda self, *a, **k: self,
                                "__init__": lambda self, *a, **k: None})
        setattr(self, name, value)
        return value


STDLIB = Path(sysconfig.get_paths()["stdlib"]).resolve()


def _imported_by_the_stdlib() -> bool:
    """Whether the import being resolved was made by a standard-library
    module: the first frame outside the import machinery is under the
    stdlib path. Its optional imports of another platform's module (``_wmi``
    in ``platform`` on Windows) are its own to fail; ``sys.stdlib_module_names``
    lists such a name only from the patch release that added it."""
    frame = sys._getframe(1)
    while frame is not None:
        file = frame.f_code.co_filename
        if file != __file__ and "importlib" not in file and not file.startswith("<"):
            return Path(file).resolve().is_relative_to(STDLIB)
        frame = frame.f_back
    return False


class _App(importlib.abc.MetaPathFinder):
    """First on ``sys.meta_path``: the server is installed, and refused."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in API_NAMES:
            REPORT["api_imports"].append(fullname)
            raise ModuleNotFoundError(f"{fullname} is the server's, not the SDK's", name=fullname)
        return None


# Installed as the server's dependencies, yet no part of the SDK's own
# requirements (collimate.shared.SDK_REQUIREMENTS): a generator that imports one
# declares it in its header and runs in an environment of its own, and the SDK
# packages must import without it. Stubbed all the same, so the walk proves both.
SERVER_ONLY = frozenset({"pydantic", "pydantic_core"})


class _Boundary(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Sees imports nothing else resolved and the server-only packages, for
    which it stands ahead of the real ones."""

    def __init__(self, early=False):
        self.early = early

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if self.early and top not in SERVER_ONLY:
            return None
        if top in ("collimate", "gen", "_agent_shared", "_env_shared", "_web_shared"):
            return None
        if top in sys.stdlib_module_names or _imported_by_the_stdlib():
            return None  # another platform's module: let it fail
        if top not in REPORT["stubbed"]:
            REPORT["stubbed"].append(top)
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        module = _Stub(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        return None


def _try(kind, name, load):
    try:
        load()
        REPORT[kind].append(name)
    except BaseException:  # noqa: BLE001 - every failure is the report's
        REPORT["failures"].append({"module": name, "error": traceback.format_exc(limit=4)})


def main():
    REPORT["api_found"] = sorted(n for n in API_NAMES if importlib.util.find_spec(n) is not None)
    sys.meta_path.insert(0, _App())
    sys.meta_path.insert(1, _Boundary(early=True))  # after _App, ahead of the real server-only packages
    sys.meta_path.append(_Boundary())
    import collimate
    REPORT["collimate_origin"] = str(Path(collimate.__file__).resolve())
    for info in pkgutil.walk_packages(collimate.__path__, "collimate."):
        if info.name.rsplit(".", 1)[-1] == "__main__":
            continue
        _try("collimate", info.name, lambda n=info.name: importlib.import_module(n))
    for name in ("gen", "gen.session"):
        _try("collimate", name, lambda n=name: importlib.import_module(n))

    generators = Path(ARGS["generators"])
    sys.path.insert(0, str(generators))
    for helper in ("_agent_shared", "_env_shared", "_web_shared"):
        for path in sorted((generators / helper).rglob("*.py")):
            parts = list(path.relative_to(generators).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            name = ".".join(parts)
            _try("content", name, lambda n=name: importlib.import_module(n))
    for number, raw in enumerate(ARGS["files"]):
        path = Path(raw)

        def load(path=path, number=number):
            sys.path.insert(0, str(path.parent))
            try:
                spec = importlib.util.spec_from_file_location(f"_walked_{number}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            finally:
                sys.path.remove(str(path.parent))
        _try("content", str(path), load)
    print(json.dumps(REPORT))


main()
