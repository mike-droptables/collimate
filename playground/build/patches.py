"""Anchored, fail-loud edits to the STAGED app copies.

Every patch names the file and the exact text it expects to find. A
missing anchor raises with both, so a main-code change that moves the
anchor breaks this build immediately instead of silently shipping a demo
that still shows the control. Keep this list short: anything the demo can
express as backend behaviour (a 400/501 JSON detail) belongs in the shim,
not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Patch:
    path: str          # relative to the stage dir
    anchor: str        # exact text that must occur exactly once
    replacement: str
    why: str


PATCHES: tuple[Patch, ...] = (
    # Verifier expiry: the host builds every deadline-passed verdict with this
    # one constructor, so the demo observes a known expired deadline (a
    # refusal record, "Verifier busy, try again") without interpreting any
    # authored error text.
    Patch(
        "api/verifier_pool.py",
        "    return {'valid': False, 'error': BUDGET_EXCEEDED}\n",
        "    from collimate_playground.verification import _expired_verdict\n    return _expired_verdict()\n",
        "present an expired verification deadline as the demo's observed busy refusal",
    ),
    # Settings: no Key manager. Accounts need a real machine. The settings
    # root's section rows and the Commands pane's Settings group both read
    # this one table, so dropping the entry hides both rows.
    Patch(
        "frontend/src/components/commandPane/settingsSections.ts",
        "    { id: 'keys', label: 'Key manager', description: 'Secrets are stored in the server’s system keyring, separate from channel files.', icon: Key },\n",
        "",
        "hide the Key manager section row and its Settings-group row",
    ),
    # Channel menu: the demo runs exactly one channel, so the new / rename /
    # delete entries (and the separator above them) go. The backend also
    # answers those routes with a 400, so this is only about not offering
    # a dead control.
    Patch(
        "frontend/src/components/AppTopBar.tsx",
        """
        { kind: 'separator', id: 'operations' },
        { kind: 'item', id: 'new', label: 'New channel…', icon: Plus, onSelect: onRequestNewChannel },
        { kind: 'item', id: 'rename', label: 'Rename current…', icon: Pencil, onSelect: () => openChannelDialog('rename') },
        { kind: 'item', id: 'delete', label: 'Delete current…', icon: Trash2, danger: true, onSelect: () => openChannelDialog('delete') },
""",
        "\n",
        "hide the new / rename / delete channel menu entries",
    ),
)


class AnchorError(RuntimeError):
    pass


def apply(stage_dir: Path, patch: Patch) -> None:
    path = stage_dir / patch.path
    if not path.is_file():
        raise AnchorError(f"patch target missing: {path} ({patch.why})")
    text = path.read_text(encoding="utf-8")
    count = text.count(patch.anchor)
    if count != 1:
        raise AnchorError(
            f"anchor found {count} times (expected 1) in {path} — {patch.why}\n"
            f"--- anchor ---\n{patch.anchor}\n--------------"
        )
    path.write_text(text.replace(patch.anchor, patch.replacement, 1), encoding="utf-8")


def run(stage_dir: Path) -> None:
    for patch in PATCHES:
        apply(stage_dir, patch)
        print(f"           {patch.path}: {patch.why}")
