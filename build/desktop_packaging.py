#!/usr/bin/env python3
"""Write the packaging config of a desktop build, and name its targets.

    python3 build/desktop_packaging.py config --platform mac|linux|win --commit SHA --dirty|--clean
        --built-at T [--server DIR] [--release] --out F
    python3 build/desktop_packaging.py targets [--dir]

build/build_desktop.sh runs both, for a package made on this machine and for
each release leg alike (release.yml's desktop legs run that script with
``--release``): every package is the same shape. ``config`` extends
``desktop/electron-builder.yml`` with:

- ``extraMetadata.version``: the one version, the root pyproject.toml's.
  desktop/package.json carries none, so the app's version (its About, the
  update feed, the artifact names) is the ``collimate`` version by
  construction, never a second number kept in step by a check.
- ``extraMetadata.collimate.build``: the stamp every package carries, which
  electron-builder writes into the packaged package.json and the shell reads
  (desktop/src/main/buildStamp.ts): the commit, whether the tree had
  uncommitted changes, the build time, and ``server``, the hash of the server
  environment the package carries (``null`` on Windows, which is
  connect-only).
- ``extraResources``: ``--server DIR`` (build/server_env.py's tree, required
  on mac and linux, refused on win) as the package's ``resources/server/``.
  The tree is re-hashed against its manifest here, so the stamp names the
  bytes that ship, and its target and collimate version must be this
  platform's and the app's.
- ``extraMetadata.collimate.signed``: whether this build carries a Developer
  ID or Authenticode signature. Only a ``--release`` build signs, and then
  exactly when ``MAC_SIGNING`` (mac) or ``WIN_SIGNING`` (win) is ``true``
  (release.yml derives them from the presence of the signing secrets; unset
  is ``false``). The shell's updater reads it: an unsigned macOS app cannot
  update itself (Squirrel.Mac refuses the swap).
- mac: a signed build notarizes; an unsigned one is ad-hoc signed
  (``identity: "-"``, the hardened runtime off, which an ad-hoc signature
  cannot satisfy), since an app electron-builder rewrote but did not re-sign
  is "damaged" once quarantined. The server environment's Mach-O files are
  already signed, by build/server_env.py before it hashed the tree
  (build/mac_codesign.py), so ``signIgnore`` keeps electron-builder's signer
  out of all of ``Contents/Resources/server``: re-signing them would change
  the bytes the stamp's hash names. The app's own signature (codesign of
  the bundle, which writes only its main executable and
  ``_CodeSignature/``) still seals them as they are, and the signer's own
  ``codesign --verify --deep --strict`` of the app runs over them. The
  environment must
  have been signed as this build signs: with a Developer ID for a signed
  release, whose SHA-1 the config then names as ``mac.identity`` (so
  electron-builder signs the app with exactly that certificate or fails),
  ad-hoc otherwise.
- win: a signed build gets ``azureSignOptions`` from the ``AZURE_SIGN_*``
  variables, every one required: ``publisherName`` must equal the
  certificate's subject CN, or every Windows update fails after a successful
  download.
- ``publish``: a ``--release`` build keeps the yml's GitHub feed (so
  app-update.yml and latest*.yml are generated); any other build sets it to
  null, so it has no update feed and the updater has nothing to check.

``targets`` prints the electron-builder target arguments for this platform
and CPU only, e.g. ``--mac dmg:arm64 zip:arm64``: the server environment and
the wheel's native binding are the host's, so another arch's artifact would
carry a server that cannot run. That narrowing cannot live in the config
(electron-builder merges an extending config's arrays into the parent's), so
it is passed on the command line, where it replaces the config's targets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from platform import machine

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mac_codesign  # noqa: E402
import server_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("mac", "linux", "win")
AZURE_VARIABLES = {
    "publisherName": "AZURE_SIGN_PUBLISHER_NAME",
    "endpoint": "AZURE_SIGN_ENDPOINT",
    "codeSigningAccountName": "AZURE_SIGN_ACCOUNT_NAME",
    "certificateProfileName": "AZURE_SIGN_CERTIFICATE_PROFILE",
}
# sys.platform as electron-builder's platform flag, with the target names
# desktop/electron-builder.yml lists for it (a test pins the mirror; the yml
# is not parsed here so the standard library suffices).
TARGETS = {
    "darwin": ("--mac", ("dmg", "zip")),
    "linux": ("--linux", ("AppImage", "deb")),
    "win32": ("--win", ("nsis",)),
}
# platform.machine() spellings, lowercased, as electron-builder's arch names.
ARCHES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
# electron-builder's platform name, as server_env's target system.
SYSTEMS = {"mac": "darwin", "linux": "linux"}
UNSIGNED_MAC = {"notarize": False, "identity": mac_codesign.ADHOC, "hardenedRuntime": False}
COMMIT = re.compile(r"[0-9a-f]{40}")
BUILT_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
SERVER_IN_BUNDLE = "/Contents/Resources/server/"
# The signer's ignore pattern (a JavaScript RegExp, tested against each
# absolute path): everything under the bundle's server environment.
SIGN_IGNORE = re.escape(SERVER_IN_BUNDLE)


class Refused(Exception):
    """The inputs do not describe one packaging config."""


def flag(env: dict, name: str) -> bool:
    try:
        return mac_codesign.flag(env, name)
    except mac_codesign.Refused as error:
        raise Refused(str(error)) from error


def app_version() -> str:
    """The one version, the root pyproject.toml's: desktop/package.json carries
    none, ``extraMetadata.version`` stamps this one into the package."""
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def server_resource(platform: str, server: Path) -> tuple[dict, dict]:
    """The environment's manifest and its extraResources entry, refused
    unless it is a whole tree for ``platform`` carrying the app's version."""
    server = server.resolve()
    try:
        manifest = server_env.read_manifest(server)
    except server_env.Refused as error:
        raise Refused(str(error)) from error
    target = manifest.get("target", "")
    if not target.startswith(SYSTEMS[platform] + "-"):
        raise Refused(f"{server} is a {target} environment, not a {platform} one")
    version = manifest.get("collimate", {}).get("version")
    if version != app_version():
        raise Refused(f"{server} carries collimate {version}, not the app's {app_version()}")
    return manifest, {"from": str(server), "to": "server"}


