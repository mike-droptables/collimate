"""The versions the running server knows itself by: the ``collimate``
distribution's (the app, lockstep with the SDK) and the SDK's own."""
from __future__ import annotations

import functools


def app_version() -> str:
    """The installed ``collimate`` distribution's version, or a dev marker
    when running from a source checkout that was never installed."""
    try:
        from importlib.metadata import version
        return version("collimate")
    except Exception:
        return "0.0.0+dev"


@functools.cache
def sdk_version() -> str:
    """The version of the SDK this server runs, read from the SDK's own
    canonical metadata (``collimate.shared``), which travels with
    its source wherever it is installed."""
    from collimate.shared import sdk_distribution
    return sdk_distribution().version
