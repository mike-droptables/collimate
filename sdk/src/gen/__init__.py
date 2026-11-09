# SPDX-License-Identifier: MIT
"""Top-level ``gen`` package — public runtime API for Collimate generators.

A generator imports this and uses ``gen.*`` to read inputs, stage
output files, update its cell, request user input, resolve API keys, and
reach OS-level helpers (ports, terminals, …)::

    import gen

    async def run():
        gen.update_file("output.txt", gen.prompt)
        await gen.commit()

    if __name__ == "__main__":
        gen.main(run)

This package re-exports exactly ``collimate.native.__all__`` (the
implementation; the names resolve to the same objects) plus the per-run
dynamic attributes ``gen.prompt``, ``gen.cancelled`` and
``gen.selected_file``. Nothing else in
:mod:`collimate.native` is reachable through ``gen``, except the Agent
generator's session machinery under :mod:`gen.session`, which is not the
authoring contract.
"""
from collimate.native import *  # noqa: F401,F403
from collimate.native import DYNAMIC_ATTRS as _DYNAMIC_ATTRS
from collimate.native import __all__  # noqa: F401
from gen import session  # noqa: F401,E402


def __getattr__(name):
    if name not in _DYNAMIC_ATTRS:
        raise AttributeError(f"module 'gen' has no attribute {name!r}")
    from collimate import native
    return getattr(native, name)
