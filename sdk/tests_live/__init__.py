# SPDX-License-Identifier: MIT
"""The opt-in LIVE test layer — real keys, real models, real containers.

Deliberately a sibling of ``tests/`` rather than a subpackage of it: pytest's
``testpaths = ["tests"]`` then excludes this tree structurally, so no marker
expression, no ``-p no:…``, and no forgotten ``-m 'not live'`` can ever let a
default ``pytest`` run spend money. Running it is always explicit::

    uv run pytest tests_live

See ``tests_live/README.md`` for the env-var contract that selects which
providers and which harnesses actually run.
"""
