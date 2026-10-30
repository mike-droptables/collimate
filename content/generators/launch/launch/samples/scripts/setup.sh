#!/usr/bin/env bash
# Sample `scripts` config folder entry — a starter setup script a runner can hand
# the launch generator. Docs-only: this sample is never staged into a run (it just
# shows the shape). A runner ships its real scripts under
# <runner-package>/files/scripts/.
#
# Every file in the staged `scripts` folder runs (sorted by filename) inside the
# container, BEFORE startup_commands and the main command / app. Use it for
# dependency installs, fetches, or environment bootstrap that should version
# with the cell. A non-zero exit aborts the run.
set -euo pipefail

echo "[setup] installing project dependencies"
if [ -f requirements.txt ]; then
    # Launch selects a writable per-user venv in built-in images; custom images own their pip policy.
    python3 -m pip install -q -r requirements.txt
fi
