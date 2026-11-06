"""Server-lifetime record of the client driving this server.

One server is driven by one client kind at a time — a browser tab or the
Electron desktop shell — and generators need to know which, because the
shell can host native guest views, capture them, and open external URLs
while a browser tab cannot.

Two writers, in precedence order:

* ``COLLIMATE_CLIENT`` in the environment, read at import. The desktop
  shell spawns the server with ``COLLIMATE_CLIENT=electron``, which
  records that an Electron client is *expected*; the shell's real
  capability set stays unknown (all False) until it handshakes.
* ``POST /api/system/client`` — the handshake. Last writer wins, a
  browser tab posting over an env-seeded electron record included: a
  browser pointed at an Electron-spawned server really is a browser and
  must not be told it has a native guest view.

Capability names are camelCase on the wire (they come straight off the
shell's contextBridge) and snake_case here, because the generator-facing
surface (``gen.client_caps()``) is Python.

Deliberately lock-free: the record is replaced by rebinding a freshly
built dict, so a concurrent reader sees either the whole old record or
the whole new one, never a half-written mix.
"""

from __future__ import annotations

import os
import re

# The capabilities the desktop shell's contextBridge declares. Every
# record carries all of them, so a generator can read a key without
# guarding — absent means False, not unknown.
CAPABILITIES: tuple[str, ...] = (
    "native_guests",
    "guest_capture",
    "persistent_partitions",
    "direct_loopback",
    "external_open",
    "custom_title_bar",
    "custom_window_controls",
)

# Client kinds we record verbatim. Anything else a caller sends is
# rejected at the endpoint; the registry itself never invents a kind.
CLIENT_KINDS: tuple[str, ...] = ("electron", "browser")

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def normalize_capabilities(caps: dict | None) -> dict[str, bool]:
    """Wire capability map → the snake_case, all-bool server form.

    Unknown names are kept (snake-cased) rather than dropped: in connect
    mode the shell and the server are separate builds that only agree on
    the bridge number, so a shell can advertise a capability this copy of
    ``CAPABILITIES`` doesn't list, and dropping it would lose something
    the shell really offers. The known names are always present even when
    the caller omits them.
    """
    out = {name: False for name in CAPABILITIES}
    for key, value in (caps or {}).items():
        out[_snake(str(key))] = bool(value)
    return out


def _record_for(
    client: str,
    *,
    capabilities: dict | None = None,
    shell_version: str | None = None,
    bridge: int | None = None,
    source: str,
) -> dict:
    return {
        "client": client,
        "shell_version": shell_version,
        "bridge": bridge,
        "capabilities": normalize_capabilities(capabilities),
        # Which writer produced this record. "env" means the shell spawned
        # us but has not handshaken yet, so the capabilities are placeholders.
        "source": source,
    }


def seed_from_env() -> dict:
    """(Re)seed the record from ``COLLIMATE_CLIENT``. Called at import;
    exposed so tests can re-run it against a patched environment."""
    global _record
    kind = (os.environ.get("COLLIMATE_CLIENT") or "").strip().lower()
    client = kind if kind in CLIENT_KINDS else "browser"
    _record = _record_for(client, source="env")
    return _record


def set_client(
    client: str,
    *,
    capabilities: dict | None = None,
    shell_version: str | None = None,
    bridge: int | None = None,
) -> dict:
    """Record the handshaking client, replacing whatever was there."""
    global _record
    _record = _record_for(
        client,
        capabilities=capabilities,
        shell_version=shell_version,
        bridge=bridge,
        source="handshake",
    )
    return _record


def client_caps() -> dict:
    """The flat generator-facing view behind ``gen.client_caps()``:
    ``{"client": ..., "<capability>": bool, ...}``. Flat because a
    generator's only question is "can I do X here", and a nested map
    would make every check two lookups deep."""
    # "client" is reserved and set last, so an unknown capability a
    # handshake names "client" cannot shadow the client kind.
    return {**_record["capabilities"], "client": _record["client"]}


_record: dict = seed_from_env()