def mac_identity(server: Path, manifest: dict, signed: bool) -> str:
    """The identity ``server``'s Mach-O files were signed with, refused
    unless it is how this build signs the app: a Developer ID for a signed
    release, ad-hoc otherwise."""
    codesign = manifest.get("codesign")
    codesign = codesign if isinstance(codesign, dict) else {}
    identity = codesign.get("identity")
    if not isinstance(identity, str) or not identity:
        raise Refused(f"{server} names no codesign identity; build/server_env.py signs a macOS environment")
    if signed and identity == mac_codesign.ADHOC:
        raise Refused(f"a signed release cannot carry {server}, signed ad-hoc: build it with "
                      "build/server_env.py --release and MAC_SIGNING=true")
    if not signed and identity != mac_codesign.ADHOC:
        raise Refused(f"an ad-hoc build cannot carry {server}, signed with {codesign.get('name') or identity}")
    return identity


def packaging_config(platform: str, env: dict, *, commit: str, dirty: bool, built_at: str,
                     server: Path | None, release: bool) -> dict:
    """The config of one build for ``platform`` under ``env``."""
    if platform not in PLATFORMS:
        raise Refused(f"platform must be one of {', '.join(PLATFORMS)}, not {platform!r}")
    if not COMMIT.fullmatch(commit):
        raise Refused(f"commit must be a full 40-hex git sha, not {commit!r}")
    if not BUILT_AT.fullmatch(built_at):
        raise Refused(f"built-at must be ISO-8601 UTC seconds (YYYY-MM-DDTHH:MM:SSZ), not {built_at!r}")
    if platform == "win" and server is not None:
        raise Refused("a Windows build carries no server environment (it is connect-only)")
    if platform != "win" and server is None:
        raise Refused(f"a {platform} build carries its server environment: name it with --server")
    mac_signing, win_signing = release and flag(env, "MAC_SIGNING"), release and flag(env, "WIN_SIGNING")
    signed = {"mac": mac_signing, "win": win_signing, "linux": False}[platform]
    config: dict = {"extends": "./electron-builder.yml"}
    server_hash = None
    manifest: dict = {}
    if server is not None:
        manifest, resource = server_resource(platform, server)
        server_hash = manifest["hash"]
        config["extraResources"] = [resource]
    config["extraMetadata"] = {"version": app_version(), "collimate": {"signed": signed, "build": {
        "commit": commit, "dirty": dirty, "builtAt": built_at, "server": server_hash}}}
    if platform == "mac":
        assert server is not None
        identity = mac_identity(server, manifest, mac_signing)
        config["mac"] = {"notarize": True, "identity": identity} if mac_signing else dict(UNSIGNED_MAC)
        config["mac"]["signIgnore"] = [SIGN_IGNORE]
    if platform == "win" and win_signing:
        options = {}
        for key, variable in AZURE_VARIABLES.items():
            value = env.get(variable, "")
            if not value.strip():
                raise Refused(f"WIN_SIGNING is true but {variable} is not set")
            options[key] = value.strip()
        config["win"] = {"azureSignOptions": options}
    if not release:
        config["publish"] = None
    return config


