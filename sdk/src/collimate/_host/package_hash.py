"""Exact package identity displayed by preview and Add to approval."""
import hashlib
from typing import Mapping


def content_hash(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256(b"collimate-package-approval-v1\0")
    for path in sorted(files, key=lambda value: value.encode("utf-8")):
        name = path.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(hashlib.sha256(files[path]).digest())
    return digest.hexdigest()
