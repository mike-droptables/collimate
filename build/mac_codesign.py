"""Sign a macOS server environment's Mach-O files, as electron-builder's
signer would sign them, before build/server_env.py hashes the tree.

A desktop package's ``Contents/Resources/server`` is sealed by the app's
signature, and every Mach-O file in it must itself be signed (notarization
refuses an unsigned or ad-hoc one in a Developer ID app; Apple silicon runs
no unsigned code). If electron-builder signed them, it would rewrite them
after the manifest named the tree's hash, so the package's server would
hash to something else than its manifest and its stamp say (the first mac
release legs, run 42157215198). So build/server_env.py signs them here, with
the identity the app will be signed with, and hashes the tree after; the
packaging config (build/desktop_packaging.py) then has electron-builder skip
the whole server tree (``signIgnore``: osx-sign skips an ignored path, it
does not sign it), and the app's own signature, which writes only the main
executable and ``_CodeSignature/``, seals those files as they are.

The command is electron-builder's for a file inside the app
(app-builder-lib's MacTargetHelper, @electron/osx-sign's signApplication):
``codesign --sign <identity> --force [--keychain K] --timestamp [--options
runtime] --entitlements E <file>``, deepest first. Two signers:

- ad-hoc (identity ``-``), for every build that is not a signed release:
  the hardened runtime off, as the unsigned config has it (an ad-hoc
  signature cannot satisfy it), and no timestamp (an ad-hoc signature has
  nothing to timestamp; ``--timestamp=none`` says so), so the same tree
  signs to the same bytes.
- Developer ID, for a ``--release`` build with ``MAC_SIGNING=true``: the
  first "Developer ID Application" identity in a temporary keychain made
  from ``CSC_LINK`` / ``CSC_KEY_PASSWORD``, as electron-builder makes its
  own from them (base64, a ``data:`` URI or a file path); without
  ``CSC_LINK``, the first such identity on the keychain search list
  (narrowed by ``CSC_NAME`` when set), as electron-builder's auto-discovery
  finds it. The hardened runtime on and a secure timestamp, which
  notarization requires. The identity's SHA-1 goes into the manifest, and
  the packaging config names it as electron-builder's ``mac.identity``, so
  the app is signed with exactly that certificate or not at all.

The entitlements (``server_env.entitlements.plist`` beside this file) are
electron-builder's default for every signed file but the app's own
(app-builder-lib's templates/entitlements.mac.plist): allow-jit,
allow-unsigned-executable-memory and disable-library-validation, the last
of which a generator run needs (its interpreter is this one, and it loads
the extension modules uv installed for the run, signed by nobody or by
their wheel's builder).
"""
from __future__ import annotations

import base64
import contextlib
import re
import secrets
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADHOC = "-"
ENTITLEMENTS = Path(__file__).resolve().with_name("server_env.entitlements.plist")
DEVELOPER_ID = "Developer ID Application:"
# The first four bytes of a Mach-O file, thin or universal, either byte order.
MACH_O = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
# A code bundle, which codesign signs as a bundle and the walk would have to
# seal inside out; the environment has none, and one appearing is refused
# rather than signed file by file.
BUNDLES = (".app", ".framework", ".bundle", ".plugin", ".xpc")
# `security find-identity -v -p codesigning`: `  1) <SHA-1> "<name>"`.
IDENTITY_LINE = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(.+)"\s*$')


class Refused(Exception):
    """The environment, or the inputs to sign it, cannot be signed as asked."""


@dataclass(frozen=True)
class Signer:
    """Who signs: ``identity`` is ``-`` (ad-hoc) or a certificate's SHA-1,
    found in ``keychain`` when one is named, else on the search list."""

    identity: str = ADHOC
    name: str | None = None
    keychain: Path | None = None

    @property
    def developer_id(self) -> bool:
        return self.identity != ADHOC

    def manifest(self) -> dict:
        """The manifest's ``codesign`` entry: who signed the environment's
        Mach-O files, and how."""
        return {"identity": self.identity, "name": self.name, "hardened_runtime": self.developer_id}


def flag(env: dict, name: str) -> bool:
    value = env.get(name, "false")
    if value not in ("true", "false"):
        raise Refused(f"{name} must be true or false, not {value!r}")
    return value == "true"


def release_signs(env: dict, release: bool) -> bool:
    """Whether a build signs its mac app with a Developer ID: only a release,
    and then exactly when ``MAC_SIGNING`` is ``true`` (release.yml derives it
    from the presence of ``CSC_LINK``)."""
    return release and flag(env, "MAC_SIGNING")


