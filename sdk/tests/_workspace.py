# SPDX-License-Identifier: MIT
"""The workspace checkout the SDK's tests exercise: this member (``sdk/``) and
the first-party trees beside it at the workspace root."""
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
WORKSPACE = SDK.parent
CONTENT = WORKSPACE / "content"
GENERATORS = CONTENT / "generators"
RENDERERS = CONTENT / "renderers"
SUGGESTED = CONTENT / "suggested"
BUILTINS = WORKSPACE / "builtins"
MODULE_SNAPSHOT = WORKSPACE / "module-snapshot"
