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
        "api/collimate_server/verifier_pool.py",
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
    # Channel menu: the demo runs exactly one channel in memory, so the
    # channel operations (and the separator above them) go: New channel…,
    # Manage channels… (the channels page: archive, move and delete, the
    # Locations and Storage tabs, all acting on a machine's disks) and
    # Rename current…. The guard answers those routes itself (a 400 for the
    # single channel, a 501 for a move and every storage write), so this is
    # only about not offering a dead control.
    Patch(
        "frontend/src/components/AppTopBar.tsx",
        """
        { kind: 'separator', id: 'operations' },
        { kind: 'item', id: 'new', label: 'New channel…', icon: Plus, onSelect: onRequestNewChannel },
        { kind: 'item', id: 'manage', label: 'Manage channels…', icon: FolderCog, hint: 'Every channel of this server: archive, the archived ones, servers', onSelect: onManageChannels },
        { kind: 'item', id: 'rename', label: 'Rename current…', icon: Pencil, onSelect: openRename },
""",
        "\n",
        "hide the new / manage / rename channel menu entries",
    ),
    # API base: the demo's page lives at /playground/ while sw.js answers the
    # site-root /api/*, so the app's "API beside the document" default
    # (./api) would call /playground/api/. These two put the demo's own base,
    # one level up, where the app reads it: the module mirror import map and
    # the UI's fallback base.
    Patch(
        "frontend/index.html",
        '{"imports":{"https://esm.sh/":"./api/mirror/esm.sh/","https://cdn.jsdelivr.net/":"./api/mirror/cdn.jsdelivr.net/"}}',
        '{"imports":{"https://esm.sh/":"../api/mirror/esm.sh/","https://cdn.jsdelivr.net/":"../api/mirror/cdn.jsdelivr.net/"}}',
        "point the module mirror import map at the site-root API the service worker answers",
    ),
    Patch(
        "frontend/src/lib/api/client.ts",
        "          return './api';\n",
        "          return '../api';\n",
        "point the UI's API base at the site-root API the service worker answers",
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