def host_platform(sys_platform: str) -> str:
    """``sys.platform``, refused unless a desktop package can be built there."""
    if sys_platform not in TARGETS:
        raise Refused(f"no desktop package for platform {sys_platform!r}")
    return sys_platform


def host_arch(machine_name: str) -> str:
    """``platform.machine()`` as electron-builder's arch name."""
    arch = ARCHES.get(machine_name.lower())
    if arch is None:
        raise Refused(f"no desktop package for machine {machine_name!r}; known: {', '.join(sorted(ARCHES))}")
    return arch


def host_targets(platform: str, arch: str, unpacked: bool) -> list[str]:
    """electron-builder's target arguments for a package built on a
    ``platform`` / ``arch`` host: the yml's targets for that platform, or the
    unpacked app alone, each for the host arch only."""
    if platform not in TARGETS:
        raise Refused(f"platform must be one of {', '.join(TARGETS)}, not {platform!r}")
    if arch not in set(ARCHES.values()):
        raise Refused(f"arch must be x64 or arm64, not {arch!r}")
    option, targets = TARGETS[platform]
    return [option, *(f"{target}:{arch}" for target in (("dir",) if unpacked else targets))]


def render(config: dict) -> str:
    """The config as JSON, one of electron-builder's config formats, so the
    standard library suffices on every runner."""
    return json.dumps(config, indent=2) + "\n"


def config_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="desktop_packaging.py config",
                                     description="Write a desktop build's packaging config.")
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--commit", required=True)
    tree = parser.add_mutually_exclusive_group(required=True)
    tree.add_argument("--dirty", dest="dirty", action="store_true", help="the tree had uncommitted changes")
    tree.add_argument("--clean", dest="dirty", action="store_false", help="it had none")
    parser.add_argument("--built-at", required=True)
    parser.add_argument("--server", type=Path, help="the server environment to carry (mac, linux)")
    parser.add_argument("--release", action="store_true", help="a published build: the update feed, and signing")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = packaging_config(args.platform, dict(os.environ), commit=args.commit, dirty=args.dirty,
                                  built_at=args.built_at, server=args.server, release=args.release)
    except Refused as error:
        print(f"desktop_packaging: {error}", file=sys.stderr)
        return 1
    args.out.write_text(render(config))
    collimate = config["extraMetadata"]["collimate"]
    build = collimate["build"]
    print(f"desktop_packaging: {args.platform} {'release' if args.release else 'build'} {build['commit'][:10]}"
          f"{' (uncommitted changes)' if build['dirty'] else ''}, {'signed' if collimate['signed'] else 'unsigned'}, "
          f"server {(build['server'] or 'none')[:16]} -> {args.out}")
    return 0


def targets_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="desktop_packaging.py targets",
                                     description="Print this host's electron-builder target arguments.")
    parser.add_argument("--dir", dest="unpacked", action="store_true", help="the unpacked app only")
    args = parser.parse_args(argv)
    try:
        arguments = host_targets(host_platform(sys.platform), host_arch(machine()), args.unpacked)
    except Refused as error:
        print(f"desktop_packaging: {error}", file=sys.stderr)
        return 1
    print(" ".join(arguments))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    commands = {"config": config_main, "targets": targets_main}
    if not argv or argv[0] not in commands:
        print(f"usage: desktop_packaging.py {{{','.join(commands)}}} ...", file=sys.stderr)
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
