#!/usr/bin/env python3
"""Write the desktop packaging config for one release leg.

    python3 build/desktop_packaging.py --platform mac|linux|win --out desktop/release.electron-builder.json

The output extends ``desktop/electron-builder.yml`` with what differs between
a signed and an unsigned build, so the committed config holds no placeholder
and the release signs exactly when the owner has set the signing secrets:

- ``extraMetadata.collimate.signed``: whether this build carries a
  Developer ID or Authenticode signature (``MAC_SIGNING`` on mac,
  ``WIN_SIGNING`` on Windows, never on Linux). electron-builder writes it
  into the packaged package.json, and the shell's updater reads it: an
  unsigned macOS app cannot update itself (Squirrel.Mac refuses the swap),
  so the shell says so instead of downloading an update it cannot install.
- mac: ``notarize`` follows ``MAC_SIGNING``. Unsigned, the app is ad-hoc
  signed (``identity: "-"``, with the hardened runtime off, which an ad-hoc
  signature cannot satisfy): an app electron-builder has rewritten but not
  re-signed carries an invalid signature, and a quarantined download of one
  is "damaged" on Apple silicon, with no way to open it; an ad-hoc-signed
  one opens through Privacy & Security.
- win: a signed build gets ``azureSignOptions`` from the ``AZURE_SIGN_*``
  variables, every one required: ``publisherName`` must equal the
  certificate's subject CN, or every Windows update fails after a successful
  download (electron-updater verifies the downloaded installer's publisher
  against the name in app-update.yml; unsigned, no name is written and
  nothing is verified).

``MAC_SIGNING`` and ``WIN_SIGNING`` are ``true`` or ``false`` (release.yml
derives them from the presence of the signing secrets); unset is ``false``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PLATFORMS = ("mac", "linux", "win")
AZURE_VARIABLES = {
    "publisherName": "AZURE_SIGN_PUBLISHER_NAME",
    "endpoint": "AZURE_SIGN_ENDPOINT",
    "codeSigningAccountName": "AZURE_SIGN_ACCOUNT_NAME",
    "certificateProfileName": "AZURE_SIGN_CERTIFICATE_PROFILE",
}


class Refused(Exception):
    """The environment does not describe one packaging config."""


def flag(env: dict, name: str) -> bool:
    value = env.get(name, "false")
    if value not in ("true", "false"):
        raise Refused(f"{name} must be true or false, not {value!r}")
    return value == "true"


def packaging_config(platform: str, env: dict) -> dict:
    """The override config for ``platform`` under ``env``."""
    if platform not in PLATFORMS:
        raise Refused(f"platform must be one of {', '.join(PLATFORMS)}, not {platform!r}")
    mac_signing, win_signing = flag(env, "MAC_SIGNING"), flag(env, "WIN_SIGNING")
    signed = {"mac": mac_signing, "win": win_signing, "linux": False}[platform]
    config: dict = {"extends": "./electron-builder.yml", "extraMetadata": {"collimate": {"signed": signed}}}
    if platform == "mac":
        config["mac"] = ({"notarize": True} if mac_signing
                         else {"notarize": False, "identity": "-", "hardenedRuntime": False})
    if platform == "win" and win_signing:
        options = {}
        for key, variable in AZURE_VARIABLES.items():
            value = env.get(variable, "")
            if not value.strip():
                raise Refused(f"WIN_SIGNING is true but {variable} is not set")
            options[key] = value.strip()
        config["win"] = {"azureSignOptions": options}
    return config


def render(config: dict) -> str:
    """The config as JSON, one of electron-builder's config formats, so the
    standard library suffices on every runner."""
    return json.dumps(config, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = packaging_config(args.platform, dict(os.environ))
    except Refused as error:
        print(f"desktop_packaging: {error}", file=sys.stderr)
        return 1
    args.out.write_text(render(config))
    signed = config["extraMetadata"]["collimate"]["signed"]
    print(f"desktop_packaging: {args.platform} {'signed' if signed else 'unsigned'} -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
