"""Stub the native-only modules the app imports so no route can crash on a
missing import. ``fcntl``/``pty``/``termios`` are imported at module level
by routes/terminal.py; ``docker``, ``keyring`` and ``uvicorn`` are
imported lazily inside functions the demo never exercises (the 501 guard
in boot.py keeps those routes unreachable).

A stub is *permissive*: every attribute is a fresh class (so it can be
raised, caught, subclassed and instantiated), instances answer any
attribute the same way, calling one returns an instance, iterating one
yields nothing and it is falsy. Only modules that genuinely fail to import
are replaced — a module pyodide ships stays real.
"""
from __future__ import annotations

import importlib
import sys
import types

# Every module the api tree imports that pyodide may not provide. Parents
# are stubbed before children so ``import docker.errors`` binds.
STUBBED = (
    "fcntl", "pty", "termios",
    "docker", "docker.errors",
    "keyring",
    "uvicorn",
)


class _PermissiveMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _permissive_class(f"{cls.__name__}.{name}")

    def __iter__(cls):
        return iter(())

    def __bool__(cls):
        return False


class _Permissive(Exception, metaclass=_PermissiveMeta):
    """An exception class whose every attribute is another such class."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _permissive_class(f"{type(self).__name__}.{name}")

    def __call__(self, *args, **kwargs):
        return type(self)()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _permissive_class(name: str):
    return _PermissiveMeta(name, (_Permissive,), {})


class StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _permissive_class(f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


def install(names: tuple[str, ...] = STUBBED) -> list[str]:
    """Stub each name in ``names`` that cannot be imported; return the list
    of names that were stubbed (logged by the page for transparency)."""
    stubbed: list[str] = []
    for name in names:
        try:
            importlib.import_module(name)
            continue
        except Exception:  # noqa: BLE001 — ImportError or a broken native module
            pass
        module = StubModule(name)
        module.__path__ = []  # a package, so submodule stubs attach
        sys.modules[name] = module
        parent, _, child = name.rpartition(".")
        if parent and parent in sys.modules:
            setattr(sys.modules[parent], child, module)
        stubbed.append(name)
    return stubbed
