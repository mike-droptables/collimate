# SPDX-License-Identifier: MIT
"""Known command-gate bypass vectors, one module per harness, for the
conformance suite's ``test_a_known_bypass_vector_runs_nothing_below_allow``.

A :class:`Vector` is a scripted-model attack: an optional ``setup`` that
seeds the cell before the turn (``setup(gen, target)``), and the mock
``action(target)`` — usually a ``("seq", [...])`` that writes a file with one
tool and triggers it with another — whose command would create ``target``
(the marker, as the harness sees the workspace). Under deny and a rejected
ask the marker must never appear."""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Vector:
    name: str
    action: Callable[[str], tuple]
    prompt: str = "Do the task."
    setup: Optional[Callable] = None


def _load() -> dict:
    vectors: dict = {}
    for module in pkgutil.iter_modules(__path__):
        found = importlib.import_module(f"{__name__}.{module.name}")
        vectors[found.HARNESS] = list(found.VECTORS)
    return vectors


VECTORS: dict = _load()
