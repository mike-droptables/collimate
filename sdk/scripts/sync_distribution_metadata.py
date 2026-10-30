#!/usr/bin/env python3
"""Generate/check runtime metadata using the SDK's canonical wheel backend.

Run with the existing Hatch build dependency, for example:
uv run --no-project --with hatchling python scripts/sync_distribution_metadata.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

from hatchling.builders.wheel import WheelBuilder

ROOT = Path(__file__).resolve().parents[1]


def expected_metadata(root: Path = ROOT) -> bytes:
    builder = WheelBuilder(str(root))
    return builder.config.core_metadata_constructor(builder.metadata).encode('utf-8')


def resource_path(root: Path = ROOT) -> Path:
    return root / 'src/collimate/_sdk_metadata/METADATA'


def verify(root: Path = ROOT) -> None:
    path = resource_path(root)
    if not path.is_file() or path.is_symlink() or path.read_bytes() != expected_metadata(root):
        raise RuntimeError('SDK runtime metadata is missing or stale; run scripts/sync_distribution_metadata.py')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        verify()
    else:
        path = resource_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected_metadata())
        verify()


if __name__ == '__main__':
    main()