def mach_o_files(tree: Path) -> list[str]:
    """The regular files under ``tree`` that are Mach-O, relative and sorted."""
    found = []
    for path in sorted(tree.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in MACH_O:
                    found.append(path.relative_to(tree).as_posix())
    return found


def signing_order(files: list[str]) -> list[str]:
    """``files`` deepest first, as osx-sign orders its walk (codesign wants
    nested code signed before what contains it); ties by path."""
    return sorted(files, key=lambda rel: (-rel.count("/"), rel))


def codesign_command(signer: Signer, path: Path) -> list[str]:
    """electron-builder's codesign command for a file inside the app."""
    command = ["codesign", "--sign", signer.identity, "--force"]
    if signer.keychain is not None:
        command += ["--keychain", str(signer.keychain)]
    if signer.developer_id:
        command += ["--timestamp", "--options", "runtime"]
    else:
        command += ["--timestamp=none"]
    return command + ["--entitlements", str(ENTITLEMENTS), str(path)]


def call(run, command: list[str]) -> subprocess.CompletedProcess:
    """``command`` run to completion; Refused, with what it said, when it fails."""
    try:
        return run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        said = (error.stderr or error.stdout or "").strip()
        # The keychain's and the certificate's passwords never reach a log.
        shown = ["***" if command[0] == "security" and before in ("-p", "-P", "-k") else part
                 for before, part in zip(["", *command], command)]
        raise Refused(f"{' '.join(shown)} failed ({error.returncode}): {said}") from error


def sign_tree(tree: Path, signer: Signer, run=subprocess.run) -> list[str]:
    """Sign every Mach-O file under ``tree`` with ``signer``, then verify each
    signature (``codesign --verify --strict``); the files signed, relative.
    Refused when the tree holds a code bundle."""
    for path in tree.rglob("*"):
        if path.is_dir() and not path.is_symlink() and path.suffix in BUNDLES:
            raise Refused(f"{path} is a code bundle; the server environment is signed file by file")
    files = mach_o_files(tree)
    if not files:
        raise Refused(f"{tree} holds no Mach-O file to sign")
    for rel in signing_order(files):
        call(run, codesign_command(signer, tree / rel))
    for rel in files:
        call(run, ["codesign", "--verify", "--strict", str(tree / rel)])
    return files


def certificate(csc_link: str, scratch: Path) -> Path:
    """``CSC_LINK`` as a .p12 file, read as electron-builder reads it: a
    ``data:…;base64,`` URI, base64 (longer than 2048 characters or ending in
    ``=``), else a path (``file://``, ``~/``, absolute, or relative to
    desktop/). A URL is refused: this builder fetches no certificate."""
    link = csc_link.strip()
    if link.startswith(("https://", "http://")):
        raise Refused("CSC_LINK is a URL; give the certificate as base64 or a file path")
    data_uri = re.match(r"^data:.*;base64,", link)
    if data_uri or len(link) > 2048 or link.endswith("="):
        p12 = scratch / "certificate.p12"
        p12.write_bytes(base64.b64decode(link[data_uri.end() if data_uri else 0:]))
        return p12
    if link.startswith("file://"):
        link = link[len("file://"):]
    path = Path(link).expanduser() if link.startswith("~/") else Path(link)
    path = path if path.is_absolute() else ROOT / "desktop" / path
    if not path.is_file():
        raise Refused(f"CSC_LINK names {path}, which is not a file")
    return path


def find_identity(listing: str, name: str | None) -> tuple[str, str]:
    """The first Developer ID Application identity in ``security
    find-identity -v -p codesigning`` output, narrowed to one whose line
    contains ``name`` when given: (SHA-1, name)."""
    for line in listing.splitlines():
        found = IDENTITY_LINE.match(line)
        if found and found.group(2).startswith(DEVELOPER_ID) and (not name or name in line):
            return found.group(1), found.group(2)
    raise Refused("no valid Developer ID Application identity" + (f" matching {name!r}" if name else "")
                  + " to sign the server environment with")


def search_list(run=subprocess.run) -> list[str]:
    said = call(run, ["security", "list-keychains", "-d", "user"]).stdout
    return [line.strip().strip('"') for line in said.splitlines() if line.strip()]


@contextlib.contextmanager
def developer_id(env: dict, run=subprocess.run) -> Iterator[Signer]:
    """The release's Developer ID signer for the duration of the block. With
    ``CSC_LINK``, its certificate imported into a temporary keychain (as
    electron-builder's createKeychain does: a random password, unlocked,
    codesign allowed, put on the user's search list so codesign can resolve
    the chain), which is removed after; without, the search list's."""
    name = env.get("CSC_NAME") or None
    if not env.get("CSC_LINK"):
        listing = call(run, ["security", "find-identity", "-v", "-p", "codesigning"]).stdout
        identity, found = find_identity(listing, name)
        yield Signer(identity, found)
        return
    with tempfile.TemporaryDirectory(prefix="server-env-keychain-") as scratch:
        keychain = Path(scratch) / "server-env.keychain"
        password = secrets.token_urlsafe(32)
        p12 = certificate(env["CSC_LINK"], Path(scratch))
        original = search_list(run)

        def security(*args: str):
            return call(run, ["security", *args])

        security("create-keychain", "-p", password, str(keychain))
        try:
            security("unlock-keychain", "-p", password, str(keychain))
            security("set-keychain-settings", str(keychain))
            security("list-keychains", "-d", "user", "-s", str(keychain), *original)
            security("import", str(p12), "-k", str(keychain), "-T", "/usr/bin/codesign",
                     "-P", env.get("CSC_KEY_PASSWORD", ""))
            security("set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", password, str(keychain))
            listing = security("find-identity", "-v", "-p", "codesigning", str(keychain)).stdout
            identity, found = find_identity(listing, name)
            yield Signer(identity, found, keychain)
        finally:
            security("list-keychains", "-d", "user", "-s", *original)
            run(["security", "delete-keychain", str(keychain)], check=False, capture_output=True)


@contextlib.contextmanager
def signer_for(env: dict, release: bool) -> Iterator[Signer]:
    """The signer of a build's server environment: a Developer ID for a
    signed release (:func:`release_signs`), else ad-hoc."""
    if release_signs(env, release):
        with developer_id(env) as signer:
            yield signer
    else:
        yield Signer()

