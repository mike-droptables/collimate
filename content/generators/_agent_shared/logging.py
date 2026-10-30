# SPDX-License-Identifier: MIT
"""Best-effort generator diagnostics shared by runtime helpers."""
import gen


def safe_log(message: str) -> None:
    try:
        from .secret_hygiene import refresh, scrub
        refresh()
        gen.log(scrub(message))
    except Exception:  # noqa: BLE001 — diagnostics cannot break cleanup
        pass
