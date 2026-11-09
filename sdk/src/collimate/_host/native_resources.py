"""The SDK's pinned native resources, and the macOS signing records that admit
their signed bytes.

Two kinds of native file are pinned by sha256 and checked before the SDK runs
or loads them:

* the renderer compilers, ``collimate/renderer_compilers/<target>/esbuild``,
  each pinned in that folder's ``manifest.json`` (the sha256 of the binary
  taken from the npm archive the lock pins by integrity,
  ``sdk/scripts/prepare_renderer_compilers.py``), checked by
  :func:`collimate._renderer_compilers.compiler_path` before esbuild is
  spawned;
* the verifier payloads, ``collimate/native_verifier/<target>/``, whose
  manifest pins ``_quickjs.abi3.so`` and its licenses (the extension also by
  the upstream wheel's ``extension_sha256`` in the contract, where one is
  named), checked by :func:`.verifier_engine.verify_payload` before the
  extension is imported.

A macOS desktop package signs every Mach-O file it carries (notarization
refuses an unsigned or ad-hoc one inside a Developer ID app, and Apple silicon
runs no unsigned code), and ``codesign`` rewrites the file it signs, so a
signed esbuild or extension can no longer hash to its pin. The pin stays what
it is, the upstream bytes; the server environment's build
(``build/server_env.py``) checks every resource against it before signing
(:func:`pinned`, strictly), signs, and writes beside each file the signing
changed a record, ``codesign.json``::

    {"schema": 1, "files": {"esbuild": {"pinned": "<the pin>", "signed": "<sha256 of the signed file>"}}}

:func:`admitted` then accepts a file whose bytes are its pin or, on macOS only,
a Mach-O file whose bytes are exactly what its folder's record says the pinned
bytes were signed to. Anything else is refused as before: a changed file, a
record naming another pin, a record off macOS, a record for a file that is not
Mach-O. Changing a signed file undetected takes rewriting its record, as
changing an unsigned one takes rewriting its manifest; the environment's own
hash (``collimate-server.json``) covers both. Nothing outside a macOS server
environment writes a record, so every other install checks exactly the pin.

``python -m collimate._host.native_resources pinned`` prints the resources
and their pins as JSON, refusing any that differs from its pin or already has
a record; ``... check`` checks every one as the SDK will at runtime, the host's
compiler and verifier payload through their own checks.
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

RECORD = 'codesign.json'
# The first four bytes of a Mach-O file, thin or universal, either byte order.
MACH_O = frozenset({b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe',
                    b'\xca\xfe\xba\xbe'})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def signed_digest(path: Path, pinned: str) -> str | None:
    """The sha256 the record beside ``path`` says its ``pinned`` bytes were
    signed to, or None when there is no such record entry."""
    record_path = path.parent / RECORD
    try:
        if not stat.S_ISREG(record_path.lstat().st_mode):
            return None
        record = json.loads(record_path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {'schema', 'files'} or record['schema'] != 1:
        return None
    files = record['files']
    entry = files.get(path.name) if isinstance(files, dict) else None
    if not isinstance(entry, dict) or set(entry) != {'pinned', 'signed'} or entry['pinned'] != pinned:
        return None
    signed = entry['signed']
    return signed if isinstance(signed, str) and signed != pinned else None


def admitted(path: Path, pinned: str, platform: str | None = None) -> bool:
    """Whether the file at ``path`` is the resource pinned as ``pinned``: its
    bytes hash to the pin, or (on macOS) it is a Mach-O file whose bytes hash
    to what its folder's signing record says the pinned bytes were signed to."""
    actual = digest(path)
    if actual == pinned:
        return True
    if (platform or sys.platform) != 'darwin' or signed_digest(path, pinned) != actual:
        return False
    with path.open('rb') as stream:
        return stream.read(4) in MACH_O


def resources(compilers: Path | None = None, payloads: Path | None = None) -> dict[Path, str]:
    """Every pinned native resource this SDK carries, with its pin: each
    compiler the renderer compilers' manifest names, and each file of every
    verifier payload present (its manifest first checked against the contract,
    :func:`.verifier_engine.verify_payload`)."""
    from .._renderer_compilers import compiler_root
    from . import verifier_engine
    compilers = compiler_root() if compilers is None else compilers
    payloads = verifier_engine.payload_root() if payloads is None else payloads
    found: dict[Path, str] = {}
    manifest = json.loads((compilers / 'manifest.json').read_text())
    for entry in manifest['targets'].values():
        found[compilers / entry['file']] = entry['sha256']
    for target in sorted(verifier_engine.contract()['targets']):
        if (payloads / target).is_dir():
            record = verifier_engine.verify_payload(payloads / target, target)
            for name, pin in record['files'].items():
                found[payloads / target / name] = pin
    return found


def pinned(compilers: Path | None = None, payloads: Path | None = None) -> dict[Path, str]:
    """:func:`resources`, each file's bytes exactly its pin and no signing
    record beside any: what a build checks before it signs anything."""
    found = resources(compilers, payloads)
    for path, pin in found.items():
        if (path.parent / RECORD).exists() or (path.parent / RECORD).is_symlink():
            raise ValueError(f'{path.parent / RECORD} exists before anything was signed')
        if not stat.S_ISREG(path.lstat().st_mode) or digest(path) != pin:
            raise ValueError(f'{path} differs from its pin {pin}')
    return found


def check() -> tuple[int, int]:
    """Every resource admitted, the host's compiler and verifier payload
    through the checks the SDK makes before using them: (resources, of which
    admitted by a signing record)."""
    from .._renderer_compilers import compiler_path
    from . import verifier_engine
    found = resources()
    for path, pin in found.items():
        if not stat.S_ISREG(path.lstat().st_mode) or not admitted(path, pin):
            raise ValueError(f'{path} is neither its pin {pin} nor its recorded signed bytes')
    contract = json.loads((Path(__file__).parent.parent / 'renderer_bundle_contract.json').read_text())
    compiler_path(contract['esbuild_version'])
    target = verifier_engine.native_target()
    verifier_engine.verify_payload(verifier_engine.payload_directory(target), target)
    return len(found), sum(digest(path) != pin for path, pin in found.items())


def main(argv: list[str]) -> int:
    if argv == ['pinned']:
        print(json.dumps({str(path): pin for path, pin in sorted(pinned().items())}, indent=2))
        return 0
    if argv == ['check']:
        count, by_record = check()
        print(f'native_resources: {count} pinned native resources admitted, {by_record} by their signing record')
        return 0
    print('usage: python -m collimate._host.native_resources pinned|check', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
