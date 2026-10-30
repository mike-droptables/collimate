"""Secret-only storage in the OS keychain. Account metadata is owned by
``accounts``.

Callers use opaque ``collimate_``-prefixed keyring slots. There is no alias
lookup, environment mirror, plaintext fallback, or index.
"""

import re

_SERVICE = "collimate"

# Cached result of the keychain probe in available(); None until the first
# probe. Kept apart from the function so the cache check can never return
# the function object itself.
_keychain_ok: bool | None = None


def _require_slot(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"collimate_[A-Za-z0-9_-]+", name):
        raise ValueError("secret slots must have a collimate_ prefix")


def available() -> bool:
    """Can we persist secrets? True when the OS keychain is reachable.

    Probed once per process: a usable ``keyring`` backend (not the
    ``keyrings.alt`` plaintext fallbacks, not a zero-priority stub) that
    answers a read without raising. After a failed probe, unlock the keyring
    and restart the app; requests never trigger another potentially blocking
    unlock prompt.
    """
    global _keychain_ok
    if _keychain_ok is not None:
        return _keychain_ok
    try:
        import keyring
        try:
            if hasattr(keyring, "get_keyring"):
                backend = keyring.get_keyring()
                if backend.priority <= 0 or backend.__class__.__module__.startswith("keyrings.alt"):
                    _keychain_ok = False
                    return False
            keyring.get_password(_SERVICE, "collimate_probe")
            _keychain_ok = True
        except Exception:
            _keychain_ok = False
    except ImportError:
        _keychain_ok = False
    return _keychain_ok


def set_key(name: str, value: str) -> bool:
    """Persist a secret. Returns True on success."""
    _require_slot(name)
    if not available():
        return False
    import keyring
    try:
        keyring.set_password(_SERVICE, name, value)
    except Exception:
        return False
    return True


def get_key(name: str) -> str | None:
    """Read a stored secret."""
    _require_slot(name)
    if not available():
        return None
    import keyring
    try:
        return keyring.get_password(_SERVICE, name)
    except Exception:
        return None


def delete_key(name: str) -> bool:
    """Remove a stored secret. Returns True if it existed."""
    _require_slot(name)
    ok = False
    if available():
        import keyring
        try:
            keyring.delete_password(_SERVICE, name)
            ok = True
        except Exception:
            pass
    return ok
