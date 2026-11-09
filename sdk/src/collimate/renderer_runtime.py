"""Verify the SDK-built renderer runtime modules (``shared/renderer-runtime``),
which the host serves as they are installed, and the verifier prelude
(``shared/verify/prelude.js``), which every verifier compiles against.

Both are build outputs, git-ignored in a checkout and shipped in the SDK's
wheel and sdist; the committed manifest beside each (``index.json``,
``prelude.json``) pins their sha256 and, for a checkout or an sdist, their
sources'. ``scripts/build_outputs.py`` builds them when missing or stale."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from .shared import shared_path

RUNTIME_INPUTS = frozenset({
    'scripts/build_renderer_runtime.mjs', 'src/collimate/renderer_bundle_contract.json',
    'shared/contracts/toolchain.json',
    'scripts/build_snapshot_codecs.mjs', 'scripts/snapshot-codecs/milkdown.ts',
    'scripts/package.json', 'scripts/package-lock.json',
    'scripts/snapshot-codecs/embed-markdown.ts',
    'scripts/snapshot-codecs/snapshot-model.ts'} | {
    'packages/renderer-hooks/src/' + name for name in ('color.ts','document-view.tsx','index.tsx','media.tsx','mirror.ts','platform.tsx','runtime.tsx','session.ts','style-control.tsx','syntax.ts','types.ts')})
RUNTIME_OUTPUTS = frozenset({'react.js', 'session.js', 'common.json', 'snapshot-codecs.json'})
PRELUDE_INPUTS = frozenset({'scripts/build_verify_prelude.mjs', 'scripts/package.json', 'scripts/package-lock.json',
                            'shared/verify/prelude.ts', 'src/collimate/renderer_bundle_contract.json'})
PRELUDE_OUTPUTS = frozenset({'prelude.js', 'dependency-licenses.txt'})


class SourceChanged(ValueError):
    """A source differs from the one its committed manifest pins: the manifest
    is stale and only regenerating it (``scripts/generate.py``) helps."""


def _verify(folder: str, manifest_name: str, label: str, inputs: frozenset, outputs: frozenset,
            root: Path | None) -> dict[str, bytes]:
    directory = root / 'shared' / folder if root else shared_path(folder)
    manifest = json.loads((directory / manifest_name).read_bytes())
    contract = json.loads(Path(__file__).with_name('renderer_bundle_contract.json').read_bytes())
    if set(manifest['inputs']) != inputs or manifest['compiler'] != contract['esbuild_version']:
        raise ValueError(f'Invalid {label} source identity')
    if manifest['schema'] != 1 or set(manifest['outputs']) != outputs:
        raise ValueError(f'Invalid {label} manifest')
    if root is not None:
        for name, digest in manifest['inputs'].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise SourceChanged(f'{label.capitalize()} source changed; rebuild: ' + name)
    bodies = {name: (directory / name).read_bytes() for name in manifest['outputs']}
    for name, body in bodies.items():
        if not body or hashlib.sha256(body).hexdigest() != manifest['outputs'][name]:
            raise ValueError(f'{label.capitalize()} resource mismatch: ' + name)
    return bodies


def verify_runtime_resources(root: Path | None = None) -> dict[str, bytes]:
    """Check built bytes and, during SDK builds, their exact source identities."""
    return _verify('renderer-runtime', 'index.json', 'renderer runtime', RUNTIME_INPUTS, RUNTIME_OUTPUTS, root)


def verify_prelude_resources(root: Path | None = None) -> dict[str, bytes]:
    """The verifier prelude's built bytes against ``prelude.json``, and with
    ``root`` (an SDK checkout or sdist) its sources too."""
    return _verify('verify', 'prelude.json', 'verifier prelude', PRELUDE_INPUTS, PRELUDE_OUTPUTS, root)
