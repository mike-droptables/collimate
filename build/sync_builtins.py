"""Developer tool: pin the built-in tree in the app's list of its built-ins.

    PYTHONPATH=api uv run python build/sync_builtins.py          # rewrite api/builtins.json from builtins/
    PYTHONPATH=api uv run python build/sync_builtins.py --check  # list what differs; exit 1 if anything does

``api/builtins.json`` names each built-in handler and renderer by kind and id
with the content hash of its files as the app's build reads them
(``builtin_packages.tree_listing``); the app refuses to start on a tree that
differs from it. An intended edit of ``builtins/`` is one step: edit, run this
tool (after ``sdk/scripts/sync_skill_references.py`` when the edit touches a
canonical guide or a handler's ``derived/`` files), commit both.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREE = ROOT / "builtins"
LIST = ROOT / "api" / "builtins.json"


def expected() -> bytes:
    """The list's bytes for the checkout's built-in tree."""
    from builtin_packages import tree_listing
    return (json.dumps(tree_listing(str(TREE)), indent=2) + "\n").encode("utf-8")


def differences() -> list[str]:
    """Each entry the committed list holds otherwise than the tree."""
    def entries(body: bytes) -> dict:
        try:
            return {(entry["kind"], entry["id"]): entry["content_hash"] for entry in json.loads(body)}
        except (ValueError, TypeError, KeyError):
            return {}

    want = expected()
    have = LIST.read_bytes() if LIST.is_file() else b""
    if have == want:
        return []
    wanted, held = entries(want), entries(have)
    out = [f"{kind} {identifier}: {held.get((kind, identifier), 'not listed')} -> "
           f"{wanted.get((kind, identifier), 'not in the tree')}"
           for kind, identifier in sorted(wanted.keys() | held.keys())
           if wanted.get((kind, identifier)) != held.get((kind, identifier))]
    return out or [f"{LIST.relative_to(ROOT)} is not in its canonical form"]


def main(argv: list[str]) -> int:
    changed = differences()
    if "--check" in argv:
        for line in changed:
            print(line)
        return 1 if changed else 0
    if changed:
        LIST.write_bytes(expected())
    for line in changed:
        print(f"synced {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
