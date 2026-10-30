"""The developer reset tool, ``build/reset_defaults.py``, imported by its
path: it lives outside api/ and imports the app's modules from
``PYTHONPATH=api``, as the test run has them."""
from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "build" / "reset_defaults.py"


def load():
    """A fresh import of the tool's module."""
    spec = importlib.util.spec_from_file_location("reset_defaults", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
