"""The version the running server knows itself by: the one ``collimate``
distribution's, which the SDK packages it runs carry too."""
from __future__ import annotations


def app_version() -> str:
    """The installed ``collimate`` distribution's version, or a dev marker
    when running from a source checkout that was never installed."""
    try:
        from importlib.metadata import version
        return version("collimate")
    except Exception:
        return "0.0.0+dev"
