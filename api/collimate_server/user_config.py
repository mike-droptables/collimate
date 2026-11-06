"""Where this user's Collimate configuration lives, per platform."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def config_root() -> Path:
    """The per-user configuration directory: ``~/Library/Application
    Support/collimate`` on macOS, ``$XDG_CONFIG_HOME/collimate`` (default
    ``~/.config/collimate``) elsewhere."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "collimate"
